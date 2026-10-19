"""The adapter contract. One interface; each ASP's quirks live behind it.

Rules every adapter must follow:
  * submit() never raises for network/HTTP problems; it classifies them into a
    SubmitOutcome. In particular a read timeout is UNKNOWN, a throttle/5xx that
    provably was not processed is RETRYABLE.
  * Statuses are mapped to Finkraft's canonical State inside the adapter.
  * Unknown ASP statuses raise UnknownAspStatus (never silently mapped).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import Capabilities, StatusUpdate, SubmitRequest, SubmitResult


class UnknownAspStatus(ValueError):
    pass


class NotSupported(Exception):
    pass


class ASPAdapter(ABC):
    asp_id: str
    capabilities: Capabilities

    @abstractmethod
    def submit(self, req: SubmitRequest) -> SubmitResult:
        """Send one invoice to C2."""

    def fetch_status(self, external_id: str) -> StatusUpdate:
        """Polling ASPs only (ASP A)."""
        raise NotSupported(f"{self.asp_id} does not support polling")

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StatusUpdate]:
        """Webhook ASPs only (ASP B). Verifies the signature, then returns canonical updates."""
        raise NotSupported(f"{self.asp_id} does not deliver webhooks")
