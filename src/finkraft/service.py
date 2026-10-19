"""Submission and status-tracking logic. Adapter-agnostic: it only talks to ASPAdapter."""
from __future__ import annotations

import random
import time
from enum import Enum
from typing import Any, Callable, Optional

from .adapters.base import ASPAdapter
from .models import (DuplicateInvoice, ErrorDetail, InvalidTransition, Invoice, StatusMode,
                     StatusUpdate, SubmitOutcome, SubmitRequest, SubmitResult)
from .state_machine import ASP_RANK, TERMINAL, State
from .store import InMemoryStore
from .validation import validate_pint_ae


class UpdateOutcome(str, Enum):
    APPLIED = "APPLIED"
    STALE = "STALE"                      # older/equal state: ignored, still recorded
    DUPLICATE_EVENT = "DUPLICATE_EVENT"  # exact event seen before
    PARKED = "PARKED"                    # we don't know this submission yet; replayed on bind
    CONFLICT = "CONFLICT"                # contradicts our state; recorded for manual review


class InvoiceService:
    def __init__(self, store: InMemoryStore, adapters: dict[str, ASPAdapter], routing: dict[str, str], *,
                 sleep: Callable[[float], None] = time.sleep, max_attempts: int = 4,
                 base_delay: float = 0.5, max_delay: float = 30.0) -> None:
        self.store, self.adapters, self.routing = store, adapters, routing  # routing: seller_id -> asp_id
        self._sleep, self.max_attempts = sleep, max_attempts
        self.base_delay, self.max_delay = base_delay, max_delay

    # ------------------------------------------------------------------ ingest
    def ingest(self, seller_id: str, invoice_number: str, payload: dict[str, Any]) -> Invoice:
        """Persist first, then validate. Raises DuplicateInvoice before anything else happens."""
        if seller_id not in self.routing:
            raise KeyError(f"seller {seller_id!r} has no ASP configured")
        inv = Invoice(seller_id=seller_id, invoice_number=invoice_number.strip(), payload=payload)
        self.store.add_invoice(inv)  # unique (seller, number)
        errors = validate_pint_ae(payload)
        with self.store.lock:
            if errors:
                inv.errors = errors
                inv.transition(State.VALIDATION_FAILED, "failed PINT-AE validation")
            else:
                inv.transition(State.READY, "validated")
        return inv

    # ------------------------------------------------------------------ submit
    def submit(self, invoice_id: str) -> Invoice:
        """Idempotent: only the caller that wins READY -> SUBMITTING talks to the ASP."""
        with self.store.lock:
            inv = self.store.get(invoice_id)
            if inv.state is not State.READY:
                return inv                                   # already claimed / submitted / not ready
            inv.asp_id = self.routing[inv.seller_id]         # pin ASP now; later routing changes don't move it
            inv.transition(State.SUBMITTING, "claimed for submission")
        adapter = self.adapters[inv.asp_id]
        req = SubmitRequest(inv.id, inv.seller_id, inv.invoice_number, inv.payload)

        last = SubmitOutcome.RETRYABLE
        for attempt in range(1, self.max_attempts + 1):
            with self.store.lock:
                inv.attempts = attempt
            result = adapter.submit(req)
            last = result.outcome
            if last is SubmitOutcome.ACCEPTED:
                return self._on_accepted(inv, result)
            if last is SubmitOutcome.REJECTED:
                with self.store.lock:
                    inv.errors = list(result.errors)
                    self._move_if_submitting(inv, State.ASP_REJECTED, "ASP rejected submission")
                return inv
            if last is SubmitOutcome.DUPLICATE:
                return self._on_duplicate(inv, result)
            if last is SubmitOutcome.UNKNOWN:
                with self.store.lock:
                    inv.possibly_sent = True
                if not adapter.capabilities.idempotent_resubmit:
                    break                                    # ASP can't dedupe: resending could double-submit
            if attempt < self.max_attempts:
                self._sleep(self._backoff(attempt))

        with self.store.lock:                                # exhausted retries, or unsafe to resend
            if last is SubmitOutcome.UNKNOWN or inv.possibly_sent:
                self._move_if_submitting(inv, State.SUBMIT_UNCERTAIN, "outcome unknown; awaiting reconciliation")
            else:
                self._move_if_submitting(inv, State.SUBMIT_FAILED, "retries exhausted; not accepted by ASP")
        return inv

    def _on_accepted(self, inv: Invoice, result: SubmitResult) -> Invoice:
        with self.store.lock:
            self.store.bind_external_id(inv, result.external_id)
            self._move_if_submitting(inv, result.state, "ASP accepted submission")
        self._replay_parked(inv)
        return inv

    def _on_duplicate(self, inv: Invoice, result: SubmitResult) -> Invoice:
        with self.store.lock:
            if inv.possibly_sent and result.external_id:
                # Our earlier attempt DID land (timeout after accept). Adopt that record.
                self.store.bind_external_id(inv, result.external_id)
                self._move_if_submitting(inv, State.SUBMITTED, "409 after ambiguous attempt: adopted existing submission")
            elif inv.possibly_sent:
                self._move_if_submitting(inv, State.SUBMIT_UNCERTAIN, "409 without submission id")
            else:
                # 409 on a first attempt: someone else owns this number at the ASP. Don't adopt it.
                inv.errors = [ErrorDetail(code="DUPLICATE_AT_ASP", message="ASP already has this invoice number")]
                self._move_if_submitting(inv, State.SUBMIT_FAILED, "duplicate at ASP not created by Finkraft")
        if inv.external_id:
            self._replay_parked(inv)
        return inv

    def _move_if_submitting(self, inv: Invoice, dst: State, reason: str) -> None:
        """A status update may already have moved the invoice on; never overwrite that."""
        if inv.state is State.SUBMITTING:
            inv.transition(dst, reason)

    def _backoff(self, attempt: int) -> float:
        return min(self.base_delay * 2 ** (attempt - 1), self.max_delay) * random.uniform(0.5, 1.0)

    # ------------------------------------------------------------ status inflow
    def handle_webhook(self, asp_id: str, headers: dict[str, str], body: bytes) -> list[UpdateOutcome]:
        updates = self.adapters[asp_id].parse_webhook(headers, body)  # raises on bad signature / unknown status
        return [self.handle_status_update(asp_id, u) for u in updates]

    def poll(self, invoice_id: str) -> Optional[UpdateOutcome]:
        inv = self.store.get(invoice_id)
        if inv.asp_id is None or inv.external_id is None or inv.state in TERMINAL:
            return None
        adapter = self.adapters[inv.asp_id]
        if adapter.capabilities.status_mode is not StatusMode.POLL:
            return None
        return self.handle_status_update(inv.asp_id, adapter.fetch_status(inv.external_id))

    def poll_all(self) -> int:
        return sum(1 for inv in self.store.all() if self.poll(inv.id) is not None)

    def handle_status_update(self, asp_id: str, update: StatusUpdate) -> UpdateOutcome:
        key = update.event_id or f"{update.external_id}|{update.raw_status}|{update.occurred_at}"
        with self.store.lock:
            if self.store.seen(asp_id, key):
                return UpdateOutcome.DUPLICATE_EVENT
            inv = self.store.find(asp_id, update.external_id, update.reference)
            if inv is None:
                self.store.park(asp_id, update)               # e.g. webhook beat the submit response
                return UpdateOutcome.PARKED
            if not self.store.bind_external_id(inv, update.external_id):
                self.store.mark_seen(asp_id, key)             # second ASP record for one invoice
                return UpdateOutcome.CONFLICT
            outcome = self._apply(inv, update)
            self.store.mark_seen(asp_id, key)
            return outcome

    def _apply(self, inv: Invoice, update: StatusUpdate) -> UpdateOutcome:
        target = update.state
        cur_rank, tgt_rank = ASP_RANK.get(inv.state), ASP_RANK.get(target)
        if inv.state is target or (cur_rank and tgt_rank and tgt_rank <= cur_rank):
            inv.asp_events.append((update.occurred_at, update.raw_status, UpdateOutcome.STALE.value))
            return UpdateOutcome.STALE
        try:
            inv.transition(target, f"asp status {update.raw_status}")
        except InvalidTransition as exc:
            inv.anomalies.append(f"{update.raw_status}: {exc}")
            inv.asp_events.append((update.occurred_at, update.raw_status, UpdateOutcome.CONFLICT.value))
            return UpdateOutcome.CONFLICT
        if target is State.ASP_REJECTED and update.errors:
            inv.errors = list(update.errors)
        inv.asp_events.append((update.occurred_at, update.raw_status, UpdateOutcome.APPLIED.value))
        return UpdateOutcome.APPLIED

    def _replay_parked(self, inv: Invoice) -> None:
        for u in self.store.pop_parked(inv.asp_id or "", inv.external_id or ""):
            self.handle_status_update(inv.asp_id, u)

    # ------------------------------------------------------------- operator ops
    def requeue(self, invoice_id: str, *, confirmed_not_received: bool = False) -> Invoice:
        """SUBMIT_FAILED can be requeued freely. SUBMIT_UNCERTAIN only after a human has
        confirmed with the ASP that it never received the invoice."""
        with self.store.lock:
            inv = self.store.get(invoice_id)
            if inv.state is State.SUBMIT_UNCERTAIN and not confirmed_not_received:
                raise PermissionError("invoice may already be at the ASP; confirm with the ASP first")
            inv.transition(State.READY, "operator requeue")
            return inv
