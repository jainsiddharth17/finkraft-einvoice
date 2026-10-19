"""Domain models and the data types that cross the adapter boundary."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional

from .state_machine import InvalidTransition, State, validate_transition


class DuplicateInvoice(Exception):
    """Same (seller, invoice_number) already exists. Raised before any ASP call."""


@dataclass(frozen=True)
class ErrorDetail:
    """ASP-specific errors normalised to one shape.
    ASP A: {"code": "E104", "field": "BT48"}  ASP B: {"rule": ..., "message": ...}"""
    code: Optional[str] = None
    field: Optional[str] = None
    rule: Optional[str] = None
    message: Optional[str] = None


# ---------------------------------------------------------------- adapter types
@dataclass(frozen=True)
class SubmitRequest:
    invoice_id: str          # Finkraft's id; also our idempotency key / ASP B "reference"
    seller_id: str
    invoice_number: str
    payload: dict[str, Any]  # PINT-AE data (a real system would pass validated XML)


class SubmitOutcome(str, Enum):
    ACCEPTED = "ACCEPTED"    # ASP took it; external_id is set
    REJECTED = "REJECTED"    # definite business rejection; errors are set
    DUPLICATE = "DUPLICATE"  # ASP says it already has this invoice (A: 409)
    RETRYABLE = "RETRYABLE"  # definitely NOT processed (429 / 503 / connect error)
    UNKNOWN = "UNKNOWN"      # request may have been processed (read timeout)


@dataclass(frozen=True)
class SubmitResult:
    outcome: SubmitOutcome
    external_id: Optional[str] = None   # A: submission_id, B: job_id
    state: Optional[State] = None       # canonical state when ACCEPTED
    errors: tuple[ErrorDetail, ...] = ()


@dataclass(frozen=True)
class StatusUpdate:
    external_id: str
    state: State                 # canonical (already mapped by the adapter)
    raw_status: str              # the ASP's own status string, kept for audit
    occurred_at: float
    event_id: Optional[str] = None     # ASP's event id if it has one (dedupe key)
    reference: Optional[str] = None    # our invoice_id echoed back (ASP B)
    errors: tuple[ErrorDetail, ...] = ()


class StatusMode(str, Enum):
    POLL = "POLL"
    WEBHOOK = "WEBHOOK"


@dataclass(frozen=True)
class Capabilities:
    status_mode: StatusMode
    idempotent_resubmit: bool   # True if re-sending the same invoice is safe (ASP dedupes)
    max_rps: int


# ---------------------------------------------------------------------- invoice
@dataclass
class Invoice:
    seller_id: str
    invoice_number: str
    payload: dict[str, Any]
    id: str = field(default_factory=lambda: uuid.uuid4().hex)
    state: State = State.RECEIVED
    asp_id: Optional[str] = None          # pinned when submission is first claimed
    external_id: Optional[str] = None
    attempts: int = 0
    possibly_sent: bool = False           # set once any attempt ended UNKNOWN
    errors: list[ErrorDetail] = field(default_factory=list)
    history: list[tuple[float, State, State, str]] = field(default_factory=list)
    asp_events: list[tuple[float, str, str]] = field(default_factory=list)  # (ts, raw_status, outcome)
    anomalies: list[str] = field(default_factory=list)

    def transition(self, dst: State, reason: str) -> None:
        validate_transition(self.state, dst)  # raises InvalidTransition
        self.history.append((time.time(), self.state, dst, reason))
        self.state = dst


__all__ = [
    "Capabilities", "DuplicateInvoice", "ErrorDetail", "InvalidTransition", "Invoice",
    "StatusMode", "StatusUpdate", "SubmitOutcome", "SubmitRequest", "SubmitResult",
]
