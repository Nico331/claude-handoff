"""
Generated index tables, ``reindex`` and ``reindex --all``.

The table sits between two markers and is regenerated from the children's
frontmatter; every line outside the markers is hand-written and never touched.
``reindex`` also rewrites ``rank`` (minimum of the children) and ``updated``
(maximum of the children) in the index's own frontmatter.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

import hf_lock
from hf_config import DEFAULT_CONFIG, Config
from hf_tree import (
    INDEX_NAME,
    Child,
    HandoffError,
    level_of,
    parse_frontmatter,
    read_children,
    read_text,
    rewrite_frontmatter,
    split_frontmatter,
    subdirs,
    write_text_preserving,
)

START = "<!-- handoff:index:start -->"
"""Start marker of the generated table."""

END = "<!-- handoff:index:end -->"
"""End marker of the generated table."""

HEADER = ["| rank | entry | summary | updated |", "|---|---|---|---|"]
"""Header and separator of the table."""

_CELL_SPLIT = re.compile(r"(?<!\\)\|")
_LINK = re.compile(r"^\[(?P<text>.*)\]\((?P<target>[^()\s]+)\)$")


@dataclass(frozen=True)
class Row:
    """A row of an index table, as read from the file.

    Attributes:
        rank: text of the rank cell.
        link: link target of the entry cell (empty when the cell is not a link).
        summary: summary, unescaped.
        updated: text of the updated cell.
        line: the original line, for exact comparisons.
    """

    rank: str
    link: str
    summary: str
    updated: str
    line: str


def _escape(text: str) -> str:
    """Make text safe inside a table cell: `|` becomes `\\|`."""
    return text.replace("|", "\\|")


def render_row(child: Child) -> str:
    """Table row for a child: rank, title linked to the child, summary, date."""
    title = _escape(child.meta.title).replace("]", "\\]")
    return (f"| {child.meta.rank} | [{title}]({child.link}) | "
            f"{_escape(child.meta.summary)} | {child.meta.updated.isoformat()} |")


def render_table(children: list[Child]) -> list[str]:
    """Full table (header included) for children already sorted."""
    return HEADER + [render_row(child) for child in children]


def _marker_positions(lines: list[str]) -> tuple[int, int] | None:
    """Positions of the markers, `None` when both are missing.

    Raises:
        HandoffError: a marker repeated, only one present, or the two swapped.
    """
    starts = [i for i, line in enumerate(lines) if line.strip() == START]
    ends = [i for i, line in enumerate(lines) if line.strip() == END]
    if not starts and not ends:
        return None
    if len(starts) != 1 or len(ends) != 1 or ends[0] < starts[0]:
        raise HandoffError("table markers repeated, unpaired or swapped")
    return starts[0], ends[0]


def splice(text: str, table: list[str]) -> str:
    """Replace what lies between the markers with `table`.

    Returns:
        The new text; when the markers are missing they are appended with the table.

    Raises:
        HandoffError: malformed markers.
    """
    lines = text.split("\n")
    positions = _marker_positions(lines)
    if positions is None:
        body = text.rstrip("\n")
        return f"{body}\n\n{START}\n" + "\n".join(table) + f"\n{END}\n"
    start, end = positions
    return "\n".join(lines[: start + 1] + table + lines[end:])


def parse_rows(text: str) -> list[Row] | None:
    """Data rows of the table between the markers.

    Returns:
        The rows (header and separator excluded), `None` when the markers are missing.

    Raises:
        HandoffError: malformed markers.
    """
    lines = text.split("\n")
    positions = _marker_positions(lines)
    if positions is None:
        return None
    rows: list[Row] = []
    for line in lines[positions[0] + 1: positions[1]]:
        stripped = line.strip()
        if not stripped or stripped in HEADER:
            continue
        cells = [c.strip() for c in _CELL_SPLIT.split(stripped.strip("|"))]
        cells += [""] * (4 - len(cells))
        match = _LINK.match(cells[1])
        rows.append(Row(cells[0], match["target"] if match else "",
                        cells[2].replace("\\|", "|"), cells[3], line))
    return rows


def expected_folder_meta(children: list[Child]) -> tuple[int, date] | None:
    """Rank (minimum) and date (maximum) the folder must carry; `None` when empty."""
    if not children:
        return None
    return (min(c.meta.rank for c in children), max(c.meta.updated for c in children))


def reindex(root: Path, folder: Path, owner: str | None, now: datetime | None = None,
            cfg: Config = DEFAULT_CONFIG) -> bool:
    """Regenerate the index table of one folder and the folder's rank and date.

    Args:
        root: handoff root.
        folder: folder (already resolved) whose INDEX.md is regenerated.
        owner: who must hold the folder lock; `None` skips the check (`--no-lock`).
        now: current instant (for the lock check).
        cfg: configuration (summary length limit).

    Returns:
        True when the file changed.

    Raises:
        HandoffError: lock not held (3/4); index missing, children with invalid
            frontmatter, malformed markers or invalid index frontmatter (1).

    Side effects:
        Atomically rewrites INDEX.md, only when it changes.
    """
    if owner is not None:
        hf_lock.require_held(folder, owner, now)
    index = folder / INDEX_NAME
    if not index.is_file():
        raise HandoffError(f"{index}: index missing")
    children, errors = read_children(root, folder, cfg)
    if errors:
        raise HandoffError("\n".join(errors))
    original = read_text(index)
    try:
        text = splice(original, render_table(children))
    except HandoffError as exc:
        raise HandoffError(f"{index}: {exc}") from exc
    if level_of(root, folder) >= 2:
        _, problems = parse_frontmatter(text, cfg)
        if problems:
            raise HandoffError("\n".join(f"{index}: {p}" for p in problems))
    folder_meta = expected_folder_meta(children)
    if folder_meta and split_frontmatter(text) is not None:
        text = rewrite_frontmatter(text, *folder_meta)
    if text == original:
        return False
    write_text_preserving(index, text)
    return True


def bottom_up(root: Path) -> list[Path]:
    """Every folder of the handoff that has an index to maintain, deepest first.

    Topics (level 3) first, then areas (level 2), then the root; within a level, by
    path. Folders deeper than level 3 are left to `check`.
    """
    areas = subdirs(root)
    topics = [topic for area in areas for topic in subdirs(area)]
    return [*topics, *areas, root]


def reindex_all(root: Path, owner: str, cfg: Config = DEFAULT_CONFIG,
                ttl: int | None = None, folders: list[Path] | None = None,
                report: Callable[[Path, bool], None] | None = None) -> list[Path]:
    """Reindex folders bottom-up, holding exactly one lock at a time.

    For each folder: take its lock (never stealing), reindex, release; the release
    happens even on failure, so no lock is left behind and no two locks are ever
    held together.

    Args:
        root: handoff root (resolved).
        owner: lock owner name.
        cfg: configuration (lock lifetime, summary limit).
        ttl: lock lifetime; default `cfg.lock_ttl_seconds`.
        folders: folders to process, already ordered deepest first; default all.
        report: called with `(folder, changed)` after each folder.

    Returns:
        The folders whose INDEX.md changed.

    Raises:
        HandoffError: the first failure (busy lock 3, content error 1); folders
            processed before it keep their new index.
    """
    changed: list[Path] = []
    for folder in folders if folders is not None else bottom_up(root):
        hf_lock.acquire(root, folder, owner, ttl or cfg.lock_ttl_seconds)
        try:
            did_change = reindex(root, folder, owner, cfg=cfg)
        finally:
            hf_lock.release(root, folder, owner)
        if did_change:
            changed.append(folder)
        if report is not None:
            report(folder, did_change)
    return changed
