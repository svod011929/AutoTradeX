"""strategy setting defaults

Revision ID: c5d8e1a27b04
Revises: 43ccacb44d4a
Create Date: 2026-09-27 05:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c5d8e1a27b04"
down_revision: Union[str, None] = "43ccacb44d4a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("strategy_settings") as batch_op:
        batch_op.alter_column(
            "atr_sl_multiplier",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default="2.0",
        )
        batch_op.alter_column(
            "take_profit_rr",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default="2.0",
        )
        batch_op.alter_column(
            "trailing_activation_atr",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default="1.5",
        )
        batch_op.alter_column(
            "trailing_atr_multiplier",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default="1.5",
        )
        batch_op.alter_column(
            "min_volume_ratio",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default="1.0",
        )


def downgrade() -> None:
    with op.batch_alter_table("strategy_settings") as batch_op:
        batch_op.alter_column(
            "atr_sl_multiplier",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "take_profit_rr",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "trailing_activation_atr",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "trailing_atr_multiplier",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default=None,
        )
        batch_op.alter_column(
            "min_volume_ratio",
            existing_type=sa.Numeric(precision=10, scale=4),
            existing_nullable=False,
            server_default=None,
        )
