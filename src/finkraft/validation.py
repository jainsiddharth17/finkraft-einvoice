"""Minimal stand-in for PINT-AE validation/enrichment (real rules are out of scope)."""
from __future__ import annotations

from typing import Any

from .models import ErrorDetail

REQUIRED = ("issue_date", "currency", "buyer_name", "total")


def validate_pint_ae(payload: dict[str, Any]) -> list[ErrorDetail]:
    errors = [ErrorDetail(code="FK-REQUIRED", field=k, message=f"{k} is required")
              for k in REQUIRED if not payload.get(k)]
    trn = str(payload.get("seller_trn", ""))
    if not (trn.isdigit() and len(trn) == 15):
        errors.append(ErrorDetail(code="FK-TRN", field="seller_trn", message="seller TRN must be 15 digits"))
    if not payload.get("lines"):
        errors.append(ErrorDetail(code="FK-LINES", field="lines", message="at least one invoice line is required"))
    return errors
