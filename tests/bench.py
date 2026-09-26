"""
Wall-clock benchmark of the Python tool against the Rust binary.

Every command runs as a fresh process, the way Claude Code runs a hook, on three
trees: the one `init` creates, a medium one and one at the configured limits
(6 areas x 20 topics x 50 entries). Prints a Markdown table of median times.

    python tests/bench.py <path of the binary> [runs]
"""

from __future__ import annotations

import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "handoff.py"
HOOK = REPO / "scripts" / "hook.py"


def fm(title: str, summary: str, rank: int, updated: str) -> str:
    return f"---\ntitle: {title}\nsummary: {summary}\nrank: {rank}\nupdated: {updated}\n---\n"


def build(project: Path, areas: int, topics: int, entries: int, binary: str) -> None:
    """A valid handoff of the given shape under `project/.claude/handoff`."""
    subprocess.run([binary, "init", "--no-seed", "--areas",
                    ",".join(f"area-{a}" for a in range(areas))],
                   cwd=project, check=True, capture_output=True)
    root = project / ".claude" / "handoff"
    for a in range(areas):
        for t in range(topics):
            topic = root / f"area-{a}" / f"topic-{t:02d}"
            topic.mkdir(parents=True, exist_ok=True)
            (topic / "INDEX.md").write_text(fm(f"Topic {t}", "A topic.", 3, "2026-01-01")
                                            + "\nPurpose.\n", encoding="utf-8")
            for e in range(entries):
                rank = 1 if (a + t + e) % 40 == 0 else 2 + (a + t + e) % 4
                body = "\n".join(f"Line {i} of entry {e}, see [x](../topic-{t:02d}/INDEX.md)."
                                 for i in range(30))
                (topic / f"entry-{e:02d}.md").write_text(
                    fm(f"Entry {e}", f"Summary of entry {e} in topic {t}.", rank,
                       f"2026-09-{1 + e % 28:02d}") + "\n" + body + "\n", encoding="utf-8")
    subprocess.run([binary, "reindex", "--all", "--owner", "bench"], cwd=project, check=True,
                   capture_output=True)
    done = subprocess.run([binary, "check"], cwd=project, capture_output=True, text=True)
    assert "structure valid" in done.stdout, done.stdout[-2000:]


COMMANDS = {
    "hook session-start": (["hook", "session-start"], [str(HOOK), "session-start"]),
    "hook prompt": (["hook", "prompt"], [str(HOOK), "prompt"]),
    "check": (["check"], [str(SCRIPT), "check"]),
    "list --max-rank 1": (["list", "--max-rank", "1"], [str(SCRIPT), "list", "--max-rank", "1"]),
    "stats": (["stats"], [str(SCRIPT), "stats"]),
    "reindex --all": (["reindex", "--all", "--owner", "b"],
                      [str(SCRIPT), "reindex", "--all", "--owner", "b"]),
    "lock + unlock": None,
}


def measure(argvs: list[list[str]], cwd: Path, runs: int) -> float:
    """Median wall time in milliseconds of running `argvs` in sequence."""
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(cwd)}
    times = []
    for _ in range(runs):
        start = time.perf_counter()
        for argv in argvs:
            subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False,
                           stdin=subprocess.DEVNULL)
        times.append((time.perf_counter() - start) * 1000)
    return statistics.median(times)


def main() -> None:
    binary = str(Path(sys.argv[1]).resolve())
    runs = int(sys.argv[2]) if len(sys.argv) > 2 else 15
    shapes = {"init (6 files)": (0, 0, 0), "medium (1,200 entries)": (6, 10, 20),
              "limit (6,000 entries)": (6, 20, 50)}
    python = sys.executable
    print(f"Python {sys.version.split()[0]} vs Rust, median of {runs} runs, ms\n")
    print("| tree | command | Python | Rust | speed-up |")
    print("|---|---|---:|---:|---:|")
    for label, shape in shapes.items():
        project = Path(tempfile.mkdtemp())
        try:
            if shape == (0, 0, 0):
                subprocess.run([binary, "init"], cwd=project, check=True, capture_output=True)
            else:
                build(project, *shape, binary)
            for name, argvs in COMMANDS.items():
                if argvs is None:
                    lock = ["lock", ".", "--owner", "b"]
                    unlock = ["unlock", ".", "--owner", "b"]
                    rust = [[binary, *lock], [binary, *unlock]]
                    py = [[python, str(SCRIPT), *lock], [python, str(SCRIPT), *unlock]]
                else:
                    rust = [[binary, *argvs[0]]]
                    py = [[python, *argvs[1]]]
                count = runs if shape[0] == 0 or "hook prompt" in name else max(3, runs // 3)
                t_py = measure(py, project, count)
                t_rs = measure(rust, project, count)
                print(f"| {label} | {name} | {t_py:.0f} | {t_rs:.1f} | {t_py / t_rs:.0f}x |")
        finally:
            shutil.rmtree(project, ignore_errors=True)


if __name__ == "__main__":
    main()
