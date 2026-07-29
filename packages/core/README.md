# rn-core — shared kernel

The bottom of the stack. Everything depends on it; it depends on nothing internal.

## Owns

- **Configuration** — typed settings loaded from the environment, validated at startup. A missing required setting must fail the boot, not the first call at 2am.
- **Errors** — the error taxonomy every layer raises and the API translates.
- **Identifiers** — UUID generation, prefixed public IDs, correlation IDs.
- **Time** — timezone-aware helpers. Naive datetimes are banned; call scheduling in IST makes them a correctness bug, not a style issue.
- **Structured logging** — the logger factory and the redaction processor. Redaction lives in the pipeline, not at call sites, because call sites forget.
- **Telemetry primitives** — OpenTelemetry setup, span helpers, the metric registry.

## Rules

- No I/O. No database, no HTTP, no broker, no vendor SDK.
- Nothing here may know what an "organization" or a "call" is — that is `rn_domain`.
- Anything added here is imported by every process including the voice gateway, so import cost is a real concern. Keep module-level work near zero.

## Does not belong here

Business rules, entities, provider adapters, or anything that would make this package a dumping ground for "shared stuff". If it does not fit one of the six categories above, it belongs in a layer that knows more.
