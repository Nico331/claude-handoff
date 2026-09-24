"""Tests of handoff.json, root resolution and init."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import handoff
import hf_check
import hf_config
import hf_init
import hf_lock
import pytest
from helpers import run_cli, write
from hf_config import Config, HandoffError, load_config, resolve_root, validate
from hf_tree import parse_frontmatter, read_text

DAY = date(2026, 9, 23)

# ---------------------------------------------------------------- config


def test_config_defaults_when_absent(tmp_path: Path) -> None:
    cfg = load_config(tmp_path)
    assert cfg == Config()
    assert (cfg.max_topics, cfg.max_entry_files, cfg.max_entry_lines, cfg.max_summary) \
        == (20, 51, 80, 160)
    assert cfg.bootstrap_max_rank == 1 and cfg.lock_ttl_seconds == 900
    assert cfg.language == "en" and cfg.inject_summaries is True
    assert cfg.default_areas == ("rules", "state", "decisions", "procedures", "open",
                                 "history")


def test_config_values_are_read(tmp_path: Path) -> None:
    write(tmp_path / "handoff.json", json.dumps({
        "max_topics": 5, "language": "it", "inject_summaries": False,
        "bootstrap_max_rank": 2, "default_areas": ["a", "b"], "legacy": "../old"}))
    cfg = load_config(tmp_path)
    assert cfg.max_topics == 5 and cfg.language == "it" and not cfg.inject_summaries
    assert cfg.bootstrap_max_rank == 2 and cfg.default_areas == ("a", "b")
    assert cfg.legacy == "../old" and cfg.max_summary == 160


@pytest.mark.parametrize(("data", "message"), [
    ([], "JSON object"),
    ({"nope": 1}, "unknown key 'nope'"),
    ({"max_topics": 0}, "max_topics must be a positive integer"),
    ({"max_entry_lines": True}, "max_entry_lines must be a positive integer"),
    ({"lock_ttl_seconds": "900"}, "lock_ttl_seconds must be a positive integer"),
    ({"bootstrap_max_rank": 6}, "bootstrap_max_rank must be an integer from 1 to 5"),
    ({"language": "EN"}, "language must be a lowercase language tag"),
    ({"inject_summaries": "yes"}, "inject_summaries must be true or false"),
    ({"default_areas": []}, "default_areas must be a non-empty list"),
    ({"default_areas": ["a", 3]}, "default_areas must be a non-empty list"),
    ({"legacy": 5}, "legacy must be a path string or null"),
])
def test_config_validation(data: object, message: str) -> None:
    cfg, problems = validate(data)
    assert cfg is None and any(message in p for p in problems), problems


def test_load_config_invalid_raises_code_2(tmp_path: Path) -> None:
    write(tmp_path / "handoff.json", "{broken")
    with pytest.raises(HandoffError) as exc:
        load_config(tmp_path)
    assert exc.value.code == 2 and "not valid JSON" in str(exc.value)


def test_cli_invalid_config_is_a_usage_error(tmp_path: Path) -> None:
    root = tmp_path / "h"
    write(root / "handoff.json", '{"language": "english"}')
    result = run_cli(root, "list")
    assert result.returncode == 2 and "lowercase language tag" in result.stderr


def test_config_json_roundtrip() -> None:
    cfg = Config(language="it", default_areas=("x",))
    assert validate(json.loads(hf_config.config_json(cfg))) == (cfg, [])


# ---------------------------------------------------------------- root resolution


def test_resolve_root_order(tmp_path: Path) -> None:
    project = tmp_path.resolve()
    assert resolve_root(None, project, {}) == project / ".claude" / "handoff"
    write(project / ".claude/handoff.json", '{"root": "docs/memory"}')
    assert resolve_root(None, project, {}) == project / "docs" / "memory"
    env = {"CLAUDE_HANDOFF_ROOT": "env-root"}
    assert resolve_root(None, project, env) == project / "env-root"
    assert resolve_root(Path("cli-root"), project, env) == project / "cli-root"


@pytest.mark.parametrize("pointer", ["{broken", "[]", '{"root": 3}', '{"root": ""}'])
def test_resolve_root_ignores_a_bad_pointer(tmp_path: Path, pointer: str) -> None:
    project = tmp_path.resolve()
    write(project / ".claude/handoff.json", pointer)
    assert resolve_root(None, project, {}) == project / ".claude" / "handoff"


def test_cli_default_root_is_relative_to_cwd(tmp_path: Path) -> None:
    import subprocess
    import sys

    from helpers import SCRIPT
    result = subprocess.run([sys.executable, str(SCRIPT), "init"], cwd=tmp_path,
                            capture_output=True, text=True, encoding="utf-8", check=False)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / ".claude/handoff/INDEX.md").is_file()


# ---------------------------------------------------------------- init


def test_init_scaffolds_a_valid_handoff(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    result = hf_init.init(root, None, "init", today=DAY)
    assert hf_check.check(root) == []
    assert sorted(p.name for p in root.iterdir()) == sorted(
        ["INDEX.md", "handoff.json", *Config().default_areas])
    for area in Config().default_areas:
        meta, problems = parse_frontmatter(read_text(root / area / "INDEX.md"))
        assert problems == [] and meta is not None
    assert (root / "rules/handoff/how-to-use.md").is_file()
    assert root / "INDEX.md" in result.created and root in result.reindexed
    assert not hf_lock.find_locks(root)
    assert load_config(root) == Config()


def test_init_seed_is_the_only_rank_1_and_root_rank_follows(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    hf_init.init(root, None, "init", today=DAY)
    import hf_report
    entries, _ = hf_report.list_entries(root, max_rank=1)
    assert [e.path for e in entries] == ["rules/handoff/how-to-use.md"]
    rules, _ = parse_frontmatter(read_text(root / "rules/INDEX.md"))
    state, _ = parse_frontmatter(read_text(root / "state/INDEX.md"))
    assert rules is not None and rules.rank == 1 and state is not None and state.rank == 3


def test_init_custom_areas_and_no_seed(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    hf_init.init(root, ["alpha", "beta"], "init", seed=False, today=DAY)
    assert sorted(p.name for p in root.iterdir() if p.is_dir()) == ["alpha", "beta"]
    assert not list(root.rglob("how-to-use.md"))
    assert load_config(root).default_areas == ("alpha", "beta")
    assert "Topics of the alpha area." in read_text(root / "alpha/INDEX.md")
    assert hf_check.check(root) == []


def test_init_uses_default_areas_from_existing_config(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    write(root / "handoff.json", '{"default_areas": ["one"]}')
    hf_init.init(root, None, "init", cfg=load_config(root), today=DAY)
    assert (root / "one/INDEX.md").is_file() and not (root / "rules").exists()
    assert read_text(root / "handoff.json") == '{"default_areas": ["one"]}'


def test_init_is_idempotent_and_never_overwrites(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    hf_init.init(root, None, "init", today=DAY)
    index = root / "state/INDEX.md"
    write(index, read_text(index).replace("Local rules", "My own words"))
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    again = hf_init.init(root, None, "init", today=date(2027, 1, 1))
    assert again.created == [] and again.reindexed == []
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before


def test_init_adds_a_missing_area_and_updates_the_root_table(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    hf_init.init(root, ["alpha"], "init", today=DAY)
    root_text = read_text(root / "INDEX.md").replace("Current state", "Hand-edited state")
    write(root / "INDEX.md", root_text)
    result = hf_init.init(root, ["alpha", "beta"], "init", today=DAY)
    assert root / "beta" in result.created and root in result.reindexed
    after = read_text(root / "INDEX.md")
    assert "Hand-edited state" in after and "(beta/INDEX.md)" in after
    assert not (root / "beta/handoff").exists()  # the seed only goes in a new handoff
    assert hf_check.check(root) == []


def test_init_rejects_bad_area_names(tmp_path: Path) -> None:
    with pytest.raises(HandoffError) as exc:
        hf_init.init(tmp_path / "h", ["Bad Name"], "init")
    assert exc.value.code == 2 and not (tmp_path / "h").exists()


def test_init_fails_on_a_locked_folder(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    hf_init.init(root, ["alpha"], "init", today=DAY)
    hf_lock.acquire(root, root, "someone")
    with pytest.raises(HandoffError) as exc:
        hf_init.init(root, ["alpha", "beta"], "init", today=DAY)
    assert exc.value.code == 3


def test_cli_init(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path / "h"
    assert handoff.main(["--root", str(root), "init", "--areas", "a, b"]) == 0
    out = capsys.readouterr().out
    assert "created:" in out and "reindexed:" in out
    assert handoff.main(["--root", str(root), "init"]) == 0
    assert "already initialised" in capsys.readouterr().out
    assert handoff.main(["--root", str(root), "check"]) == 0
