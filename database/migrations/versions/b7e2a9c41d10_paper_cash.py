"""paper cash on bot settings

Revision ID: b7e2a9c41d10
Revises: c5d8e1a27b04
Create Date: 2026-09-27 06:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7e2a9c41d10"
down_revision: Union[str, None] = "c5d8e1a27b04"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("bot_settings") as batch_op:
        batch_op.add_column(sa.Column("paper_cash", sa.Numeric(precision=36, scale=18), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("bot_settings") as batch_op:
        batch_op.drop_column("paper_cash")
