import pytest

from finkraft import InMemoryStore, InvoiceService
from finkraft.adapters import MockAspA, MockAspB

GOOD = {"seller_trn": "100123456700003", "issue_date": "2026-10-01", "currency": "AED",
        "buyer_name": "Acme LLC", "total": "105.00", "lines": [{"desc": "Widget", "amount": "100.00"}]}


@pytest.fixture
def asp_a():
    return MockAspA()


@pytest.fixture
def asp_b():
    return MockAspB()


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def svc(asp_a, asp_b, sleeps):
    routing = {"seller_a": "asp_a", "seller_b": "asp_b"}
    return InvoiceService(InMemoryStore(), {"asp_a": asp_a, "asp_b": asp_b}, routing, sleep=sleeps.append)


def new_ready(svc, seller="seller_a", number="INV-1", **overrides):
    return svc.ingest(seller, number, {**GOOD, **overrides})
