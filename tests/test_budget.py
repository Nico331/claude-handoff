"""Tests of the bootstrap token budget (`bootstrap_budget_tokens`)."""

from __future__ import annotations

import json
from pathlib import Path

import handoff
import hf_check
import hf_report
import hook
import pytest
from helpers import build_tree, write
from hf_config import Config, load_config, validate


def test_budget_default_and_values(tmp_path: Path) -> None:
    assert load_config(tmp_path).bootstrap_budget_tokens == 50000
    write(tmp_path / "handoff.json", json.dumps({"bootstrap_budget_tokens": 1234}))
    assert load_config(tmp_path).bootstrap_budget_tokens == 1234


@pytest.mark.parametrize("value", [0, -5, True, "50000"])
def test_budget_must_be_a_positive_integer(value: object) -> None:
    cfg, problems = validate({"bootstrap_budget_tokens": value})
    assert cfg is None
    assert any("bootstrap_budget_tokens must be a positive integer" in p for p in problems)


def test_bootstrap_cost_counts_indexes_and_bootstrap_entries(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    cost = hf_report.bootstrap_cost(root)
    stats = hf_report.compute_stats(root, None)
    assert (cost.files, cost.size, cost.tokens) == (
        stats.bootstrap.files, stats.bootstrap.size, stats.bootstrap.tokens)
    assert cost.files == 5
    assert hf_report.bootstrap_cost(tmp_path / "missing").files == 0


def test_check_fails_only_when_over_budget(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert hf_check.check(root, cfg=Config()) == []
    errors = hf_check.check(root, cfg=Config(bootstrap_budget_tokens=10))
    assert len(errors) == 1
    assert "over the budget of 10" in errors[0] and "demote rank <= 1" in errors[0]


def test_check_reads_the_budget_from_the_config_file(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    write(root / "handoff.json", json.dumps({"bootstrap_budget_tokens": 10}))
    assert handoff.main(["--root", str(root), "check"]) == 1
    assert "over the budget of 10" in capsys.readouterr().out


def test_stats_shows_the_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    assert handoff.main(["--root", str(root), "stats"]) == 0
    out = capsys.readouterr().out
    assert "bootstrap budget: 50000 tokens; the bootstrap uses" in out
    assert "OVER BUDGET" not in out
    write(root / "handoff.json", json.dumps({"bootstrap_budget_tokens": 10}))
    assert handoff.main(["--root", str(root), "stats"]) == 0
    assert "(OVER BUDGET)" in capsys.readouterr().out


def _hook_text(project: Path, capsys: pytest.CaptureFixture[str]) -> str:
    env = {"CLAUDE_PROJECT_DIR": str(project)}
    assert hook.main(["hook.py", "session-start"], environ=env) == 0
    raw = capsys.readouterr().out
    raw.encode("ascii")
    return str(json.loads(raw)["hookSpecificOutput"]["additionalContext"])


def test_hook_reports_the_bootstrap_cost(tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    project = tmp_path.resolve()
    root = build_tree(project / ".claude")
    assert root == project / ".claude" / "handoff"
    text = _hook_text(project, capsys)
    assert "Bootstrap read: ~" in text and "of a 50000-token budget" in text
    assert "OVER BUDGET" not in text


@pytest.mark.parametrize(("language", "warning"), [
    ("en", "OVER BUDGET: read the indexes"),
    ("it", "OLTRE IL BUDGET: leggere gli indici"),
])
def test_hook_warns_when_over_budget(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                     language: str, warning: str) -> None:
    project = tmp_path.resolve()
    root = build_tree(project / ".claude")
    write(root / "handoff.json",
          json.dumps({"bootstrap_budget_tokens": 1, "language": language}))
    text = _hook_text(project, capsys)
    assert warning in text
