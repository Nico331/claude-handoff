"""
Configuration of a handoff: where its root is and which limits apply.

The configuration lives in ``<root>/handoff.json`` and is optional: every key has a
default. Because the file lives *inside* the root, the root itself is found first,
in this order:

1. the ``--root`` command-line option;
2. the ``CLAUDE_HANDOFF_ROOT`` environment variable;
3. the ``root`` key of the optional project pointer ``<project>/.claude/handoff.json``;
4. the default ``<project>/.claude/handoff``.

This module is imported by the session hook too, so it keeps to syntax that
parses on any Python 3.7+ (the tool itself requires 3.10) and never needs a
third-party package.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

CONFIG_NAME = "handoff.json"
"""Name of the configuration file at the handoff root."""

POINTER_PATH = Path(".claude") / "handoff.json"
"""Optional project-level pointer, relative to the project directory."""

DEFAULT_ROOT = Path(".claude") / "handoff"
"""Default root, relative to the project directory."""

ENV_ROOT = "CLAUDE_HANDOFF_ROOT"
"""Environment variable that overrides the root."""

DEFAULT_AREAS = ("rules", "state", "decisions", "procedures", "open", "history")
"""Area names `init` creates when neither `--areas` nor the config says otherwise."""

DEFAULT_LANGUAGE = "en"
"""Language of the handoff content when `handoff.json` does not set one."""

HOOK_LANGUAGES = ("en", "it")
"""Languages the hook text is translated into; any other language gets English."""

LANGUAGE_TAG = re.compile(r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*")
"""Accepted language tags (full match): lowercase BCP 47-like, e.g. `en`, `pt-br`."""


class HandoffError(Exception):
    """Usage or content error that stops a command.

    Attributes:
        code: exit code for the process (1 content, 2 usage, 3 busy, 4 not owner).
    """

    def __init__(self, message: str, code: int = 1) -> None:
        """Create the error.

        Args:
            message: text for the user, including the path concerned.
            code: exit code to return.
        """
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Config:
    """Validated configuration of one handoff.

    Attributes:
        max_topics: most topic folders in one area (level 2).
        max_entry_files: most `.md` files in one topic, its INDEX.md included.
        max_entry_lines: most lines in one entry.
        max_summary: most characters in a `summary`.
        bootstrap_max_rank: entries with rank <= this are read at every session start.
        lock_ttl_seconds: default lifetime of a lock.
        language: language the handoff content is written in (entries, summaries,
            hand-written index text); also selects the hook text when a translation
            exists (see `HOOK_LANGUAGES`).
        inject_summaries: whether the SessionStart hook lists the bootstrap entries.
        default_areas: areas `init` creates by default.
        legacy: old flat handoff for `stats` to compare with, relative to the root.
    """

    max_topics: int = 20
    max_entry_files: int = 51
    max_entry_lines: int = 80
    max_summary: int = 160
    bootstrap_max_rank: int = 1
    lock_ttl_seconds: int = 900
    language: str = DEFAULT_LANGUAGE
    inject_summaries: bool = True
    default_areas: Tuple[str, ...] = field(default=DEFAULT_AREAS)
    legacy: Optional[str] = None


DEFAULT_CONFIG = Config()
"""Configuration used when `handoff.json` is absent."""

_POSITIVE_INTS = ("max_topics", "max_entry_files", "max_entry_lines", "max_summary",
                  "lock_ttl_seconds")


def _is_int(value: Any) -> bool:
    """True for a JSON integer (booleans excluded)."""
    return isinstance(value, int) and not isinstance(value, bool)


def language_problem(value: Any) -> Optional[str]:
    """Why `value` is not an acceptable language tag.

    Args:
        value: candidate tag, as read from JSON or the command line.

    Returns:
        `None` when it is a lowercase tag such as `en`, `it`, `de` or `pt-br`,
        otherwise a problem message for the user.
    """
    if isinstance(value, str) and LANGUAGE_TAG.fullmatch(value):
        return None
    return (f"language must be a lowercase language tag such as en, it, de or pt-br "
            f"(got {value!r})")


def validate(data: Any) -> Tuple[Optional[Config], List[str]]:
    """Turn the decoded JSON of `handoff.json` into a `Config`.

    Args:
        data: the decoded JSON value.

    Returns:
        `(Config, [])` when valid, otherwise `(None, problems)`.
    """
    if not isinstance(data, dict):
        return None, ["must be a JSON object"]
    known = {f.name for f in fields(Config)}
    problems = [f"unknown key {key!r}" for key in sorted(data) if key not in known]
    values: Dict[str, Any] = {}
    for key in _POSITIVE_INTS:
        if key in data:
            if _is_int(data[key]) and data[key] > 0:
                values[key] = data[key]
            else:
                problems.append(f"{key} must be a positive integer")
    if "bootstrap_max_rank" in data:
        value = data["bootstrap_max_rank"]
        if _is_int(value) and 1 <= value <= 5:
            values["bootstrap_max_rank"] = value
        else:
            problems.append("bootstrap_max_rank must be an integer from 1 to 5")
    if "language" in data:
        problem = language_problem(data["language"])
        if problem is None:
            values["language"] = data["language"]
        else:
            problems.append(problem)
    if "inject_summaries" in data:
        if isinstance(data["inject_summaries"], bool):
            values["inject_summaries"] = data["inject_summaries"]
        else:
            problems.append("inject_summaries must be true or false")
    if "default_areas" in data:
        areas = data["default_areas"]
        if (isinstance(areas, list) and areas
                and all(isinstance(a, str) and a for a in areas)):
            values["default_areas"] = tuple(areas)
        else:
            problems.append("default_areas must be a non-empty list of names")
    if "legacy" in data:
        if data["legacy"] is None or (isinstance(data["legacy"], str) and data["legacy"]):
            values["legacy"] = data["legacy"]
        else:
            problems.append("legacy must be a path string or null")
    if problems:
        return None, problems
    return replace(DEFAULT_CONFIG, **values), []


def config_problems(root: Path) -> List[str]:
    """Problems of `<root>/handoff.json`, empty if it is absent or valid."""
    path = root / CONFIG_NAME
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        return [f"not valid JSON ({exc})"]
    return validate(data)[1]


def load_config(root: Path) -> Config:
    """Read `<root>/handoff.json`.

    Args:
        root: handoff root.

    Returns:
        The configuration, or the defaults when the file is absent.

    Raises:
        HandoffError: (code 2) when the file exists but is not valid.
    """
    problems = config_problems(root)
    if problems:
        raise HandoffError(f"{root / CONFIG_NAME}: " + "; ".join(problems), 2)
    path = root / CONFIG_NAME
    if not path.is_file():
        return DEFAULT_CONFIG
    config, _ = validate(json.loads(path.read_bytes().decode("utf-8")))
    return config if config is not None else DEFAULT_CONFIG


def config_json(config: Config) -> str:
    """Pretty JSON of a configuration, as `init` writes it."""
    data = {f.name: getattr(config, f.name) for f in fields(Config)}
    data["default_areas"] = list(config.default_areas)
    return json.dumps(data, indent=2) + "\n"


def resolve_root(cli_root: Optional[Path], project: Optional[Path] = None,
                 environ: Optional[Dict[str, str]] = None) -> Path:
    """Find the handoff root (see the module docstring for the order).

    Args:
        cli_root: value of `--root`, if given.
        project: project directory (default: the current directory).
        environ: environment (default: `os.environ`).

    Returns:
        The root as an absolute path; it may not exist yet.
    """
    project = (project or Path.cwd()).resolve()
    env = os.environ if environ is None else environ
    if cli_root is not None:
        return (project / cli_root).resolve()
    if env.get(ENV_ROOT):
        return (project / env[ENV_ROOT]).resolve()
    pointer = project / POINTER_PATH
    if pointer.is_file():
        try:
            data = json.loads(pointer.read_bytes().decode("utf-8"))
            if isinstance(data, dict) and isinstance(data.get("root"), str) and data["root"]:
                return (project / data["root"]).resolve()
        except (OSError, UnicodeDecodeError, ValueError):
            pass  # a broken pointer falls back to the default; `check` shows the root used
    return (project / DEFAULT_ROOT).resolve()
