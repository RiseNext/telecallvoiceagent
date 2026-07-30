"""Resolving a stored agent version into a runtime snapshot.

The composition that the layer graph requires to live here rather than in
`rn_services`:

    rn_services.AgentConfigurationSource   reads the database, returns domain data
    rn_agent.snapshot.build_snapshot       builds the runtime object, purely
    this module                            joins the two

`AgentSnapshot` is defined in `rn_agent`, which sits *above* `rn_services`, so a
resolver in `rn_services` returning one would be an upward import. And `rn_agent`
may not import `rn_persistence`, so the read arrives through a protocol rather than
as three repositories. Neither of those is a contract being worked around — together
they are what keeps the pure builder testable with no database, which is why the
snapshot tests need no container.
"""

from __future__ import annotations

from rn_agent.snapshot import AgentSnapshot, build_snapshot
from rn_agent.tools.registry import ToolRegistry
from rn_domain.identifiers import AgentVersionId

# `rn_services.contracts`, not `rn_services.agents`: the concrete loader imports
# repositories, and importing it here would load the ORM into every process that
# resolves a snapshot — including, eventually, the voice gateway.
from rn_services.contracts import AgentConfigurationSource, PublishedAgentConfiguration

__all__ = ["resolve_published_snapshot", "snapshot_from_configuration"]


def snapshot_from_configuration(
    configuration: PublishedAgentConfiguration, *, registry: ToolRegistry
) -> AgentSnapshot:
    """Build a snapshot from already-loaded configuration. Pure.

    Separate from `resolve_published_snapshot` so the build can be tested against
    hand-constructed configuration, and so a caller that already holds the
    configuration — a dashboard rendering a version diff — does not re-read it.
    """
    return build_snapshot(
        version=configuration.version,
        enabled_tool_names=configuration.enabled_tool_names,
        knowledge_base_ids=configuration.knowledge_base_ids,
        registry=registry,
        organization_instructions=configuration.organization_instructions,
    )


async def resolve_published_snapshot(
    *,
    agent_version_id: AgentVersionId,
    source: AgentConfigurationSource,
    registry: ToolRegistry,
) -> AgentSnapshot:
    """Load and build the snapshot for one published agent version.

    Tenant scoping belongs entirely to `source`: it was constructed with a
    server-derived `TenantContext` and exposes no organization parameter, so a
    version belonging to another tenant raises `NotFoundError` — not "forbidden",
    which would confirm the row exists.

    Raises:
        NotFoundError: no such version in this tenant.
        SnapshotResolutionError: the version exists but is a draft or archived.
            Refused twice, here via `build_snapshot` and again at the write
            boundary, because serving draft configuration on a real call cannot be
            undone afterwards.
        AgentConfigurationError: stored JSONB configuration is malformed.
    """
    configuration = await source.load_published(agent_version_id)
    return snapshot_from_configuration(configuration, registry=registry)
