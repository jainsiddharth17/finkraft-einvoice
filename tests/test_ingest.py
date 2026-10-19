import pytest

from finkraft import DuplicateInvoice, State
from conftest import new_ready


def test_valid_invoice_becomes_ready(svc):
    assert new_ready(svc).state is State.READY


def test_invalid_invoice_fails_validation_with_field_errors(svc):
    inv = new_ready(svc, seller_trn="123", buyer_name="")
    assert inv.state is State.VALIDATION_FAILED
    assert {e.field for e in inv.errors} == {"seller_trn", "buyer_name"}


def test_duplicate_number_rejected_per_seller(svc):
    new_ready(svc, "seller_a", "INV-1")
    with pytest.raises(DuplicateInvoice):
        new_ready(svc, "seller_a", "INV-1")
    with pytest.raises(DuplicateInvoice):
        new_ready(svc, "seller_a", "  INV-1 ")      # whitespace-insensitive


def test_same_number_allowed_for_different_seller(svc):
    new_ready(svc, "seller_a", "INV-1")
    assert new_ready(svc, "seller_b", "INV-1").state is State.READY


def test_number_reusable_only_if_previous_never_left_finkraft(svc):
    bad = new_ready(svc, "seller_a", "INV-2", currency="")
    assert bad.state is State.VALIDATION_FAILED
    assert new_ready(svc, "seller_a", "INV-2").state is State.READY   # corrected upload
    with pytest.raises(DuplicateInvoice):
        new_ready(svc, "seller_a", "INV-2")


def test_unknown_seller_rejected(svc):
    with pytest.raises(KeyError):
        svc.ingest("nobody", "INV-1", {})
