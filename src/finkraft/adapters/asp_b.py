"""Mock ASP B: asynchronous submit (202 + job_id), webhook-only status, NO duplicate check.

Because B will happily create a second job for a resend, the service never
auto-resends to B after an ambiguous outcome. We pass our invoice_id as
`reference` and rely on B echoing it in webhooks (assumption A2) to recover.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Optional

from ..models import (Capabilities, ErrorDetail, StatusMode, StatusUpdate,
                      SubmitOutcome, SubmitRequest, SubmitResult)
from ..state_machine import State
from .base import ASPAdapter, UnknownAspStatus

STATUS_MAP = {
    "QUEUED": State.SUBMITTED,
    "ACCEPTED": State.ASP_ACCEPTED,
    "FAILED": State.ASP_REJECTED,
    "BUYER_RECEIVED": State.DELIVERED,   # C3
    "TAX_CONFIRMED": State.REPORTED,     # C5
}


class InvalidSignature(Exception):
    pass


class MockAspB(ASPAdapter):
    asp_id = "asp_b"
    capabilities = Capabilities(status_mode=StatusMode.WEBHOOK, idempotent_resubmit=False, max_rps=10)

    def __init__(self, secret: bytes = b"test-secret") -> None:
        self.secret = secret
        self.jobs: dict[str, dict] = {}
        self.faults: list[str] = []
        self.submit_calls = 0
        self._events = 0

    # ---- fake server -------------------------------------------------------
    def _post(self, req: SubmitRequest) -> tuple[int, dict]:
        fault = self.faults.pop(0) if self.faults else None
        if fault == "unavailable":
            return 503, {}
        if fault == "timeout_lost":
            raise TimeoutError
        job_id = f"B-{len(self.jobs) + 1}"                # no duplicate check: every POST makes a job
        self.jobs[job_id] = {"reference": req.invoice_id, "number": req.invoice_number}
        if fault == "timeout_after_accept":
            raise TimeoutError
        return 202, {"job_id": job_id}

    def webhook(self, job_id: str, status: str, *, event_id: Optional[str] = None,
                errors: Optional[list[dict]] = None) -> tuple[dict[str, str], bytes]:
        """Test helper: build a signed webhook request as ASP B would send it."""
        self._events += 1
        body = json.dumps({
            "event_id": event_id or f"evt-{self._events}", "job_id": job_id, "status": status,
            "reference": self.jobs[job_id]["reference"], "occurred_at": time.time(),
            "errors": errors or [],
        }).encode()
        return {"X-Signature": hmac.new(self.secret, body, hashlib.sha256).hexdigest()}, body

    # ---- adapter contract --------------------------------------------------
    def submit(self, req: SubmitRequest) -> SubmitResult:
        self.submit_calls += 1
        try:
            code, body = self._post(req)
        except TimeoutError:
            return SubmitResult(SubmitOutcome.UNKNOWN)
        if code == 202:
            return SubmitResult(SubmitOutcome.ACCEPTED, body["job_id"], State.SUBMITTED)
        if code in (429, 502, 503, 504):
            return SubmitResult(SubmitOutcome.RETRYABLE)
        return SubmitResult(SubmitOutcome.UNKNOWN)

    def parse_webhook(self, headers: dict[str, str], body: bytes) -> list[StatusUpdate]:
        expected = hmac.new(self.secret, body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, headers.get("X-Signature", "")):
            raise InvalidSignature("bad webhook signature")
        d = json.loads(body)
        try:
            state = STATUS_MAP[d["status"]]
        except KeyError:
            raise UnknownAspStatus(f"ASP B status {d['status']!r}") from None
        errors = tuple(ErrorDetail(rule=e.get("rule"), message=e.get("message")) for e in d.get("errors", []))
        return [StatusUpdate(d["job_id"], state, d["status"], d["occurred_at"],
                             event_id=d.get("event_id"), reference=d.get("reference"), errors=errors)]
