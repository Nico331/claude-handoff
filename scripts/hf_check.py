"""
The ``check`` command: validation of the whole structure.

Every defect becomes a ``path: message`` line. The check never modifies anything
and does not stop at the first error: a structure with three defects yields three
lines.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import hf_lock
from hf_config import CONFIG_NAME, DEFAULT_CONFIG, Config, config_problems
from hf_index import Row, expected_folder_meta, parse_rows, render_row
from hf_tree import (
    INDEX_NAME,
    KEBAB,
    LOCK_LOG_NAME,
    LOCK_NAME,
    Child,
    HandoffError,
    child_sources,
    parse_frontmatter,
    read_children,
    read_text,
    split_frontmatter,
)

_FENCE = re.compile(r"^\s*(```|~~~)")
_INLINE_CODE = re.compile(r"`+[^`]*`+")
_INLINE_LINK = re.compile(r"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+\"[^\"]*\")?\s*\)")
_REF_LINK = re.compile(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")


def check(root: Path, now: datetime | None = None,
          cfg: Config = DEFAULT_CONFIG) -> list[str]:
    """Validate the structure under `root`.

    Args:
        root: handoff root.
        now: current instant (for lock expiry).
        cfg: configuration (limits).

    Returns:
        The errors, one per line, in path order; empty when everything is valid.
    """
    if not root.is_dir():
        return [f"{root}: root does not exist (run init)"]
    now = now or hf_lock.utcnow()
    errors = [f"{root / CONFIG_NAME}: {p}" for p in config_problems(root)]
    _check_folder(root, root, errors, cfg)
    for path in sorted(root.rglob("*.md")):
        errors.extend(_check_links(path))
    for folder, info in hf_lock.find_locks(root):
        if info.expired(now):
            errors.append(f"{folder / LOCK_NAME}: expired lock, "
                          f"{hf_lock.describe(info, now)}")
    return errors


def _check_folder(root: Path, folder: Path, errors: list[str], cfg: Config) -> None:
    """Check one folder and recurse into the allowed sub-folders."""
    level = len(folder.relative_to(root).parts) + 1
    entries = sorted(folder.iterdir())
    dirs = [p for p in entries if p.is_dir()]
    files = [p for p in entries if p.is_file()]
    allowed = {LOCK_NAME, INDEX_NAME}
    if level == 1:
        allowed |= {LOCK_LOG_NAME, CONFIG_NAME}
    index = folder / INDEX_NAME
    if not index.is_file():
        errors.append(f"{index}: index missing")
    for path in files:
        if path.name in allowed:
            continue
        if level == 3 and path.suffix == ".md":
            if not KEBAB.match(path.stem):
                errors.append(f"{path}: name is not kebab-case")
            continue
        where = "only .md files" if level == 3 else f"only {INDEX_NAME} and folders"
        errors.append(f"{path}: file not allowed at level {level} ({where})")
    if level == 3:
        errors.extend(f"{d}: folder not allowed at level 3" for d in dirs)
        md_count = sum(1 for p in files if p.suffix == ".md")
        if md_count > cfg.max_entry_files:
            errors.append(f"{folder}: {md_count} .md files (max {cfg.max_entry_files})")
        for path in files:
            if path.suffix == ".md" and path.name != INDEX_NAME:
                _check_entry(path, errors, cfg)
    else:
        if level == 2 and len(dirs) > cfg.max_topics:
            errors.append(f"{folder}: {len(dirs)} topics (max {cfg.max_topics})")
        for sub in dirs:
            if not KEBAB.match(sub.name):
                errors.append(f"{sub}: name is not kebab-case")
                continue
            _check_folder(root, sub, errors, cfg)
    if index.is_file():
        _check_index(root, folder, level, errors, cfg)


def _check_entry(path: Path, errors: list[str], cfg: Config) -> None:
    """Frontmatter and length of a level-3 entry."""
    text = read_text(path)
    _, problems = parse_frontmatter(text, cfg)
    errors.extend(f"{path}: {problem}" for problem in problems)
    lines = len(text.splitlines())
    if lines > cfg.max_entry_lines:
        errors.append(f"{path}: {lines} lines (max {cfg.max_entry_lines}); split it")


def _check_index(root: Path, folder: Path, level: int, errors: list[str],
                 cfg: Config) -> None:
    """Frontmatter, table and rank of one INDEX.md."""
    index = folder / INDEX_NAME
    text = read_text(index)
    meta, problems = parse_frontmatter(text, cfg)
    if level >= 2 or split_frontmatter(text) is not None:  # root: frontmatter optional
        errors.extend(f"{index}: {problem}" for problem in problems)
    children, _ = read_children(root, folder, cfg)
    try:
        rows = parse_rows(text)
    except HandoffError as exc:
        errors.append(f"{index}: {exc}")
        return
    if rows is None:
        errors.append(f"{index}: table markers missing (run reindex)")
    else:
        _compare_rows(index, folder, root, rows, children, errors)
    expected = expected_folder_meta(children)
    if meta is not None and expected is not None:
        if meta.rank != expected[0]:
            errors.append(f"{index}: folder rank {meta.rank}, expected {expected[0]} "
                          "(minimum of the children)")
        if meta.updated != expected[1]:
            errors.append(f"{index}: updated {meta.updated}, expected {expected[1]} "
                          "(most recent child)")


def _compare_rows(index: Path, folder: Path, root: Path, rows: list[Row],
                  children: list[Child], errors: list[str]) -> None:
    """Compare the table rows with the real children.

    Reports missing children, ghost or duplicate rows, wrong order, and rows whose
    rank, summary, date or title differ from the child's frontmatter.
    """
    known = {link for _, link, _ in child_sources(root, folder)}
    by_link = {child.link: child for child in children}
    seen: list[str] = []
    for row in rows:
        if row.link not in known:
            errors.append(f"{index}: ghost row in the table: "
                          f"{row.link or repr(row.line.strip())}")
            continue
        if row.link in seen:
            errors.append(f"{index}: repeated row in the table: {row.link}")
            continue
        seen.append(row.link)
        child = by_link.get(row.link)
        if child is not None:
            _compare_row(index, row, child, errors)
    errors.extend(f"{index}: entry missing from the table: {child.link}"
                  for child in children if child.link not in seen)
    listed = [link for link in seen if link in by_link]
    wanted = [child.link for child in children if child.link in listed]
    if listed != wanted:
        errors.append(f"{index}: wrong table order (by rank, then name): "
                      f"expected {', '.join(wanted)}")


def _compare_row(index: Path, row: Row, child: Child, errors: list[str]) -> None:
    """Differences between one table row and the child's frontmatter."""
    if row.line.strip() == render_row(child):
        return
    meta = child.meta
    before = len(errors)
    if row.summary != meta.summary:
        errors.append(f"{index}: summary of {child.link} differs from the file")
    if row.rank != str(meta.rank):
        errors.append(f"{index}: rank of {child.link} is {row.rank}, "
                      f"in the file {meta.rank}")
    if row.updated != meta.updated.isoformat():
        errors.append(f"{index}: date of {child.link} is {row.updated}, "
                      f"in the file {meta.updated}")
    if len(errors) == before:
        errors.append(f"{index}: row of {child.link} differs from the generated one "
                      "(title or format)")


def link_targets(text: str) -> list[str]:
    """Targets of the Markdown links in the text, code blocks and spans excluded.

    Returns:
        Raw targets (inline links, images and reference definitions), in order.
    """
    targets: list[str] = []
    fenced = False
    for line in text.split("\n"):
        if _FENCE.match(line):
            fenced = not fenced
            continue
        if fenced:
            continue
        line = _INLINE_CODE.sub("", line)
        targets.extend(m.group(1) for m in _INLINE_LINK.finditer(line))
        ref = _REF_LINK.match(line)
        if ref:
            targets.append(ref.group(1))
    return targets


def _check_links(path: Path) -> list[str]:
    """Relative links of the file that do not resolve on disk.

    URLs with a scheme (`https:`, `mailto:`), pure anchors (`#x`) and absolute paths
    are ignored; fragment and query are dropped before resolving. Links leaving the
    handoff towards the rest of the repository are checked like any other.
    """
    errors: list[str] = []
    for raw in link_targets(read_text(path)):
        target = raw[1:-1] if raw.startswith("<") and raw.endswith(">") else raw
        if not target or target.startswith(("#", "/")) or _SCHEME.match(target):
            continue
        relative = unquote(target.split("#", 1)[0].split("?", 1)[0])
        if relative and not (path.parent / relative).exists():
            errors.append(f"{path}: broken link: {raw}")
    return errors
