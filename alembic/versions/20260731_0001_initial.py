"""Create the initial Web console metadata schema.

Revision ID: 20260731_0001
Revises:
Create Date: 2026-07-31 00:00:00
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260731_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "thread_ui_metadata",
        sa.Column("thread_id", sa.String(length=128), nullable=False),
        sa.Column("project_key", sa.String(length=64), nullable=True),
        sa.Column("pinned", sa.Boolean(), server_default="0", nullable=False),
        sa.Column("custom_label", sa.String(length=200), nullable=True),
        sa.Column("last_opened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("thread_id"),
    )
    op.create_index(
        "ix_thread_ui_metadata_project_key",
        "thread_ui_metadata",
        ["project_key"],
    )
    op.create_table(
        "app_settings",
        sa.Column("setting_key", sa.String(length=100), nullable=False),
        sa.Column("setting_value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("setting_key"),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_index(
        "ix_thread_ui_metadata_project_key",
        table_name="thread_ui_metadata",
    )
    op.drop_table("thread_ui_metadata")
