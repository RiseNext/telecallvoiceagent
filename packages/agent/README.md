# rn-agent — agent definitions, tools, guardrails

Everything about *what an agent is* — and deliberately nothing about *how a graph runs*.

## Owns

- **Agent definition resolution and versioning.** Loading a versioned definition, snapshotting it for a call, and pinning `agent_version_id` so "which configuration handled this call?" is always answerable.
- **Instruction composition.** The layered prompt: platform safety layer, organization layer, agent layer, per-call context. The platform layer is not overridable by tenant configuration.
- **The typed tool registry.** Each tool declared once with a Pydantic argument schema, plus its permission requirements and idempotency semantics.
- **Guardrails.** AI disclosure, opt-out detection, refusal rules, output constraints.
- **Turn policy configuration.** VAD/endpointing parameters as per-agent data, tunable without a deploy.

## The reason this package has no LangChain dependency

The tool registry has two consumers with incompatible needs:

- `rn_voice` exports it as **flat** OpenAI Realtime function specs — and must not pull an orchestration framework into the audio process.
- `rn_orchestration` wraps it as LangChain `StructuredTool`s.

Declaring tools with plain Pydantic satisfies both. Declaring them with `@tool` would force LangChain into the media plane.

Related trap: `langchain-core`'s `convert_to_openai_tool()` emits the **nested** Chat-Completions shape, which the Realtime API rejects. The flat spec is generated from the Pydantic schema directly. This fails silently — the model simply never calls the tool.

## Rules

- No LangChain, no LangGraph, no database, no SQLAlchemy. Enforced by contracts.
- Business logic belongs in `rn_services`; a tool handler here is a thin, validated, authorized shim over a service call.
- Tenant identity is never a tool parameter. `organization_id`, `call_id` and `agent_version_id` are injected from server-side session context and ignored if the model emits them.
