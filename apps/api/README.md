# api — the control plane

FastAPI. Configuration, authorization, and everything the dashboard talks to.

## Owns

- REST API under `/api/v1` for organizations, users and teams, agents and versions, phone numbers, campaigns, contacts and leads, knowledge bases, calls and transcripts, analytics, exports, integrations.
- Inbound webhooks: telephony status callbacks, messaging delivery receipts, identity-provider events.
- File uploads (CSV/XLSX) — accepted, validated at the header level, stored, then handed to a worker. Parsing a large spreadsheet in a request handler is how an API falls over.
- Enqueueing jobs. It never executes them.

## Rules

- **No business logic in route handlers.** A handler validates input, resolves the actor, delegates to `rn_services`, and serializes the result. If a handler has a branch about business rules, it is in the wrong file.
- **External schemas are not persistence models.** Never return an ORM object.
- **The acting organization comes from the verified token**, never from a request body, query parameter or header the client controls.
- Consistent pagination, filtering, sorting and error shape across every collection endpoint — the dashboard and the export path share the same filter vocabulary.
- Idempotency keys on anything with an external side effect.

## Webhook rules

Telephony status callbacks are **unsigned** and may be delayed or dropped entirely with no provider retry. So:

- Handlers are idempotent on the provider's call identifier.
- Authenticity is defended in layers: HTTPS, a high-entropy secret path segment, an IP allowlist, strict schema validation.
- A webhook alone never authorizes a state change with a financial effect.
- A reconciliation job — not this app — is what actually guarantees call state converges.

Identity-provider webhooks *are* signed. Verify the HMAC over the **raw** body before parsing; re-serializing the JSON breaks verification.
