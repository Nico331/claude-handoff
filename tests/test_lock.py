"""Tests of the folder locks: acquisition, contention, stealing, release."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from datetime import timedelta
from pathlib import Path

import hf_lock
import pytest
from helpers import NOW, SCRIPT, build_tree, write
from hf_tree import HandoffError


def test_lock_file_has_expected_fields(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    folder = root / "area-a"
    info = hf_lock.acquire(root, folder, "M1", 60, now=NOW)
    data = json.loads((folder / ".lock").read_text(encoding="utf-8"))
    assert set(data) == {"owner", "pid", "host", "acquired_at", "ttl_seconds"}
    assert data["owner"] == "M1" and data["ttl_seconds"] == 60 and info.owner == "M1"
    assert data["acquired_at"].endswith("+00:00")


def test_second_lock_refused_with_code_3(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1", now=NOW)
    with pytest.raises(HandoffError) as exc:
        hf_lock.acquire(root, root / "area-a", "M2", now=NOW + timedelta(seconds=30))
    assert exc.value.code == 3 and "M1" in str(exc.value) and "2026-09-23" in str(exc.value)


def test_lock_is_not_reentrant(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root, "M1", now=NOW)
    with pytest.raises(HandoffError) as exc:
        hf_lock.acquire(root, root, "M1", now=NOW)
    assert exc.value.code == 3


def test_concurrent_threads_one_wins(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    barrier = threading.Barrier(16)
    results: list[str] = []

    def contend(name: str) -> None:
        barrier.wait()
        try:
            hf_lock.acquire(root, root / "area-b", name)
            results.append("ok")
        except HandoffError as exc:
            results.append(str(exc.code))

    threads = [threading.Thread(target=contend, args=(f"T{i}",)) for i in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert results.count("ok") == 1 and results.count("3") == 15


def test_concurrent_processes_one_wins(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    command = [sys.executable, str(SCRIPT), "--root", str(root), "lock", "area-a/topic-x"]
    procs = [subprocess.Popen([*command, "--owner", f"P{i}"], stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL) for i in range(8)]
    codes = sorted(proc.wait() for proc in procs)
    assert codes == [0] + [3] * 7


def test_expired_lock_refused_without_steal(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1", ttl=60, now=NOW)
    with pytest.raises(HandoffError) as exc:
        hf_lock.acquire(root, root / "area-a", "M2", now=NOW + timedelta(minutes=5))
    assert exc.value.code == 3 and "--steal-stale" in str(exc.value)
    assert not (root / ".lock-log").exists()


def test_steal_stale_breaks_and_logs(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1", ttl=60, now=NOW)
    info = hf_lock.acquire(root, root / "area-a", "M2", steal_stale=True,
                           now=NOW + timedelta(minutes=5))
    assert info.owner == "M2"
    record = json.loads((root / ".lock-log").read_text(encoding="utf-8").splitlines()[0])
    assert record["action"] == "steal-stale" and record["actor"] == "M2"
    assert record["folder"] == "area-a" and record["previous"]["owner"] == "M1"


def test_steal_stale_does_not_steal_a_valid_lock(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1", now=NOW)
    with pytest.raises(HandoffError) as exc:
        hf_lock.acquire(root, root / "area-a", "M2", steal_stale=True, now=NOW)
    assert exc.value.code == 3 and not (root / ".lock-log").exists()


@pytest.mark.parametrize("content", ["not json", '{"owner": "x"}',
                                     '{"owner": "x", "pid": 1, "host": "h", '
                                     '"acquired_at": "2026-09-23T12:00:00", "ttl_seconds": 9}'])
def test_unreadable_lock_treated_as_busy(tmp_path: Path, content: str) -> None:
    root = build_tree(tmp_path)
    write(root / "area-a" / ".lock", content)
    with pytest.raises(HandoffError) as exc:
        hf_lock.acquire(root, root / "area-a", "M2")
    assert exc.value.code == 3 and "owner=?" in str(exc.value)


@pytest.mark.parametrize(("owner", "ttl"), [("", 60), ("  ", 60), ("M1", 0), ("M1", -5)])
def test_invalid_lock_arguments(tmp_path: Path, owner: str, ttl: int) -> None:
    root = build_tree(tmp_path)
    with pytest.raises(HandoffError) as exc:
        hf_lock.acquire(root, root, owner, ttl)
    assert exc.value.code == 2


def test_unlock_by_owner(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1")
    hf_lock.release(root, root / "area-a", "M1")
    assert not (root / "area-a" / ".lock").exists()


def test_unlock_by_someone_else_refused_with_code_4(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1")
    with pytest.raises(HandoffError) as exc:
        hf_lock.release(root, root / "area-a", "M2")
    assert exc.value.code == 4 and (root / "area-a" / ".lock").exists()


def test_forced_unlock_is_logged(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1")
    hf_lock.release(root, root / "area-a", "coordinator", force=True)
    assert not (root / "area-a" / ".lock").exists()
    record = json.loads((root / ".lock-log").read_text(encoding="utf-8"))
    assert record["action"] == "force-unlock" and record["actor"] == "coordinator"


def test_unlock_without_lock(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    with pytest.raises(HandoffError) as exc:
        hf_lock.release(root, root, "M1")
    assert exc.value.code == 1


def test_find_locks_sorted(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-b", "B")
    hf_lock.acquire(root, root / "area-a/topic-x", "A")
    assert [(f.relative_to(root).as_posix(), i.owner) for f, i in hf_lock.find_locks(root)] \
        == [("area-a/topic-x", "A"), ("area-b", "B")]


def test_two_thieves_of_an_expired_lock_one_wins(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    folder = root / "area-a"
    hf_lock.acquire(root, folder, "M1", ttl=1, now=NOW - timedelta(hours=1))
    barrier = threading.Barrier(8)
    results: list[str] = []

    def steal(name: str) -> None:
        barrier.wait()
        try:
            hf_lock.acquire(root, folder, name, steal_stale=True)
            results.append(name)
        except HandoffError:
            results.append("-")

    threads = [threading.Thread(target=steal, args=(f"L{i}",)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    winners = [r for r in results if r != "-"]
    assert len(winners) == 1
    assert json.loads((folder / ".lock").read_text(encoding="utf-8"))["owner"] == winners[0]
    log = (root / ".lock-log").read_text(encoding="utf-8").splitlines()
    record = json.loads(log[0])  # the thief who broke it may not be the O_EXCL winner
    assert len(log) == 1 and record["actor"].startswith("L")
    assert record["previous"]["owner"] == "M1"
    assert not list(folder.glob(".lock.stale-*"))
