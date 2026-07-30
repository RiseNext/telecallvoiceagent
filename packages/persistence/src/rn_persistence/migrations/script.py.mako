"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

Review checklist before merging:
  - Does `downgrade()` fully reverse `upgrade()`? The round-trip is tested.
  - Does any statement take a lock that would block writes on a live table?
    `CREATE INDEX` must be `CONCURRENTLY` (outside a transaction) on a hot table.
  - Are enum/permission CHECK values a FROZEN literal snapshot? A migration must
    never import a live application catalog — an old migration whose meaning
    changes when today's code changes is not a migration.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision: str = ${repr(up_revision)}
down_revision: str | None = ${repr(down_revision)}
branch_labels: str | Sequence[str] | None = ${repr(branch_labels)}
depends_on: str | Sequence[str] | None = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
