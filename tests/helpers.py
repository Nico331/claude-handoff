"""Shared builders for the tests: frontmatter, files, a valid three-level tree, CLI."""

from __future__ import annotations

import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import hf_index

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
SCRIPT = SCRIPTS / "handoff.py"
HOOK = SCRIPTS / "hook.py"
NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def fm(title: str, summary: str, rank: int | str, updated: str) -> str:
    """Frontmatter block with the four keys."""
    return (f"---\ntitle: {title}\nsummary: {summary}\nrank: {rank}\n"
            f"updated: {updated}\n---\n")


def write(path: Path, text: str) -> Path:
    """Write a file, creating intermediate folders."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def build_tree(tmp_path: Path) -> Path:
    """Valid three-level structure with indexes already generated.

    root -> area-a (topic-x: alpha r2, beta r1, gamma r1; topic-y: delta r3)
         -> area-b (topic-z: eta r4)
    """
    root = tmp_path.resolve() / "handoff"
    write(root / "INDEX.md", "# Handoff\n\nRoot rules.\n")
    for area in ("area-a", "area-b"):
        write(root / area / "INDEX.md", fm(area, f"Area {area}.", 5, "2000-01-01")
              + f"\nPurpose of {area}, hand-written.\n")
    topics = {"area-a/topic-x": [("alpha", 2, "2026-09-20"), ("beta", 1, "2026-09-21"),
                                 ("gamma", 1, "2026-09-19")],
              "area-a/topic-y": [("delta", 3, "2026-09-10")],
              "area-b/topic-z": [("eta", 4, "2026-08-01")]}
    for topic, entries in topics.items():
        write(root / topic / "INDEX.md", fm(topic, f"Topic {topic}.", 5, "2000-01-01")
              + "\nReferences of the topic.\n")
        for name, rank, day in entries:
            write(root / topic / f"{name}.md",
                  fm(name.capitalize(), f"Summary of {name}.", rank, day) + f"\nText {name}.\n")
    for folder in (*topics, "area-a", "area-b", "."):
        hf_index.reindex(root, (root / folder).resolve(), None)
    return root


def run_cli(root: Path, *args: str, cwd: Path | None = None,
            python: str | None = None) -> subprocess.CompletedProcess[str]:
    """Run the tool in a separate process, as an agent would."""
    return subprocess.run([python or sys.executable, str(SCRIPT), "--root", str(root), *args],
                          capture_output=True, text=True, encoding="utf-8", check=False,
                          cwd=cwd)
