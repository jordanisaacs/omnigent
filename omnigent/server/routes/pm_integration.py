"""Read-only REST projection of live PM projects."""

from __future__ import annotations

from fastapi import APIRouter, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.pm_integration import PmIntegrationService
from omnigent.server.routes._auth_helpers import require_user


def create_pm_integration_router(
    service: PmIntegrationService,
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the live PM integration router."""
    router = APIRouter()

    @router.get("/integrations/pm")
    async def get_pm_integration(request: Request) -> dict[str, object]:
        user_id = require_user(request, auth_provider)
        hosts = service.visible_connections(user_id)
        return {
            "object": "pm.integration",
            "enabled": bool(hosts),
            "protocol_version": 1,
            "host_count": len(hosts),
        }

    @router.get("/integrations/pm/projects")
    async def list_pm_projects(request: Request) -> dict[str, object]:
        user_id = require_user(request, auth_provider)
        projects = await service.list_projects(user_id)
        return {"object": "list", "data": [project.as_dict() for project in projects]}

    @router.get("/integrations/pm/projects/{view_id}")
    async def get_pm_project(request: Request, view_id: str) -> dict[str, object]:
        user_id = require_user(request, auth_provider)
        project = await service.get_project(user_id, view_id)
        if project is None:
            raise OmnigentError("PM project not found", code=ErrorCode.NOT_FOUND)
        return project.as_dict()

    return router
