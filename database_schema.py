"""Recognize schemas created by releases that used create_all without Alembic."""

from __future__ import annotations

from sqlalchemy import inspect
from sqlalchemy.engine import Connection
from sqlalchemy.engine.reflection import Inspector

INITIAL_REVISION = "20260731_0001"
TENANT_REVISION = "20260903_0002"

# Keep these historical shapes independent of future ORM model changes.
_METADATA_COLUMNS = {
    "thread_ui_metadata": {
        "thread_id", "project_key", "pinned", "custom_label",
        "last_opened_at", "created_at", "updated_at",
    },
    "app_settings": {"setting_key", "setting_value", "updated_at"},
}
_METADATA_KEYS = {
    "thread_ui_metadata": ["thread_id"],
    "app_settings": ["setting_key"],
}
_TENANT_COLUMNS = {
    "tenant_id", "external_subject", "username", "directory_name",
    "created_at", "updated_at",
}


def _matches_table(
    inspector: Inspector,
    name: str,
    columns: set[str],
    primary_key: list[str],
) -> bool:
    return (
        {column["name"] for column in inspector.get_columns(name)} == columns
        and inspector.get_pk_constraint(name)["constrained_columns"] == primary_key
    )


def validate_existing_tenants(connection: Connection) -> bool:
    """Allow the tenants table that create_all added beside legacy metadata."""
    inspector = inspect(connection)
    if not inspector.has_table("tenants"):
        return False
    unique_keys = {
        tuple(constraint["column_names"])
        for constraint in inspector.get_unique_constraints("tenants")
    }
    if not _matches_table(inspector, "tenants", _TENANT_COLUMNS, ["tenant_id"]) or not {
        ("external_subject",), ("directory_name",),
    }.issubset(unique_keys):
        raise RuntimeError("Unrecognized tenants schema; refusing to adopt existing tables")
    return True


def existing_schema_revision(connection: Connection) -> str | None:
    """Identify a complete known schema, refusing partial or unexpected tables."""
    inspector = inspect(connection)
    tables = set(inspector.get_table_names())
    metadata_tables = set(_METADATA_COLUMNS)
    if not tables.intersection(metadata_tables | {"tenants"}):
        return None
    if not metadata_tables.issubset(tables):
        raise RuntimeError("Incomplete metadata schema; refusing to adopt existing tables")

    has_tenants = validate_existing_tenants(connection)
    for tenant_scoped, revision in ((False, INITIAL_REVISION), (True, TENANT_REVISION)):
        if tenant_scoped and not has_tenants:
            continue
        if all(
            _matches_table(
                inspector,
                name,
                columns | ({"tenant_id"} if tenant_scoped else set()),
                (["tenant_id"] if tenant_scoped else []) + _METADATA_KEYS[name],
            )
            for name, columns in _METADATA_COLUMNS.items()
        ):
            index_name = (
                "ix_thread_ui_metadata_tenant_project"
                if tenant_scoped else "ix_thread_ui_metadata_project_key"
            )
            index_columns = (["tenant_id"] if tenant_scoped else []) + ["project_key"]
            if any(
                index["name"] == index_name and index["column_names"] == index_columns
                for index in inspector.get_indexes("thread_ui_metadata")
            ):
                return revision
    raise RuntimeError("Unrecognized metadata schema; refusing to adopt existing tables")
