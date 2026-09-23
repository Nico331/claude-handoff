"""
The ``init`` command: scaffold a handoff root, its areas and ``handoff.json``.

``init`` is idempotent and never overwrites a file: it only creates what is
missing. Afterwards it regenerates the index tables of the folders it touched and
of their ancestors, bottom-up and one lock at a time; hand-written text in an
existing index is left alone (only the generated table between the markers and
the folder's rank/date change).
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date
from pathlib import Path

import hf_index
from hf_config import CONFIG_NAME, DEFAULT_CONFIG, Config, config_json
from hf_tree import INDEX_NAME, KEBAB, HandoffError, level_of

AREA_SUMMARIES = {
    "rules": "Rules every session must know beyond the project docs: preferences, "
             "things not to touch, conventions.",
    "state": "What exists today: components, numbers, environments, access, known "
             "gaps and discrepancies.",
    "decisions": "Decisions in force: what was decided, when, why, and where it is "
                 "formalised.",
    "procedures": "How things are done here: exact commands and the traps that cost "
                  "time.",
    "open": "Open items by topic; decisions awaited from the user at rank 1; closed "
            "items archived at rank 5.",
    "history": "Past context: previous systems, closed work, lessons learned.",
}
"""Summaries of the suggested default areas; other areas get a generic one."""

SEED_TOPIC = "handoff"
"""Topic created in the first area of a brand-new handoff, explaining the handoff."""


@dataclass
class InitResult:
    """What `init` did.

    Attributes:
        created: files and folders created, in creation order.
        reindexed: folders whose INDEX.md table changed.
    """

    created: list[Path] = field(default_factory=list)
    reindexed: list[Path] = field(default_factory=list)


def _frontmatter(title: str, summary: str, rank: int, today: date) -> str:
    """Frontmatter block with the four keys."""
    return (f"---\ntitle: {title}\nsummary: {summary}\nrank: {rank}\n"
            f"updated: {today.isoformat()}\n---\n")


def root_index(today: date) -> str:
    """Hand-written part of a new root INDEX.md."""
    return _frontmatter(
        "Project handoff",
        "Consolidated project memory in three levels; start here and read in rank order.",
        1, today) + """
# Project handoff

Current state of the project, not a diary. It holds what cannot be deduced from the
code and the docs, and links to them instead of copying them.

**Read at session start, in this order:** this file, then the `INDEX.md` of every
area, then every rank-1 entry (`handoff.py list --max-rank 1`), then, for today's
task, the rank <= 2 entries of the topics it touches; the rest on demand, starting
from the summaries in the indexes.

**Rank:** 1 critical (always read) - 2 current state and frequent procedures -
3 useful detail - 4 history - 5 archive. A folder is worth the minimum of what it
contains.

**Write:** `handoff.py lock <folder> --owner <name>`, edit, `reindex`, `unlock`,
then the same on the parent, up to the root. Never hold two locks at once. Then
`handoff.py check`. One fact in one place; never secrets; closed items go to rank 5
or are deleted, never struck through.
"""


def area_index(name: str, today: date) -> str:
    """Hand-written part of a new area INDEX.md."""
    title = name.replace("-", " ").capitalize()
    summary = AREA_SUMMARIES.get(name, f"Topics of the {name} area.")
    return _frontmatter(title, summary, 3, today) + f"""
# {title}

{summary}

Local rules of this area, if any, go here in a few hand-written lines.
"""


def seed_files(today: date) -> dict[str, str]:
    """The seed topic of a brand-new handoff: file name -> content."""
    return {
        INDEX_NAME: _frontmatter(
            "The handoff itself",
            "How this handoff is read and written: bootstrap order, lock protocol, "
            "entry format.", 1, today) + """
# The handoff itself

Rules of this handoff. The claude-handoff plugin injects a short version at every
session start; these entries are the reference.
""",
        "how-to-use.md": _frontmatter(
            "How to read and update the handoff",
            "Read root, area indexes and rank-1 entries first; update in the same action "
            "with one lock at a time; one fact in one place; no secrets.", 1, today) + """
# How to read and update the handoff

## Read (every session)

1. `INDEX.md` of the root;
2. `INDEX.md` of every area;
3. every rank-1 entry, wherever it is (`handoff.py list --max-rank 1`);
4. for today's task: the rank <= 2 entries of the topics it touches, then the rest
   on demand, starting from the summaries in the indexes.

## When to write

At every prompt and after every action: does this change a fact written here, or
add one a future session must know? If so, update it **in the same action**, not
at the end. Typical triggers: a user decision, a new rule, a component added or
removed, a procedure discovered, an open item opened or closed, a number changed.

## How to write

- Hold the lock of the folder of the file you edit:
  `handoff.py lock <area>/<topic> --owner <name>`.
- Edit the entry and its frontmatter (`updated` = today).
- `handoff.py reindex <area>/<topic> --owner <name>`, then `unlock`.
- Same three steps on `<area>`, then on `.` (the root).
- **Never hold two locks at once**; release the child before taking the parent.
- `handoff.py check` at the end must print `structure valid`.
- Exit code 3 from `lock` means someone else is writing: wait and retry. Break an
  expired lock only with `--steal-stale`, never by deleting the file.

## What to write

- One fact in **one place**; if it is already in a README, an ADR or the code,
  link to it instead.
- **Never secrets**: say where a credential lives and its state, never its value.
- A closed item is **not struck through**: lower it to rank 5 or delete it.
""",
        "entry-format.md": _frontmatter(
            "Shape of folders, entries and indexes",
            "Three levels, at most 20 topics per area and 51 files per topic; "
            "frontmatter title, summary, rank, updated; entries under 80 lines.",
            2, today) + """
# Shape of folders, entries and indexes

- Level 1 (root): only `INDEX.md`, `handoff.json` and folders.
- Level 2 (area): only `INDEX.md` and at most 20 topic folders.
- Level 3 (topic): only `.md` files, at most 51 including `INDEX.md`.
- Names in kebab-case, ASCII, no spaces.
- **One entry, one topic**, under 80 lines (aim for 20-50). Longer: split it.

Every entry and every area/topic `INDEX.md` starts with:

```yaml
---
title: Short title
summary: One line, at most 160 characters, saying what is here.
rank: 2
updated: 2026-01-31
---
```

The table between the `handoff:index` markers of each `INDEX.md` is generated by
`handoff.py reindex`; never edit it by hand. A folder's rank is the minimum of its
children's; keep rank 1 short, it is the fixed cost of every session.
""",
    }


def _create(path: Path, text: str, result: InitResult) -> None:
    """Write `text` to `path` only if `path` does not exist yet."""
    if path.exists():
        return
    path.write_text(text, encoding="utf-8", newline="\n")
    result.created.append(path)


def _mkdir(path: Path, result: InitResult) -> None:
    """Create a folder if missing, recording it."""
    if path.is_dir():
        return
    path.mkdir(parents=True)
    result.created.append(path)


def init(root: Path, areas: list[str] | None, owner: str, cfg: Config = DEFAULT_CONFIG,
         seed: bool = True, today: date | None = None) -> InitResult:
    """Scaffold a handoff (see the module docstring).

    Args:
        root: handoff root (resolved); created if missing.
        areas: area names; default `cfg.default_areas`.
        owner: lock owner used for the final reindex.
        cfg: configuration already in force (from an existing `handoff.json`).
        seed: create the seed topic when the root index is created now.
        today: date written in new frontmatter (for tests).

    Returns:
        What was created and reindexed.

    Raises:
        HandoffError: (2) an area name is not kebab-case; (3) a folder to reindex is
            locked by someone else.
    """
    today = today or date.today()
    names = list(areas) if areas else list(cfg.default_areas)
    for name in names:
        if not KEBAB.match(name):
            raise HandoffError(f"area name is not kebab-case: {name!r}", 2)
    result = InitResult()
    _mkdir(root, result)
    _create(root / CONFIG_NAME, config_json(replace(cfg, default_areas=tuple(names))),
            result)
    fresh = not (root / INDEX_NAME).exists()
    _create(root / INDEX_NAME, root_index(today), result)
    for name in names:
        _mkdir(root / name, result)
        _create(root / name / INDEX_NAME, area_index(name, today), result)
    if fresh and seed:
        topic = root / names[0] / SEED_TOPIC
        _mkdir(topic, result)
        for file_name, text in seed_files(today).items():
            _create(topic / file_name, text, result)
    touched = {path if path.is_dir() else path.parent for path in result.created}
    folders = set()
    for folder in touched:
        folders.update(p for p in (folder, *folder.parents)
                       if p == root or root in p.parents)
    ordered = sorted(folders, key=lambda p: (-level_of(root, p), p.as_posix()))
    result.reindexed = hf_index.reindex_all(root, owner, cfg, folders=ordered)
    return result
