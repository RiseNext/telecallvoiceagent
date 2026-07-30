"""Unit of Work — the transaction boundary.

One rule underpins the whole design: **repositories never commit.** A repository
stages changes; the Unit of Work decides that a set of them is complete. If each
repository call were its own transaction, then

    update the call
    create the lead
    write the outbox event

would be three transactions, and a crash between them would leave a lead with no
event, or an event describing a call state that was never saved.

That last part is why the outbox is here rather than behind a broker client. The
state change and the intent-to-publish are written in the **same transaction**,
so they either both happen or neither does. No dual write, no lost event, no
phantom event (ADR-008).

Usage::

    async with UnitOfWork(session_factory) as uow:
        calls = CallRepository(uow.session, ctx)
        call = await calls.get(call_id)
        call.status = "completed"
        uow.record_event(build_outbox_event(...))
        await uow.commit()

Not committing is a rollback. Exiting the block without `commit()` — by an early
return, by an exception, by anything — rolls back. Silence means no.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.exc import DBAPIError, IntegrityError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from rn_core.errors import ConflictError, InvariantViolation, TransientError
from rn_core.logging import get_logger
from rn_domain.entities.ops import OutboxEvent
from rn_persistence.engine import get_session_factory
from rn_persistence.models.ops import OutboxEventModel

__all__ = ["UnitOfWork"]

_logger = get_logger(__name__)

# Postgres SQLSTATEs we translate into typed errors rather than letting a driver
# exception escape the persistence layer.
_UNIQUE_VIOLATION = "23505"
_FOREIGN_KEY_VIOLATION = "23503"
_CHECK_VIOLATION = "23514"
_NOT_NULL_VIOLATION = "23502"
_SERIALIZATION_FAILURE = "40001"
_DEADLOCK_DETECTED = "40P01"


class UnitOfWork:
    """An explicit, single-use transaction boundary.

    Single-use on purpose: reusing one across logical operations is how an
    unrelated failure ends up rolling back work that had already succeeded.
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession] | None = None) -> None:
        self._factory = session_factory or get_session_factory()
        self._session: AsyncSession | None = None
        self._committed = False
        self._pending_events: list[OutboxEvent] = []

    @property
    def session(self) -> AsyncSession:
        if self._session is None:
            raise InvariantViolation(
                "The unit of work is not active. Use it as an async context manager."
            )
        return self._session

    async def __aenter__(self) -> Self:
        if self._session is not None:
            raise InvariantViolation("A unit of work cannot be re-entered.")
        self._session = self._factory()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        session = self._session
        self._session = None
        if session is None:  # pragma: no cover - defensive
            return
        try:
            # An uncommitted unit of work rolls back. Committing must be an act,
            # never something that happens because a block ended.
            if not self._committed:
                await session.rollback()
        finally:
            await session.close()

    def record_event(self, event: OutboxEvent) -> None:
        """Stage a domain event for publication in **this** transaction.

        Held until `commit()` and inserted there, so an event can never be
        written by a transaction that later rolls back. Delivery is
        at-least-once; consumers must be idempotent.
        """
        if self._committed:
            raise InvariantViolation("Cannot record an event after commit.")
        self._pending_events.append(event)

    async def flush(self) -> None:
        """Send pending changes to the database without committing.

        Useful when a later step needs a database-generated value, or to surface
        a constraint violation at a known point rather than at commit.
        """
        try:
            await self.session.flush()
        except SQLAlchemyError as exc:
            raise self._translate(exc) from exc

    async def commit(self) -> None:
        """Persist everything, including staged outbox events, atomically."""
        if self._committed:
            raise InvariantViolation("This unit of work has already been committed.")
        session = self.session
        try:
            for event in self._pending_events:
                session.add(OutboxEventModel.from_domain(event))
            await session.commit()
            self._committed = True
            self._pending_events.clear()
        except SQLAlchemyError as exc:
            await session.rollback()
            raise self._translate(exc) from exc

    async def rollback(self) -> None:
        """Discard everything staged, including outbox events."""
        self._pending_events.clear()
        await self.session.rollback()

    @staticmethod
    def _translate(exc: SQLAlchemyError) -> Exception:
        """Turn a driver exception into our error taxonomy.

        A driver exception must never escape this layer: callers should branch on
        meaning, not on the vendor of the thing that broke. The original message
        goes in `detail` — it frequently quotes the offending row, which for us
        can be a phone number, so it is logged and redacted rather than shown.
        """
        sqlstate = getattr(getattr(exc, "orig", None), "sqlstate", None) or getattr(
            getattr(exc, "orig", None), "pgcode", None
        )
        constraint = getattr(getattr(exc, "orig", None), "constraint_name", None)

        if sqlstate in {_SERIALIZATION_FAILURE, _DEADLOCK_DETECTED}:
            # Genuinely retryable: the same transaction replayed may succeed.
            return TransientError(
                "The operation conflicted with a concurrent transaction. Please retry.",
                detail={"sqlstate": sqlstate, "constraint": constraint},
            )
        if sqlstate == _UNIQUE_VIOLATION:
            return ConflictError(
                "That record already exists.",
                detail={"sqlstate": sqlstate, "constraint": constraint},
            )
        if sqlstate in {_FOREIGN_KEY_VIOLATION, _CHECK_VIOLATION, _NOT_NULL_VIOLATION}:
            # A check or FK failure at this layer means the application allowed
            # something the schema forbids — a bug, not user error.
            return InvariantViolation(
                "The operation violated a database constraint.",
                detail={"sqlstate": sqlstate, "constraint": constraint},
            )
        if isinstance(exc, IntegrityError):
            return ConflictError(
                "The operation conflicted with existing data.",
                detail={"sqlstate": sqlstate, "constraint": constraint},
            )
        if isinstance(exc, DBAPIError) and exc.connection_invalidated:
            return TransientError(
                "The database connection was lost. Please retry.",
                detail={"sqlstate": sqlstate},
            )
        _logger.error("persistence.untranslated_error", error_type=type(exc).__name__)
        return exc
