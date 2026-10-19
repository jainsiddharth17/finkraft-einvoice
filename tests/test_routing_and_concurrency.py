import threading

from finkraft import State
from conftest import new_ready


def test_switching_asp_only_affects_unsent_invoices(svc, asp_a, asp_b):
    first = svc.submit(new_ready(svc, "seller_a", "INV-1").id)
    assert first.asp_id == "asp_a"
    svc.routing["seller_a"] = "asp_b"                    # customer switches ASP
    second = svc.submit(new_ready(svc, "seller_a", "INV-2").id)
    assert second.asp_id == "asp_b"
    assert first.asp_id == "asp_a"                       # in-flight invoice stays with its original ASP


def test_submit_is_idempotent(svc, asp_a):
    inv = new_ready(svc)
    svc.submit(inv.id)
    svc.submit(inv.id)
    assert asp_a.submit_calls == 1


def test_concurrent_submit_calls_reach_the_asp_once(svc, asp_a):
    inv = new_ready(svc)
    threads = [threading.Thread(target=svc.submit, args=(inv.id,)) for _ in range(20)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert asp_a.submit_calls == 1 and inv.state is State.SUBMITTED


def test_invoice_not_ready_is_never_submitted(svc, asp_a):
    bad = new_ready(svc, buyer_name="")
    svc.submit(bad.id)
    assert asp_a.submit_calls == 0 and bad.state is State.VALIDATION_FAILED
