"""Tests of reindex, reindex --all and the command line (locks, exit codes, status)."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import handoff
import hf_index
import hf_lock
import pytest
from helpers import NOW, build_tree, fm, run_cli, write
from hf_tree import HandoffError, parse_frontmatter, read_text

# ---------------------------------------------------------------- reindex


def test_reindex_sorts_by_rank_then_name(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    rows = hf_index.parse_rows(read_text(root / "area-a/topic-x/INDEX.md"))
    assert rows is not None
    assert [r.link for r in rows] == ["beta.md", "gamma.md", "alpha.md"]
    assert rows[0].line == "| 1 | [Beta](beta.md) | Summary of beta. | 2026-09-21 |"


def test_reindex_folder_rank_is_min_and_date_is_max(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    meta, _ = parse_frontmatter(read_text(root / "area-a/topic-x/INDEX.md"))
    assert meta is not None and meta.rank == 1 and meta.updated == date(2026, 9, 21)
    area, _ = parse_frontmatter(read_text(root / "area-b/INDEX.md"))
    assert area is not None and area.rank == 4 and area.updated == date(2026, 8, 1)
    rows = hf_index.parse_rows(read_text(root / "INDEX.md"))
    assert rows is not None and [r.link for r in rows] == ["area-a/INDEX.md", "area-b/INDEX.md"]


def test_reindex_leaves_hand_written_text_alone(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-a/topic-x/INDEX.md"
    before = read_text(index)
    marker = before.index(hf_index.START)
    hand = before[:marker].replace("References", "References edited by hand")
    tail = "\n\nClosing note written by hand.\n"
    write(index, hand + before[marker:].rstrip("\n") + tail)
    write(root / "area-a/topic-x/beta.md", fm("Beta", "New summary.", 1, "2026-09-22"))
    assert hf_index.reindex(root, index.parent, None)
    after = read_text(index)
    assert "References edited by hand" in after and after.endswith(tail)
    assert "New summary." in after and "updated: 2026-09-22" in after


def test_reindex_is_idempotent(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert not hf_index.reindex(root, root / "area-a", None)


def test_reindex_adds_markers_when_missing(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = write(root / "area-b/topic-z/INDEX.md",
                  fm("z", "Topic z.", 4, "2026-08-01") + "\nText only.\n")
    assert hf_index.reindex(root, index.parent, None)
    text = read_text(index)
    assert text.index("Text only.") < text.index(hf_index.START) < text.index("eta.md")


def test_reindex_keeps_crlf_line_endings(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-b/topic-z/INDEX.md"
    index.write_bytes(index.read_bytes().replace(b"\n", b"\r\n"))
    write(root / "area-b/topic-z/eta.md", fm("Eta", "Other.", 4, "2026-08-01"))
    assert hf_index.reindex(root, index.parent, None)
    data = index.read_bytes()
    assert b"Other." in data and data.count(b"\n") == data.count(b"\r\n")


def test_reindex_escapes_pipes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/eta.md", fm("Eta", "a | b", 4, "2026-08-01"))
    hf_index.reindex(root, root / "area-b/topic-z", None)
    rows = hf_index.parse_rows(read_text(root / "area-b/topic-z/INDEX.md"))
    assert rows is not None and rows[0].summary == "a | b" and "a \\| b" in rows[0].line


def test_reindex_malformed_markers(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-a/INDEX.md"
    write(index, read_text(index) + f"\n{hf_index.START}\n")
    with pytest.raises(HandoffError, match="markers"):
        hf_index.reindex(root, index.parent, None)


def test_reindex_refuses_child_with_invalid_frontmatter(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-a/topic-y/broken.md", "no frontmatter\n")
    before = read_text(root / "area-a/topic-y/INDEX.md")
    with pytest.raises(HandoffError, match="broken.md"):
        hf_index.reindex(root, root / "area-a/topic-y", None)
    assert read_text(root / "area-a/topic-y/INDEX.md") == before


def test_reindex_refuses_topic_without_index(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / "area-b/empty").mkdir()
    with pytest.raises(HandoffError, match="index missing"):
        hf_index.reindex(root, root / "area-b", None)


def test_reindex_refuses_invalid_index_frontmatter(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-b/INDEX.md"
    write(index, read_text(index).replace("rank: 4", "rank: nine"))
    with pytest.raises(HandoffError, match="invalid rank"):
        hf_index.reindex(root, root / "area-b", None)


def test_reindex_requires_the_callers_lock(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    folder = root / "area-a"
    with pytest.raises(HandoffError) as exc:
        hf_index.reindex(root, folder, "M1")
    assert exc.value.code == 4
    hf_lock.acquire(root, folder, "M2")
    with pytest.raises(HandoffError) as exc:
        hf_index.reindex(root, folder, "M1")
    assert exc.value.code == 4
    hf_lock.release(root, folder, "M2")
    hf_lock.acquire(root, folder, "M1")
    hf_index.reindex(root, folder, "M1")


def test_reindex_with_expired_lock_refused(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root, "M1", ttl=1, now=NOW - timedelta(hours=1))
    with pytest.raises(HandoffError) as exc:
        hf_index.reindex(root, root, "M1")
    assert exc.value.code == 3


# ---------------------------------------------------------------- reindex --all


def test_bottom_up_order(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert [p.relative_to(root).as_posix() or "." for p in hf_index.bottom_up(root)] == [
        "area-a/topic-x", "area-a/topic-y", "area-b/topic-z", "area-a", "area-b", "."]


def test_reindex_all_propagates_a_change_to_the_root(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/eta.md", fm("Eta", "Now critical.", 1, "2026-09-23"))
    changed = hf_index.reindex_all(root, "M1")
    assert [p.relative_to(root).as_posix() or "." for p in changed] == [
        "area-b/topic-z", "area-b", "."]
    rows = hf_index.parse_rows(read_text(root / "INDEX.md"))
    assert rows is not None and [r.rank for r in rows] == ["1", "1"]
    assert not hf_lock.find_locks(root)


def test_reindex_all_never_holds_two_locks(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    root = build_tree(tmp_path)
    held: list[int] = []
    real_acquire = hf_lock.acquire

    def spy(*args: object, **kwargs: object) -> hf_lock.LockInfo:
        held.append(len(hf_lock.find_locks(root)))
        return real_acquire(*args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(hf_lock, "acquire", spy)
    hf_index.reindex_all(root, "M1")
    assert held == [0] * 6


def test_reindex_all_stops_on_a_busy_folder_and_releases(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "other")
    with pytest.raises(HandoffError) as exc:
        hf_index.reindex_all(root, "M1")
    assert exc.value.code == 3
    assert [(f.name, i.owner) for f, i in hf_lock.find_locks(root)] == [("area-a", "other")]


def test_reindex_all_releases_the_lock_on_content_error(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-a/topic-y/broken.md", "no frontmatter\n")
    with pytest.raises(HandoffError, match="broken.md"):
        hf_index.reindex_all(root, "M1")
    assert not hf_lock.find_locks(root)


def test_cli_reindex_all(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-a/topic-y/delta.md", fm("Delta", "Changed.", 3, "2026-09-11"))
    result = run_cli(root, "reindex", "--all", "--owner", "M1")
    assert result.returncode == 0, result.stderr
    assert "2 indexes updated" in result.stdout  # topic-y and area-a
    assert run_cli(root, "check").returncode == 0
    assert run_cli(root, "reindex", "--all").returncode == 2
    assert run_cli(root, "reindex", "area-a", "--all", "--owner", "M1").returncode == 2


# ---------------------------------------------------------------- command line


def test_cli_full_protocol(tmp_path: Path) -> None:
    """lock, edit, reindex, unlock, then the parent, up to the root."""
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/eta.md", fm("Eta", "Now critical.", 1, "2026-09-23"))
    for folder in ("area-b/topic-z", "area-b", "."):
        assert run_cli(root, "lock", folder, "--owner", "M3").returncode == 0
        assert run_cli(root, "reindex", folder, "--owner", "M3").returncode == 0
        assert run_cli(root, "unlock", folder, "--owner", "M3").returncode == 0
    rows = hf_index.parse_rows(read_text(root / "INDEX.md"))
    assert rows is not None and rows[0].rank == "1"
    assert run_cli(root, "check").returncode == 0


def test_cli_exit_codes(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    assert run_cli(root, "lock", "area-a", "--owner", "M1").returncode == 0
    busy = run_cli(root, "lock", "area-a", "--owner", "M2")
    assert busy.returncode == 3 and "M1" in busy.stderr
    assert run_cli(root, "unlock", "area-a", "--owner", "M2").returncode == 4
    assert run_cli(root, "reindex", "area-a", "--owner", "M2").returncode == 4
    assert run_cli(root, "reindex", "area-a").returncode == 2
    assert run_cli(root, "reindex").returncode == 2
    assert run_cli(root, "lock", "missing", "--owner", "M1").returncode == 2
    assert run_cli(root, "lock", "../..", "--owner", "M1").returncode == 2
    assert run_cli(root, "lock", "area-a", "--owner", "M1", "--ttl", "0").returncode == 2
    assert run_cli(root, "lock", "area-a", "--owner", "M1", "--ttl", "x").returncode == 2
    forced = run_cli(root, "unlock", "area-a", "--owner", "coord", "--force")
    assert forced.returncode == 0 and "forced" in forced.stdout


def test_cli_commands_need_an_existing_root(tmp_path: Path) -> None:
    for args in (("lock", ".", "--owner", "M"), ("status",), ("list",), ("stats",),
                 ("reindex", "--all", "--owner", "M")):
        result = run_cli(tmp_path / "none", *args)
        assert result.returncode == 2 and "run init" in result.stderr


def test_cli_lock_beyond_level_3(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / "area-a/topic-x/too-deep").mkdir()
    assert run_cli(root, "lock", "area-a/topic-x/too-deep", "--owner", "M1").returncode == 2


def test_cli_lock_uses_configured_ttl(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "handoff.json", '{"lock_ttl_seconds": 42}')
    result = run_cli(root, "lock", "area-a", "--owner", "M1")
    assert result.returncode == 0 and "ttl 42s" in result.stdout


def test_cli_status(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    assert handoff.main(["--root", str(root), "status"]) == 0
    assert "no locks" in capsys.readouterr().out
    hf_lock.acquire(root, root / "area-a", "M1")
    hf_lock.acquire(root, root, "M2", ttl=1, now=hf_lock.utcnow() - timedelta(hours=1))
    handoff.main(["--root", str(root), "status"])
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith(".: owner=M2") and lines[0].endswith("EXPIRED")
    assert lines[1].startswith("area-a: owner=M1") and "EXPIRED" not in lines[1]


def test_cli_reindex_no_lock(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    assert handoff.main(["--root", str(root), "reindex", "area-a", "--no-lock"]) == 0
    assert "already aligned" in capsys.readouterr().out


def test_cli_without_command_exits_2() -> None:
    with pytest.raises(SystemExit) as exc:
        handoff.main([])
    assert exc.value.code == 2


def test_cli_help_lists_every_command() -> None:
    text = handoff.build_parser().format_help()
    for command in ("init", "lock", "unlock", "status", "reindex", "check", "list", "stats"):
        assert command in text
    assert "4 lock owned by someone else" in " ".join(text.split())
