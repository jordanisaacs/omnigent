"""Integration coverage for PM-backed virtual session placement."""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.host.frames import HostHelloFrame, HostLaunchRunnerFrame, decode_host_frame
from omnigent.server.pm_integration import (
    PmIntegrationService,
    PmLeaseSnapshot,
    PmProjectView,
    PmWorktree,
)
from omnigent.server.routes._sessions import orchestration
from omnigent.server.routes.sessions import routes_core
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.host_store import HostStore
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio

_HOST_ID = "4f64b6ee625f4e8259185c35c6e63f3d"
_PROJECT_PATH = "/projects/demo"
_REQUESTED_WORKTREE = "/projects/demo/app"
_CANONICAL_WORKTREE = "/worktrees/app/slot-a"


@dataclass
class _FakeWebSocket:
    sent: list[str] = field(default_factory=list)

    async def send_text(self, data: str) -> None:
        self.sent.append(data)


def _project() -> PmProjectView:
    return PmProjectView(
        id="pm-view",
        name="demo",
        path=_PROJECT_PATH,
        host_id=_HOST_ID,
        host_name="test-host",
        lease_count=0,
        worktrees=(
            PmWorktree(
                name="app",
                repo="app",
                path=_REQUESTED_WORKTREE,
                slot_uuid="slot-a",
            ),
        ),
    )


def _snapshot(project: PmProjectView) -> PmLeaseSnapshot:
    return PmLeaseSnapshot(project=project, worktrees=project.worktrees)


def _register_host(db_uri: str) -> None:
    HostStore(db_uri).upsert_on_connect(_HOST_ID, "test-host", "local")


def _patch_path_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _workspace(**kwargs: Any) -> str:
        assert kwargs["workspace"] == _PROJECT_PATH
        return _PROJECT_PATH

    async def _directory(**kwargs: Any) -> str:
        path = kwargs["directory"]
        return _CANONICAL_WORKTREE if path == _REQUESTED_WORKTREE else path

    monkeypatch.setattr(orchestration, "_validate_session_workspace", _workspace)
    monkeypatch.setattr(orchestration, "_validate_session_directory", _directory)
    monkeypatch.setattr(routes_core, "_validate_session_workspace", _workspace)
    monkeypatch.setattr(routes_core, "_validate_session_directory", _directory)


def _patch_pm(
    monkeypatch: pytest.MonkeyPatch,
    *,
    acquired: list[str],
    released: list[str],
) -> PmProjectView:
    project = _project()

    async def _find_exact(self: PmIntegrationService, **kwargs: Any) -> PmProjectView | None:
        if kwargs.get("host_id") == _HOST_ID and kwargs.get("workspace") == _PROJECT_PATH:
            return project
        return None

    async def _acquire(
        self: PmIntegrationService,
        requested_project: PmProjectView,
        session_id: str,
    ) -> PmLeaseSnapshot:
        assert requested_project == project
        acquired.append(session_id)
        return _snapshot(project)

    async def _release(
        self: PmIntegrationService,
        requested_project: PmProjectView,
        session_id: str,
    ) -> None:
        assert requested_project == project
        released.append(session_id)

    monkeypatch.setattr(PmIntegrationService, "find_exact", _find_exact)
    monkeypatch.setattr(PmIntegrationService, "acquire", _acquire)
    monkeypatch.setattr(PmIntegrationService, "release", _release)
    return project


async def _agent_id(client: httpx.AsyncClient) -> str:
    agent = await create_test_agent(client, name="pm-route-agent")
    return agent["id"]


async def _create_pm_session(client: httpx.AsyncClient, agent_id: str) -> httpx.Response:
    return await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _PROJECT_PATH,
            "directories": [{"path": _REQUESTED_WORKTREE}],
        },
    )


async def test_json_create_uses_durable_session_id_as_lease_id(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)

    response = await _create_pm_session(client, agent_id)

    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert acquired == [session_id]
    assert released == []
    stored = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert stored is not None
    assert stored.host_id == _HOST_ID
    assert stored.workspace == _PROJECT_PATH
    assert [directory.path for directory in stored.directories] == [
        _PROJECT_PATH,
        _CANONICAL_WORKTREE,
    ]
    assert stored.directories[1].name == "app"


async def test_create_releases_provisional_lease_when_persistence_fails(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)

    def _fail_create(self: SqlAlchemyConversationStore, **kwargs: Any) -> object:
        raise RuntimeError("persistence failed")

    monkeypatch.setattr(SqlAlchemyConversationStore, "create_conversation", _fail_create)
    with pytest.raises(RuntimeError, match="persistence failed"):
        await _create_pm_session(client, agent_id)

    assert len(acquired) == 1
    assert released == acquired


async def test_create_releases_lease_when_supplied_directories_do_not_match(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)

    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _PROJECT_PATH,
            "directories": [{"path": "/projects/demo/not-app"}],
        },
    )

    assert response.status_code == 400
    assert len(acquired) == 1
    assert released == acquired


async def test_pm_project_rejects_git_worktree_creation_before_acquiring(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)

    response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "host_id": _HOST_ID,
            "workspace": _PROJECT_PATH,
            "git": {"branch_name": "feature/pm"},
        },
    )

    assert response.status_code == 400
    assert acquired == []
    assert released == []


async def test_child_and_fork_each_acquire_their_own_durable_lease(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)
    parent_response = await _create_pm_session(client, agent_id)
    assert parent_response.status_code == 201, parent_response.text
    parent = parent_response.json()

    child_response = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id,
            "parent_session_id": parent["id"],
            "directory_ids": [directory["id"] for directory in parent["directories"]],
        },
    )
    fork_response = await client.post(f"/v1/sessions/{parent['id']}/fork", json={})

    assert child_response.status_code == 201, child_response.text
    assert fork_response.status_code == 201, fork_response.text
    child = child_response.json()
    fork = fork_response.json()
    assert acquired == [parent["id"], child["id"], fork["id"]]
    assert released == []
    store = SqlAlchemyConversationStore(db_uri)
    for session_id in (child["id"], fork["id"]):
        stored = store.get_conversation(session_id)
        assert stored is not None
        assert (stored.host_id, stored.workspace) == (_HOST_ID, _PROJECT_PATH)


async def test_multipart_create_uses_lease_snapshot_and_durable_id(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)
    bundle = build_agent_bundle(name="pm-multipart-agent")

    response = await client.post(
        "/v1/sessions",
        data={
            "metadata": json.dumps(
                {
                    "host_id": _HOST_ID,
                    "workspace": _PROJECT_PATH,
                    "directories": [{"path": _REQUESTED_WORKTREE}],
                }
            )
        },
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )

    assert response.status_code == 201, response.text
    session_id = response.json()["session_id"]
    assert acquired == [session_id]
    assert released == []
    stored = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert stored is not None
    assert (stored.host_id, stored.workspace) == (_HOST_ID, _PROJECT_PATH)
    assert [directory.path for directory in stored.directories] == [
        _PROJECT_PATH,
        _CANONICAL_WORKTREE,
    ]


async def test_delete_commits_before_release_and_failed_delete_does_not_release(
    client: httpx.AsyncClient,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    _register_host(db_uri)
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)
    created = await _create_pm_session(client, agent_id)
    assert created.status_code == 201, created.text
    session_id = created.json()["id"]
    store = SqlAlchemyConversationStore(db_uri)
    events: list[str] = []
    original_delete = SqlAlchemyConversationStore.delete_conversation

    async def _delete(
        self: SqlAlchemyConversationStore,
        requested_session_id: str,
    ) -> bool:
        result = await original_delete(self, requested_session_id)
        events.append("delete-committed")
        return result

    async def _release(
        self: PmIntegrationService,
        project: PmProjectView,
        requested_session_id: str,
    ) -> None:
        assert store.get_conversation(requested_session_id) is None
        events.append("release")
        released.append(requested_session_id)

    monkeypatch.setattr(SqlAlchemyConversationStore, "delete_conversation", _delete)
    monkeypatch.setattr(PmIntegrationService, "release", _release)
    response = await client.delete(f"/v1/sessions/{session_id}")

    assert response.status_code == 200, response.text
    assert events == ["delete-committed", "release"]
    assert released == [session_id]

    # Recreate, then force the committed-delete seam to report failure.
    monkeypatch.setattr(SqlAlchemyConversationStore, "delete_conversation", original_delete)
    created = await _create_pm_session(client, agent_id)
    assert created.status_code == 201, created.text
    failed_id = created.json()["id"]
    released.clear()

    async def _failed_delete(
        self: SqlAlchemyConversationStore,
        requested_session_id: str,
    ) -> bool:
        return False

    monkeypatch.setattr(SqlAlchemyConversationStore, "delete_conversation", _failed_delete)
    response = await client.delete(f"/v1/sessions/{failed_id}")
    assert response.status_code == 404
    assert released == []


async def test_failed_host_launch_keeps_committed_session_lease(
    client: httpx.AsyncClient,
    app: FastAPI,
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_id = await _agent_id(client)
    host_store = HostStore(db_uri)
    host_store.upsert_on_connect(_HOST_ID, "test-host", "local")
    app.state.host_store = host_store
    conn = app.state.host_registry.register(
        _HOST_ID,
        _FakeWebSocket(),
        HostHelloFrame(version="test", frame_protocol_version=1, name="test-host"),
        owner="local",
    )
    _patch_path_validation(monkeypatch)
    acquired: list[str] = []
    released: list[str] = []
    _patch_pm(monkeypatch, acquired=acquired, released=released)

    async def _fail_launch() -> None:
        encoded = await conn.outbound_queue.get()
        assert encoded is not None
        frame = decode_host_frame(encoded)
        assert isinstance(frame, HostLaunchRunnerFrame)
        conn.pending_launches[frame.request_id].set_result(
            {"status": "failed", "error": "test launch failure"}
        )

    responder = asyncio.create_task(_fail_launch())
    response = await _create_pm_session(client, agent_id)
    await responder

    assert response.status_code == 201, response.text
    session_id = response.json()["id"]
    assert acquired == [session_id]
    assert released == []
    assert SqlAlchemyConversationStore(db_uri).get_conversation(session_id) is not None
