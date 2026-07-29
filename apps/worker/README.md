# worker — the processing plane

Taskiq workers plus the scheduler. Everything expensive, retryable and analytical.

## Owns

- **Post-call pipeline** — transcript assembly, structured analysis, lead qualification, follow-up extraction, campaign metrics, usage and cost metering, outbound integration webhooks.
- **Knowledge ingestion** — parse, normalise, chunk, embed, index.
- **Imports and exports** — CSV/XLSX contact import; asynchronous Excel report generation with an expiring download link.
- **Campaign dispatch** — the scheduled job that computes an eligible dial budget and enqueues dial jobs through the compliance gate.
- **The outbox relay** — publishes what services recorded transactionally.
- **Reconciliation** — polls the telephony provider for calls stuck without a terminal status event, because callbacks are not guaranteed.

## Job rules

- **Idempotent.** At-least-once delivery is the contract. A job that runs twice must not place two calls, send two messages or double a metric.
- **Bounded.** Explicit timeout, bounded retries with backoff and jitter, then the dead-letter table. Never an infinite retry on a job that dials a phone number.
- **Observable.** Every job carries the correlation IDs of whatever triggered it.
- **Never blindly retried when not idempotent.** Dialling, messaging and booking need an idempotency key, not a retry decorator.

## Broker configuration is not optional detail

Only the Redis **stream** broker acknowledges messages. The PubSub and List brokers silently drop in-flight work when a worker dies — which for us means a campaign contact that was never called and never marked failed. Stream broker, `when_executed` acknowledgement. See [ADR-005](../../docs/DECISIONS/ADR-005-taskiq-job-system.md).

## The scheduler runs exactly once

Same image, different entrypoint, **one active replica**, holding a Postgres advisory lock on a direct (non-pooled) connection. Two schedulers means the campaign dispatcher fires twice, which means duplicate calls to real phone numbers. This is the highest-consequence operational rule in the repository.
