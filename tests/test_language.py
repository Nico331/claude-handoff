"""Tests of the content language: validation, the `language` command, `init --language`."""

from __future__ import annotations

import json
from pathlib import Path

import handoff
import hf_init
import hf_language
import pytest
from helpers import run_cli, write
from hf_config import (HOOK_LANGUAGES, HandoffError, language_problem, load_config,
                       validate)
from hf_tree import read_text

# ---------------------------------------------------------------- validation


@pytest.mark.parametrize("code", ["en", "it", "de", "pt-br", "zh-hant", "gsw", "es-419"])
def test_config_accepts_language_tags(code: str) -> None:
    cfg, problems = validate({"language": code})
    assert problems == [] and cfg is not None and cfg.language == code


@pytest.mark.parametrize("code", ["EN", "english", "", "e", "pt_br", "pt-", "en\n", " en",
                                  5, None, True, ["en"]])
def test_config_rejects_bad_language(code: object) -> None:
    cfg, problems = validate({"language": code})
    assert cfg is None and len(problems) == 1
    assert "language must be a lowercase language tag" in problems[0]
    assert language_problem(code) == problems[0]


def test_hook_languages_match_the_translations() -> None:
    import hook
    assert set(HOOK_LANGUAGES) == set(hook.TEXTS)


# ---------------------------------------------------------------- language command


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """An existing, empty handoff root."""
    folder = tmp_path.resolve() / "h"
    folder.mkdir()
    return folder


def test_show_default(root: Path) -> None:
    result = run_cli(root, "language")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "language: en (default)"
    assert not (root / "handoff.json").exists()


def test_show_configured(root: Path) -> None:
    write(root / "handoff.json", '{"max_topics": 7, "language": "pt-br"}')
    result = run_cli(root, "language")
    assert result.returncode == 0 and result.stdout.strip() == "language: pt-br"


def test_show_default_when_key_absent(root: Path) -> None:
    write(root / "handoff.json", '{"max_topics": 7}')
    assert run_cli(root, "language").stdout.strip() == "language: en (default)"


def test_set_preserves_other_keys_and_order(root: Path) -> None:
    write(root / "handoff.json", json.dumps(
        {"max_topics": 7, "language": "en", "inject_summaries": False,
         "default_areas": ["a", "b"]}))
    result = run_cli(root, "language", "it")
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "language: it (was en)"
    text = read_text(root / "handoff.json")
    assert list(json.loads(text).items()) == [
        ("max_topics", 7), ("language", "it"), ("inject_summaries", False),
        ("default_areas", ["a", "b"])]
    assert text.endswith("}\n") and '\n  "max_topics": 7,' in text
    assert load_config(root).language == "it"
    assert [p.name for p in root.iterdir()] == ["handoff.json"]  # no temp file left


def test_set_appends_a_missing_key(root: Path) -> None:
    write(root / "handoff.json", '{"max_topics": 7}')
    result = run_cli(root, "language", "de")
    assert result.stdout.strip() == "language: de (was en, the default)"
    assert list(json.loads(read_text(root / "handoff.json"))) == ["max_topics", "language"]


def test_set_creates_the_file_when_absent(root: Path) -> None:
    result = run_cli(root, "language", "de")
    assert result.returncode == 0, result.stderr
    assert (root / "handoff.json").read_bytes() == b'{\n  "language": "de"\n}\n'


def test_set_same_language_writes_nothing(root: Path) -> None:
    write(root / "handoff.json", '{"language":"it"}')
    result = run_cli(root, "language", "it")
    assert result.returncode == 0 and result.stdout.strip() == "language: it (unchanged)"
    assert read_text(root / "handoff.json") == '{"language":"it"}'


@pytest.mark.parametrize("code", ["EN", "english", "pt_BR"])
def test_set_invalid_code_is_a_usage_error(root: Path, code: str) -> None:
    write(root / "handoff.json", '{"language": "en"}')
    result = run_cli(root, "language", code)
    assert result.returncode == 2 and "lowercase language tag" in result.stderr
    assert read_text(root / "handoff.json") == '{"language": "en"}'


def test_set_invalid_code_without_file_creates_nothing(root: Path) -> None:
    assert run_cli(root, "language", "EN").returncode == 2
    assert not (root / "handoff.json").exists()


@pytest.mark.parametrize("broken", ["{broken", '{"nope": 1}', '{"language": "EN"}', "[]"])
def test_set_with_broken_config_leaves_it_alone(root: Path, broken: str) -> None:
    write(root / "handoff.json", broken)
    result = run_cli(root, "language", "it")
    assert result.returncode == 2 and "handoff.json" in result.stderr
    assert read_text(root / "handoff.json") == broken
    assert [p.name for p in root.iterdir()] == ["handoff.json"]


def test_show_with_broken_config_is_a_usage_error(root: Path) -> None:
    write(root / "handoff.json", "{broken")
    assert run_cli(root, "language").returncode == 2


def test_no_root_is_a_usage_error(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    for args in (("language",), ("language", "it")):
        result = run_cli(missing, *args)
        assert result.returncode == 2 and "run init first" in result.stderr
    assert not missing.exists()


def test_set_language_function_checks_the_root(tmp_path: Path) -> None:
    with pytest.raises(HandoffError) as exc:
        hf_language.set_language(tmp_path / "missing", "it")
    assert exc.value.code == 2


def test_failed_write_leaves_file_and_no_temp(root: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    write(root / "handoff.json", '{"language": "en"}')

    def refuse(*_: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(hf_language.os, "replace", refuse)
    with pytest.raises(OSError):
        hf_language.set_language(root, "it")
    assert read_text(root / "handoff.json") == '{"language": "en"}'
    assert [p.name for p in root.iterdir()] == ["handoff.json"]


# ---------------------------------------------------------------- init --language


def test_init_language_writes_new_config(tmp_path: Path,
                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path.resolve() / "h"
    assert handoff.main(["--root", str(root), "init", "--language", "it"]) == 0
    assert load_config(root).language == "it"
    capsys.readouterr()
    assert handoff.main(["--root", str(root), "check"]) == 0


def test_init_language_ignored_when_config_exists(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = tmp_path.resolve() / "h"
    write(root / "handoff.json", '{"language": "de"}')
    assert handoff.main(["--root", str(root), "init", "--language", "it"]) == 0
    assert "--language ignored" in capsys.readouterr().out
    assert read_text(root / "handoff.json") == '{"language": "de"}'


def test_init_invalid_language_creates_nothing(tmp_path: Path) -> None:
    root = tmp_path / "h"
    result = run_cli(root, "init", "--language", "Italian")
    assert result.returncode == 2 and "lowercase language tag" in result.stderr
    assert not root.exists()


def test_init_without_language_writes_english(tmp_path: Path) -> None:
    root = tmp_path.resolve() / "h"
    hf_init.init(root, None, "init")
    assert load_config(root).language == "en"
