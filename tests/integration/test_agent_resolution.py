"""Resolving a snapshot from the database, and the tenancy boundary around it.

Two Phase-2 definition-of-done items live here, and neither can be tested without a
real database:

* **"Changing instructions creates a new version; prior conversation still resolves
  the old one."** The whole versioning scheme exists for this, and it is what makes
  "which configuration handled this call?" answerable in March about a call in
  January.
* **Draft and archived versions are refused.** Serving draft configuration on a real
  call cannot be undone afterwards.

Plus the tenancy property that everything else rests on: knowing another tenant's
`agent_version_id` gets you a `NotFoundError`, not their agent.
"""

from __future__ import annotations

import dataclasses

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from rn_agent.errors import SnapshotResolutionError
from rn_agent.resolve import resolve_published_snapshot
from rn_agent.tools import REGISTRY
from rn_core.clock import now_utc
from rn_core.errors import NotFoundError
from rn_core.ids import new_id
from rn_domain.entities.agents import AgentVersion
from rn_domain.enums import AgentVersionStatus
from rn_domain.identifiers import AgentId, AgentVersionId, OrganizationId
from rn_domain.tenancy import TenantContext
from rn_domain.values import LanguagePolicy, LanguageTag
from rn_persistence.models import AgentVersionModel
from rn_persistence.repositories import (
    AgentToolConfigRepository,
    AgentVersionKnowledgeBaseRepository,
    AgentVersionRepository,
    KnowledgeBaseRepository,
)
from rn_services.agents import AgentConfigurationService, KnowledgeCatalogService
from tests.integration import factories

pytestmark = [pytest.mark.integration]

_PERMS = frozenset({"org:knowledge:read", "org:agent:read"})


def _context(organization_id: object) -> TenantContext:
    return TenantContext(
        organization_id=OrganizationId(organization_id),  # type: ignore[arg-type]
        permissions=_PERMS,
    )


def _source(session: AsyncSession, context: TenantContext) -> AgentConfigurationService:
    return AgentConfigurationService(
        versions=AgentVersionRepository(session, context),
        tool_configs=AgentToolConfigRepository(session, context),
        knowledge_bindings=AgentVersionKnowledgeBaseRepository(session, context),
    )


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


async def test_a_published_version_resolves_with_its_tools_and_bindings(
    session: AsyncSession,
) -> None:
    org = await factories.create_organization(session)
    version = await factories.create_agent_version(session, organization_id=org.id)
    knowledge_base = await factories.create_knowledge_base(session, organization_id=org.id)
    await factories.enable_tool(
        session,
        organization_id=org.id,
        agent_version_id=version.id,
        tool_name="list_knowledge_bases",
    )
    await factories.enable_tool(
        session,
        organization_id=org.id,
        agent_version_id=version.id,
        tool_name="find_knowledge_base",
        enabled=False,
    )
    await factories.bind_knowledge_base(
        session,
        organization_id=org.id,
        agent_version_id=version.id,
        knowledge_base_id=knowledge_base.id,
    )
    await session.commit()

    context = _context(org.id)
    snapshot = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(version.id),
        source=_source(session, context),
        registry=REGISTRY,
    )

    assert snapshot.agent_version_id == version.id
    assert snapshot.organization_id == org.id
    # The disabled row is dropped rather than carried with a flag: "enabled but
    # disabled" is not a state anything above needs to reason about.
    assert snapshot.enabled_tools == frozenset({"list_knowledge_bases"})
    assert snapshot.knowledge_base_ids == (knowledge_base.id,)
    assert snapshot.language_policy.primary == LanguageTag("en")
    assert snapshot.content_hash


async def test_resolution_is_deterministic_across_reads(session: AsyncSession) -> None:
    """Two resolutions of the same stored version produce the same snapshot.

    Not a repeat of the unit test: this one reads through Postgres, where JSONB key
    order and array ordering are the database's choice rather than ours.
    """
    org = await factories.create_organization(session)
    version = await factories.create_agent_version(
        session,
        organization_id=org.id,
        turn_policy={"eagerness": "high", "mode": "server_vad"},
        voice_map={"en": {"provider": "openai", "voice_id": "marin"}},
    )
    for name in ("find_knowledge_base", "list_knowledge_bases"):
        await factories.enable_tool(
            session, organization_id=org.id, agent_version_id=version.id, tool_name=name
        )
    await session.commit()

    context = _context(org.id)
    first = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(version.id),
        source=_source(session, context),
        registry=REGISTRY,
    )
    second = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(version.id),
        source=_source(session, context),
        registry=REGISTRY,
    )
    assert first.content_hash == second.content_hash
    assert first == second


# ---------------------------------------------------------------------------
# Publication state
# ---------------------------------------------------------------------------


async def test_a_draft_version_is_refused(session: AsyncSession) -> None:
    org = await factories.create_organization(session)
    version = await factories.create_agent_version(session, organization_id=org.id, published=False)
    await session.commit()

    with pytest.raises(SnapshotResolutionError):
        await resolve_published_snapshot(
            agent_version_id=AgentVersionId(version.id),
            source=_source(session, _context(org.id)),
            registry=REGISTRY,
        )


async def test_an_archived_version_is_refused(session: AsyncSession) -> None:
    """Archived served calls once; it must not serve new ones."""
    org = await factories.create_organization(session)
    version = await factories.create_agent_version(
        session, organization_id=org.id, status=AgentVersionStatus.ARCHIVED
    )
    await session.commit()

    with pytest.raises(SnapshotResolutionError):
        await resolve_published_snapshot(
            agent_version_id=AgentVersionId(version.id),
            source=_source(session, _context(org.id)),
            registry=REGISTRY,
        )


# ---------------------------------------------------------------------------
# Immutable versioning: the definition-of-done gate
# ---------------------------------------------------------------------------


async def test_a_prior_conversation_still_resolves_the_old_version(
    session: AsyncSession,
) -> None:
    """The reason the versioning scheme exists.

    Publishing a new version must not change what an in-flight call is doing, and
    "which configuration handled this call?" must stay answerable exactly, forever.
    """
    org = await factories.create_organization(session)
    v1 = await factories.create_agent_version(
        session, organization_id=org.id, instructions="Version one instructions, be brief."
    )
    await factories.enable_tool(
        session, organization_id=org.id, agent_version_id=v1.id, tool_name="list_knowledge_bases"
    )
    await session.commit()

    context = _context(org.id)
    pinned = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(v1.id),
        source=_source(session, context),
        registry=REGISTRY,
    )

    # A second version of the same agent, with different instructions and a
    # different tool set — which is what "editing an agent" actually does.
    v2 = AgentVersionModel(
        id=new_id(),
        organization_id=org.id,
        agent_id=v1.agent_id,
        version_number=2,
        instructions="Version two instructions, be thorough and ask questions.",
        languages=factories.DEFAULT_LANGUAGE_POLICY.projection,
        language_policy=factories.DEFAULT_LANGUAGE_POLICY.to_storage(),
        status=AgentVersionStatus.PUBLISHED.value,
        voice_map={},
        turn_policy={},
        guardrail_config={},
        published_at=now_utc(),
        created_at=now_utc(),
    )
    session.add(v2)
    await session.flush()
    await factories.enable_tool(
        session, organization_id=org.id, agent_version_id=v2.id, tool_name="find_knowledge_base"
    )
    await session.commit()

    # The pinned id still resolves the old configuration, unchanged.
    again = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(v1.id),
        source=_source(session, context),
        registry=REGISTRY,
    )
    assert again.content_hash == pinned.content_hash
    assert "Version one instructions" in again.instruction_prefix
    assert "Version two" not in again.instruction_prefix
    assert again.enabled_tools == frozenset({"list_knowledge_bases"})

    # And the new one is genuinely different.
    fresh = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(v2.id),
        source=_source(session, context),
        registry=REGISTRY,
    )
    assert fresh.content_hash != pinned.content_hash
    assert fresh.enabled_tools == frozenset({"find_knowledge_base"})
    assert fresh.version_number == 2


# ---------------------------------------------------------------------------
# Tenancy
# ---------------------------------------------------------------------------


async def test_another_tenants_version_is_not_found(session: AsyncSession) -> None:
    """The realistic attacker position: the id is known exactly.

    Ids leak — through URLs, exports, logs. `NotFoundError` rather than "forbidden",
    because a distinguishable answer confirms a row with that id exists somewhere.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    version_b = await factories.create_agent_version(session, organization_id=org_b.id)
    await factories.enable_tool(
        session,
        organization_id=org_b.id,
        agent_version_id=version_b.id,
        tool_name="list_knowledge_bases",
    )
    await session.commit()

    with pytest.raises(NotFoundError):
        await resolve_published_snapshot(
            agent_version_id=AgentVersionId(version_b.id),
            source=_source(session, _context(org_a.id)),
            registry=REGISTRY,
        )


async def test_tool_configuration_does_not_leak_across_tenants(
    session: AsyncSession,
) -> None:
    """Two versions with the same tool names in different tenants stay separate.

    A `list_for_version` that filtered on `agent_version_id` alone — forgetting the
    tenant predicate — would pass every other test in this file and fail here.
    """
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    version_a = await factories.create_agent_version(session, organization_id=org_a.id)
    version_b = await factories.create_agent_version(session, organization_id=org_b.id)
    await factories.enable_tool(
        session,
        organization_id=org_a.id,
        agent_version_id=version_a.id,
        tool_name="list_knowledge_bases",
    )
    await factories.enable_tool(
        session,
        organization_id=org_b.id,
        agent_version_id=version_b.id,
        tool_name="find_knowledge_base",
    )
    await session.commit()

    snapshot_a = await resolve_published_snapshot(
        agent_version_id=AgentVersionId(version_a.id),
        source=_source(session, _context(org_a.id)),
        registry=REGISTRY,
    )
    assert snapshot_a.enabled_tools == frozenset({"list_knowledge_bases"})

    tool_configs_a = AgentToolConfigRepository(session, _context(org_a.id))
    assert await tool_configs_a.list_for_version(version_b.id) == []


# ---------------------------------------------------------------------------
# The knowledge catalog a tool reads
# ---------------------------------------------------------------------------


async def test_the_knowledge_catalog_is_tenant_scoped(session: AsyncSession) -> None:
    org_a = await factories.create_organization(session)
    org_b = await factories.create_organization(session)
    await factories.create_knowledge_base(session, organization_id=org_a.id, name="Pricing")
    await factories.create_knowledge_base(session, organization_id=org_b.id, name="Secrets")
    await session.commit()

    catalog_a = KnowledgeCatalogService(KnowledgeBaseRepository(session, _context(org_a.id)))
    names = {summary.name for summary in await catalog_a.list_knowledge_bases(limit=50)}
    assert names == {"Pricing"}

    # Knowing the name is not access to it.
    with pytest.raises(NotFoundError):
        await catalog_a.find_knowledge_base(name="Secrets")


async def test_a_soft_deleted_knowledge_base_is_invisible(session: AsyncSession) -> None:
    org = await factories.create_organization(session)
    knowledge_base = await factories.create_knowledge_base(
        session, organization_id=org.id, name="Retired"
    )
    knowledge_base.deleted_at = now_utc()
    await session.commit()

    catalog = KnowledgeCatalogService(KnowledgeBaseRepository(session, _context(org.id)))
    assert await catalog.list_knowledge_bases(limit=50) == []
    with pytest.raises(NotFoundError):
        await catalog.find_knowledge_base(name="Retired")


async def test_language_policy_survives_a_round_trip_through_postgres(
    session: AsyncSession,
) -> None:
    """The projection and the policy are written together and read back as one fact."""
    org = await factories.create_organization(session)
    policy = LanguagePolicy(
        primary=LanguageTag("te-IN"),
        allowed=(LanguageTag("te-IN"), LanguageTag("en")),
        follow_caller=False,
        code_switch=True,
    )
    version = await factories.create_agent_version(
        session, organization_id=org.id, language_policy=policy
    )
    await session.commit()
    session.expunge_all()

    reloaded = await AgentVersionRepository(session, _context(org.id)).get(version.id)
    entity = reloaded.to_domain()
    assert entity.language_policy == policy
    # `languages` is derived, so it cannot disagree.
    assert entity.languages == policy.allowed
    assert reloaded.languages == policy.projection


# ---------------------------------------------------------------------------
# The ORM mapper: `from_domain` / `apply` / `to_domain`
# ---------------------------------------------------------------------------


async def test_from_domain_writes_the_projection_and_the_policy_together(
    session: AsyncSession,
) -> None:
    """Closes a gap mutation testing found: this mapper had no coverage at all.

    A mutation making `from_domain` write a projection that disagreed with the policy
    survived the whole suite, because every other test builds the model directly. The
    database CHECK is the real backstop — this proves the mapper does not rely on it.
    """
    org = await factories.create_organization(session)
    agent = await factories.create_agent_version(session, organization_id=org.id)
    await session.commit()

    policy = LanguagePolicy(
        primary=LanguageTag("hi-IN"),
        allowed=(LanguageTag("hi-IN"), LanguageTag("en"), LanguageTag("te-IN")),
        follow_caller=False,
        code_switch=False,
    )
    entity = AgentVersion(
        id=AgentVersionId(new_id()),
        organization_id=OrganizationId(org.id),
        agent_id=AgentId(agent.agent_id),
        version_number=99,
        instructions="Written through from_domain, not through the factory.",
        language_policy=policy,
        status=AgentVersionStatus.DRAFT,
    )
    session.add(AgentVersionModel.from_domain(entity))
    await session.commit()
    session.expunge_all()

    reloaded = await AgentVersionRepository(session, _context(org.id)).get(entity.id)
    assert reloaded.languages == policy.projection
    assert reloaded.to_domain().language_policy == policy


async def test_apply_keeps_the_projection_in_step_on_a_draft(session: AsyncSession) -> None:
    """`apply` edits a draft. If it wrote one field and not the other the row would be
    rejected by the CHECK — which is the right outcome, but the mapper must not get
    there in the first place."""
    org = await factories.create_organization(session)
    row = await factories.create_agent_version(session, organization_id=org.id, published=False)
    await session.commit()

    entity = row.to_domain()
    edited = dataclasses.replace(
        entity,
        language_policy=LanguagePolicy(
            primary=LanguageTag("te-IN"), allowed=(LanguageTag("te-IN"),)
        ),
    )
    row.apply(edited)
    await session.commit()
    session.expunge_all()

    reloaded = await AgentVersionRepository(session, _context(org.id)).get(row.id)
    assert reloaded.languages == ["te-IN"]
    assert reloaded.to_domain().language_policy.primary == LanguageTag("te-IN")
