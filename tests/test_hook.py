"""Tests of the session hook (scripts/hook.py).

The hook runs at every session start and every prompt: it must always exit 0 and
print ASCII JSON (or nothing), whatever the state of the project.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import hf_init
import hook
import pytest
from helpers import HOOK, fm, write


def entry(path: Path, rank: str, summary: str) -> None:
    """Write an entry with minimal frontmatter."""
    write(path, fm("T", summary, rank, "2026-09-23") + "\n# T\n")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A project with an empty handoff root at the default place."""
    (tmp_path / ".claude" / "handoff").mkdir(parents=True)
    return tmp_path.resolve()


def run(project: Path, event: str, capsys: pytest.CaptureFixture[str],
        extra_env: dict[str, str] | None = None) -> str:
    """Call the hook in-process and return its raw stdout."""
    env = {"CLAUDE_PROJECT_DIR": str(project), **(extra_env or {})}
    assert hook.main(["hook.py", event], environ=env) == 0
    return capsys.readouterr().out


def context(raw: str, name: str) -> str:
    """Parse the payload, check it is ASCII and for `name`, return the context."""
    raw.encode("ascii")
    data = json.loads(raw)
    assert data["hookSpecificOutput"]["hookEventName"] == name
    return str(data["hookSpecificOutput"]["additionalContext"])


def test_lists_only_level_3_entries_within_bootstrap_rank(project: Path) -> None:
    root = project / ".claude/handoff"
    entry(root / "rules/cluster/shared.md", "1", "Do not touch other namespaces.")
    entry(root / "state/numbers/tests.md", "2", "Counts.")
    entry(root / "rules/cluster/INDEX.md", "1", "Index.")
    entry(root / "rules/INDEX.md", "1", "Area index.")
    assert hook.bootstrap_entries(root, 1) == [("rules/cluster/shared.md",
                                                "Do not touch other namespaces.")]
    assert [p for p, _ in hook.bootstrap_entries(root, 2)] == [
        "rules/cluster/shared.md", "state/numbers/tests.md"]


def test_malformed_files_do_not_break(project: Path) -> None:
    root = project / ".claude/handoff"
    write(root / "a/b/plain.md", "# no frontmatter\n")
    write(root / "a/b/unclosed.md", "---\nrank: 1\nsummary: x\n")
    write(root / "a/b/binary.md", "")
    (root / "a/b/binary.md").write_bytes(b"\xff\xfe---\n")
    entry(root / "a/b/wordy.md", "one", "x")
    entry(root / "a/b/zero.md", "0", "x")
    assert hook.bootstrap_entries(root, 5) == []


def test_session_start_injects_protocol_and_summaries(
        project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    entry(project / ".claude/handoff/rules/user/prefs.md", "1", "Outcome before détail.")
    text = context(run(project, "session-start", capsys), "SessionStart")
    assert "ALL entries with rank <= 1" in text
    assert ".claude/handoff/rules/user/prefs.md: Outcome before détail." in text
    assert "handoff.py" in text and "list --max-rank 1" in text


def test_prompt_injects_the_update_reminder(project: Path,
                                            capsys: pytest.CaptureFixture[str]) -> None:
    text = context(run(project, "prompt", capsys), "UserPromptSubmit")
    assert "never hold two locks" in text and "never secrets" in text
    assert ".claude/handoff/" in text


def test_no_handoff_hint_at_start_silence_at_prompt(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    text = context(run(tmp_path.resolve(), "session-start", capsys), "SessionStart")
    assert "/claude-handoff:init" in text and "\n" not in text
    assert run(tmp_path.resolve(), "prompt", capsys) == ""


def test_italian_texts(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = project / ".claude/handoff"
    write(root / "handoff.json", '{"language": "it"}')
    entry(root / "regole/utente/preferenze.md", "1", "Italiano, esito prima del dettaglio.")
    start = context(run(project, "session-start", capsys), "SessionStart")
    assert "TUTTE le voci con rank <= 1" in start and "preferenze.md" in start
    prompt = context(run(project, "prompt", capsys), "UserPromptSubmit")
    assert "mai due lock insieme" in prompt and "mai segreti" in prompt


def test_inject_summaries_off_and_bootstrap_rank(
        project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = project / ".claude/handoff"
    entry(root / "a/b/two.md", "2", "Second.")
    write(root / "handoff.json", '{"bootstrap_max_rank": 2}')
    assert "a/b/two.md: Second." in context(run(project, "session-start", capsys),
                                            "SessionStart")
    write(root / "handoff.json", '{"bootstrap_max_rank": 2, "inject_summaries": false}')
    text = context(run(project, "session-start", capsys), "SessionStart")
    assert "Second." not in text and "list --max-rank 2" in text


def test_invalid_config_falls_back_to_defaults(project: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    root = project / ".claude/handoff"
    write(root / "handoff.json", "{broken")
    entry(root / "a/b/one.md", "1", "One.")
    text = context(run(project, "session-start", capsys), "SessionStart")
    assert "handoff.json ignored" in text and "a/b/one.md: One." in text


def test_listing_is_capped(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = project / ".claude/handoff"
    for i in range(hook.MAX_LISTED + 5):
        entry(root / f"a/t{i // 40}/e{i:03d}.md", "1", f"S{i}.")
    text = context(run(project, "session-start", capsys), "SessionStart")
    assert "... and 5 more" in text and "S0." in text


def test_root_from_env_variable(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path.resolve()
    entry(project / "docs/memory/a/b/x.md", "1", "Custom root.")
    raw = run(project, "session-start", capsys, {"CLAUDE_HANDOFF_ROOT": "docs/memory"})
    assert "docs/memory/a/b/x.md: Custom root." in context(raw, "SessionStart")


def test_project_from_stdin_cwd(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path.resolve()
    entry(project / ".claude/handoff/a/b/x.md", "1", "From stdin.")
    stdin = json.dumps({"cwd": str(project), "hook_event_name": "SessionStart"})
    assert hook.main(["hook.py", "session-start"], environ={}, stdin_text=stdin) == 0
    assert "From stdin." in context(capsys.readouterr().out, "SessionStart")


def test_internal_error_still_exits_0(project: Path, capsys: pytest.CaptureFixture[str],
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*_: object) -> None:
        raise RuntimeError("x")

    monkeypatch.setattr(hook, "build_message", boom)
    text = context(run(project, "prompt", capsys), "UserPromptSubmit")
    assert "could not read the handoff (RuntimeError)" in text


def test_real_init_output_is_listed(project: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hf_init.init(project / ".claude/handoff", None, "init")
    text = context(run(project, "session-start", capsys), "SessionStart")
    assert "rules/handoff/how-to-use.md" in text


@pytest.mark.parametrize(("event", "name"), [("session-start", "SessionStart"),
                                             ("prompt", "UserPromptSubmit")])
def test_subprocess_ascii_json(project: Path, event: str, name: str) -> None:
    """As Claude Code runs it: separate process, JSON on stdin, exit 0, ASCII stdout."""
    entry(project / ".claude/handoff/a/b/x.md", "1", "Café — règle.")
    result = subprocess.run(
        [sys.executable, str(HOOK), event],
        input=json.dumps({"cwd": str(project)}).encode(), capture_output=True, check=False,
        env={"SYSTEMROOT": _systemroot(), "PATH": ""})
    assert result.returncode == 0
    context(result.stdout.decode("ascii"), name)


def _systemroot() -> str:
    """SYSTEMROOT is needed by Python on Windows even in a minimal environment."""
    import os
    return os.environ.get("SYSTEMROOT", "")
