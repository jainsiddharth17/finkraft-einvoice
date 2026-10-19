import pytest

from finkraft import State, UpdateOutcome
from finkraft.adapters import InvalidSignature, NotSupported
from conftest import new_ready


def submit_b(svc, number="INV-1"):
    return svc.submit(new_ready(svc, "seller_b", number).id)


def hook(svc, asp_b, job_id, status, **kw):
    headers, body = asp_b.webhook(job_id, status, **kw)
    return svc.handle_webhook("asp_b", headers, body)[0]


def test_happy_path_via_webhooks(svc, asp_b):
    inv = submit_b(svc)
    assert inv.state is State.SUBMITTED and inv.external_id == "B-1"
    for status, expected in [("ACCEPTED", State.ASP_ACCEPTED), ("BUYER_RECEIVED", State.DELIVERED),
                             ("TAX_CONFIRMED", State.REPORTED)]:
        assert hook(svc, asp_b, "B-1", status) is UpdateOutcome.APPLIED
        assert inv.state is expected


def test_failed_status_maps_to_rejected_with_rule_errors(svc, asp_b):
    inv = submit_b(svc)
    hook(svc, asp_b, "B-1", "FAILED", errors=[{"rule": "PINT-AE-R001", "message": "bad TRN"}])
    assert inv.state is State.ASP_REJECTED and inv.errors[0].rule == "PINT-AE-R001"


def test_duplicate_webhook_is_ignored(svc, asp_b):
    inv = submit_b(svc)
    headers, body = asp_b.webhook("B-1", "ACCEPTED", event_id="evt-x")
    assert svc.handle_webhook("asp_b", headers, body) == [UpdateOutcome.APPLIED]
    assert svc.handle_webhook("asp_b", headers, body) == [UpdateOutcome.DUPLICATE_EVENT]
    assert inv.state is State.ASP_ACCEPTED


def test_out_of_order_webhook_never_moves_state_backwards(svc, asp_b):
    inv = submit_b(svc)
    assert hook(svc, asp_b, "B-1", "TAX_CONFIRMED") is UpdateOutcome.APPLIED   # skips ahead
    assert hook(svc, asp_b, "B-1", "BUYER_RECEIVED") is UpdateOutcome.STALE    # late
    assert hook(svc, asp_b, "B-1", "ACCEPTED") is UpdateOutcome.STALE
    assert inv.state is State.REPORTED
    assert [e[1] for e in inv.asp_events] == ["TAX_CONFIRMED", "BUYER_RECEIVED", "ACCEPTED"]  # raw history kept


def test_contradictory_update_is_flagged_not_applied(svc, asp_b):
    inv = submit_b(svc)
    hook(svc, asp_b, "B-1", "BUYER_RECEIVED")
    assert hook(svc, asp_b, "B-1", "FAILED") is UpdateOutcome.CONFLICT
    assert inv.state is State.DELIVERED and inv.anomalies


def test_webhook_before_submit_response_is_parked_then_replayed(svc, asp_b):
    """The ASP's webhook for job B-1 arrives before we have processed the 202."""
    asp_b.jobs["B-1"] = {"reference": "not-yet-known", "number": "INV-1"}
    headers, body = asp_b.webhook("B-1", "ACCEPTED")
    del asp_b.jobs["B-1"]
    assert svc.handle_webhook("asp_b", headers, body) == [UpdateOutcome.PARKED]
    inv = submit_b(svc)                                  # 202 arrives, binds B-1, replays parked
    assert inv.state is State.ASP_ACCEPTED


def test_bad_signature_rejected(svc, asp_b):
    submit_b(svc)
    headers, body = asp_b.webhook("B-1", "ACCEPTED")
    with pytest.raises(InvalidSignature):
        svc.handle_webhook("asp_b", {"X-Signature": "nope"}, body)


def test_B_does_not_support_polling(svc, asp_b):
    inv = submit_b(svc)
    assert svc.poll(inv.id) is None
    with pytest.raises(NotSupported):
        asp_b.fetch_status("B-1")


def test_FAILURE_timeout_after_accept_is_never_resent_and_recovers_via_reference(svc, asp_b):
    """B has no duplicate check, so an ambiguous submit must NOT be retried."""
    asp_b.faults = ["timeout_after_accept"]
    inv = submit_b(svc)
    assert inv.state is State.SUBMIT_UNCERTAIN
    assert asp_b.submit_calls == 1 and len(asp_b.jobs) == 1          # exactly one job at the ASP
    with __import__("pytest").raises(PermissionError):
        svc.requeue(inv.id)                                          # can't blindly requeue either
    # B's webhook echoes our reference, which identifies the invoice even though we lost the job_id.
    assert hook(svc, asp_b, "B-1", "ACCEPTED") is UpdateOutcome.APPLIED
    assert inv.external_id == "B-1" and inv.state is State.ASP_ACCEPTED


def test_second_job_for_same_invoice_is_flagged(svc, asp_b):
    inv = submit_b(svc)
    asp_b.jobs["B-2"] = {"reference": inv.id, "number": "INV-1"}     # ASP created a duplicate job
    assert hook(svc, asp_b, "B-2", "ACCEPTED") is UpdateOutcome.CONFLICT
    assert inv.external_id == "B-1" and inv.anomalies
