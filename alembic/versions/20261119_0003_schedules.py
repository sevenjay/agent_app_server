"""Add recurring prompt schedules.

Revision ID: 20261119_0003
Revises: 20260903_0002
"""

import sqlalchemy as sa

from alembic import op

revision = "20261119_0003"
down_revision = "20260903_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # The service's create_all can already have added this table.
    op.create_table(
        "schedules",
        sa.Column("tenant_id", sa.String(64), nullable=False),
        sa.Column("id", sa.String(32), nullable=False),
        sa.Column("project_key", sa.String(64), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("model", sa.String(100), nullable=True),
        sa.Column("reasoning_effort", sa.String(10), nullable=True),
        sa.Column("frequency", sa.String(6), nullable=False),
        sa.Column("days_of_week", sa.JSON(), nullable=False),
        sa.Column("time", sa.String(5), nullable=False),
        sa.Column("timezone", sa.String(255), nullable=False),
        sa.Column("next_run_at", sa.Integer(), nullable=True),
        sa.Column("last_run_at", sa.Integer(), nullable=True),
        sa.Column("last_thread_id", sa.String(128), nullable=True),
        sa.Column("last_run_error", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("tenant_id", "id"),
        if_not_exists=True,
    )
    op.create_index("ix_schedules_next_run_at", "schedules", ["next_run_at"], if_not_exists=True)


def downgrade() -> None:
    op.drop_index("ix_schedules_next_run_at", table_name="schedules")
    op.drop_table("schedules")
