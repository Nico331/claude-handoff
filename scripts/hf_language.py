"""
The ``language`` command: read or set the language the handoff content is written in.

The language lives in the ``language`` key of ``<root>/handoff.json``. Setting it
rewrites only that key: every other key keeps its value and its position, and a
missing file is created holding just ``{"language": ...}``. The write is atomic
(temporary file in the root, then ``os.replace``), so a reader never sees half a
file. Existing entries are never translated here: that is a content change, done
on request with the lock protocol.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from hf_config import (CONFIG_NAME, DEFAULT_LANGUAGE, HandoffError, language_problem,
                       validate)


def _read_raw(root: Path) -> dict[str, Any] | None:
    """Decoded `<root>/handoff.json`, key order kept, or `None` when absent.

    Raises:
        HandoffError: (2) the file is not valid JSON or its keys are not valid.
    """
    path = root / CONFIG_NAME
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes().decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise HandoffError(f"{path}: not valid JSON ({exc})", 2) from exc
    problems = validate(data)[1]
    if problems:
        raise HandoffError(f"{path}: " + "; ".join(problems), 2)
    checked: dict[str, Any] = data  # validate() succeeded, so this is a dict
    return checked


def current_language(root: Path) -> tuple[str, bool]:
    """Language configured for the handoff at `root`.

    Args:
        root: handoff root.

    Returns:
        `(language, explicit)`; `explicit` is false when the default applies
        because `handoff.json` or its `language` key is absent.

    Raises:
        HandoffError: (2) `handoff.json` exists but is not valid.
    """
    data = _read_raw(root)
    if data is None or "language" not in data:
        return DEFAULT_LANGUAGE, False
    return str(data["language"]), True


def _write_atomic(path: Path, text: str) -> None:
    """Replace `path` with `text` (UTF-8, LF) through a temporary file.

    Raises:
        OSError: the temporary file cannot be written or moved into place; the
            temporary file is removed and `path` is left as it was.
    """
    handle, temp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp",
                                    dir=str(path.parent))
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temp, path)
    except BaseException:
        if os.path.exists(temp):
            os.unlink(temp)
        raise


def set_language(root: Path, code: str) -> tuple[str | None, bool]:
    """Write `code` as the `language` of `<root>/handoff.json`.

    Args:
        root: handoff root; it must exist.
        code: new language tag.

    Returns:
        `(previous, changed)`: `previous` is the language set before, or `None`
        when none was (the default applied); nothing is written, and `changed` is
        false, when the file already says `code`.

    Raises:
        HandoffError: (2) `root` does not exist, `code` is not a valid tag, or the
            existing `handoff.json` is not valid (it is then left untouched).
    """
    if not root.is_dir():
        raise HandoffError(f"{root}: no handoff here (run init first)", 2)
    problem = language_problem(code)
    if problem is not None:
        raise HandoffError(problem, 2)
    data = _read_raw(root)
    if data is None:
        data = {}
    previous = str(data["language"]) if "language" in data else None
    if previous == code:
        return previous, False
    data["language"] = code  # an existing key keeps its position, a new one goes last
    _write_atomic(root / CONFIG_NAME, json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    return previous, True
