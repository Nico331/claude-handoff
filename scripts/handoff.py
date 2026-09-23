"""
Command-line tool of the three-level handoff (claude-handoff plugin).

Usage, from the project directory:

    python3 <plugin>/scripts/handoff.py [--root DIR] <command> [options]

Commands: init, lock, unlock, status, reindex, check, list, stats. Python 3.10+,
standard library only.

Exit codes: 0 success; 1 content errors or failed `check`; 2 usage error;
3 folder already locked (or expired lock not broken); 4 lock owned by someone else.
"""

from __future__ import annotations

import sys

if sys.version_info < (3, 10):  # pragma: no cover - exercised only on old interpreters
    sys.stderr.write("handoff.py needs Python 3.10 or newer\n")
    sys.exit(2)

import argparse  # noqa: E402
from collections.abc import Callable  # noqa: E402
from pathlib import Path  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hf_check  # noqa: E402
import hf_index  # noqa: E402
import hf_init  # noqa: E402
import hf_lock  # noqa: E402
import hf_report  # noqa: E402
from hf_config import Config, load_config, resolve_root  # noqa: E402
from hf_tree import INDEX_NAME, HandoffError, resolve_folder  # noqa: E402

EXIT_CODES = "exit: 0 ok, 1 content errors/check failed, 2 usage error, " \
             "3 folder locked, 4 lock owned by someone else"


def _out(line: str) -> None:
    """Print one line on stdout."""
    print(line)


def _err(line: str) -> None:
    """Print one line on stderr."""
    print(line, file=sys.stderr)


def _context(args: argparse.Namespace) -> tuple[Path, Config]:
    """Resolved root and configuration of the handoff the command works on."""
    root = resolve_root(args.root)
    return root, load_config(root)


def _existing_root(args: argparse.Namespace) -> tuple[Path, Config]:
    """Like `_context`, but the root must exist (code 2 otherwise)."""
    root, cfg = _context(args)
    if not root.is_dir():
        raise HandoffError(f"{root}: no handoff here (run init first)", 2)
    return root, cfg


def cmd_init(args: argparse.Namespace) -> int:
    """`init`: scaffold root, areas and handoff.json without overwriting anything."""
    root, cfg = _context(args)
    areas = [a.strip() for a in args.areas.split(",") if a.strip()] if args.areas else None
    result = hf_init.init(root, areas, args.owner, cfg, seed=not args.no_seed)
    for path in result.created:
        _out(f"created: {path}")
    for folder in result.reindexed:
        _out(f"reindexed: {folder / INDEX_NAME}")
    if not result.created:
        _out(f"{root}: already initialised, nothing created")
    return 0


def cmd_lock(args: argparse.Namespace) -> int:
    """`lock`: take the lock of a folder."""
    root, cfg = _existing_root(args)
    folder = resolve_folder(root, args.folder)
    info = hf_lock.acquire(root, folder, args.owner, args.ttl or cfg.lock_ttl_seconds,
                           args.steal_stale)
    _out(f"locked: {folder} ({info.owner}, ttl {info.ttl_seconds}s)")
    return 0


def cmd_unlock(args: argparse.Namespace) -> int:
    """`unlock`: release a lock; only its owner may, unless `--force`."""
    root, _ = _existing_root(args)
    folder = resolve_folder(root, args.folder)
    info = hf_lock.release(root, folder, args.owner, args.force)
    forced = " (forced, logged)" if info.owner != args.owner else ""
    _out(f"unlocked: {folder} (was {info.owner}){forced}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    """`status`: list the locks present, with age, marking expired ones."""
    root, _ = _existing_root(args)
    now = hf_lock.utcnow()
    locks = hf_lock.find_locks(root)
    if not locks:
        _out("no locks")
    for folder, info in locks:
        name = folder.relative_to(root).as_posix() or "."
        _out(f"{name}: {hf_lock.describe(info, now)}")
    return 0


def cmd_reindex(args: argparse.Namespace) -> int:
    """`reindex`: regenerate one index (lock held) or all of them (`--all`)."""
    root, cfg = _existing_root(args)
    if args.all:
        if args.folder or args.no_lock:
            raise HandoffError("reindex --all takes no folder and no --no-lock", 2)
        if not args.owner:
            raise HandoffError("reindex --all requires --owner", 2)
        changed = hf_index.reindex_all(
            root, args.owner, cfg,
            report=lambda folder, did: _out(
                f"{folder / INDEX_NAME}: {'updated' if did else 'already aligned'}"))
        _out(f"{len(changed)} indexes updated")
        return 0
    if not args.folder:
        raise HandoffError("reindex needs a <folder> or --all", 2)
    if not args.no_lock and not args.owner:
        raise HandoffError("reindex requires --owner (the lock holder) or --no-lock", 2)
    folder = resolve_folder(root, args.folder)
    changed_one = hf_index.reindex(root, folder, None if args.no_lock else args.owner,
                                   cfg=cfg)
    _out(f"{folder / INDEX_NAME}: {'updated' if changed_one else 'already aligned'}")
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    """`check`: validate the structure; 1 on any error unless `--warn-only`."""
    root, _ = _context(args)
    try:
        cfg = load_config(root)
    except HandoffError:
        cfg = Config()  # check reports the config problems itself
    errors = hf_check.check(root, cfg=cfg)
    for line in errors:
        _out(line)
    _out(f"{len(errors)} errors" if errors else "structure valid")
    return 1 if errors and not args.warn_only else 0


def cmd_list(args: argparse.Namespace) -> int:
    """`list`: entries with rank and summary, sorted by rank."""
    root, cfg = _existing_root(args)
    entries, warnings = hf_report.list_entries(root, args.max_rank, args.area, cfg)
    for warning in warnings:
        _err(warning)
    for entry in entries:
        _out(f"{entry.rank}  {entry.path}  {entry.summary}")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    """`stats`: bytes and lines per level, bootstrap cost, optional comparison."""
    root, cfg = _existing_root(args)
    legacy = args.legacy
    if legacy is None and cfg.legacy:
        legacy = root / cfg.legacy
    stats = hf_report.compute_stats(root, legacy, cfg)
    for line in hf_report.format_stats(stats, legacy, cfg):
        _out(line)
    return 0


def _positive(value: str) -> int:
    """argparse type: integer > 0."""
    try:
        number = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be a positive integer") from exc
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    """Command-line parser with its eight sub-commands."""
    parser = argparse.ArgumentParser(
        prog="handoff.py",
        description="Three-level project handoff: init, locks, indexes, checks, stats.",
        epilog=EXIT_CODES)
    parser.add_argument("--root", type=Path, default=None,
                        help="handoff root (default: $CLAUDE_HANDOFF_ROOT, then the "
                             "'root' of .claude/handoff.json, then .claude/handoff)")
    sub = parser.add_subparsers(dest="command", required=True, metavar="<command>")

    def add(name: str, handler: Callable[[argparse.Namespace], int],
            text: str) -> argparse.ArgumentParser:
        command = sub.add_parser(name, help=text, description=text, epilog=EXIT_CODES)
        command.set_defaults(handler=handler)
        return command

    init = add("init", cmd_init, "create the root, the areas and handoff.json "
                                 "(idempotent, never overwrites)")
    init.add_argument("--areas", help="comma-separated area names "
                                      "(default: rules,state,decisions,procedures,open,"
                                      "history or default_areas of handoff.json)")
    init.add_argument("--owner", default="init", help="lock owner for the final reindex")
    init.add_argument("--no-seed", action="store_true",
                      help="do not create the seed topic explaining the handoff")

    lock = add("lock", cmd_lock, "take the lock of a folder (relative to the root)")
    lock.add_argument("folder", metavar="<folder>")
    lock.add_argument("--owner", required=True, help="name of the lock holder")
    lock.add_argument("--ttl", type=_positive, default=None,
                      help="lifetime in seconds (default: lock_ttl_seconds, 900)")
    lock.add_argument("--steal-stale", action="store_true",
                      help="break an expired lock, logging it in .lock-log")

    unlock = add("unlock", cmd_unlock, "release the lock of a folder")
    unlock.add_argument("folder", metavar="<folder>")
    unlock.add_argument("--owner", required=True, help="who releases")
    unlock.add_argument("--force", action="store_true",
                        help="release someone else's lock (coordinator); logged")

    add("status", cmd_status, "list the locks present, with age, expired ones marked")

    reindex = add("reindex", cmd_reindex,
                  "regenerate the INDEX.md table and the folder rank")
    reindex.add_argument("folder", metavar="<folder>", nargs="?")
    reindex.add_argument("--owner", help="holder of the folder lock")
    reindex.add_argument("--all", action="store_true",
                         help="every folder bottom-up, taking one lock at a time")
    reindex.add_argument("--no-lock", action="store_true",
                         help="skip the lock check (tests and repairs only)")

    check = add("check", cmd_check, "validate structure, frontmatter, indexes, links, "
                                    "locks and handoff.json")
    check.add_argument("--warn-only", action="store_true",
                       help="print the errors but exit 0")

    listing = add("list", cmd_list, "list entries by rank (bootstrap: --max-rank 1)")
    listing.add_argument("--max-rank", type=_positive, help="only rank <= N")
    listing.add_argument("--area", help="only the entries of one area")

    stats = add("stats", cmd_stats, "bytes and lines per level and bootstrap cost")
    stats.add_argument("--legacy", type=Path,
                       help="old flat handoff folder to compare with "
                            "(default: 'legacy' of handoff.json, if set)")
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: arguments without the program name (default: `sys.argv[1:]`).

    Returns:
        The exit code (see the module docstring).
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8")
    args = build_parser().parse_args(argv)
    try:
        code: int = args.handler(args)
    except HandoffError as exc:
        _err(str(exc))
        return exc.code
    return code


if __name__ == "__main__":
    sys.exit(main())
