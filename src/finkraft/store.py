"""In-memory store. In production this is Postgres:
  - UNIQUE (seller_id, invoice_number)          -> duplicate rejection
  - UNIQUE (asp_id, external_id)                 -> one ASP record per invoice
  - UNIQUE (asp_id, event_key)                   -> webhook dedupe
  - UPDATE ... WHERE state='READY' (CAS)         -> single owner of a submission
"""
from __future__ import annotations

import threading
from typing import Optional

from .models import DuplicateInvoice, Invoice, StatusUpdate
from .state_machine import State


class InMemoryStore:
    def __init__(self) -> None:
        self.lock = threading.RLock()  # coarse lock; guards state mutation, never held during ASP I/O
        self._invoices: dict[str, Invoice] = {}
        self._numbers: dict[tuple[str, str], str] = {}          # (seller, number) -> invoice id
        self._by_external: dict[tuple[str, str], str] = {}      # (asp, external_id) -> invoice id
        self._seen_events: set[tuple[str, str]] = set()
        self._parked: dict[tuple[str, str], list[StatusUpdate]] = {}

    # -- invoices
    def add_invoice(self, inv: Invoice) -> None:
        """Atomically reserve (seller, number). A number whose earlier invoice never
        left Finkraft (VALIDATION_FAILED) may be reused by a corrected upload."""
        with self.lock:
            key = (inv.seller_id, inv.invoice_number)
            existing_id = self._numbers.get(key)
            if existing_id is not None:
                if self._invoices[existing_id].state is not State.VALIDATION_FAILED:
                    raise DuplicateInvoice(
                        f"invoice {inv.invoice_number!r} already exists for seller {inv.seller_id!r}"
                    )
            self._invoices[inv.id] = inv
            self._numbers[key] = inv.id

    def get(self, invoice_id: str) -> Invoice:
        return self._invoices[invoice_id]

    def all(self) -> list[Invoice]:
        return list(self._invoices.values())

    # -- ASP correlation
    def bind_external_id(self, inv: Invoice, external_id: str) -> bool:
        """Returns False (and records an anomaly) if the invoice is already bound to a
        different external id, which would mean the ASP created a second record."""
        with self.lock:
            if inv.external_id is None:
                inv.external_id = external_id
                self._by_external[(inv.asp_id or "", external_id)] = inv.id
                return True
            if inv.external_id != external_id:
                inv.anomalies.append(
                    f"second external id {external_id} for already-bound {inv.external_id}"
                )
                return False
            return True

    def find(self, asp_id: str, external_id: str, reference: Optional[str]) -> Optional[Invoice]:
        with self.lock:
            inv_id = self._by_external.get((asp_id, external_id))
            if inv_id is None and reference is not None:
                cand = self._invoices.get(reference)
                if cand is not None and cand.asp_id == asp_id:
                    return cand
            return self._invoices[inv_id] if inv_id else None

    # -- webhook/poll bookkeeping
    def seen(self, asp_id: str, event_key: str) -> bool:
        return (asp_id, event_key) in self._seen_events

    def mark_seen(self, asp_id: str, event_key: str) -> None:
        self._seen_events.add((asp_id, event_key))

    def park(self, asp_id: str, update: StatusUpdate) -> None:
        with self.lock:
            self._parked.setdefault((asp_id, update.external_id), []).append(update)

    def pop_parked(self, asp_id: str, external_id: str) -> list[StatusUpdate]:
        with self.lock:
            return self._parked.pop((asp_id, external_id), [])
