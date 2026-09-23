"""
Model of the three-level handoff: names, frontmatter and the children of a folder.

Frontmatter is parsed by hand (no PyYAML): one ``key: value`` per line between two
``---`` lines, with the four keys ``title``, ``summary``, ``rank`` and ``updated``.
Nothing here writes to disk except ``write_text_preserving``, used by ``reindex``.
"""

from __future__ import annotations

import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from hf_config import DEFAULT_CONFIG, Config, HandoffError

__all__ = ["HandoffError"]

INDEX_NAME = "INDEX.md"
"""Name of the index, the only `.md` file allowed at levels 1 and 2."""

LOCK_NAME = ".lock"
"""Name of the lock file inside the folder being modified."""

LOCK_LOG_NAME = ".lock-log"
"""Append-only log of stolen and force-released locks, at the root."""

RANKS = range(1, 6)
"""Allowed values for `rank`: 1 (critical) to 5 (archive)."""

KEYS = ("title", "summary", "rank", "updated")
"""Frontmatter keys: all required, and the only ones allowed."""

KEBAB = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
"""kebab-case name: ASCII lowercase letters and digits separated by single hyphens."""


@dataclass(frozen=True)
class Meta:
    """Valid frontmatter of an entry or an index.

    Attributes:
        title: short title, not empty.
        summary: one line, 1 to `max_summary` characters.
        rank: importance, 1 to 5 (1 = critical).
        updated: date of the last update.
    """

    title: str
    summary: str
    rank: int
    updated: date


@dataclass(frozen=True)
class Child:
    """A child of a folder as it appears in the folder's index table.

    Attributes:
        name: sort name (file stem or sub-folder name).
        link: relative link target (`entry.md` or `topic/INDEX.md`).
        path: absolute path of the file carrying the frontmatter.
        meta: frontmatter of the child.
    """

    name: str
    link: str
    path: Path
    meta: Meta


def read_text(path: Path) -> str:
    """Read a UTF-8 file, normalising line endings to `\\n`.

    Raises:
        OSError, UnicodeDecodeError: when the file cannot be read as UTF-8.
    """
    return path.read_bytes().decode("utf-8").replace("\r\n", "\n")


def write_text_preserving(path: Path, text: str) -> None:
    """Atomically write `text`, keeping the line-ending style of the existing file.

    Args:
        path: file to (re)write; if it exists and uses CRLF, it stays CRLF.
        text: new content with `\\n` line endings.

    Side effects:
        Creates a temporary file in the same folder and moves it over `path`.
    """
    crlf = path.exists() and b"\r\n" in path.read_bytes()
    data = (text.replace("\n", "\r\n") if crlf else text).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".md")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def split_frontmatter(text: str) -> tuple[list[str], int] | None:
    """Locate the frontmatter block at the top of the text.

    Returns:
        `(inner lines, index of the closing line)`, or `None` when the text does not
        start with `---` or the block is not closed.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None
    for index in range(1, len(lines)):
        if lines[index].strip() == "---":
            return lines[1:index], index
    return None


def unquote(value: str) -> str:
    """Strip one pair of matching single or double quotes around a value."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def parse_frontmatter(text: str, cfg: Config = DEFAULT_CONFIG
                      ) -> tuple[Meta | None, list[str]]:
    """Read and validate the frontmatter.

    Args:
        text: file content.
        cfg: configuration (for the summary length limit).

    Returns:
        `(Meta, [])` when valid, otherwise `(None, problems)`.
    """
    block = split_frontmatter(text)
    if block is None:
        return None, ["frontmatter missing or not closed by '---'"]
    raw: dict[str, str] = {}
    errors: list[str] = []
    for line in block[0]:
        if not line.strip():
            continue
        key, sep, value = line.partition(":")
        key = key.strip()
        if not sep:
            errors.append(f"frontmatter line without ':': {line.strip()!r}")
        elif key not in KEYS:
            errors.append(f"unknown frontmatter key: {key!r}")
        elif key in raw:
            errors.append(f"repeated frontmatter key: {key!r}")
        else:
            raw[key] = unquote(value.strip())
    errors.extend(f"missing frontmatter key: {k!r}" for k in KEYS if k not in raw)
    if errors:
        return None, errors
    return _validate(raw, cfg)


def _validate(raw: dict[str, str], cfg: Config) -> tuple[Meta | None, list[str]]:
    """Convert and check the four raw frontmatter values."""
    errors: list[str] = []
    if not raw["title"]:
        errors.append("empty title")
    summary = raw["summary"]
    if not summary:
        errors.append("empty summary")
    elif len(summary) > cfg.max_summary:
        errors.append(f"summary of {len(summary)} characters (max {cfg.max_summary})")
    rank = 0
    if raw["rank"].isdigit() and int(raw["rank"]) in RANKS:
        rank = int(raw["rank"])
    else:
        errors.append(f"invalid rank: {raw['rank']!r} (allowed 1-5)")
    updated = date.min
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw["updated"]):
            raise ValueError(raw["updated"])
        updated = date.fromisoformat(raw["updated"])
    except ValueError:
        errors.append(f"updated is not an ISO date: {raw['updated']!r}")
    if errors:
        return None, errors
    return Meta(raw["title"], summary, rank, updated), []


def rewrite_frontmatter(text: str, rank: int, updated: date) -> str:
    """Rewrite only the `rank` and `updated` values of the frontmatter.

    Raises:
        HandoffError: when there is no frontmatter.
    """
    block = split_frontmatter(text)
    if block is None:
        raise HandoffError("frontmatter missing: cannot update rank and date")
    lines = text.split("\n")
    for index in range(1, block[1]):
        key = lines[index].partition(":")[0].strip()
        if key == "rank":
            lines[index] = f"rank: {rank}"
        elif key == "updated":
            lines[index] = f"updated: {updated.isoformat()}"
    return "\n".join(lines)


def level_of(root: Path, folder: Path) -> int:
    """Level of a folder: 1 the root, 2 an area, 3 a topic (deeper is > 3)."""
    return len(folder.relative_to(root).parts) + 1


def resolve_folder(root: Path, name: str) -> Path:
    """Turn the `<folder>` command-line argument into an absolute path.

    Args:
        root: handoff root.
        name: path relative to the root (`.` for the root itself) or absolute.

    Raises:
        HandoffError: (code 2) when missing, not a folder, outside the root, or deeper
            than level 3.
    """
    root = root.resolve()
    candidate = Path(name)
    folder = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if folder != root and root not in folder.parents:
        raise HandoffError(f"{name}: outside the root {root}", 2)
    if not folder.is_dir():
        raise HandoffError(f"{name}: no such folder", 2)
    if level_of(root, folder) > 3:
        raise HandoffError(f"{name}: deeper than level 3", 2)
    return folder


def subdirs(folder: Path) -> list[Path]:
    """Non-hidden sub-folders, sorted by name."""
    return sorted(p for p in folder.iterdir() if p.is_dir() and not p.name.startswith("."))


def child_sources(root: Path, folder: Path) -> list[tuple[str, str, Path]]:
    """Children a folder's index must list, without reading them.

    Levels 1 and 2 list their sub-folders (through each sub-folder's INDEX.md);
    level 3 lists its `.md` files other than INDEX.md.

    Returns:
        `(name, relative link, file carrying the frontmatter)` triples, by name.
    """
    if level_of(root, folder) >= 3:
        files = sorted(p for p in folder.glob("*.md") if p.name != INDEX_NAME)
        return [(p.stem, p.name, p) for p in files]
    return [(d.name, f"{d.name}/{INDEX_NAME}", d / INDEX_NAME) for d in subdirs(folder)]


def read_children(root: Path, folder: Path, cfg: Config = DEFAULT_CONFIG
                  ) -> tuple[list[Child], list[str]]:
    """Read the children of a folder with their frontmatter.

    Returns:
        `(valid children sorted by rank then name, errors)`; a child without an
        index or with invalid frontmatter only appears among the errors.
    """
    children: list[Child] = []
    errors: list[str] = []
    for name, link, path in child_sources(root, folder):
        if not path.is_file():
            errors.append(f"{path}: index missing")
            continue
        meta, problems = parse_frontmatter(read_text(path), cfg)
        if meta is None:
            errors.extend(f"{path}: {problem}" for problem in problems)
            continue
        children.append(Child(name, link, path, meta))
    children.sort(key=lambda c: (c.meta.rank, c.name))
    return children, errors
