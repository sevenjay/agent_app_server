"""SQLAlchemy models for non-authoritative Web console metadata."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    """Stable external identity to workspace-directory assignment."""

    __tablename__ = "tenants"
    __table_args__ = (
        UniqueConstraint("external_subject", name="uq_tenants_external_subject"),
        UniqueConstraint("directory_name", name="uq_tenants_directory_name"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    external_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    username: Mapped[str] = mapped_column(String(255), nullable=False)
    directory_name: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class ThreadUIMetadata(Base):
    """Presentation preferences; Codex remains the thread source of truth."""

    __tablename__ = "thread_ui_metadata"
    __table_args__ = (
        Index("ix_thread_ui_metadata_tenant_project", "tenant_id", "project_key"),
    )

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    thread_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    project_key: Mapped[str | None] = mapped_column(String(64))
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    custom_label: Mapped[str | None] = mapped_column(String(200))
    last_opened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class AppSetting(Base):
    """A deliberately small key/value store for browser UI preferences."""

    __tablename__ = "app_settings"

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    setting_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    setting_value: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class Schedule(Base):
    """A tenant's recurring prompt and its persisted dispatch cursor."""

    __tablename__ = "schedules"
    __table_args__ = (Index("ix_schedules_next_run_at", "next_run_at"),)

    tenant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_key: Mapped[str] = mapped_column(String(64))
    prompt: Mapped[str] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(String(100))
    reasoning_effort: Mapped[str | None] = mapped_column(String(10))
    frequency: Mapped[str] = mapped_column(String(6))
    days_of_week: Mapped[list[str]] = mapped_column(JSON)
    time: Mapped[str] = mapped_column(String(5))
    timezone: Mapped[str] = mapped_column(String(255))
    # UTC epoch seconds avoid SQLite's loss of UTC offsets on datetime columns.
    next_run_at: Mapped[int | None] = mapped_column(Integer)
    last_run_at: Mapped[int | None] = mapped_column(Integer)
    last_thread_id: Mapped[str | None] = mapped_column(String(128))
    last_run_error: Mapped[str | None] = mapped_column(Text)
