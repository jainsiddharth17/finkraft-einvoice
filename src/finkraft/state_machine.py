"""Finkraft's own invoice state machine.

Finkraft states are deliberately coarser than any ASP's statuses. Each adapter
maps its ASP-specific statuses onto these; the raw ASP status is kept in the
invoice's `asp_events` history so nothing is lost.
"""
from __future__ import annotations

from enum import Enum


class State(str, Enum):
    # --- Finkraft-side, before anything leaves our system ---
    RECEIVED = "RECEIVED"                    # persisted, number reserved
    VALIDATION_FAILED = "VALIDATION_FAILED"  # our PINT-AE checks failed (terminal)
    READY = "READY"                          # validated + enriched, queued for submit
    # --- Submission to C2 ---
    SUBMITTING = "SUBMITTING"                # exactly one worker owns the attempt
    SUBMIT_UNCERTAIN = "SUBMIT_UNCERTAIN"    # sent, outcome unknown; NEVER blind-resend
    SUBMIT_FAILED = "SUBMIT_FAILED"          # definitely not accepted; safe to requeue
    # --- ASP phase (driven by ASP statuses) ---
    SUBMITTED = "SUBMITTED"                  # ASP holds it (A: RECEIVED, B: QUEUED)
    ASP_ACCEPTED = "ASP_ACCEPTED"            # passed ASP validation (A: VALIDATED, B: ACCEPTED)
    ASP_REJECTED = "ASP_REJECTED"            # ASP rejected it (terminal)
    DELIVERED = "DELIVERED"                  # C3 / buyer's ASP has it
    REPORTED = "REPORTED"                    # C5 / FTA has the tax data (terminal)


S = State

_ASP_FORWARD = frozenset({S.SUBMITTED, S.ASP_ACCEPTED, S.ASP_REJECTED, S.DELIVERED, S.REPORTED})

TRANSITIONS: dict[State, frozenset[State]] = {
    S.RECEIVED: frozenset({S.READY, S.VALIDATION_FAILED}),
    S.VALIDATION_FAILED: frozenset(),
    S.READY: frozenset({S.SUBMITTING}),
    # A status update can overtake the submit response (webhook beats the 202),
    # so SUBMITTING may jump straight into the ASP phase.
    S.SUBMITTING: frozenset({S.SUBMIT_UNCERTAIN, S.SUBMIT_FAILED}) | _ASP_FORWARD,
    # Resolved by a webhook/poll/409 that identifies the submission, or by an
    # operator who confirmed with the ASP that it never arrived (-> READY).
    S.SUBMIT_UNCERTAIN: frozenset({S.READY}) | _ASP_FORWARD,
    S.SUBMIT_FAILED: frozenset({S.READY}),
    S.SUBMITTED: frozenset({S.ASP_ACCEPTED, S.ASP_REJECTED, S.DELIVERED, S.REPORTED}),
    S.ASP_ACCEPTED: frozenset({S.DELIVERED, S.REPORTED}),  # skipping DELIVERED = missed/out-of-order event
    S.ASP_REJECTED: frozenset(),
    S.DELIVERED: frozenset({S.REPORTED}),
    S.REPORTED: frozenset(),
}

TERMINAL = frozenset(s for s, nxt in TRANSITIONS.items() if not nxt)

# Forward-only ordering of the ASP-phase "happy path". An update whose rank is
# <= the current rank is stale or a duplicate and is ignored (never applied).
ASP_RANK: dict[State, int] = {
    S.SUBMITTED: 1,
    S.ASP_ACCEPTED: 2,
    S.DELIVERED: 3,
    S.REPORTED: 4,
}


class InvalidTransition(Exception):
    def __init__(self, src: State, dst: State):
        super().__init__(f"transition {src.value} -> {dst.value} is not allowed")
        self.src, self.dst = src, dst


def validate_transition(src: State, dst: State) -> None:
    if dst not in TRANSITIONS[src]:
        raise InvalidTransition(src, dst)
