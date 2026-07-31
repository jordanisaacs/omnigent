"""Bounded, argv-only bridge from the host daemon to the local ``pm`` CLI."""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Mapping
from typing import cast

from omnigent.json_types import JsonObject as _JsonObject
from omnigent.process_logging import env_truthy

PM_INTEGRATION_ENABLED_ENV = "OMNIGENT_PM_INTEGRATION_ENABLED"
PM_PROTOCOL_VERSION = 1
_TIMEOUT_S = 15.0
_MAX_OUTPUT_BYTES = 2 * 1024 * 1024
_MAX_KEY_LENGTH = 255


class PmIntegrationError(RuntimeError):
    """A safe-to-display failure from the local PM bridge."""


def _bounded_key(arguments: Mapping[str, object], name: str) -> str:
    value = arguments.get(name)
    if not isinstance(value, str) or not value.strip():
        raise PmIntegrationError(f"{name} must be a non-empty string")
    value = value.strip()
    if len(value) > _MAX_KEY_LENGTH or "\n" in value or "\r" in value:
        raise PmIntegrationError(f"{name} is invalid")
    return value


def _argv(operation: str, arguments: Mapping[str, object]) -> list[str]:
    """Translate one protocol operation into a fixed PM argv allowlist."""
    if operation == "projects" and not arguments:
        return ["pm", "integration", "projects", "--json"]
    if operation in {"lease_acquire", "lease_release"}:
        expected = {"project", "holder", "lease_id"}
        if set(arguments) != expected:
            raise PmIntegrationError(f"{operation} requires exactly {sorted(expected)}")
        action = "acquire" if operation == "lease_acquire" else "release"
        return [
            "pm",
            "project",
            "lease",
            action,
            "--project",
            _bounded_key(arguments, "project"),
            "--holder",
            _bounded_key(arguments, "holder"),
            "--lease-id",
            _bounded_key(arguments, "lease_id"),
            "--json",
        ]
    if operation == "lease_list":
        if not set(arguments).issubset({"project", "holder"}) or "holder" not in arguments:
            raise PmIntegrationError("lease_list requires holder and optional project")
        argv = ["pm", "project", "lease", "ls"]
        project = arguments.get("project")
        if project is None:
            argv.append("--all")
        else:
            argv.extend(["--project", _bounded_key(arguments, "project")])
        argv.extend(["--holder", _bounded_key(arguments, "holder"), "--json"])
        return argv
    raise PmIntegrationError(f"unsupported PM operation: {operation!r}")


def _run(argv: list[str], *, timeout: float = _TIMEOUT_S) -> object:
    try:
        result = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except FileNotFoundError as exc:
        raise PmIntegrationError("pm executable was not found on the host PATH") from exc
    except OSError as exc:
        raise PmIntegrationError(f"pm could not be executed: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise PmIntegrationError(f"pm command timed out after {timeout:.0f}s") from exc
    if len(result.stdout) > _MAX_OUTPUT_BYTES or len(result.stderr) > _MAX_OUTPUT_BYTES:
        raise PmIntegrationError("pm command output exceeded the integration limit")
    stderr = result.stderr.decode("utf-8", errors="replace").strip()
    if result.returncode != 0:
        raise PmIntegrationError(stderr or f"pm exited with status {result.returncode}")
    try:
        return cast(object, json.loads(result.stdout))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PmIntegrationError("pm returned invalid JSON") from exc


def advertised_capabilities() -> dict[str, _JsonObject]:
    """Probe PM and return the hello capability map when explicitly enabled."""
    if not env_truthy(os.environ.get(PM_INTEGRATION_ENABLED_ENV)):
        return {}
    try:
        info = _run(["pm", "integration", "info", "--json"], timeout=5.0)
    except PmIntegrationError:
        return {}
    if not isinstance(info, dict) or info.get("protocol_version") != PM_PROTOCOL_VERSION:
        return {}
    return {"pm": {"protocol_version": PM_PROTOCOL_VERSION}}


def execute(operation: str, arguments: Mapping[str, object]) -> object:
    """Execute one PM integration operation after feature and argv validation."""
    if not env_truthy(os.environ.get(PM_INTEGRATION_ENABLED_ENV)):
        raise PmIntegrationError("PM integration is disabled on this host")
    return _run(_argv(operation, arguments))
