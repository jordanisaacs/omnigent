"""Tests for virtual PM project views and durable session leases."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

import pytest

from omnigent.entities import Conversation, PagedList
from omnigent.errors import ErrorCode
from omnigent.host.frames import HostHelloFrame, HostIntegrationRequestFrame, decode_host_frame
from omnigent.server import pm_integration as pm_module
from omnigent.server.host_registry import HostRegistry
from omnigent.server.pm_integration import PmIntegrationService
from omnigent.server.routes._host_integration import (
    HostIntegrationError,
    request_host_integration,
)


@dataclass
class _FakeWebSocket:
    sent: list[str]

    async def send_text(self, data: str) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        await asyncio.sleep(3600)
        return ""  # pragma: no cover


class _ConversationStore:
    def __init__(self, conversations: list[Conversation] = []) -> None:  # noqa: B006
        self.conversations = {conversation.id: conversation for conversation in conversations}

    def get_conversation(self, conversation_id: str) -> Conversation | None:
        return self.conversations.get(conversation_id)

    def list_conversations(self, **kwargs: Any) -> PagedList[Conversation]:
        host_id = kwargs.get("host_id")
        rows = [
            conversation
            for conversation in self.conversations.values()
            if host_id is None or conversation.host_id == host_id
        ]
        return PagedList(data=rows, first_id=None, last_id=None, has_more=False)


def _conversation(session_id: str, *, host_id: str, workspace: str) -> Conversation:
    return Conversation(
        id=session_id,
        created_at=1,
        updated_at=1,
        root_conversation_id=session_id,
        host_id=host_id,
        workspace=workspace,
    )


def _register(
    registry: HostRegistry,
    host_id: str,
    *,
    owner: str,
    capable: bool = True,
) -> None:
    registry.register(
        host_id,
        _FakeWebSocket([]),
        HostHelloFrame(
            version="test",
            frame_protocol_version=1,
            name=f"host-{owner}",
            integrations={"pm": {"protocol_version": 1}} if capable else {},
        ),
        owner=owner,
    )


def _projects_payload(path: str = "/projects/demo") -> dict[str, object]:
    return {
        "protocol_version": 1,
        "projects": [
            {
                "name": "demo",
                "path": path,
                "lease_count": 2,
                "worktrees": [
                    {
                        "name": "app",
                        "repo": "app",
                        "path": f"{path}/app",
                        "status": "active",
                        "slot_uuid": "slot-a",
                    },
                    {
                        "name": "api",
                        "repo": "api",
                        "path": f"{path}/api",
                        "status": "detached",
                        "detail": "not attached",
                    },
                ],
            }
        ],
    }


@pytest.mark.asyncio
async def test_virtual_projects_are_owner_scoped_and_exclude_unhealthy_worktrees(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = HostRegistry()
    _register(registry, "a" * 32, owner="alice")
    _register(registry, "b" * 32, owner="bob")
    _register(registry, "c" * 32, owner="alice", capable=False)
    monkeypatch.setattr(pm_module, "get_installation_id", lambda: "install-1")

    async def _request(**kwargs: Any) -> object:
        assert kwargs["operation"] == "projects"
        return _projects_payload()

    monkeypatch.setattr(pm_module, "request_host_integration", _request)
    service = PmIntegrationService(registry, _ConversationStore())  # type: ignore[arg-type]

    projects = await service.list_projects("alice")

    assert len(projects) == 1
    project = projects[0]
    assert project.host_id == "a" * 32
    assert service.connection("b" * 32, "alice") is None
    assert service.connection("c" * 32, "alice") is None
    assert project.id == (await service.get_project("alice", project.id)).id  # type: ignore[union-attr]
    payload = project.as_dict()
    assert payload["directories"] == [{"name": "app", "path": "/projects/demo/app"}]
    assert [entry["included"] for entry in payload["worktrees"]] == [True, False]


@pytest.mark.asyncio
async def test_reconcile_releases_nonmembers_and_acquires_missing_exact_members(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    host_id = "a" * 32
    project_path = "/projects/demo"
    registry = HostRegistry()
    _register(registry, host_id, owner="alice")
    store = _ConversationStore(
        [
            _conversation("retain", host_id=host_id, workspace=project_path),
            _conversation("moved", host_id=host_id, workspace="/projects/elsewhere"),
            _conversation("acquire", host_id=host_id, workspace=project_path),
            _conversation("unrelated", host_id=host_id, workspace="/tmp"),
        ]
    )
    monkeypatch.setattr(pm_module, "get_installation_id", lambda: "install-1")
    operations: list[tuple[str, dict[str, Any]]] = []

    async def _request(**kwargs: Any) -> object:
        operation = kwargs["operation"]
        arguments = kwargs.get("arguments") or {}
        operations.append((operation, arguments))
        if operation == "projects":
            return _projects_payload(project_path)
        if operation == "lease_list":
            return [
                {"project": "demo", "holder": "omnigent:install-1", "lease_id": "retain"},
                {"project": "demo", "holder": "omnigent:install-1", "lease_id": "missing"},
                {"project": "demo", "holder": "omnigent:install-1", "lease_id": "moved"},
            ]
        if operation == "lease_acquire":
            return [
                {
                    "project": "demo",
                    "holder": arguments["holder"],
                    "lease_id": arguments["lease_id"],
                    "worktrees": [
                        {
                            "name": "app",
                            "repo": "app",
                            "path": f"{project_path}/app",
                            "slot_uuid": "slot-a",
                        }
                    ],
                }
            ]
        if operation == "lease_release":
            return [{"released": True}]
        raise AssertionError(operation)

    monkeypatch.setattr(pm_module, "request_host_integration", _request)
    service = PmIntegrationService(registry, store)  # type: ignore[arg-type]

    await service.reconcile_host(host_id, "alice")

    released = {
        arguments["lease_id"]
        for operation, arguments in operations
        if operation == "lease_release"
    }
    acquired = {
        arguments["lease_id"]
        for operation, arguments in operations
        if operation == "lease_acquire"
    }
    assert released == {"missing", "moved"}
    assert acquired == {"acquire"}
    assert all(
        arguments.get("holder") == "omnigent:install-1"
        for operation, arguments in operations
        if operation.startswith("lease_")
    )


@pytest.mark.asyncio
async def test_generic_host_request_round_trip_uses_pending_future() -> None:
    """The server proxy correlates a host result and removes pending state."""
    registry = HostRegistry()
    _register(registry, "a" * 32, owner="alice")
    conn = registry.get("a" * 32)
    assert conn is not None

    task = asyncio.create_task(
        request_host_integration(
            host_registry=registry,
            host_conn=conn,
            integration="pm",
            operation="projects",
        )
    )
    encoded = await conn.outbound_queue.get()
    assert encoded is not None
    frame = decode_host_frame(encoded)
    assert isinstance(frame, HostIntegrationRequestFrame)
    conn.pending_integration_requests[frame.request_id].set_result(
        {"status": "ok", "payload": {"projects": []}, "error": None}
    )

    assert await task == {"projects": []}
    assert conn.pending_integration_requests == {}


@pytest.mark.asyncio
async def test_generic_host_failure_is_a_controlled_service_unavailable() -> None:
    registry = HostRegistry()
    _register(registry, "a" * 32, owner="alice")
    conn = registry.get("a" * 32)
    assert conn is not None

    task = asyncio.create_task(
        request_host_integration(
            host_registry=registry,
            host_conn=conn,
            integration="pm",
            operation="projects",
        )
    )
    encoded = await conn.outbound_queue.get()
    assert encoded is not None
    frame = decode_host_frame(encoded)
    assert isinstance(frame, HostIntegrationRequestFrame)
    conn.pending_integration_requests[frame.request_id].set_result(
        {"status": "failed", "payload": None, "error": "pm unavailable"}
    )

    with pytest.raises(HostIntegrationError) as exc_info:
        await task
    assert exc_info.value.code == ErrorCode.RUNNER_UNAVAILABLE
    assert conn.pending_integration_requests == {}
