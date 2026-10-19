# Finkraft e-invoicing: ASP submission core (Part 2)

A minimal, in-memory implementation of the design in `Finkraft_CTO_Submission.pdf` (Part 1).
Python 3.10+, no runtime dependencies.

## Run

```bash
python -m venv .venv && source .venv/bin/activate
pip install pytest
pytest -q          # 38 tests, ~0.1s (pythonpath is set in pyproject.toml; no install needed)
```

## Layout

```
src/finkraft/
  state_machine.py   States, allowed transitions, terminal states, forward-only ranking
  models.py          Invoice + the types that cross the adapter boundary
  adapters/base.py   ASPAdapter contract (submit / fetch_status / parse_webhook / capabilities)
  adapters/asp_a.py  Mock ASP A: sync submit, polling, 409 on duplicate numbers
  adapters/asp_b.py  Mock ASP B: 202 + job_id, signed webhooks, NO duplicate check
  store.py           In-memory store; comments show the Postgres constraints it stands in for
  service.py         ingest, submit (retry/claim/classify), status inflow, operator requeue
  validation.py      Stand-in for PINT-AE validation
tests/               state machine, ingest/duplicates, ASP A, ASP B, routing/concurrency
```

## What the tests prove

- Invalid transitions are rejected (e.g. `READY -> DELIVERED`, anything out of a terminal state).
- Duplicate `(seller, invoice_number)` is rejected before any ASP call; same number for another seller is fine.
- `submit()` is idempotent and race-safe: 20 concurrent callers produce exactly one ASP call.
- **ASP A timeout after accept**: retry gets 409, we adopt the existing submission. One invoice at the ASP.
- **ASP B timeout after accept**: *not* resent (B has no dedupe). Invoice goes to `SUBMIT_UNCERTAIN`, and
  is resolved by a webhook that echoes our `reference`. One job at the ASP.
- Webhooks: duplicate ignored, out-of-order never moves state backwards, contradictory update flagged as
  `CONFLICT` (not applied), update arriving before the submit response is parked and replayed, bad signature rejected.
- Switching a seller's ASP only affects invoices not yet submitted.

## Assumptions (beyond the spec)

- **A1 (ASP A):** the 409 body includes the existing `submission_id`; validation errors are 422; 429/503 mean "not processed".
- **A2 (ASP B):** accepts a caller `reference` on submit and echoes it in every webhook, with a unique `event_id`;
  webhooks are HMAC-signed and retried until 2xx. If A2 is false, `SUBMIT_UNCERTAIN` invoices on B need manual reconciliation.
- **A3:** invoice numbers are unique per seller, exact match after trimming. A number can be reused only if the earlier
  invoice failed *our* validation. One that reached an ASP needs a new number / credit note.
- **A4:** a seller routes to one ASP at a time; the ASP is pinned to the invoice when submission is first claimed.

## Skipped (deliberately, within the time box)

- Persistence, a real queue/outbox and worker loop. `submit()` is called directly and retries inline.
- Rate limiter (10 rps for ASP A). Design: shared token bucket per ASP account at ~8 rps, applied before `adapter.submit` and `fetch_status`.
- Real PINT-AE (XML/Schematron) validation and enrichment; HTTP clients; webhook HTTP endpoint.
- Poll scheduling (`poll_all` exists, but no scheduler or age-based backoff).
- Crash recovery: a reaper that treats long-stuck `SUBMITTING` invoices as possibly sent (A: resubmit is safe; B: reconcile).
- Stuck-invoice alerting for `SUBMIT_UNCERTAIN` / no-webhook-for-N-minutes, daily ledger-vs-ASP reconciliation.
- AuthN/Z, tenant isolation, secrets handling, audit-log immutability.

## Next

1. Postgres schema with the unique constraints noted in `store.py`, plus a transactional outbox.
2. A shared adapter *contract test suite* run against the mocks and each ASP's sandbox.
3. Rate limiter, poll scheduler, reconciliation sweeper, operator console for `SUBMIT_UNCERTAIN`/`SUBMIT_FAILED`.
4. Revision flow for invoices rejected by an ASP (credit note / re-issue), once FTA rules are confirmed.
