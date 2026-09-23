"""
Folder locks of the handoff.

A lock is the file ``<folder>/.lock``, created with ``O_CREAT|O_EXCL`` (atomic on
POSIX file systems and on NTFS), holding owner, pid, host, acquisition time and
lifetime. An expired lock does not vanish by itself: it is broken only with
``--steal-stale``, and the act is appended to ``<root>/.lock-log``, like every
forced release.
"""

from __future__ import annotations

import json
import os
import socket
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from hf_config import DEFAULT_CONFIG
from hf_tree import LOCK_LOG_NAME, LOCK_NAME, HandoffError

DEFAULT_TTL = DEFAULT_CONFIG.lock_ttl_seconds
"""Default lock lifetime in seconds (15 minutes), overridable in `handoff.json`."""

BUSY = 3
"""Exit code: the folder is already locked (by someone else or by an expired lock)."""

NOT_OWNER = 4
"""Exit code: the caller is not the owner of the lock."""


@dataclass(frozen=True)
class LockInfo:
    """Content of a `.lock` file.

    Attributes:
        owner: name declared by the holder (e.g. `agent-1`).
        pid: pid of the process that took it.
        host: machine name.
        acquired_at: ISO 8601 instant in UTC.
        ttl_seconds: lifetime in seconds (> 0).
    """

    owner: str
    pid: int
    host: str
    acquired_at: str
    ttl_seconds: int

    def acquired(self) -> datetime:
        """Acquisition instant as an aware `datetime`."""
        return datetime.fromisoformat(self.acquired_at)

    def age(self, now: datetime) -> timedelta:
        """Age of the lock at `now`."""
        return now - self.acquired()

    def expired(self, now: datetime) -> bool:
        """True when the age exceeds the declared lifetime."""
        return self.age(now) > timedelta(seconds=self.ttl_seconds)


def utcnow() -> datetime:
    """Current instant in UTC, to the second."""
    return datetime.now(timezone.utc).replace(microsecond=0)


def read_lock(path: Path) -> LockInfo | None:
    """Read a lock file.

    Returns:
        `None` when the file does not exist. An unreadable lock (broken JSON, missing
        fields) becomes a lock owned by `?`, dated at the file's modification time with
        the default lifetime, so that it can only be broken with `--steal-stale`.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = json.loads(raw)
        info = LockInfo(str(data["owner"]), int(data["pid"]), str(data["host"]),
                        str(data["acquired_at"]), int(data["ttl_seconds"]))
        if info.acquired().tzinfo is None:
            raise ValueError("naive timestamp")
        return info
    except (ValueError, KeyError, TypeError):
        mtime = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
        return LockInfo("?", 0, "?", mtime.replace(microsecond=0).isoformat(), DEFAULT_TTL)


def describe(info: LockInfo, now: datetime) -> str:
    """One-line description: who holds the lock, since when, whether it expired."""
    seconds = max(0, int(info.age(now).total_seconds()))
    state = " EXPIRED" if info.expired(now) else ""
    return (f"owner={info.owner} pid={info.pid} host={info.host} "
            f"since {info.acquired_at} (age {seconds // 60}m{seconds % 60:02d}s, "
            f"ttl {info.ttl_seconds}s){state}")


def append_log(root: Path, action: str, folder: Path, actor: str,
               previous: LockInfo | None, now: datetime) -> None:
    """Append one JSON line to `<root>/.lock-log`.

    Args:
        root: handoff root.
        action: `steal-stale` or `force-unlock`.
        folder: folder concerned.
        actor: who performed the act.
        previous: the lock that was broken or released.
        now: instant of the act.
    """
    record = {
        "at": now.isoformat(), "action": action, "actor": actor,
        "folder": folder.relative_to(root).as_posix() or ".",
        "previous": asdict(previous) if previous else None,
    }
    with (root / LOCK_LOG_NAME).open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")


def acquire(root: Path, folder: Path, owner: str, ttl: int = DEFAULT_TTL,
            steal_stale: bool = False, now: datetime | None = None) -> LockInfo:
    """Take the lock of a folder.

    Args:
        root: handoff root.
        folder: folder to lock (already resolved).
        owner: caller name, not empty.
        ttl: lifetime in seconds, > 0.
        steal_stale: break an expired lock (and log it) instead of refusing.
        now: current instant (for tests).

    Returns:
        The lock written.

    Raises:
        HandoffError: code 2 for invalid arguments; code 3 when the folder is locked
            (valid lock, expired lock without `steal_stale`, or a lost race).
    """
    if not owner.strip():
        raise HandoffError("--owner must not be empty", 2)
    if ttl <= 0:
        raise HandoffError("--ttl must be positive", 2)
    now = now or utcnow()
    path = folder / LOCK_NAME
    info = LockInfo(owner, os.getpid(), socket.gethostname(), now.isoformat(), ttl)
    try:
        return _contend(root, folder, path, info, steal_stale, now)
    except PermissionError as exc:  # Windows: file open or being deleted by another process
        raise HandoffError(f"{folder}: lock contended by another process, retry",
                           BUSY) from exc


def _contend(root: Path, folder: Path, path: Path, info: LockInfo, steal_stale: bool,
             now: datetime) -> LockInfo:
    """The acquisition attempt proper; see `acquire` for contract and errors."""
    if _create_exclusive(path, info):
        return info
    current = read_lock(path)
    if current is None:  # released between the two steps: one more try, no more
        if _create_exclusive(path, info):
            return info
        raise HandoffError(f"{folder}: lock just taken by another process", BUSY)
    if not current.expired(now):
        raise HandoffError(f"{folder}: locked by {describe(current, now)}", BUSY)
    if not steal_stale:
        raise HandoffError(f"{folder}: expired lock of {describe(current, now)}; "
                           "use --steal-stale to break it", BUSY)
    if not _remove_if_unchanged(path, current):
        raise HandoffError(f"{folder}: the expired lock changed, retry", BUSY)
    append_log(root, "steal-stale", folder, info.owner, current, now)
    if _create_exclusive(path, info):
        return info
    raise HandoffError(f"{folder}: lock taken by another process after the steal", BUSY)


def _create_exclusive(path: Path, info: LockInfo) -> bool:
    """Create the lock file only if it does not exist; True on success."""
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(asdict(info), handle, ensure_ascii=False)
    return True


def _remove_if_unchanged(path: Path, expected: LockInfo) -> bool:
    """Remove the lock only if it is still the expected one.

    The lock is renamed to a unique name and its content compared; if the moved file
    is not the expected one (someone broke and re-took it meanwhile) it is linked
    back. On Windows a rename works on handles and two thieves can both "succeed":
    the deletion decides, and it succeeds for one only. The new lock is still
    assigned by `O_EXCL`, not by this function.

    Returns:
        True when the expected lock was removed.
    """
    aside = path.with_name(f"{LOCK_NAME}.stale-{uuid.uuid4().hex}")
    try:
        os.rename(path, aside)
    except (FileNotFoundError, PermissionError):  # another thief was first
        return False
    moved = read_lock(aside)
    if moved is None:  # Windows: a concurrent thief already moved it elsewhere
        return False
    if moved == expected:
        try:  # deletion is the referee: it succeeds for one thief only
            aside.unlink()
        except (FileNotFoundError, PermissionError):
            return False
        return True
    try:  # link, not rename: on POSIX a rename would overwrite a fresh lock
        os.link(aside, path)
    except OSError:
        pass  # a new lock is already there, or the file moved again
    try:
        aside.unlink()
    except FileNotFoundError:
        pass  # removed by a concurrent thief; nothing left to clean
    return False


def release(root: Path, folder: Path, owner: str, force: bool = False,
            now: datetime | None = None) -> LockInfo:
    """Release the lock of a folder.

    Args:
        root: handoff root.
        folder: locked folder.
        owner: who releases.
        force: release even when the owner differs (coordinator); logged.
        now: current instant (for tests).

    Returns:
        The released lock.

    Raises:
        HandoffError: code 1 when there is no lock; code 4 when the owner differs
            and `force` is not set.
    """
    now = now or utcnow()
    path = folder / LOCK_NAME
    current = read_lock(path)
    if current is None:
        raise HandoffError(f"{folder}: no lock to release")
    if current.owner != owner:
        if not force:
            raise HandoffError(
                f"{folder}: the lock belongs to {current.owner!r}, not {owner!r}",
                NOT_OWNER)
        append_log(root, "force-unlock", folder, owner, current, now)
    try:
        path.unlink()
    except FileNotFoundError:
        pass  # released concurrently: the goal is reached anyway
    return current


def require_held(folder: Path, owner: str, now: datetime | None = None) -> LockInfo:
    """Check that `owner` holds a valid lock on the folder.

    Raises:
        HandoffError: code 4 when the lock is missing or someone else's; code 3 when
            it is the caller's but expired (take it again before writing).
    """
    now = now or utcnow()
    current = read_lock(folder / LOCK_NAME)
    if current is None:
        raise HandoffError(f"{folder}: the folder lock is required (handoff.py lock)",
                           NOT_OWNER)
    if current.owner != owner:
        raise HandoffError(f"{folder}: the lock belongs to {current.owner!r}, "
                           f"not {owner!r}", NOT_OWNER)
    if current.expired(now):
        raise HandoffError(f"{folder}: your lock expired, take it again", BUSY)
    return current


def find_locks(root: Path) -> list[tuple[Path, LockInfo]]:
    """Every lock under the root, the root included, sorted by path."""
    found: list[tuple[Path, LockInfo]] = []
    for path in sorted(root.rglob(LOCK_NAME)):
        info = read_lock(path)
        if path.is_file() and info is not None:
            found.append((path.parent, info))
    return found
