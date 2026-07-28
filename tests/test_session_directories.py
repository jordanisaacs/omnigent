"""Unit coverage for stable multi-directory session scopes."""

from __future__ import annotations

import pytest

from omnigent.session_directories import (
    DEFAULT_DIRECTORY_ID,
    MAX_SESSION_DIRECTORIES,
    SessionDirectory,
    build_session_directories,
    decode_session_directories,
    encode_session_directories,
    replace_default_directory,
    select_session_directories,
    validate_session_directories,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _directory(index: int, path: str | None = None) -> SessionDirectory:
    """Build one deterministic additional directory for a test."""
    return SessionDirectory(f"dir_{index:032x}", path or f"/repo/{index}")


def test_encode_decode_round_trip_and_legacy_workspace_fallback() -> None:
    """Stored ids round-trip while old workspace-only rows gain ``default``."""
    values = (SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/main"), _directory(1))

    encoded = encode_session_directories(values)

    assert encoded is not None
    assert decode_session_directories(encoded, workspace="/repo/main") == values
    assert decode_session_directories(None, workspace="/repo/legacy") == (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/legacy"),
    )
    assert decode_session_directories(None, workspace="   ") == ()


def test_child_scope_inherits_all_or_an_explicit_subset_in_parent_order() -> None:
    """Omitted, empty, and subset scopes have distinct stable semantics."""
    values = (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/main"),
        _directory(1),
        _directory(2),
    )

    assert select_session_directories(values, None) == values
    assert select_session_directories(values, []) == ()
    assert select_session_directories(values, [values[2].id, values[0].id]) == (
        values[0],
        values[2],
    )
    with pytest.raises(ValueError, match="outside the parent scope"):
        select_session_directories(values[1:], [DEFAULT_DIRECTORY_ID])


def test_directory_set_rejects_duplicate_paths_and_more_than_sixteen_roots() -> None:
    """Canonical path uniqueness and the root-count bound are enforced."""
    with pytest.raises(ValueError, match="paths must be unique"):
        validate_session_directories((_directory(1, "/same"), _directory(2, "/same")))

    too_many = tuple(_directory(index) for index in range(MAX_SESSION_DIRECTORIES + 1))
    with pytest.raises(ValueError, match="at most 16"):
        validate_session_directories(too_many)


def test_replacing_default_workspace_preserves_additional_ids() -> None:
    """A managed/fork host bind changes only the primary root path."""
    extra = _directory(1)
    assert replace_default_directory((extra,), "/new/main") == (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/new/main"),
        extra,
    )


def test_store_round_trips_stable_directories_and_host_rebinds(db_uri: str) -> None:
    """SQL metadata retains ids and keeps ``workspace`` consistent."""
    store = SqlAlchemyConversationStore(db_uri)
    directories = build_session_directories("/repo/main", ["/repo/shared"])
    created = store.create_conversation(
        workspace="/repo/main",
        directories=directories,
    )

    fetched = store.get_conversation(created.id)
    assert fetched is not None
    assert fetched.directories == directories

    rebound = store.set_host_id(created.id, "1" * 32, workspace="/repo/rebound")
    assert rebound.workspace == "/repo/rebound"
    assert rebound.directories[0] == SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/rebound")
    assert rebound.directories[1:] == directories[1:]

    cleared = store.clear_host_binding(created.id)
    assert cleared.workspace is None
    assert cleared.directories == ()


def test_store_rejects_mismatched_default_directory_and_workspace(db_uri: str) -> None:
    """Invalid metadata never lands as a row that fails during hydration."""
    store = SqlAlchemyConversationStore(db_uri)

    with pytest.raises(ValueError, match="must match workspace"):
        store.create_conversation(
            workspace="/repo/main",
            directories=(SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/other"),),
        )
