"""
Session hooks of the claude-handoff plugin.

Called by ``hooks/hooks.json`` at two moments:

- ``session-start``: injects the read protocol of the handoff and, unless
  ``inject_summaries`` is false, the list of bootstrap entries (rank <=
  ``bootstrap_max_rank``) with their summaries, so the session knows what is
  critical before opening a file;
- ``prompt`` (UserPromptSubmit): reminds, at every prompt, to decide whether the
  handoff must be updated, with the lock protocol.

Behaviour in a project without a handoff: at session start a single hint line
suggests ``/claude-handoff:init``; at each prompt nothing is printed.

The hook must never break a session: it always exits 0, prints ASCII-only JSON
(the stdout of a hook on Windows may not be UTF-8), tolerates malformed files and
keeps to syntax any Python 3.7+ can run, falling back to a minimal message when
something unexpected happens.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SCRIPTS = Path(__file__).resolve().parent
"""Folder of the plugin scripts (this file and the handoff tool)."""

TOOL = SCRIPTS / "handoff.py"
"""The handoff tool, whose path the injected text quotes."""

MAX_LISTED = 60
"""Most bootstrap entries listed in the injected text; the rest are counted."""

INDEX_NAME = "INDEX.md"

if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

TEXTS = {
    "en": {
        "start": (
            "HANDOFF PROTOCOL (claude-handoff plugin): this project keeps its memory in "
            "{root}/. Before any other action read, in order: {root}/INDEX.md, then the "
            "INDEX.md of every area, then ALL entries with rank <= {rank} (listed below "
            "with their summary), then the rank <= 2 entries of the topics today's task "
            "touches; the rest on demand, starting from the summaries in the indexes. "
            "Tool: {tool} <command> (list --max-rank {rank}, check, lock, reindex, "
            "unlock, status)."),
        "listed": "Entries with rank <= {rank}:\n{items}",
        "none": "  (no entry with rank <= {rank} found)",
        "more": "  ... and {count} more: run `{tool} list --max-rank {rank}`",
        "unlisted": "List them with: {tool} list --max-rank {rank}",
        "prompt": (
            "HANDOFF REMINDER: for this prompt and every action that follows, decide "
            "whether it changes a fact recorded in {root}/ or adds one a future session "
            "needs. If so, update it in the same action, never at the end, with the lock "
            "protocol: {tool} lock <folder> --owner <name> -> edit -> reindex -> unlock "
            "-> same on the parent, up to the root; never hold two locks; then check. "
            "One fact in one place; closed items go to rank 5 or are deleted, never "
            "struck through; never secrets."),
        "absent": ("claude-handoff: no handoff at {root}/. To create one, run "
                   "/claude-handoff:init."),
    },
    "it": {
        "start": (
            "PROTOCOLLO DELL'HANDOFF (plugin claude-handoff): la memoria del progetto sta "
            "in {root}/. Prima di qualunque altra azione leggere, in ordine: "
            "{root}/INDEX.md, poi l'INDEX.md di ogni area, poi TUTTE le voci con rank <= "
            "{rank} (elencate sotto con il sommario), poi le voci rank <= 2 degli "
            "argomenti del giorno; il resto su bisogno, partendo dai sommari negli "
            "indici. Strumento: {tool} <comando> (list --max-rank {rank}, check, lock, "
            "reindex, unlock, status)."),
        "listed": "Voci con rank <= {rank}:\n{items}",
        "none": "  (nessuna voce con rank <= {rank} trovata)",
        "more": "  ... e altre {count}: `{tool} list --max-rank {rank}`",
        "unlisted": "Elenco: {tool} list --max-rank {rank}",
        "prompt": (
            "PROMEMORIA DELL'HANDOFF: per questo prompt e per ogni azione che ne segue, "
            "valutare se cambia un fatto scritto in {root}/ o ne aggiunge uno che una "
            "sessione futura deve sapere. Se si', aggiornarlo nella stessa azione, mai in "
            "coda, con il protocollo dei lock: {tool} lock <cartella> --owner <nome> -> "
            "modifica -> reindex -> unlock -> genitore, fino alla radice; mai due lock "
            "insieme; poi check. Un fatto in un solo posto; voci chiuse a rank 5 o "
            "cancellate, mai barrate; mai segreti nell'handoff."),
        "absent": ("claude-handoff: nessun handoff in {root}/. Per crearne uno: "
                   "/claude-handoff:init."),
    },
}
"""Injected texts per language; `{root}`, `{tool}`, `{rank}` are filled in."""


def read_frontmatter(path: Path) -> Dict[str, str]:
    """Leniently read the frontmatter keys of a Markdown file.

    Returns:
        Keys and one-line values; empty when the file has no frontmatter or cannot be
        read. A reminder must never fail because of a malformed file.
    """
    try:
        lines = path.read_bytes().decode("utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return {}
    if not lines or lines[0].strip() != "---":
        return {}
    values: Dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return values
        key, sep, value = line.partition(":")
        if sep:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            values[key.strip()] = value
    return {}  # never closed: not frontmatter


def bootstrap_entries(root: Path, max_rank: int) -> List[Tuple[str, str]]:
    """Level-3 entries with rank <= `max_rank`, sorted by rank then path.

    Returns:
        `(path relative to the root, summary)` pairs; malformed entries are skipped.
    """
    found: List[Tuple[int, str, str]] = []
    for path in root.glob("*/*/*.md"):
        if path.name == INDEX_NAME:
            continue
        fields = read_frontmatter(path)
        rank_text = fields.get("rank", "")
        if not rank_text.isdigit():
            continue
        rank = int(rank_text)
        if 1 <= rank <= max_rank:
            found.append((rank, path.relative_to(root).as_posix(), fields.get("summary", "")))
    found.sort()
    return [(rel, summary) for _, rel, summary in found]


def project_dir(environ: Dict[str, str], stdin_text: Optional[str]) -> Path:
    """Project directory: `CLAUDE_PROJECT_DIR`, else the `cwd` of the hook input."""
    if environ.get("CLAUDE_PROJECT_DIR"):
        return Path(environ["CLAUDE_PROJECT_DIR"])
    if stdin_text:
        try:
            data = json.loads(stdin_text)
            if isinstance(data, dict) and isinstance(data.get("cwd"), str):
                return Path(data["cwd"])
        except ValueError:
            pass  # not JSON: fall back to the working directory
    return Path.cwd()


def _display(root: Path, project: Path) -> str:
    """Root as shown in the text: relative to the project when inside it."""
    try:
        return root.relative_to(project.resolve()).as_posix()
    except ValueError:
        return root.as_posix()


def build_message(event: str, project: Path, environ: Dict[str, str]) -> Optional[str]:
    """Text to inject for `event`, or `None` to stay silent.

    Raises:
        Any exception from the configuration module; `main` turns it into a fallback.
    """
    import hf_config  # deferred: a broken module must not break the fallback path

    root = hf_config.resolve_root(None, project, environ)
    shown = _display(root, project)
    if not root.is_dir():
        return TEXTS["en"]["absent"].format(root=shown) if event == "session-start" else None
    problems = hf_config.config_problems(root)
    cfg = hf_config.DEFAULT_CONFIG if problems else hf_config.load_config(root)
    texts = TEXTS.get(cfg.language, TEXTS["en"])
    python = Path(sys.executable).as_posix() if sys.executable else "python3"
    tool = f'"{python}" "{TOOL.as_posix()}" --root "{shown}"'
    rank = cfg.bootstrap_max_rank
    if event != "session-start":
        return texts["prompt"].format(root=shown, tool=tool)
    parts = [texts["start"].format(root=shown, tool=tool, rank=rank)]
    if problems:
        parts.append(f"(handoff.json ignored: {'; '.join(problems)})")
    if cfg.inject_summaries:
        entries = bootstrap_entries(root, rank)
        items = [f"  - {shown}/{rel}: {summary}" for rel, summary in entries[:MAX_LISTED]]
        if len(entries) > MAX_LISTED:
            items.append(texts["more"].format(count=len(entries) - MAX_LISTED, tool=tool,
                                              rank=rank))
        parts.append(texts["listed"].format(
            rank=rank, items="\n".join(items) or texts["none"].format(rank=rank)))
    else:
        parts.append(texts["unlisted"].format(tool=tool, rank=rank))
    return "\n".join(parts)


def payload(event: str, text: str) -> str:
    """ASCII JSON understood by Claude Code as additional context."""
    name = "SessionStart" if event == "session-start" else "UserPromptSubmit"
    return json.dumps({"hookSpecificOutput": {"hookEventName": name,
                                              "additionalContext": text},
                       "suppressOutput": True}, ensure_ascii=True)


def _stdin_text() -> Optional[str]:
    """Hook input from stdin, or `None` on a terminal or when unreadable."""
    try:
        if sys.stdin is None or sys.stdin.isatty():
            return None
        return sys.stdin.read()
    except (OSError, ValueError):
        return None


def main(argv: List[str], environ: Optional[Dict[str, str]] = None,
         stdin_text: Optional[str] = None) -> int:
    """Entry point.

    Args:
        argv: command line; `argv[1]` is the event (`session-start` or `prompt`).
        environ: environment (default: `os.environ`).
        stdin_text: hook input (default: read stdin only if `CLAUDE_PROJECT_DIR`
            is not set).

    Returns:
        Always 0: a reminder must never block a session.
    """
    event = argv[1] if len(argv) > 1 else "prompt"
    env = dict(os.environ) if environ is None else environ
    try:
        if stdin_text is None and not env.get("CLAUDE_PROJECT_DIR"):
            stdin_text = _stdin_text()
        text = build_message(event, project_dir(env, stdin_text), env)
    except Exception as exc:  # noqa: BLE001 - last line of defence, reported in the text
        text = (f"claude-handoff: the hook could not read the handoff "
                f"({type(exc).__name__}); run {TOOL.as_posix()} check.")
    if text:
        sys.stdout.write(payload(event, text))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
