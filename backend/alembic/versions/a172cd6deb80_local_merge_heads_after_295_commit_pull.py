"""local merge heads after 295 commit upstream pull

Revision ID: a172cd6deb80
Revises: 7d5203079191, f57f35403f6c
Create Date: 2026-07-25 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a172cd6deb80'
down_revision = ('7d5203079191', 'f57f35403f6c')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
