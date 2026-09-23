"""
Read-only commands ``list`` and ``stats``.

``list`` is what an agent uses at bootstrap (``list --max-rank 1``); ``stats``
measures what that bootstrap costs against the whole structure and, optionally,
against the old flat handoff it replaced.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from hf_config import DEFAULT_CONFIG, Config
from hf_tree import INDEX_NAME, HandoffError, parse_frontmatter, read_text, subdirs

BYTES_PER_TOKEN = 3.5
"""Rough bytes-per-token estimate, used for the bootstrap cost."""


@dataclass(frozen=True)
class Entry:
    """A level-3 entry as `list` prints it.

    Attributes:
        path: path relative to the root, with `/`.
        rank: declared rank.
        summary: declared summary.
    """

    path: str
    rank: int
    summary: str


def entry_files(root: Path, area: str | None = None) -> list[Path]:
    """Level-3 entry files (INDEX.md excluded), in path order.

    Raises:
        HandoffError: (code 2) when `area` does not exist.
    """
    areas = subdirs(root)
    if area is not None:
        areas = [a for a in areas if a.name == area]
        if not areas:
            raise HandoffError(f"no such area: {area}", 2)
    return [path for a in areas for topic in subdirs(a)
            for path in sorted(topic.glob("*.md")) if path.name != INDEX_NAME]


def list_entries(root: Path, max_rank: int | None = None, area: str | None = None,
                 cfg: Config = DEFAULT_CONFIG) -> tuple[list[Entry], list[str]]:
    """Entries sorted by rank, then path.

    Returns:
        `(entries, warnings)`: an entry with invalid frontmatter cannot be ranked, so
        it is left out of the entries and produces a warning instead.
    """
    entries: list[Entry] = []
    warnings: list[str] = []
    for path in entry_files(root, area):
        meta, problems = parse_frontmatter(read_text(path), cfg)
        if meta is None:
            warnings.append(f"{path}: invalid frontmatter, entry skipped ({problems[0]})")
            continue
        if max_rank is None or meta.rank <= max_rank:
            entries.append(Entry(path.relative_to(root).as_posix(), meta.rank,
                                 meta.summary))
    entries.sort(key=lambda e: (e.rank, e.path))
    return entries, warnings


@dataclass
class Tally:
    """Sum of files, bytes and lines.

    Attributes:
        files: number of files.
        size: bytes on disk.
        lines: lines of text.
    """

    files: int = 0
    size: int = 0
    lines: int = 0

    def add(self, path: Path) -> None:
        """Add one file to the sum."""
        data = path.read_bytes()
        self.files += 1
        self.size += len(data)
        self.lines += len(data.splitlines())

    @property
    def tokens(self) -> int:
        """Estimated tokens: bytes / 3.5."""
        return round(self.size / BYTES_PER_TOKEN)


@dataclass
class Stats:
    """Measures of the structure.

    Attributes:
        levels: sums per level (1, 2, 3).
        total: sum of every `.md`.
        bootstrap: root + area indexes + entries with rank <= the bootstrap rank.
        legacy_size: bytes of the old handoff, `None` when not compared.
    """

    levels: dict[int, Tally] = field(default_factory=lambda: {1: Tally(), 2: Tally(),
                                                               3: Tally()})
    total: Tally = field(default_factory=Tally)
    bootstrap: Tally = field(default_factory=Tally)
    legacy_size: int | None = None


def compute_stats(root: Path, legacy: Path | None,
                  cfg: Config = DEFAULT_CONFIG) -> Stats:
    """Measure the structure and the bootstrap cost.

    Args:
        root: handoff root.
        legacy: old handoff folder to compare with (ignored when missing).
        cfg: configuration (bootstrap rank).
    """
    stats = Stats()
    for path in sorted(root.rglob("*.md")):
        level = min(len(path.parent.relative_to(root).parts) + 1, 3)
        stats.levels[level].add(path)
        stats.total.add(path)
        if level < 3:
            stats.bootstrap.add(path)
    boot_entries, _ = list_entries(root, max_rank=cfg.bootstrap_max_rank, cfg=cfg)
    for entry in boot_entries:
        stats.bootstrap.add(root / entry.path)
    if legacy is not None and legacy.is_dir():
        stats.legacy_size = sum(p.stat().st_size for p in legacy.rglob("*") if p.is_file())
    return stats


def _percent(part: int, whole: int) -> str:
    """Percentage with one decimal, `-` when the whole is zero."""
    return f"{100 * part / whole:.1f}%" if whole else "-"


def format_stats(stats: Stats, legacy: Path | None,
                 cfg: Config = DEFAULT_CONFIG) -> list[str]:
    """Text lines of `stats`."""
    lines = [f"level {level}: {t.files} files, {t.size} bytes, {t.lines} lines"
             for level, t in stats.levels.items()]
    total, boot = stats.total, stats.bootstrap
    lines.append(f"total: {total.files} files, {total.size} bytes, {total.lines} lines, "
                 f"~{total.tokens} tokens")
    lines.append(f"bootstrap (root + area indexes + rank <= {cfg.bootstrap_max_rank}): "
                 f"{boot.files} files, {boot.size} bytes, ~{boot.tokens} tokens, "
                 f"{_percent(boot.size, total.size)} of the total")
    if stats.legacy_size is not None:
        legacy_tokens = round(stats.legacy_size / BYTES_PER_TOKEN)
        lines.append(f"legacy handoff ({legacy}): {stats.legacy_size} bytes, "
                     f"~{legacy_tokens} tokens; the bootstrap is "
                     f"{_percent(boot.size, stats.legacy_size)} of it")
    return lines
