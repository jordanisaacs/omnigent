"""Server-side request/response proxy for generic host integrations."""

from __future__ import annotations

import asyncio
import secrets
from typing import Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.host.frames import HostIntegrationRequestFrame, encode_host_frame
from omnigent.server.host_registry import HostConnection, HostRegistry

_INTEGRATION_TIMEOUT_S = 25.0


class HostIntegrationError(OmnigentError):
    """An integration operation failed or its host became unavailable."""

    def __init__(self, message: str) -> None:
        super().__init__(message, code=ErrorCode.RUNNER_UNAVAILABLE)


async def request_host_integration(
    *,
    host_registry: HostRegistry,
    host_conn: HostConnection,
    integration: str,
    operation: str,
    arguments: dict[str, Any] | None = None,
) -> Any:
    """Send one integration frame and return its decoded JSON payload."""
    capability = host_conn.hello.integrations.get(integration)
    if not isinstance(capability, dict):
        raise HostIntegrationError(f"host does not advertise {integration!r} integration")
    request_id = secrets.token_hex(8)
    future: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
    host_conn.pending_integration_requests[request_id] = future
    frame = encode_host_frame(
        HostIntegrationRequestFrame(
            request_id=request_id,
            integration=integration,
            operation=operation,
            arguments=arguments or {},
        )
    )
    try:
        try:
            host_registry.send_text(host_conn, frame)
        except ConnectionError as exc:
            raise HostIntegrationError(f"host {host_conn.host_id!r} connection was lost") from exc
        try:
            result = await asyncio.wait_for(future, timeout=_INTEGRATION_TIMEOUT_S)
        except asyncio.TimeoutError as exc:
            raise HostIntegrationError(
                f"host {host_conn.host_id!r} did not answer {integration}.{operation}"
            ) from exc
    finally:
        host_conn.pending_integration_requests.pop(request_id, None)
    if result.get("status") != "ok":
        raise HostIntegrationError(str(result.get("error") or f"{integration}.{operation} failed"))
    return result.get("payload")
