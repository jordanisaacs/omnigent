"""Live, non-persistent PM project views and session topology leases."""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from typing import Any

from omnigent.server.host_registry import HostConnection, HostRegistry
from omnigent.server.routes._host_integration import (
    HostIntegrationError,
    request_host_integration,
)
from omnigent.stores import ConversationStore
from omnigent.telemetry.installation_id import get_installation_id

_logger = logging.getLogger(__name__)
_VIEW_NAMESPACE = uuid.UUID("f30b5913-60a1-49bf-a29b-ab5a099fa490")


@dataclass(frozen=True)
class PmWorktree:
    """One worktree reported by PM discovery or a lease snapshot."""

    name: str
    repo: str
    path: str
    status: str = "active"
    slot_uuid: str | None = None
    branch: str | None = None
    detail: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "repo": self.repo,
            "path": self.path,
            "status": self.status,
            "included": self.status == "active",
            "slot_uuid": self.slot_uuid,
            "branch": self.branch,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class PmProjectView:
    """A live PM project on one owned, connected host."""

    id: str
    name: str
    path: str
    host_id: str
    host_name: str
    lease_count: int
    worktrees: tuple[PmWorktree, ...]

    @property
    def active_worktrees(self) -> tuple[PmWorktree, ...]:
        return tuple(worktree for worktree in self.worktrees if worktree.status == "active")

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "object": "pm.project",
            "name": self.name,
            "host_id": self.host_id,
            "host_name": self.host_name,
            "path": self.path,
            "workspace": self.path,
            "lease_count": self.lease_count,
            "worktrees": [worktree.as_dict() for worktree in self.worktrees],
            "directories": [
                {"name": worktree.name, "path": worktree.path}
                for worktree in self.active_worktrees
            ],
        }


@dataclass(frozen=True)
class PmLeaseSnapshot:
    """The immutable active-worktree snapshot returned by lease acquisition."""

    project: PmProjectView
    worktrees: tuple[PmWorktree, ...]


def _view_id(host_id: str, path: str) -> str:
    return uuid.uuid5(_VIEW_NAMESPACE, f"{host_id}\0{path}").hex


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise HostIntegrationError(f"PM returned an invalid {field}")
    return value


def _worktree(raw: Any, *, discovery: bool) -> PmWorktree:
    if not isinstance(raw, dict):
        raise HostIntegrationError("PM returned an invalid worktree entry")
    status = _string(raw.get("status"), "worktree status") if discovery else "active"
    return PmWorktree(
        name=_string(raw.get("name"), "worktree name"),
        repo=_string(raw.get("repo"), "worktree repo"),
        path=_string(raw.get("path"), "worktree path"),
        status=status,
        slot_uuid=raw.get("slot_uuid") if isinstance(raw.get("slot_uuid"), str) else None,
        branch=raw.get("branch") if isinstance(raw.get("branch"), str) else None,
        detail=raw.get("detail") if isinstance(raw.get("detail"), str) else None,
    )


class PmIntegrationService:
    """Coordinate live PM views and leases without persisting PM membership."""

    def __init__(self, host_registry: HostRegistry, conversation_store: ConversationStore) -> None:
        self.host_registry = host_registry
        self.conversation_store = conversation_store
        installation_id = get_installation_id()
        self.holder = f"omnigent:{installation_id}" if installation_id else None

    @staticmethod
    def _owned(conn: HostConnection, user_id: str | None) -> bool:
        return user_id is None or conn.owner == user_id

    @staticmethod
    def _capable(conn: HostConnection) -> bool:
        capability = conn.hello.integrations.get("pm")
        return isinstance(capability, dict) and capability.get("protocol_version") == 1

    def connection(self, host_id: str, user_id: str | None) -> HostConnection | None:
        conn = self.host_registry.get(host_id)
        if conn is None or not self._owned(conn, user_id) or not self._capable(conn):
            return None
        return conn

    def visible_connections(self, user_id: str | None) -> list[HostConnection]:
        result: list[HostConnection] = []
        for host_id in self.host_registry.online_host_ids():
            conn = self.connection(host_id, user_id)
            if conn is not None:
                result.append(conn)
        return result

    async def _projects_on(self, conn: HostConnection) -> list[PmProjectView]:
        payload = await request_host_integration(
            host_registry=self.host_registry,
            host_conn=conn,
            integration="pm",
            operation="projects",
        )
        if not isinstance(payload, dict) or payload.get("protocol_version") != 1:
            raise HostIntegrationError("PM returned an unsupported projects payload")
        raw_projects = payload.get("projects")
        if not isinstance(raw_projects, list):
            raise HostIntegrationError("PM returned an invalid projects list")
        projects: list[PmProjectView] = []
        for raw in raw_projects:
            if not isinstance(raw, dict):
                raise HostIntegrationError("PM returned an invalid project entry")
            name = _string(raw.get("name"), "project name")
            path = _string(raw.get("path"), "project path")
            raw_worktrees = raw.get("worktrees", [])
            if not isinstance(raw_worktrees, list):
                raise HostIntegrationError("PM returned an invalid worktrees list")
            lease_count = raw.get("lease_count", 0)
            if not isinstance(lease_count, int) or isinstance(lease_count, bool):
                raise HostIntegrationError("PM returned an invalid lease count")
            projects.append(
                PmProjectView(
                    id=_view_id(conn.host_id, path),
                    name=name,
                    path=path,
                    host_id=conn.host_id,
                    host_name=conn.hello.name,
                    lease_count=lease_count,
                    worktrees=tuple(_worktree(item, discovery=True) for item in raw_worktrees),
                )
            )
        return projects

    async def list_projects(self, user_id: str | None) -> list[PmProjectView]:
        connections = self.visible_connections(user_id)
        results = await asyncio.gather(
            *(self._projects_on(conn) for conn in connections),
            return_exceptions=True,
        )
        projects: list[PmProjectView] = []
        for conn, result in zip(connections, results, strict=True):
            if isinstance(result, BaseException):
                _logger.warning("PM discovery failed on host %s: %s", conn.host_id, result)
                continue
            projects.extend(result)
        return sorted(projects, key=lambda project: (project.name.casefold(), project.host_name))

    async def get_project(self, user_id: str | None, view_id: str) -> PmProjectView | None:
        return next(
            (project for project in await self.list_projects(user_id) if project.id == view_id),
            None,
        )

    async def find_exact(
        self,
        *,
        user_id: str | None,
        host_id: str | None,
        workspace: str | None,
    ) -> PmProjectView | None:
        if host_id is None or workspace is None:
            return None
        conn = self.connection(host_id, user_id)
        if conn is None:
            return None
        return next(
            (project for project in await self._projects_on(conn) if project.path == workspace),
            None,
        )

    async def acquire(self, project: PmProjectView, session_id: str) -> PmLeaseSnapshot:
        if self.holder is None:
            raise HostIntegrationError("Omnigent installation ID is unavailable")
        conn = self.host_registry.get(project.host_id)
        if conn is None or not self._capable(conn):
            raise HostIntegrationError("PM host went offline before lease acquisition")
        payload = await request_host_integration(
            host_registry=self.host_registry,
            host_conn=conn,
            integration="pm",
            operation="lease_acquire",
            arguments={
                "project": project.name,
                "holder": self.holder,
                "lease_id": session_id,
            },
        )
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise HostIntegrationError("PM returned an invalid lease acquisition payload")
        raw = payload[0]
        if raw.get("lease_id") != session_id or raw.get("holder") != self.holder:
            raise HostIntegrationError("PM returned a mismatched lease")
        worktrees = raw.get("worktrees", [])
        if not isinstance(worktrees, list):
            raise HostIntegrationError("PM returned an invalid lease worktree snapshot")
        return PmLeaseSnapshot(
            project=project,
            worktrees=tuple(_worktree(item, discovery=False) for item in worktrees),
        )

    async def release(self, project: PmProjectView, session_id: str) -> None:
        if self.holder is None:
            raise HostIntegrationError("Omnigent installation ID is unavailable")
        conn = self.host_registry.get(project.host_id)
        if conn is None or not self._capable(conn):
            raise HostIntegrationError("PM host is offline; lease remains for reconciliation")
        await request_host_integration(
            host_registry=self.host_registry,
            host_conn=conn,
            integration="pm",
            operation="lease_release",
            arguments={
                "project": project.name,
                "holder": self.holder,
                "lease_id": session_id,
            },
        )

    async def reconcile_host(self, host_id: str, owner: str | None) -> None:
        """Reconcile only this installation's leases after a host reconnect."""
        if self.holder is None:
            return
        conn = self.connection(host_id, owner)
        if conn is None:
            return
        projects = await self._projects_on(conn)
        payload = await request_host_integration(
            host_registry=self.host_registry,
            host_conn=conn,
            integration="pm",
            operation="lease_list",
            arguments={"holder": self.holder},
        )
        if not isinstance(payload, list):
            raise HostIntegrationError("PM returned an invalid lease list")
        existing_pairs: set[tuple[str, str]] = set()
        projects_by_name = {project.name: project for project in projects}
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            project_name = raw.get("project")
            lease_id = raw.get("lease_id")
            if not isinstance(project_name, str) or not isinstance(lease_id, str):
                continue
            existing_pairs.add((project_name, lease_id))
            session = await asyncio.to_thread(
                self.conversation_store.get_conversation,
                lease_id,
            )
            project = projects_by_name.get(project_name)
            if project is not None and (
                session is None or session.host_id != host_id or session.workspace != project.path
            ):
                await self.release(project, lease_id)

        projects_by_path = {project.path: project for project in projects}
        after: str | None = None
        while True:
            page = await asyncio.to_thread(
                self.conversation_store.list_conversations,
                limit=1000,
                after=after,
                kind=None,
                include_archived=True,
                host_id=host_id,
            )
            for session in page.data:
                project = projects_by_path.get(session.workspace or "")
                if project is not None and (project.name, session.id) not in existing_pairs:
                    await self.acquire(project, session.id)
            if not page.has_more or page.last_id is None:
                break
            after = page.last_id
