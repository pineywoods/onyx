"""local merge heads after 90 commit upstream pull

Revision ID: 69defdb3658e
Revises: cdcb22ca82a4, 287021f3b46c
Create Date: 2026-09-09 10:26:42.938071

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '69defdb3658e'
down_revision = ('cdcb22ca82a4', '287021f3b46c')
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
