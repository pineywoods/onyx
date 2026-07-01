"""local: merge heads after rebase onto upstream main

Unifies the two real divergent alembic heads that resulted from rebasing the
local patch branch onto a newer upstream:
  - 6dd881f104db : local DockerStatusTool + June-23 upstream merge point
                   (already includes upstream has_been_indexed / c7bf5721733e)
  - 582269841f06 : upstream add granted_scopes to external_app_user
They touch unrelated tables, so this is a no-op join to restore a single head.

Revision ID: 3f3f1d1c4b78
Revises: 6dd881f104db, 582269841f06
Create Date: 2026-07-01
"""

# revision identifiers, used by Alembic.
revision = "3f3f1d1c4b78"
down_revision = ("6dd881f104db", "582269841f06")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
