from finkraft import State, UpdateOutcome
from conftest import new_ready


def test_happy_path_via_polling(svc, asp_a):
    inv = svc.submit(new_ready(svc).id)
    assert inv.state is State.SUBMITTED and inv.asp_id == "asp_a"
    for asp_status, expected in [("VALIDATED", State.ASP_ACCEPTED), ("DELIVERED", State.DELIVERED),
                                 ("REPORTED", State.REPORTED)]:
        asp_a.advance(inv.external_id, asp_status)
        assert svc.poll(inv.id) is UpdateOutcome.APPLIED
        assert inv.state is expected
    assert svc.poll(inv.id) is None                     # terminal: polling stops


def test_repeated_poll_of_same_status_is_not_reapplied(svc, asp_a):
    inv = svc.submit(new_ready(svc).id)
    asp_a.advance(inv.external_id, "VALIDATED")
    assert svc.poll(inv.id) is UpdateOutcome.APPLIED
    assert svc.poll(inv.id) is UpdateOutcome.DUPLICATE_EVENT
    assert len(inv.history) == 4                        # RECEIVED->READY->SUBMITTING->SUBMITTED->ASP_ACCEPTED


def test_asp_rejection_keeps_normalised_errors(svc, asp_a):
    inv = svc.submit(new_ready(svc, force_reject=True).id)
    assert inv.state is State.ASP_REJECTED
    assert (inv.errors[0].code, inv.errors[0].field) == ("E104", "BT48")


def test_throttled_then_success_backs_off_and_retries(svc, asp_a, sleeps):
    asp_a.faults = ["unavailable", "unavailable"]
    inv = svc.submit(new_ready(svc).id)
    assert inv.state is State.SUBMITTED and inv.attempts == 3
    assert len(sleeps) == 2 and sleeps[1] > 0


def test_retries_exhausted_is_safe_to_requeue(svc, asp_a):
    asp_a.faults = ["unavailable"] * 4
    inv = svc.submit(new_ready(svc).id)
    assert inv.state is State.SUBMIT_FAILED and not inv.possibly_sent
    assert svc.requeue(inv.id).state is State.READY
    assert svc.submit(inv.id).state is State.SUBMITTED


def test_FAILURE_timeout_after_accept_does_not_create_a_second_invoice(svc, asp_a):
    """ASP A accepted the invoice but the response timed out. The retry gets a 409,
    which we adopt (we know our earlier attempt may have landed)."""
    asp_a.faults = ["timeout_after_accept"]
    inv = svc.submit(new_ready(svc).id)
    assert inv.state is State.SUBMITTED
    assert inv.external_id == "A-1" and len(asp_a.records) == 1
    assert inv.possibly_sent and inv.attempts == 2


def test_409_on_first_attempt_is_not_adopted(svc, asp_a):
    """Someone else (not an earlier Finkraft attempt) already used this number at the ASP."""
    asp_a.records[("seller_a", "INV-1")] = {"id": "A-99", "status": "RECEIVED", "at": 0}
    inv = svc.submit(new_ready(svc).id)
    assert inv.state is State.SUBMIT_FAILED and inv.external_id is None
    assert inv.errors[0].code == "DUPLICATE_AT_ASP"
