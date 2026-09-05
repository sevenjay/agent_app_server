"""Add tenant identities and scope Web console metadata.

Revision ID: 20260903_0002
Revises: 20260731_0001
Create Date: 2026-09-03 00:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from database_schema import TENANT_REVISION, existing_schema_revision, validate_existing_tenants

revision: str = "20260903_0002"
down_revision: str | Sequence[str] | None = "20260731_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

LOCAL_TENANT_ID = "local-service-user"


def _tenant_metadata_tables() -> None:
    op.create_table(
        "thread_ui_metadata_tenant",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
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
        sa.PrimaryKeyConstraint("tenant_id", "thread_id"),
    )
    op.create_table(
        "app_settings_tenant",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("setting_key", sa.String(length=100), nullable=False),
        sa.Column("setting_value", sa.Text(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("tenant_id", "setting_key"),
    )


def _create_tenants_table() -> None:
    # Older startup code may already have created this table without upgrading
    # the two metadata tables or recording an Alembic revision.
    if not op.get_context().as_sql and validate_existing_tenants(op.get_bind()):
        return
    op.create_table(
        "tenants",
        sa.Column("tenant_id", sa.String(length=64), nullable=False),
        sa.Column("external_subject", sa.String(length=255), nullable=False),
        sa.Column("username", sa.String(length=255), nullable=False),
        sa.Column("directory_name", sa.String(length=128), nullable=False),
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
        sa.PrimaryKeyConstraint("tenant_id"),
        sa.UniqueConstraint(
            "directory_name",
            name="uq_tenants_directory_name",
        ),
        sa.UniqueConstraint(
            "external_subject",
            name="uq_tenants_external_subject",
        ),
    )


def upgrade() -> None:
    if not op.get_context().as_sql and existing_schema_revision(op.get_bind()) == TENANT_REVISION:
        return
    _create_tenants_table()
    _tenant_metadata_tables()
    op.execute(
        sa.text(
            """
            INSERT INTO thread_ui_metadata_tenant (
                tenant_id, thread_id, project_key, pinned, custom_label,
                last_opened_at, created_at, updated_at
            )
            SELECT
                :tenant_id, thread_id, project_key, pinned, custom_label,
                last_opened_at, created_at, updated_at
            FROM thread_ui_metadata
            """
        ).bindparams(tenant_id=LOCAL_TENANT_ID)
    )
    op.execute(
        sa.text(
            """
            INSERT INTO app_settings_tenant (
                tenant_id, setting_key, setting_value, updated_at
            )
            SELECT :tenant_id, setting_key, setting_value, updated_at
            FROM app_settings
            """
        ).bindparams(tenant_id=LOCAL_TENANT_ID)
    )

    op.drop_index(
        "ix_thread_ui_metadata_project_key",
        table_name="thread_ui_metadata",
    )
    op.drop_table("thread_ui_metadata")
    op.drop_table("app_settings")
    op.rename_table("thread_ui_metadata_tenant", "thread_ui_metadata")
    op.rename_table("app_settings_tenant", "app_settings")
    op.create_index(
        "ix_thread_ui_metadata_tenant_project",
        "thread_ui_metadata",
        ["tenant_id", "project_key"],
    )


def downgrade() -> None:
    op.create_table(
        "thread_ui_metadata_single",
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
    op.create_table(
        "app_settings_single",
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
    op.execute(
        sa.text(
            """
            INSERT INTO thread_ui_metadata_single (
                thread_id, project_key, pinned, custom_label,
                last_opened_at, created_at, updated_at
            )
            SELECT
                thread_id, project_key, pinned, custom_label,
                last_opened_at, created_at, updated_at
            FROM thread_ui_metadata
            WHERE tenant_id = :tenant_id
            """
        ).bindparams(tenant_id=LOCAL_TENANT_ID)
    )
    op.execute(
        sa.text(
            """
            INSERT INTO app_settings_single (
                setting_key, setting_value, updated_at
            )
            SELECT setting_key, setting_value, updated_at
            FROM app_settings
            WHERE tenant_id = :tenant_id
            """
        ).bindparams(tenant_id=LOCAL_TENANT_ID)
    )

    op.drop_index(
        "ix_thread_ui_metadata_tenant_project",
        table_name="thread_ui_metadata",
    )
    op.drop_table("thread_ui_metadata")
    op.drop_table("app_settings")
    op.rename_table("thread_ui_metadata_single", "thread_ui_metadata")
    op.rename_table("app_settings_single", "app_settings")
    op.create_index(
        "ix_thread_ui_metadata_project_key",
        "thread_ui_metadata",
        ["project_key"],
    )
    op.drop_table("tenants")
