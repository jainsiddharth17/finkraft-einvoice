"""Mock ASP A: synchronous submit, polling-only status, 409 on duplicate invoice numbers.

`_post` / `_get` play the role of the HTTP server. A real adapter would replace
them with an HTTP client; the classification logic in submit() stays the same.
"""
from __future__ import annotations

import time
from typing import Optional

from ..models import (Capabilities, ErrorDetail, StatusMode, StatusUpdate,
                      SubmitOutcome, SubmitRequest, SubmitResult)
from ..state_machine import State
from .base import ASPAdapter, UnknownAspStatus

STATUS_MAP = {
    "RECEIVED": State.SUBMITTED,
    "VALIDATED": State.ASP_ACCEPTED,
    "REJECTED": State.ASP_REJECTED,
    "DELIVERED": State.DELIVERED,   # C3
    "REPORTED": State.REPORTED,     # C5
}


class MockAspA(ASPAdapter):
    asp_id = "asp_a"
    capabilities = Capabilities(status_mode=StatusMode.POLL, idempotent_resubmit=True, max_rps=10)

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], dict] = {}   # (seller, number) -> record
        self.by_id: dict[str, dict] = {}
        self.faults: list[str] = []                      # consumed one per submit call
        self.submit_calls = 0

    # ---- fake server -------------------------------------------------------
    def _post(self, req: SubmitRequest) -> tuple[int, dict]:
        fault = self.faults.pop(0) if self.faults else None
        if fault == "unavailable":
            return 503, {}
        if fault == "timeout_lost":                      # request never processed
            raise TimeoutError
        key = (req.seller_id, req.invoice_number)
        if key in self.records:
            return 409, {"code": "E409", "submission_id": self.records[key]["id"]}  # assumption A1
        if req.payload.get("force_reject"):
            return 422, {"code": "E104", "field": "BT48"}
        rec = {"id": f"A-{len(self.by_id) + 1}", "status": "RECEIVED", "at": time.time()}
        self.records[key] = rec
        self.by_id[rec["id"]] = rec
        if fault == "timeout_after_accept":              # processed, but the client sees a timeout
            raise TimeoutError
        return 200, {"submission_id": rec["id"], "status": rec["status"]}

    def _get(self, submission_id: str) -> tuple[int, dict]:
        rec = self.by_id.get(submission_id)
        return (200, {"submission_id": submission_id, "status": rec["status"], "at": rec["at"]}) if rec else (404, {})

    def advance(self, submission_id: str, status: str) -> None:
        """Test helper: the ASP moves the invoice on."""
        self.by_id[submission_id].update(status=status, at=time.time())

    # ---- adapter contract --------------------------------------------------
    def submit(self, req: SubmitRequest) -> SubmitResult:
        self.submit_calls += 1
        try:
            code, body = self._post(req)
        except TimeoutError:
            return SubmitResult(SubmitOutcome.UNKNOWN)
        if code == 200:
            return SubmitResult(SubmitOutcome.ACCEPTED, body["submission_id"], self._map(body["status"]))
        if code == 409:
            return SubmitResult(SubmitOutcome.DUPLICATE, body.get("submission_id"))
        if code in (429, 502, 503, 504):
            return SubmitResult(SubmitOutcome.RETRYABLE)
        if code == 422:
            return SubmitResult(SubmitOutcome.REJECTED, errors=(ErrorDetail(code=body.get("code"), field=body.get("field")),))
        return SubmitResult(SubmitOutcome.UNKNOWN)       # anything unexpected is treated as ambiguous

    def fetch_status(self, external_id: str) -> StatusUpdate:
        code, body = self._get(external_id)
        if code != 200:
            raise LookupError(f"ASP A has no submission {external_id}")
        return StatusUpdate(external_id, self._map(body["status"]), body["status"], body["at"])

    @staticmethod
    def _map(raw: str) -> State:
        try:
            return STATUS_MAP[raw]
        except KeyError:
            raise UnknownAspStatus(f"ASP A status {raw!r}") from None
