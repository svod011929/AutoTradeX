"""paper seed and close-positions command

Revision ID: d4a8b1c06e22
Revises: b7e2a9c41d10
Create Date: 2026-09-27 08:10:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a8b1c06e22"
down_revision: Union[str, None] = "b7e2a9c41d10"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("bot_settings") as batch_op:
        batch_op.add_column(sa.Column("paper_seed", sa.Numeric(precision=36, scale=18), nullable=True))
        batch_op.add_column(
            sa.Column("close_positions_requested", sa.Boolean(), nullable=False, server_default="0")
        )


def downgrade() -> None:
    with op.batch_alter_table("bot_settings") as batch_op:
        batch_op.drop_column("close_positions_requested")
        batch_op.drop_column("paper_seed")
