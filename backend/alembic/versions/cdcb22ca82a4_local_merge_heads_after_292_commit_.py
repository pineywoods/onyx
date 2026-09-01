"""local merge heads after 292 commit upstream pull

Revision ID: cdcb22ca82a4
Revises: 0a0fe5a31791, 34fe28843029
Create Date: 2026-09-01 16:47:11.251989

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'cdcb22ca82a4'
down_revision = ('0a0fe5a31791', '34fe28843029')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
