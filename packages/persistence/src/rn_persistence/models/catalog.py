"""Agent configuration and knowledge tables.

`agent_versions` is the important one: it is **immutable once published**, and a
call pins exactly one row of it forever. Immutability is enforced by a database
trigger created in the baseline migration, not by convention — a rule that only
holds when the application remembers it is not a rule.

Knowledge is **metadata only** in Phase 1. `documents` and `document_chunks`
arrive in Phase 3, once open decision **D-8** settles the embedding model, width,
column type and index strategy (ADR-010). There is deliberately no vector column
anywhere in this schema.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column

from rn_domain.entities.agents import Agent, AgentToolConfig, AgentVersion, KnowledgeBase
from rn_domain.enums import AgentStatus, AgentVersionStatus
from rn_domain.identifiers import (
    AgentId,
    AgentVersionId,
    KnowledgeBaseId,
    OrganizationId,
    UserId,
)
from rn_domain.values import LanguageTag
from rn_persistence.base import (
    TenantOwnedBase,
    created_at_column,
    enum_check,
    json_column,
)

__all__ = [
    "AgentModel",
    "AgentToolConfigModel",
    "AgentVersionKnowledgeBaseModel",
    "AgentVersionModel",
    "KnowledgeBaseModel",
]


class AgentModel(TenantOwnedBase):
    """An agent definition. Mutable metadata only."""

    __tablename__ = "agents"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    # Nullable, set after the first version exists. `agents` and `agent_versions`
    # form a cycle; it is resolved by writing this in the same transaction rather
    # than with a DEFERRABLE constraint, whose failures surface at COMMIT with
    # unhelpful diagnostics. No FK here for the same reason.
    current_version_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        # Target of the composite FKs on children — this is what makes
        # cross-tenant parentage structurally impossible.
        UniqueConstraint("organization_id", "id", name="uq_agents_organization_id_id"),
        UniqueConstraint("organization_id", "name", name="uq_agents_organization_id_name"),
        enum_check("status", AgentStatus, "status"),
        # The dashboard list: one org's agents, newest first.
        Index("ix_agents_organization_id_created_at", "organization_id", "created_at"),
    )

    def to_domain(self) -> Agent:
        return Agent(
            id=AgentId(self.id),
            organization_id=OrganizationId(self.organization_id),
            name=self.name,
            description=self.description,
            status=AgentStatus(self.status),
            current_version_id=(
                AgentVersionId(self.current_version_id) if self.current_version_id else None
            ),
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, entity: Agent) -> AgentModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            name=entity.name,
            description=entity.description,
            status=entity.status.value,
            current_version_id=entity.current_version_id,
            created_at=entity.created_at,
            deleted_at=entity.deleted_at,
        )

    def apply(self, entity: Agent) -> None:
        self.name = entity.name
        self.description = entity.description
        self.status = entity.status.value
        self.current_version_id = entity.current_version_id
        self.deleted_at = entity.deleted_at


class AgentVersionModel(TenantOwnedBase):
    """An immutable behavioural snapshot of an agent."""

    __tablename__ = "agent_versions"

    agent_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    instructions: Mapped[str] = mapped_column(Text, nullable=False)
    languages: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    voice_map: Mapped[dict[str, Any]] = json_column()
    turn_policy: Mapped[dict[str, Any]] = json_column()
    guardrail_config: Mapped[dict[str, Any]] = json_column()
    realtime_model: Mapped[str | None] = mapped_column(String(100), nullable=True)
    telephony_sample_rate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(nullable=True)
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_id"],
            ["agents.organization_id", "agents.id"],
            name="fk_agent_versions_organization_id_agent_id_agents",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("organization_id", "id", name="uq_agent_versions_organization_id_id"),
        UniqueConstraint(
            "agent_id", "version_number", name="uq_agent_versions_agent_id_version_number"
        ),
        enum_check("status", AgentVersionStatus, "status"),
        CheckConstraint("version_number >= 1", name="version_number"),
        # Only the three sample rates the telephony provider accepts (HC-3).
        # Nullable until the realtime phase chooses one per agent (ADR-003).
        CheckConstraint(
            "telephony_sample_rate IS NULL OR telephony_sample_rate IN (8000, 16000, 24000)",
            name="telephony_sample_rate",
        ),
        # A published version must carry its publish instant. The trigger in the
        # baseline migration enforces immutability; this enforces coherence.
        CheckConstraint(
            "status <> 'published' OR published_at IS NOT NULL",
            name="published_at",
        ),
        Index("ix_agent_versions_agent_id", "agent_id"),
    )

    def to_domain(self) -> AgentVersion:
        return AgentVersion(
            id=AgentVersionId(self.id),
            organization_id=OrganizationId(self.organization_id),
            agent_id=AgentId(self.agent_id),
            version_number=self.version_number,
            instructions=self.instructions,
            languages=tuple(LanguageTag(tag) for tag in self.languages),
            status=AgentVersionStatus(self.status),
            voice_map=dict(self.voice_map),
            turn_policy=dict(self.turn_policy),
            guardrail_config=dict(self.guardrail_config),
            realtime_model=self.realtime_model,
            telephony_sample_rate=self.telephony_sample_rate,
            published_at=self.published_at,
            created_by_user_id=(
                UserId(self.created_by_user_id) if self.created_by_user_id else None
            ),
            created_at=self.created_at,
        )

    @classmethod
    def from_domain(cls, entity: AgentVersion) -> AgentVersionModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            agent_id=entity.agent_id,
            version_number=entity.version_number,
            instructions=entity.instructions,
            languages=[str(tag) for tag in entity.languages],
            status=entity.status.value,
            voice_map=dict(entity.voice_map),
            turn_policy=dict(entity.turn_policy),
            guardrail_config=dict(entity.guardrail_config),
            realtime_model=entity.realtime_model,
            telephony_sample_rate=entity.telephony_sample_rate,
            published_at=entity.published_at,
            created_by_user_id=entity.created_by_user_id,
            created_at=entity.created_at,
        )

    def apply(self, entity: AgentVersion) -> None:
        """Copy mutable fields.

        Only `status` and `published_at` may legitimately change after publish;
        the database trigger rejects anything else, so an attempt to mutate
        behaviour here fails loudly rather than silently rewriting history.
        """
        self.status = entity.status.value
        self.published_at = entity.published_at
        if entity.published_at is None:
            self.instructions = entity.instructions
            self.languages = [str(tag) for tag in entity.languages]
            self.voice_map = dict(entity.voice_map)
            self.turn_policy = dict(entity.turn_policy)
            self.guardrail_config = dict(entity.guardrail_config)
            self.realtime_model = entity.realtime_model
            self.telephony_sample_rate = entity.telephony_sample_rate


class AgentToolConfigModel(TenantOwnedBase):
    """One tool enabled on one agent version."""

    __tablename__ = "agent_tool_configs"

    agent_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    config: Mapped[dict[str, Any]] = json_column()
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.id"],
            # Shortened deliberately: Postgres truncates identifiers at 63
            # characters, and a truncated name cannot be dropped by the name the
            # migration thinks it has. Asserted by a test.
            name="fk_agent_tool_configs_org_id_agent_version_id",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "agent_version_id", "tool_name", name="uq_agent_tool_configs_version_tool"
        ),
    )

    def to_domain(self) -> AgentToolConfig:
        return AgentToolConfig(
            organization_id=OrganizationId(self.organization_id),
            agent_version_id=AgentVersionId(self.agent_version_id),
            tool_name=self.tool_name,
            enabled=self.enabled,
            config=dict(self.config),
        )


class KnowledgeBaseModel(TenantOwnedBase):
    """Knowledge-base metadata. No documents, no chunks, no vectors in Phase 1."""

    __tablename__ = "knowledge_bases"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = created_at_column()
    deleted_at: Mapped[datetime | None] = mapped_column(nullable=True)

    __table_args__ = (
        UniqueConstraint("organization_id", "id", name="uq_knowledge_bases_organization_id_id"),
        UniqueConstraint("organization_id", "name", name="uq_knowledge_bases_organization_id_name"),
    )

    def to_domain(self) -> KnowledgeBase:
        return KnowledgeBase(
            id=KnowledgeBaseId(self.id),
            organization_id=OrganizationId(self.organization_id),
            name=self.name,
            description=self.description,
            created_at=self.created_at,
            deleted_at=self.deleted_at,
        )

    @classmethod
    def from_domain(cls, entity: KnowledgeBase) -> KnowledgeBaseModel:
        return cls(
            id=entity.id,
            organization_id=entity.organization_id,
            name=entity.name,
            description=entity.description,
            created_at=entity.created_at,
            deleted_at=entity.deleted_at,
        )


class AgentVersionKnowledgeBaseModel(TenantOwnedBase):
    """Which knowledge bases an agent version is bound to.

    The binding belongs to the *version* rather than the agent: what an agent
    knows is part of its behaviour, and a call must be able to answer "which
    knowledge was in scope" as precisely as "which prompt".
    """

    __tablename__ = "agent_version_knowledge_bases"

    agent_version_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    knowledge_base_id: Mapped[uuid.UUID] = mapped_column(nullable=False)
    created_at: Mapped[datetime] = created_at_column()

    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "agent_version_id"],
            ["agent_versions.organization_id", "agent_versions.id"],
            name="fk_avkb_organization_id_agent_version_id_agent_versions",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "knowledge_base_id"],
            ["knowledge_bases.organization_id", "knowledge_bases.id"],
            name="fk_avkb_organization_id_knowledge_base_id_knowledge_bases",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "agent_version_id", "knowledge_base_id", name="uq_avkb_version_knowledge_base"
        ),
    )
