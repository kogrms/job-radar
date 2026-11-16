"""allow himalayas source

Revision ID: 6ba46df4d571
Revises: 4ac9de152e68
Create Date: 2026-11-11 15:50:03.032706

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "6ba46df4d571"
down_revision: str | Sequence[str] | None = "4ac9de152e68"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


OLD = "source IN ('ashby', 'greenhouse', 'lever', 'workable')"
NEW = "source IN ('ashby', 'greenhouse', 'lever', 'workable', 'himalayas')"


def upgrade() -> None:
    """Upgrade schema."""
    # Autogenerate does not see CHECK changes: written by hand (see skill add-ats-collector, step 4).
    op.drop_constraint("source_known", "boards", type_="check")
    op.create_check_constraint("source_known", "boards", NEW)


def downgrade() -> None:
    """Downgrade schema. Fails while himalayas boards exist: delete them (and their vacancies) first."""
    op.drop_constraint("source_known", "boards", type_="check")
    op.create_check_constraint("source_known", "boards", OLD)
