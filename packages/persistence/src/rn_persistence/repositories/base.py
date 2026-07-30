"""Repository base classes.

The single most important property in this file: **a tenant-scoped repository
cannot be constructed without a `TenantContext`, and cannot be asked to read
outside it.** There is no `organization_id` parameter on any read method, so
there is no way for a caller to pass the wrong one — the scope comes from the
context the repository was built with.

Compare the shape we deliberately did not build:

    repo.get_contacts(organization_id)      # caller supplies the tenant: one
                                            # forgotten variable is a breach

against what is here:

    repo = ContactRepository(session, ctx)  # tenant fixed at construction
    await repo.get(contact_id)              # scope is not the caller's problem

A miss returns `NotFoundError`, never "forbidden". Distinguishing "does not
exist" from "exists but belongs to someone else" tells an attacker that a row
with that id exists somewhere, which is exactly the fact tenant isolation is
meant to hide.

Cross-tenant access is not reachable from here at all. It lives in
`PlatformRepository`, which takes a `PlatformContext` — a different type, a
different class name, and therefore something that shows up in a code review.

**Phase 1 has no row-level security.** Isolation here is application-level
scoping plus the composite foreign keys in the schema. RLS is defence in depth
scheduled for Phase 15; nothing in this file should be read as providing it.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from rn_core.errors import InvariantViolation, NotFoundError
from rn_domain.tenancy import PlatformContext, TenantContext
from rn_persistence.base import Base, TenantOwnedBase

__all__ = ["Page", "PlatformRepository", "TenantScopedRepository"]

#: Hard ceiling on any single page. A caller asking for a million rows gets a
#: thousand — an unbounded query against a tenant's call history is how one
#: dashboard request takes the database down for everyone.
MAX_PAGE_SIZE = 1000
DEFAULT_PAGE_SIZE = 50


class Page[ModelT: Base]:
    """One page of results plus the cursor needed to continue.

    Deliberately not a total count. `COUNT(*)` over a large tenant's calls is a
    full scan on every page request; the UI gets "there is more" instead, which
    is what it actually needs to render a next button.
    """

    __slots__ = ("has_more", "items", "next_offset")

    def __init__(self, items: Sequence[ModelT], *, has_more: bool, next_offset: int) -> None:
        self.items = list(items)
        self.has_more = has_more
        self.next_offset = next_offset

    def __len__(self) -> int:
        return len(self.items)

    def __iter__(self) -> Any:
        return iter(self.items)


class TenantScopedRepository[ModelT: TenantOwnedBase]:
    """Base for every repository over a tenant-owned table.

    Bound to `TenantOwnedBase`, not to `Base`: the type parameter is constrained
    to models that provably carry an `organization_id`. That is what lets the
    tenant predicate below be type-checked rather than asserted — a model without
    a tenant key cannot be used with this class at all.

    Subclasses set `model` and may add query methods. Every query must start from
    `self._scoped()`, which applies the tenant predicate; a subclass that builds a
    bare `select(Model)` has silently opted out of isolation, so that is the one
    thing reviewers should look for here.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession, context: TenantContext) -> None:
        if not isinstance(context, TenantContext):
            # Guards the realistic mistake: passing a PlatformContext because it
            # "also has permissions". Static typing catches this in checked code;
            # this catches it at the boundary of anything dynamic.
            raise InvariantViolation(
                "A tenant-scoped repository requires a TenantContext.",
                detail={"received": type(context).__name__},
            )
        self._session = session
        self._context = context

    @property
    def organization_id(self) -> uuid.UUID:
        return self._context.organization_id

    def _scoped(self) -> Select[tuple[ModelT]]:
        """The only sanctioned starting point for a query in this class."""
        return select(self.model).where(self.model.organization_id == self.organization_id)

    async def get(self, entity_id: uuid.UUID) -> ModelT:
        """Fetch by id within this tenant, or raise `NotFoundError`."""
        found = await self.find(entity_id)
        if found is None:
            raise NotFoundError(
                f"{self.model.__name__.removesuffix('Model')} not found.",
                detail={
                    "id": str(entity_id),
                    "organization_id": str(self.organization_id),
                },
            )
        return found

    async def find(self, entity_id: uuid.UUID) -> ModelT | None:
        """Fetch by id within this tenant, or `None`.

        Note this is a filtered `SELECT`, not `session.get()`. `session.get()`
        looks up by primary key alone and would happily return another tenant's
        row — and worse, would serve it straight from the identity map without
        touching the database at all.
        """
        result = await self._session.execute(self._scoped().where(self.model.id == entity_id))
        return result.scalar_one_or_none()

    async def list(
        self,
        *,
        offset: int = 0,
        limit: int = DEFAULT_PAGE_SIZE,
        order_by: Any | None = None,
    ) -> Page[ModelT]:
        """A bounded page of this tenant's rows.

        Fetches `limit + 1` to decide `has_more` without a second count query.
        """
        if offset < 0:
            raise InvariantViolation("Pagination offset cannot be negative.")
        effective = max(1, min(limit, MAX_PAGE_SIZE))
        statement = self._scoped()
        if order_by is not None:
            statement = statement.order_by(order_by)
        statement = statement.offset(offset).limit(effective + 1)

        rows = list((await self._session.execute(statement)).scalars().all())
        has_more = len(rows) > effective
        return Page(rows[:effective], has_more=has_more, next_offset=offset + effective)

    async def count(self) -> int:
        """Count this tenant's rows.

        Separate from `list` on purpose: it is a full scan for a large tenant and
        should be an explicit decision, not something every page pays for.
        """
        statement = (
            select(func.count())
            .select_from(self.model)
            .where(self.model.organization_id == self.organization_id)
        )
        return int((await self._session.execute(statement)).scalar_one())

    def add(self, instance: ModelT) -> ModelT:
        """Stage a new row, forcing it into this tenant.

        Overwrites `organization_id` rather than trusting the caller's. The
        tenant comes from the server-side context; an object arriving with a
        different one is either a bug or an attack, and in both cases the right
        answer is the context's value.
        """
        instance.organization_id = self.organization_id
        self._session.add(instance)
        return instance

    async def delete(self, entity_id: uuid.UUID) -> None:
        """Hard-delete one row within this tenant.

        Loads first so the tenant predicate applies. A bare
        `DELETE WHERE id = :id` would cross tenants.
        """
        await self._session.delete(await self.get(entity_id))


class PlatformRepository[ModelT: Base]:
    """Base for repositories over platform-global tables, and for cross-tenant reads.

    **Every use of this class is a cross-tenant capability.** It is separate,
    differently named and requires a `PlatformContext` precisely so that `grep -r
    PlatformRepository` is a complete list of the places tenant isolation does
    not apply. If a subclass of this appears in a diff, it wants a security
    review.
    """

    model: type[ModelT]

    def __init__(self, session: AsyncSession, context: PlatformContext) -> None:
        if not isinstance(context, PlatformContext):
            raise InvariantViolation(
                "A platform repository requires a PlatformContext.",
                detail={"received": type(context).__name__},
            )
        self._session = session
        self._context = context

    async def find(self, entity_id: uuid.UUID) -> ModelT | None:
        result = await self._session.execute(select(self.model).where(self.model.id == entity_id))
        return result.scalar_one_or_none()

    async def get(self, entity_id: uuid.UUID) -> ModelT:
        found = await self.find(entity_id)
        if found is None:
            raise NotFoundError(
                f"{self.model.__name__.removesuffix('Model')} not found.",
                detail={"id": str(entity_id)},
            )
        return found

    def add(self, instance: ModelT) -> ModelT:
        self._session.add(instance)
        return instance
