# Payout flow

`POST /api/payouts/generate` -> batch `PENDING_REVIEW`; each eligible affiliate (APPROVED, payable balance >= `min_payout`) gets a payout and a
negative `PAYOUT` ledger entry (funds reserved, so nothing can be paid twice). Others are counted as "on hold".
`approve` -> `APPROVED`; `process` -> payouts `PROCESSING` with a provider reference.
Provider webhook `POST /api/webhooks/payment` (header `X-Signature` = hex HMAC-SHA256 of the raw body with `PAYMENT_WEBHOOK_SECRET`; body
`{"event_id","type":"payout.completed|payout.failed","reference"}`) -> `PAID`, or `FAILED` plus a `PAYOUT_RETURN` ledger entry. Events are idempotent by `event_id`.
Failed payouts can be retried. `python -m app.workers.monthly` generates batches for all organizations (cron / scheduler container).
