"""
Differential tests: the Rust binary must behave exactly like the Python tool.

Every case builds the same tree twice, runs the same command lines with both
implementations, and compares exit codes, stdout, stderr and every file left on disk.
The comparison ignores only what legitimately differs: the temporary folder, the pid,
host and time inside lock records, the tool path quoted by the hook, the name of the
tool (`handoff.py` vs `handoff`), the wording of JSON decoder errors and of argparse
usage errors (only the exit code is compared there).

Runs only when HANDOFF_BIN names the binary:

    HANDOFF_BIN=rust/dist/handoff.exe python -m pytest tests/test_parity.py

HANDOFF_FUZZ sets the number of random trees (default 150).
"""

from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

from helpers import HOOK, SCRIPT, build_tree, fm, write
from hf_tree import read_text

BIN = os.environ.get("HANDOFF_BIN")
pytestmark = pytest.mark.skipif(not BIN, reason="HANDOFF_BIN is not set")

Result = tuple[int, str, str]


# ---------------------------------------------------------------- running both tools


def _env(extra: dict[str, str] | None) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items()
           if k not in ("CLAUDE_HANDOFF_ROOT", "CLAUDE_PROJECT_DIR")}
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(extra or {})
    return env


def _run(argv: list[str], cwd: Path, env: dict[str, str]) -> Result:
    done = subprocess.run(argv, cwd=cwd, env=env, capture_output=True, check=False,
                          stdin=subprocess.DEVNULL)
    # Python writes CRLF on a Windows console stream, Rust always LF.
    out = done.stdout.decode("utf-8", "replace").replace("\r\n", "\n")
    err = done.stderr.decode("utf-8", "replace").replace("\r\n", "\n")
    return done.returncode, out, err


def run_python(args: list[str], cwd: Path, env: dict[str, str]) -> Result:
    if args[0] == "hook":
        return _run([sys.executable, str(HOOK), *args[1:]], cwd, env)
    return _run([sys.executable, str(SCRIPT), *args], cwd, env)


def run_rust(args: list[str], cwd: Path, env: dict[str, str]) -> Result:
    return _run([str(Path(BIN).resolve()), *args], cwd, env)


# ---------------------------------------------------------------- normalisation


_PY_TOOL = re.compile(r'"[^"]*" "[^"]*handoff\.py"')
_LOCK_FIELDS = [(re.compile(r"pid=\d+"), "pid=N"), (re.compile(r"host=\S+"), "host=H"),
                (re.compile(r"since \S+ \(age \d+m\d\ds"), "since T (age A"),
                (re.compile(r"not valid JSON \(.*?\)(;|$)", re.M), r"not valid JSON (...)\1"),
                (re.compile(r"\(handoff(?:\.py)? lock\)"), "(handoff lock)")]


def _bases(base: Path) -> list[str]:
    """Spellings of the temporary folder that can appear in the output."""
    forms = {str(base), str(base.resolve()), base.resolve().as_posix(), base.as_posix()}
    return sorted(forms, key=len, reverse=True)


def normalise(text: str, base: Path) -> str:
    for form in _bases(base):
        text = text.replace(form, "<BASE>")
    text = _PY_TOOL.sub("<TOOL>", text)
    text = text.replace(f'"{Path(BIN).resolve().as_posix()}"', "<TOOL>")
    text = text.replace("scripts/handoff.py check", "<TOOL> check")
    text = text.replace("handoff.py ", "handoff ")
    for pattern, replacement in _LOCK_FIELDS:
        text = pattern.sub(replacement, text)
    return text.replace("\\", "/")


def normalise_result(result: Result, base: Path) -> Result:
    code, out, err = result
    if code == 2 and err.startswith("usage:"):
        return code, normalise(out, base), "<usage error>"
    if out.startswith('{"hookSpecificOutput"'):
        data = json.loads(out)
        data["hookSpecificOutput"]["additionalContext"] = normalise(
            data["hookSpecificOutput"]["additionalContext"], base)
        out = json.dumps(data, sort_keys=True)
    return code, normalise(out, base), normalise(err, base)


def _normalise_lock_record(text: str) -> str:
    """A `.lock` or `.lock-log` content without the fields that depend on the run."""
    lines = []
    for line in text.splitlines():
        try:
            data = json.loads(line)
        except ValueError:
            lines.append(line)
            continue
        for record in (data, data.get("previous") if isinstance(data, dict) else None):
            if isinstance(record, dict):
                for key in ("pid", "host", "acquired_at", "at"):
                    if key in record:
                        record[key] = "<X>"
        lines.append(json.dumps(data, sort_keys=True))
    return "\n".join(lines)


def snapshot(base: Path) -> dict[str, str]:
    """Every file and folder under `base`, contents normalised."""
    found: dict[str, str] = {}
    for path in sorted(base.rglob("*")):
        rel = path.relative_to(base).as_posix()
        if path.is_dir():
            found[rel + "/"] = ""
            continue
        data = path.read_bytes()
        if path.name in (".lock", ".lock-log"):
            found[rel] = _normalise_lock_record(data.decode("utf-8", "replace"))
        elif path.name.startswith((".tmp-", ".handoff.json.")):
            found[rel] = "<temporary file left behind>"
        else:
            found[rel] = normalise(data.decode("utf-8", "replace"), base)
    return found


# ---------------------------------------------------------------- scenarios


def fresh(base: Path) -> None:
    build_tree(base)


def mutate(change: Callable[[Path], None]) -> Callable[[Path], None]:
    def build(base: Path) -> None:
        build_tree(base)
        change(base / "handoff")
    return build


def _replace(rel: str, old: str, new: str, count: int = -1) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        path = root / rel
        path.write_bytes(read_text(path).replace(old, new, count).encode("utf-8"))
    return change


def _write(rel: str, text: str) -> Callable[[Path], None]:
    return lambda root: write(root / rel, text)


def _many(*changes: Callable[[Path], None]) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        for step in changes:
            step(root)
    return change


def _extra_topics(root: Path) -> None:
    for i in range(21):
        write(root / f"area-b/extra-{i:02d}/INDEX.md", fm(f"x{i}", "x", 4, "2026-08-01"))


def _extra_files(root: Path) -> None:
    for i in range(51):
        write(root / f"area-b/topic-z/entry-{i:02d}.md", fm("v", "v", 4, "2026-08-01"))


def _lock(rel: str, content: str) -> Callable[[Path], None]:
    return lambda root: write(root / rel / ".lock", content)


def _crlf(rel: str) -> Callable[[Path], None]:
    def change(root: Path) -> None:
        path = root / rel
        path.write_bytes(path.read_bytes().replace(b"\r\n", b"\n").replace(b"\n", b"\r\n"))
    return change


def _swap_rows(root: Path) -> None:
    index = root / "area-a/topic-x/INDEX.md"
    lines = read_text(index).split("\n")
    rows = [i for i, line in enumerate(lines) if line.startswith("| 1 |")]
    lines[rows[0]], lines[rows[1]] = lines[rows[1]], lines[rows[0]]
    index.write_bytes("\n".join(lines).encode("utf-8"))


def _repeat_row(root: Path) -> None:
    index = root / "area-a/topic-x/INDEX.md"
    lines = read_text(index).split("\n")
    row = next(line for line in lines if "alpha.md" in line)
    index.write_bytes(read_text(index).replace(row, f"{row}\n{row}").encode("utf-8"))


EXPIRED = json.dumps({"owner": "dead", "pid": 1, "host": "h",
                      "acquired_at": "2020-01-01T00:00:00+00:00", "ttl_seconds": 60})
VALID = json.dumps({"owner": "alive", "pid": 1, "host": "h",
                    "acquired_at": "2999-01-01T00:00:00+00:00", "ttl_seconds": 60})

LINKS = ["[a](beta.md)", "[a](../topic-y/delta.md#section)", "[a](../../area-b/INDEX.md)",
         "[a](../../../outside/exists.md)", "[a](<beta.md>)", '![img](beta.md "title")',
         "[a](https://example.org/x.md)", "[a](mailto:x@example.org)", "[a](#anchor)",
         "`[a](in-code.md)`", "```\n[a](in-block.md)\n```", "[a](missing.md)",
         "[a](../../../outside/missing.md)", "[a](../topic-y/missing.md#x)",
         "[ref]: missing-ref.md", "[a](beta.md?x=1)", "[a](be%74a.md)", "[a](/abs/path.md)",
         "[a](C:/win/path.md)", "~~~\n[a](fenced.md)\n~~~", "[a](missing%20file.md)"]

FRONTMATTERS = [
    "", "---\ntitle: T\n", fm("T", "S", 6, "2026-08-01"), fm("T", "S", 0, "2026-08-01"),
    fm("T", "S", "one", "2026-08-01"), fm("T", "S", 4, "01/08/2026"),
    fm("T", "S", 4, "2026-02-30"), fm("T", "", 4, "2026-08-01"),
    fm("T", "x" * 161, 4, "2026-08-01"), fm("", "S", 4, "2026-08-01"),
    "---\ntitle: T\nsummary: S\nrank: 4\n---\n",
    fm("T", "S", 4, "2026-08-01").replace("---\n", "---\nauthor: x\n", 1),
    fm("T", "S", 4, "2026-08-01").replace("---\n", "---\nrank: 4\n", 1),
    fm("T", "S", 4, "2026-08-01").replace("---\n", "---\nno colon here\n", 1),
    fm("'Quoted: title'", '"S | with pipe"', "04", "2026-08-01"),
    fm("Caffè ☕ [x] | y", "Summary with ünïcødé and emoji 🎉", 2, "2026-08-02"),
    "\ufeff" + fm("T", "S", 4, "2026-08-01"),
    fm("T", "é" * 160, 4, "2026-08-01"), fm("T", "é" * 161, 4, "2026-08-01"),
    fm("T", "S", " 3 ", "2026-08-01"), fm("T", "S", 4, " 2026-08-01 "),
]

SCENARIOS: dict[str, Callable[[Path], None]] = {
    "valid": fresh,
    "lock-log-and-config": mutate(_many(_write(".lock-log", "{}\n"),
                                        _write("handoff.json", '{"language": "it"}'))),
    "config-in-area": mutate(_write("area-a/handoff.json", "{}")),
    "config-invalid": mutate(_write("handoff.json", '{"max_topics": 0, "colour": "red"}')),
    "config-not-json": mutate(_write("handoff.json", "{not json")),
    "config-array": mutate(_write("handoff.json", "[]")),
    "config-limits": mutate(_write("handoff.json", '{"max_topics": 1, "max_entry_files": 3, '
                                                   '"max_entry_lines": 5, "max_summary": 10}')),
    "config-bootstrap-2": mutate(_write("handoff.json", '{"bootstrap_max_rank": 2, '
                                                        '"language": "it-ch"}')),
    "config-budget-over": mutate(_write("handoff.json", '{"bootstrap_budget_tokens": 10, '
                                                        '"language": "it"}')),
    "config-budget-edge": mutate(_write("handoff.json", '{"bootstrap_budget_tokens": 1180, '
                                                        '"bootstrap_max_rank": 3}')),
    "config-budget-invalid": mutate(_write("handoff.json", '{"bootstrap_budget_tokens": 0}')),
    "config-budget-no-summaries": mutate(_write("handoff.json", '{"bootstrap_budget_tokens": 1, '
                                                                '"inject_summaries": false}')),
    "config-no-summaries": mutate(_write("handoff.json", '{"inject_summaries": false, '
                                                         '"language": "de"}')),
    "config-legacy": mutate(_many(_write("handoff.json", '{"legacy": "../archive"}'),
                                  lambda root: write(root.parent / "archive/n.md", "x" * 7000))),
    "extra-root-file": mutate(_write("notes.md", fm("n", "n", 1, "2026-09-23"))),
    "extra-level-2-file": mutate(_write("area-a/readme.txt", "x")),
    "extra-level-3-file": mutate(_write("area-b/topic-z/data.json", "{}")),
    "folder-at-level-3": mutate(_write("area-b/topic-z/sub/INDEX.md", "x")),
    "folder-at-level-4": mutate(_write("area-b/topic-z/sub/deeper/x.md", "x")),
    "missing-index": lambda base: (build_tree(base),
                                   (base / "handoff/area-a/topic-y/INDEX.md").unlink()),
    "too-many-topics": mutate(_extra_topics),
    "too-many-files": mutate(_extra_files),
    "non-kebab-file": mutate(_write("area-b/topic-z/Bad_Name.md", fm("v", "v", 4, "2026-08-01"))),
    "non-kebab-dir": mutate(_write("area-b/UPPER/INDEX.md", fm("v", "v", 4, "2026-08-01"))),
    "double-hyphen": mutate(_write("area-b/topic-z/a--b.md", fm("v", "v", 4, "2026-08-01"))),
    "uppercase-md": mutate(_write("area-b/topic-z/shout.MD", fm("v", "v", 4, "2026-08-01"))),
    "lowercase-index": mutate(_write("area-b/topic-z/index.md", fm("v", "v", 4, "2026-08-01"))),
    "hidden-md": mutate(_write("area-b/topic-z/.draft.md", fm("v", "v", 1, "2026-08-01"))),
    "hidden-dir": mutate(_write("area-b/.cache/INDEX.md", fm("v", "v", 1, "2026-08-01"))),
    "entry-too-long": mutate(lambda root: write(root / "area-b/topic-z/eta.md",
                                                read_text(root / "area-b/topic-z/eta.md")
                                                + "line\n" * 80)),
    "unicode-line-breaks": mutate(lambda root: write(root / "area-b/topic-z/eta.md",
                                                     read_text(root / "area-b/topic-z/eta.md")
                                                     + "a\u2028b\x0cc\x1dd\n" * 20)),
    "index-rank-9": mutate(_replace("area-a/INDEX.md", "rank: 1", "rank: 9")),
    "entry-missing-from-table": mutate(_write("area-b/topic-z/theta.md",
                                              fm("Theta", "New.", 4, "2026-08-01"))),
    "ghost-row": lambda base: (build_tree(base),
                               (base / "handoff/area-a/topic-x/alpha.md").unlink()),
    "repeated-row": mutate(_repeat_row),
    "wrong-order": mutate(_swap_rows),
    "row-summary": mutate(_write("area-b/topic-z/eta.md", fm("Eta", "Changed.", 4, "2026-08-01"))),
    "row-rank": mutate(_write("area-b/topic-z/eta.md", fm("Eta", "Summary of eta.", 3,
                                                          "2026-08-01"))),
    "row-date": mutate(_write("area-b/topic-z/eta.md", fm("Eta", "Summary of eta.", 4,
                                                          "2026-08-02"))),
    "row-title": mutate(_write("area-b/topic-z/eta.md", fm("ETA", "Summary of eta.", 4,
                                                           "2026-08-01"))),
    "markers-missing": mutate(lambda root: write(
        root / "area-a/INDEX.md",
        read_text(root / "area-a/INDEX.md").split("<!-- handoff:index:start -->")[0])),
    "marker-end-missing": mutate(_replace("area-a/INDEX.md", "<!-- handoff:index:end -->", "")),
    "markers-swapped": mutate(_many(
        _replace("area-a/INDEX.md", "<!-- handoff:index:end -->", "@@"),
        _replace("area-a/INDEX.md", "<!-- handoff:index:start -->", "<!-- handoff:index:end -->"),
        _replace("area-a/INDEX.md", "@@", "<!-- handoff:index:start -->"))),
    "folder-rank": mutate(_replace("area-a/topic-y/INDEX.md", "rank: 3", "rank: 2")),
    "folder-date": mutate(_replace("area-b/INDEX.md", "updated: 2026-08-01", "updated: 2026-09-01")),
    "root-without-frontmatter": fresh,
    "root-with-bad-frontmatter": mutate(lambda root: write(
        root / "INDEX.md", "---\ntitle: x\n---\n" + read_text(root / "INDEX.md"))),
    "empty-topic": mutate(_write("area-b/empty/INDEX.md", fm("e", "Empty.", 5, "2026-01-01"))),
    "empty-area": mutate(_write("area-c/INDEX.md", fm("c", "Area c.", 5, "2026-01-01"))),
    "crlf-entry-and-index": mutate(_many(_crlf("area-b/topic-z/eta.md"),
                                         _crlf("area-b/topic-z/INDEX.md"),
                                         _write("area-b/topic-z/new.md",
                                                fm("New", "Added.", 2, "2026-09-24")))),
    "not-utf8-config": mutate(lambda root: (root / "handoff.json").write_bytes(b"\xff\xfe{}")),
    "expired-lock": mutate(_lock("area-a", EXPIRED)),
    "valid-lock": mutate(_lock("area-a/topic-x", VALID)),
    "garbage-lock": mutate(_lock("area-b", "not json at all")),
    "naive-lock": mutate(_lock("area-b", EXPIRED.replace("+00:00", ""))),
    "lock-string-fields": mutate(_lock("area-b", EXPIRED.replace('"pid": 1', '"pid": "7"'))),
    "root-lock": mutate(_lock(".", VALID)),
}
for number, text in enumerate(FRONTMATTERS):
    SCENARIOS[f"frontmatter-{number:02d}"] = mutate(
        _many(_write("area-b/topic-z/eta.md", text + "\nText.\n")))
for number, link in enumerate(LINKS):
    SCENARIOS[f"link-{number:02d}"] = mutate(_many(
        lambda root: write(root.parent / "outside/exists.md", "x"),
        lambda root, link=link: write(root / "area-a/topic-x/alpha.md",
                                      read_text(root / "area-a/topic-x/alpha.md") + f"\n{link}\n")))

R = ["--root", "handoff"]
HOOK_ENV = {"CLAUDE_HANDOFF_ROOT": "handoff"}

SEQUENCES: dict[str, list[list[str]]] = {
    "check": [[*R, "check"]],
    "check-warn": [[*R, "check", "--warn-only"]],
    "list": [[*R, "list"], [*R, "list", "--max-rank", "1"], [*R, "list", "--area", "area-b"],
             [*R, "list", "--area", "nope"]],
    "stats": [[*R, "stats"], [*R, "stats", "--legacy", "outside"]],
    "status": [[*R, "status"]],
    "reindex-all": [[*R, "reindex", "--all", "--owner", "x"], [*R, "check"]],
    "reindex-one": [[*R, "reindex", "area-a/topic-x", "--no-lock"],
                    [*R, "reindex", "area-b/topic-z", "--owner", "x"],
                    [*R, "lock", "area-b/topic-z", "--owner", "x"],
                    [*R, "reindex", "area-b/topic-z", "--owner", "x"],
                    [*R, "unlock", "area-b/topic-z", "--owner", "x"], [*R, "check"]],
    "locks": [[*R, "lock", "area-a", "--owner", "a"], [*R, "lock", "area-a", "--owner", "b"],
              [*R, "lock", "area-a", "--owner", "b", "--steal-stale"],
              [*R, "unlock", "area-a", "--owner", "b"],
              [*R, "unlock", "area-a", "--owner", "b", "--force"],
              [*R, "unlock", "area-a", "--owner", "b"], [*R, "status"]],
    "steal": [[*R, "lock", "area-b", "--owner", "t", "--steal-stale"], [*R, "status"],
              [*R, "lock", "area-a", "--owner", "t"],
              [*R, "lock", "area-a", "--owner", "t", "--steal-stale"], [*R, "check"]],
    "hook": [["hook", "session-start"], ["hook", "prompt"], ["hook"]],
    "language": [[*R, "language"], [*R, "language", "it"], [*R, "language", "it"],
                 [*R, "language", "XX"], [*R, "language"]],
    "init": [[*R, "init", "--areas", "area-a,new-area", "--owner", "i"], [*R, "check"],
             [*R, "init"]],
    "usage": [[*R, "lock", "area-a"], [*R, "lock", "nowhere", "--owner", "a"],
              [*R, "lock", "../..", "--owner", "a"], [*R, "lock", "area-a", "--owner", " "],
              [*R, "reindex"], [*R, "reindex", "--all"], [*R, "reindex", "--all", "x"],
              [*R, "list", "--max-rank", "0"], [*R, "bogus"], [*R, "check", "--wa"]],
}


def _run_sequence(build: Callable[[Path], None], steps: list[list[str]], base: Path,
                  runner: Callable[[list[str], Path, dict[str, str]], Result]) -> list[Result]:
    base.mkdir(parents=True)
    build(base)
    results = []
    for args in steps:
        env = _env(HOOK_ENV | {"CLAUDE_PROJECT_DIR": str(base)} if args[0] == "hook" else None)
        results.append(normalise_result(runner(args, base, env), base))
    return results


def compare(build: Callable[[Path], None], steps: list[list[str]], tmp_path: Path) -> None:
    python_base, rust_base = tmp_path / "py", tmp_path / "rs"
    expected = _run_sequence(build, steps, python_base, run_python)
    actual = _run_sequence(build, steps, rust_base, run_rust)
    for args, want, got in zip(steps, expected, actual):
        if want[0] == 1 and "Traceback" in want[2]:
            # Python crashed (e.g. a file that is not UTF-8); Rust must fail too.
            assert got[0] == 1, (args, got)
            continue
        assert got == want, f"{' '.join(args)}\npython: {want}\nrust:   {got}"
    assert snapshot(rust_base) == snapshot(python_base)


@pytest.mark.parametrize("sequence", sorted(SEQUENCES))
@pytest.mark.parametrize("scenario", sorted(SCENARIOS))
def test_parity(scenario: str, sequence: str, tmp_path: Path) -> None:
    if sequence in ("init", "language", "hook", "usage") and not scenario.startswith(
            ("valid", "config", "lock-log")):
        pytest.skip("covered on the configuration scenarios")
    compare(SCENARIOS[scenario], SEQUENCES[sequence], tmp_path)


def test_parity_init_from_scratch(tmp_path: Path) -> None:
    steps = [["init"], ["check"], ["init", "--language", "it"], ["list"],
             ["--root", "other", "init", "--areas", "x,y", "--no-seed", "--language", "pt-br"],
             ["--root", "other", "language"], ["--root", "bad", "init", "--areas", "Bad"],
             ["--root", "bad2", "init", "--language", "EN"], ["hook", "session-start"],
             ["hook", "prompt"]]
    compare(lambda base: None, steps, tmp_path)


def test_parity_pointer_and_missing_root(tmp_path: Path) -> None:
    def build(base: Path) -> None:
        build_tree(base)
        write(base / ".claude/handoff.json", '{"root": "handoff"}')
    steps = [["check"], ["list", "--max-rank", "1"], ["hook", "session-start"],
             ["--root", "nowhere", "check"], ["--root", "nowhere", "list"],
             ["hook", "prompt"]]
    compare(build, steps, tmp_path)


# ---------------------------------------------------------------- random trees


WORDS = ["alpha", "beta", "gamma", "Delta", "eps_ilon", "zeta", "x--y", "caffè", "n1", "-bad"]
TEXTS = ["plain", "with | pipe", "with ] bracket", "ünïcødé 🎉", "'quoted'", '"double"',
         "a: colon", "", "x" * 170, "tab\there"]


def random_tree(seed: int, base: Path) -> None:
    rng = random.Random(seed)
    build_tree(base)
    root = base / "handoff"
    for _ in range(rng.randint(1, 12)):
        area = rng.choice(["area-a", "area-b", "area-c", "Area-D"])
        topic = rng.choice(["topic-x", "topic-y", "topic-z", "topic-new", "bad topic"])
        name = rng.choice(WORDS)
        rank = rng.choice(["1", "2", "3", "5", "0", "9", "x", ""])
        date = rng.choice(["2026-09-20", "2026-01-01", "2026-13-01", "yesterday"])
        text = fm(rng.choice(TEXTS), rng.choice(TEXTS), rank, date)
        roll = rng.random()
        if roll < 0.1:
            text = text.replace("---\n", "", 1)
        elif roll < 0.2:
            text += "\n" + rng.choice(LINKS) + "\n"
        elif roll < 0.25:
            text += "line\n" * rng.randint(70, 90)
        if not (root / area / topic / "INDEX.md").exists() and rng.random() < 0.7:
            write(root / area / "INDEX.md", fm(area, "Area.", 3, "2026-01-01"))
            write(root / area / topic / "INDEX.md", fm(topic, "Topic.", 3, "2026-01-01"))
        write(root / area / topic / f"{name}.md", text)
    if rng.random() < 0.3:
        write(root / rng.choice(["area-a", "area-b"]) / ".lock",
              rng.choice([EXPIRED, VALID, "garbage"]))
    if rng.random() < 0.3:
        write(root / "handoff.json", rng.choice(['{"bootstrap_max_rank": 3}', '{"language": "it"}',
                                                 '{"max_summary": 20}', '{"bogus": 1}']))
    if rng.random() < 0.2:
        _crlf("area-a/topic-x/INDEX.md")(root)


FUZZ = int(os.environ.get("HANDOFF_FUZZ", "150"))


@pytest.mark.parametrize("seed", range(FUZZ))
def test_parity_random_tree(seed: int, tmp_path: Path) -> None:
    steps = [[*R, "check"], [*R, "list"], [*R, "list", "--max-rank", "2"], [*R, "stats"],
             ["hook", "session-start"], [*R, "status"], [*R, "reindex", "--all", "--owner", "f"],
             [*R, "check"], [*R, "list", "--max-rank", "1"]]
    compare(lambda base: random_tree(seed, base), steps, tmp_path)
