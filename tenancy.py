"""Trusted-proxy identity parsing and per-tenant workspace selection."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request

from models import Tenant
from projects import ProjectRegistry, ProjectRegistryError

LOCAL_TENANT_ID = "local-service-user"
USERNAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._@-]{0,127}$")
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]+$")
SUBJECT_MAX_BYTES = 255
DeploymentMode = Literal["single_user", "multi_tenant"]


class WebIdentityError(RuntimeError):
    status_code = 403
    code = "invalid_web_identity"
    safe_message = "The authenticated user identity is not valid for this service."


class WebIdentityRequired(WebIdentityError):
    status_code = 401
    code = "web_identity_required"
    safe_message = "Authentication is required."


class WebIdentityConflict(WebIdentityError):
    code = "web_identity_conflict"
    safe_message = "This account name is already assigned to a different workspace owner."


@dataclass(frozen=True, slots=True)
class WebUserContext:
    tenant_id: str
    username: str
    directory_name: str | None
    workspace_root: Path
    registry: ProjectRegistry


def deployment_mode(settings_obj: Any) -> DeploymentMode:
    value = str(getattr(settings_obj, "deployment_mode", "single_user")).strip().lower()
    if value not in {"single_user", "multi_tenant"}:
        raise RuntimeError(f"Unsupported deployment_mode: {value!r}")
    return value  # type: ignore[return-value]


def _single_header(request: Request, name: str) -> str:
    values = request.headers.getlist(name)
    if len(values) != 1:
        raise WebIdentityRequired
    value = values[0].strip()
    if not value:
        raise WebIdentityRequired
    return value


def _canonical_username(value: str) -> str:
    canonical = value.strip().lower()
    if not USERNAME_PATTERN.fullmatch(canonical):
        raise WebIdentityError
    return canonical


class TenantWorkspaceManager:
    """Resolve one request identity to a stable, tenant-local ProjectRegistry."""

    def __init__(
        self,
        *,
        mode: DeploymentMode,
        base_root: Path,
        hidden_projects: tuple[str, ...] = (),
        subject_header: str = "X-Forwarded-User",
        username_header: str = "X-Forwarded-Preferred-Username",
        single_registry: ProjectRegistry | None = None,
    ) -> None:
        if mode not in {"single_user", "multi_tenant"}:
            raise RuntimeError(f"Unsupported deployment_mode: {mode!r}")
        self.mode = mode
        self.subject_header = subject_header.strip()
        self.username_header = username_header.strip()
        if (
            not HEADER_NAME_PATTERN.fullmatch(self.subject_header)
            or not HEADER_NAME_PATTERN.fullmatch(self.username_header)
            or self.subject_header.lower() == self.username_header.lower()
        ):
            raise RuntimeError("oauth2-proxy identity header names are invalid")
        self.hidden_projects = hidden_projects
        try:
            self.base_root = base_root.expanduser().resolve(strict=True)
        except OSError as exc:
            raise ProjectRegistryError("Configured codex_projects_root does not exist") from exc
        if not self.base_root.is_dir():
            raise ProjectRegistryError("Configured codex_projects_root is not a directory")
        self._contexts: dict[str, WebUserContext] = {}
        self._single_context: WebUserContext | None = None
        if mode == "single_user":
            registry = single_registry or ProjectRegistry.from_root(
                self.base_root,
                hidden_projects=hidden_projects,
            )
            self._single_context = WebUserContext(
                tenant_id=LOCAL_TENANT_ID,
                username=LOCAL_TENANT_ID,
                directory_name=None,
                workspace_root=self.base_root,
                registry=registry,
            )

    @classmethod
    def from_settings(
        cls,
        settings_obj: Any,
        *,
        single_registry: ProjectRegistry | None = None,
    ) -> TenantWorkspaceManager:
        mode = deployment_mode(settings_obj)
        configured_root = str(getattr(settings_obj, "codex_projects_root", "") or "").strip()
        if not configured_root:
            if mode == "multi_tenant" or single_registry is None:
                raise ProjectRegistryError("codex_projects_root is required for tenant workspaces")
            root = single_registry.root or Path.cwd()
        else:
            root = Path(configured_root)
        raw_hidden = getattr(settings_obj, "codex_hidden_projects", ()) or ()
        if isinstance(raw_hidden, str):
            raw_hidden = (raw_hidden,)
        return cls(
            mode=mode,
            base_root=root,
            hidden_projects=tuple(str(value) for value in raw_hidden),
            subject_header=str(
                getattr(settings_obj, "oauth2_proxy_subject_header", "X-Forwarded-User")
            ),
            username_header=str(
                getattr(
                    settings_obj,
                    "oauth2_proxy_username_header",
                    "X-Forwarded-Preferred-Username",
                )
            ),
            single_registry=single_registry,
        )

    @property
    def single_context(self) -> WebUserContext | None:
        return self._single_context

    def _workspace_root(self, directory_name: str) -> Path:
        if not USERNAME_PATTERN.fullmatch(directory_name):
            raise ProjectRegistryError("Stored tenant workspace directory is invalid")
        target = self.base_root / directory_name
        created = False
        try:
            target.mkdir(mode=0o700)
            created = True
        except FileExistsError:
            pass
        except OSError as exc:
            raise ProjectRegistryError("Tenant workspace directory cannot be created") from exc
        try:
            if target.is_symlink():
                raise ProjectRegistryError("Tenant workspace directory cannot be a symbolic link")
            resolved = target.resolve(strict=True)
        except OSError as exc:
            raise ProjectRegistryError("Tenant workspace directory cannot be resolved") from exc
        if not resolved.is_dir() or resolved.parent != self.base_root:
            raise ProjectRegistryError("Tenant workspace directory is unsafe")
        if created:
            try:
                resolved.chmod(0o700)
            except OSError as exc:
                raise ProjectRegistryError(
                    "Tenant workspace directory permissions cannot be secured"
                ) from exc
        return resolved

    async def _tenant(
        self,
        session: AsyncSession,
        *,
        subject: str,
        username: str,
        directory_name: str,
    ) -> Tenant:
        tenant = await session.scalar(
            select(Tenant).where(Tenant.external_subject == subject)
        )
        if tenant is not None:
            if tenant.username != username:
                tenant.username = username
                await session.commit()
            return tenant

        assigned = await session.scalar(
            select(Tenant).where(Tenant.directory_name == directory_name)
        )
        if assigned is not None:
            raise WebIdentityConflict

        tenant = Tenant(
            tenant_id=uuid.uuid4().hex,
            external_subject=subject,
            username=username,
            directory_name=directory_name,
        )
        session.add(tenant)
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            existing = await session.scalar(
                select(Tenant).where(Tenant.external_subject == subject)
            )
            if existing is None:
                raise WebIdentityConflict from exc
            tenant = existing
        return tenant

    async def resolve(
        self,
        request: Request,
        session: AsyncSession,
    ) -> WebUserContext:
        if self._single_context is not None:
            return self._single_context

        subject = _single_header(request, self.subject_header)
        if len(subject.encode("utf-8")) > SUBJECT_MAX_BYTES or any(
            ord(character) < 32 or ord(character) == 127 for character in subject
        ):
            raise WebIdentityError
        raw_username = _single_header(request, self.username_header)
        directory_name = _canonical_username(raw_username)
        tenant = await self._tenant(
            session,
            subject=subject,
            username=raw_username.strip(),
            directory_name=directory_name,
        )
        cached = self._contexts.get(tenant.tenant_id)
        if cached is not None and cached.username == tenant.username:
            return cached
        workspace_root = self._workspace_root(tenant.directory_name)
        context = WebUserContext(
            tenant_id=tenant.tenant_id,
            username=tenant.username,
            directory_name=tenant.directory_name,
            workspace_root=workspace_root,
            registry=ProjectRegistry.from_root(
                workspace_root,
                hidden_projects=self.hidden_projects,
                directory_mode=0o700,
            ),
        )
        self._contexts[tenant.tenant_id] = context
        return context
