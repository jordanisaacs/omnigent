"""Stable directory identities for multi-directory sessions."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import PurePath

DEFAULT_DIRECTORY_ID = "default"
DIRECTORY_ID_PREFIX = "dir_"
MAX_SESSION_DIRECTORIES = 16


@dataclass(frozen=True)
class SessionDirectory:
    """One project directory attached to a session.

    ``default`` is the session's primary cwd (the legacy ``workspace``
    field). Additional roots receive opaque ``dir_<uuid>`` identifiers that
    remain stable when the directory set is inherited by child sessions.
    """

    id: str
    path: str

    @property
    def name(self) -> str:
        """Return a compact display name without making it an identity."""
        return PurePath(self.path).name or self.path

    def as_dict(self) -> dict[str, str]:
        """Return the public JSON representation."""
        return {"id": self.id, "path": self.path, "name": self.name}


def generate_directory_id() -> str:
    """Mint an opaque stable id for an additional directory."""
    return f"{DIRECTORY_ID_PREFIX}{uuid.uuid4().hex}"


def validate_directory_id(directory_id: str) -> str:
    """Validate and return a public session-directory identifier."""
    if directory_id == DEFAULT_DIRECTORY_ID:
        return directory_id
    suffix = directory_id.removeprefix(DIRECTORY_ID_PREFIX)
    if (
        not directory_id.startswith(DIRECTORY_ID_PREFIX)
        or len(suffix) != 32
        or any(ch not in "0123456789abcdef" for ch in suffix)
    ):
        raise ValueError(f"invalid session directory id: {directory_id!r}")
    return directory_id


def build_session_directories(
    workspace: str | None,
    additional_paths: Iterable[str] = (),
) -> tuple[SessionDirectory, ...]:
    """Build a new session directory set from canonical paths."""
    directories: list[SessionDirectory] = []
    if workspace is not None and workspace.strip():
        directories.append(SessionDirectory(DEFAULT_DIRECTORY_ID, workspace))
    directories.extend(
        SessionDirectory(generate_directory_id(), path) for path in additional_paths
    )
    validate_session_directories(directories)
    return tuple(directories)


def validate_session_directories(
    directories: Iterable[SessionDirectory],
) -> tuple[SessionDirectory, ...]:
    """Validate size, identifier, and canonical-path uniqueness invariants."""
    values = tuple(directories)
    if len(values) > MAX_SESSION_DIRECTORIES:
        raise ValueError(f"a session supports at most {MAX_SESSION_DIRECTORIES} directories")
    ids = [directory.id for directory in values]
    if len(ids) != len(set(ids)):
        raise ValueError("session directory ids must be unique")
    paths = [directory.path for directory in values]
    if len(paths) != len(set(paths)):
        raise ValueError("session directory paths must be unique")
    for directory in values:
        if not directory.path:
            raise ValueError("session directory paths must be non-empty")
        validate_directory_id(directory.id)
    return values


def validate_workspace_directory_consistency(
    directories: Iterable[SessionDirectory],
    workspace: str | None,
) -> tuple[SessionDirectory, ...]:
    """Ensure ``workspace`` and the stable ``default`` root agree.

    An empty directory tuple is the legacy representation and remains valid
    with a non-null workspace. A non-empty set must include exactly the same
    default path when workspace is set; additional-only child scopes require
    workspace to be null.
    """
    values = validate_session_directories(directories)
    if not values:
        return values
    default = next(
        (directory for directory in values if directory.id == DEFAULT_DIRECTORY_ID),
        None,
    )
    if workspace is None and default is not None:
        raise ValueError("a default session directory requires workspace")
    if workspace is not None and (default is None or default.path != workspace):
        raise ValueError("the default session directory must match workspace")
    return values


def select_session_directories(
    parent_directories: Iterable[SessionDirectory],
    directory_ids: Iterable[str] | None,
) -> tuple[SessionDirectory, ...]:
    """Return an inherited child scope, rejecting attempts to widen it.

    ``None`` inherits all parent roots. An explicit empty list creates a
    private scratch child with no project roots. Order always follows the
    parent's order rather than caller input.
    """
    parent = validate_session_directories(parent_directories)
    if directory_ids is None:
        return parent
    selected_ids = tuple(directory_ids)
    if len(selected_ids) != len(set(selected_ids)):
        raise ValueError("directory_ids must not contain duplicates")
    parent_ids = {directory.id for directory in parent}
    unknown = sorted(set(selected_ids) - parent_ids)
    if unknown:
        raise ValueError(f"directory_ids are outside the parent scope: {unknown}")
    wanted = set(selected_ids)
    return tuple(directory for directory in parent if directory.id in wanted)


def encode_session_directories(directories: Iterable[SessionDirectory]) -> str | None:
    """Encode a directory set for the session metadata table."""
    values = validate_session_directories(directories)
    if not values:
        return None
    return json.dumps(
        [{"id": directory.id, "path": directory.path} for directory in values],
        separators=(",", ":"),
    )


def replace_default_directory(
    directories: Iterable[SessionDirectory],
    workspace: str,
) -> tuple[SessionDirectory, ...]:
    """Set the primary workspace while preserving additional stable roots."""
    values = validate_session_directories(directories)
    additional = tuple(directory for directory in values if directory.id != DEFAULT_DIRECTORY_ID)
    return validate_session_directories(
        (SessionDirectory(DEFAULT_DIRECTORY_ID, workspace), *additional)
    )


def decode_session_directories(
    raw: str | bytes | None,
    *,
    workspace: str | None,
) -> tuple[SessionDirectory, ...]:
    """Decode stored roots, falling back to legacy ``workspace`` rows."""
    if raw is None:
        return build_session_directories(workspace)
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8")
    payload: object = json.loads(raw)
    if not isinstance(payload, list):
        raise ValueError("stored session directories must be a list")
    directories: list[SessionDirectory] = []
    for value in payload:
        if not isinstance(value, dict):
            raise ValueError("stored session directory entries must be objects")
        directory_id = value.get("id")
        path = value.get("path")
        if not isinstance(directory_id, str) or not isinstance(path, str):
            raise ValueError("stored session directories require string id and path")
        directories.append(SessionDirectory(directory_id, path))
    values = validate_session_directories(directories)
    return validate_workspace_directory_consistency(values, workspace)
