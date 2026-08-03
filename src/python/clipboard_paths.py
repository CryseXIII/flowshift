"""Strict remote path validation for clipboard transfer V2."""
from __future__ import annotations

import ntpath
import os
import stat
from pathlib import Path


MAX_RELATIVE_PATH_BYTES = 1024
MAX_PATH_COMPONENT_BYTES = 255

_INVALID_WINDOWS_CHARS = frozenset('<>:"|?*')
_RESERVED_DEVICE_NAMES = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CLOCK$", "CONIN$", "CONOUT$"}
    | {f"COM{number}" for number in "123456789"}
    | {f"LPT{number}" for number in "123456789"}
    | {f"COM{chr(codepoint)}" for codepoint in (0x00B9, 0x00B2, 0x00B3)}
    | {f"LPT{chr(codepoint)}" for codepoint in (0x00B9, 0x00B2, 0x00B3)}
)


class PathValidationError(ValueError):
    """Raised when a remote path cannot be represented safely on Windows."""


def windows_collision_key(value: str) -> str:
    """Approximate Windows case matching without expanding characters."""
    lowered = []
    for character in value.replace("\\", "/"):
        candidate = character.lower()
        lowered.append(candidate if len(candidate) == 1 else character)
    return "".join(lowered)


def canonical_relative_path(path: str) -> str:
    """Validate a remote path and return its slash-separated wire form."""
    if not isinstance(path, str) or not path:
        raise PathValidationError("remote path must be a non-empty string")
    if "\x00" in path:
        raise PathValidationError("remote path contains NUL")
    if path.startswith(("/", "\\")) or ntpath.splitdrive(path)[0]:
        raise PathValidationError("absolute, UNC, device, and drive paths are forbidden")

    canonical = path.replace("\\", "/")
    components = canonical.split("/")
    if any(not component for component in components):
        raise PathValidationError("remote path contains an empty component")

    for component in components:
        if component in (".", ".."):
            raise PathValidationError("dot path components are forbidden")
        if any(ord(char) < 32 or char in _INVALID_WINDOWS_CHARS for char in component):
            raise PathValidationError("remote path contains Windows-invalid characters")
        if component.endswith((".", " ")):
            raise PathValidationError("remote path component ends in dot or space")
        try:
            component_size = len(component.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise PathValidationError("remote path is not valid UTF-8") from exc
        if component_size > MAX_PATH_COMPONENT_BYTES:
            raise PathValidationError("remote path component exceeds 255 UTF-8 bytes")

        device_stem = component.split(".", 1)[0].rstrip(" .").upper()
        if device_stem in _RESERVED_DEVICE_NAMES:
            raise PathValidationError("remote path uses a reserved Windows device name")

    try:
        path_size = len(canonical.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise PathValidationError("remote path is not valid UTF-8") from exc
    if path_size > MAX_RELATIVE_PATH_BYTES:
        raise PathValidationError("remote path exceeds 1024 UTF-8 bytes")
    return canonical


def validate_path_entries(entries) -> list[str]:
    """Validate entry paths, Windows collisions, and file ancestry."""
    canonical_paths = []
    by_windows_path = {}
    canonical_prefixes = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") not in ("file", "directory"):
            raise PathValidationError("path entry must be a file or directory object")
        path = canonical_relative_path(entry.get("path"))
        windows_path = windows_collision_key(path)
        components = path.split("/")
        windows_components = windows_path.split("/")
        for end in range(1, len(components) + 1):
            prefix = "/".join(components[:end])
            windows_prefix = "/".join(windows_components[:end])
            previous_prefix = canonical_prefixes.setdefault(windows_prefix, prefix)
            if previous_prefix != prefix:
                raise PathValidationError(
                    f"Windows case collision: {previous_prefix} and {prefix}"
                )
        if windows_path in by_windows_path:
            previous = by_windows_path[windows_path][0]
            if previous == path:
                raise PathValidationError(f"duplicate remote path: {path}")
            raise PathValidationError(f"Windows case collision: {previous} and {path}")
        by_windows_path[windows_path] = (path, entry["type"])
        canonical_paths.append(path)

    for windows_path, (path, _entry_type) in by_windows_path.items():
        windows_components = windows_path.split("/")
        for end in range(1, len(windows_components)):
            ancestor = "/".join(windows_components[:end])
            ancestor_entry = by_windows_path.get(ancestor)
            if ancestor_entry is not None and ancestor_entry[1] == "file":
                raise PathValidationError(
                    f"file path is an ancestor of another entry: {ancestor_entry[0]} and {path}"
                )
    return canonical_paths


def _is_reparse_point(path: Path, path_stat) -> bool:
    if stat.S_ISLNK(path_stat.st_mode):
        return True
    return bool(getattr(path_stat, "st_file_attributes", 0) & 0x400)


def safe_target_path(root, relative_path: str) -> Path:
    """Resolve a validated target below an existing non-reparse root."""
    canonical = canonical_relative_path(relative_path)
    root_path = Path(os.path.abspath(os.fspath(root)))
    try:
        root_stat = os.lstat(root_path)
    except OSError as exc:
        raise PathValidationError("target root is not accessible") from exc
    if not stat.S_ISDIR(root_stat.st_mode) or _is_reparse_point(root_path, root_stat):
        raise PathValidationError("target root must be a non-reparse directory")

    target = root_path.joinpath(*canonical.split("/"))
    try:
        if os.path.commonpath((os.fspath(root_path), os.fspath(target))) != os.fspath(root_path):
            raise PathValidationError("target path escapes its root")
    except ValueError as exc:
        raise PathValidationError("target path escapes its root") from exc

    current = root_path
    components = canonical.split("/")
    for index, component in enumerate(components):
        current /= component
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            break
        except OSError as exc:
            raise PathValidationError("target path is not accessible") from exc
        if _is_reparse_point(current, current_stat):
            raise PathValidationError("target path traverses a reparse point")
        if index < len(components) - 1 and not stat.S_ISDIR(current_stat.st_mode):
            raise PathValidationError("target path traverses a non-directory")
    return target


def ensure_safe_directory_root(root) -> Path:
    """Create a directory root without traversing an existing reparse point."""
    root_path = Path(os.path.abspath(os.fspath(root)))
    current = Path(root_path.anchor)
    for component in root_path.parts[1:]:
        current /= component
        try:
            current_stat = os.lstat(current)
        except FileNotFoundError:
            try:
                os.mkdir(current)
                current_stat = os.lstat(current)
            except OSError as exc:
                raise PathValidationError("target root cannot be created safely") from exc
        except OSError as exc:
            raise PathValidationError("target root is not accessible") from exc
        if not stat.S_ISDIR(current_stat.st_mode) or _is_reparse_point(current, current_stat):
            raise PathValidationError("target root traverses a reparse point or non-directory")
    return root_path
