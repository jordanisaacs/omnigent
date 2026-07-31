"""Tests for the host-side, argv-only PM bridge."""

from __future__ import annotations

import json
import subprocess

import pytest

from omnigent.host import pm_integration


def _completed(payload: object, *, returncode: int = 0) -> subprocess.CompletedProcess[bytes]:
    return subprocess.CompletedProcess(
        args=["pm"],
        returncode=returncode,
        stdout=json.dumps(payload).encode(),
        stderr=b"",
    )


def test_capability_probe_is_explicitly_feature_gated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A default host neither executes PM nor advertises its capability."""
    called = False

    def _run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        nonlocal called
        called = True
        return _completed({"protocol_version": 1})

    monkeypatch.delenv(pm_integration.PM_INTEGRATION_ENABLED_ENV, raising=False)
    monkeypatch.setattr(subprocess, "run", _run)

    assert pm_integration.advertised_capabilities() == {}
    assert called is False


def test_capability_probe_requires_matching_protocol(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Only a successful protocol-v1 probe enters the hello capability map."""
    monkeypatch.setenv(pm_integration.PM_INTEGRATION_ENABLED_ENV, "1")
    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: _completed({"protocol_version": 1})
    )

    assert pm_integration.advertised_capabilities() == {"pm": {"protocol_version": 1}}

    monkeypatch.setattr(
        subprocess, "run", lambda *args, **kwargs: _completed({"protocol_version": 2})
    )
    assert pm_integration.advertised_capabilities() == {}


def test_lease_operations_build_fixed_argv_without_a_shell() -> None:
    """Protocol values occupy argv slots and cannot introduce extra commands."""
    assert pm_integration._argv(
        "lease_acquire",
        {
            "project": "demo; touch /tmp/not-run",
            "holder": "omnigent:install",
            "lease_id": "conv_123",
        },
    ) == [
        "pm",
        "project",
        "lease",
        "acquire",
        "--project",
        "demo; touch /tmp/not-run",
        "--holder",
        "omnigent:install",
        "--lease-id",
        "conv_123",
        "--json",
    ]
    assert pm_integration._argv(
        "lease_list",
        {"holder": "omnigent:install"},
    ) == [
        "pm",
        "project",
        "lease",
        "ls",
        "--all",
        "--holder",
        "omnigent:install",
        "--json",
    ]


@pytest.mark.parametrize(
    ("operation", "arguments"),
    [
        ("projects", {"unexpected": "value"}),
        ("lease_acquire", {"project": "demo"}),
        ("lease_release", {"project": "demo", "holder": "h", "lease_id": "id", "x": 1}),
        ("lease_list", {"project": "demo"}),
        ("unknown", {}),
    ],
)
def test_argv_rejects_operations_outside_the_allowlist(
    operation: str,
    arguments: dict[str, object],
) -> None:
    with pytest.raises(pm_integration.PmIntegrationError):
        pm_integration._argv(operation, arguments)


def test_run_maps_timeout_and_os_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    """Host execution failures become safe integration errors."""

    def _timeout(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise subprocess.TimeoutExpired(cmd=["pm"], timeout=1)

    monkeypatch.setattr(subprocess, "run", _timeout)
    with pytest.raises(pm_integration.PmIntegrationError, match="timed out"):
        pm_integration._run(["pm"], timeout=1)

    def _os_error(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        raise PermissionError("denied")

    monkeypatch.setattr(subprocess, "run", _os_error)
    with pytest.raises(pm_integration.PmIntegrationError, match="could not be executed"):
        pm_integration._run(["pm"])


def test_execute_rejects_disabled_integration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(pm_integration.PM_INTEGRATION_ENABLED_ENV, raising=False)
    with pytest.raises(pm_integration.PmIntegrationError, match="disabled"):
        pm_integration.execute("projects", {})
