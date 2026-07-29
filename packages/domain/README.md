# rn-domain — the pure domain model

Entities, value objects, domain events and business policies. **No I/O of any kind.**

## Owns

- **Entities and value objects** — Organization, Agent, Call, Contact, Lead, Campaign, PhoneNumber, ConsentRecord, and the invariants that make them valid.
- **Domain events** — `call.completed`, `lead.qualified`, `meeting.booked`, and friends.
- **Policies** — pure decisions that need no database: is this contact eligible to be dialled right now? does this utterance constitute an opt-out? is this callback time ambiguous? what is the retry schedule for this outcome?

## Rules

Enforced by an import-linter contract — `rn_domain` may not import `sqlalchemy`, `alembic`, `fastapi`, `redis`, `httpx`, `langchain`, `langgraph`, `taskiq`, `openai`, or any cloud SDK.

`phonenumbers` is permitted: E.164 validation for an India-first dialler is a domain invariant, and the library is pure computation with no I/O.

## Why the purity matters here specifically

The rules in this package are the ones that must be exhaustively unit-tested — calling windows, consent, opt-out, retry, dedup. Those tests must run in milliseconds with no fixtures, because they will be run thousands of times as the rules evolve. Every dependency added here makes that slower and the rules harder to reason about.

If a policy needs to look something up, it does not belong here. Split it: the pure decision stays, the lookup moves to `rn_services` and passes the result in.
