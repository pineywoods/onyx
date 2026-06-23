"""merge local docker_status tool into upstream head

Revision ID: 6dd881f104db
Revises: f4d0c5e21a90, 8f2c4a1d9e3b
Create Date: 2026-06-23

Local-only join point. After pulling upstream, the tree had two real heads:
the local DockerStatusTool migration (f4d0c5e21a90) and the upstream
agent-sharing tip (8f2c4a1d9e3b). Merge them so `alembic upgrade head`
resolves to a single head again. No schema changes.
"""

# revision identifiers, used by Alembic.
revision = "6dd881f104db"
down_revision = ("f4d0c5e21a90", "8f2c4a1d9e3b")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
