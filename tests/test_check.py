"""Tests of check, list and stats.

Every check test starts from the valid structure of `build_tree`, introduces one
violation and verifies that the expected error appears.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import handoff
import hf_check
import hf_index
import hf_lock
import hf_report
import pytest
from helpers import build_tree, fm, write
from hf_config import Config
from hf_tree import HandoffError, parse_frontmatter, read_text


def errors_of(root: Path, cfg: Config | None = None) -> list[str]:
    """Errors of `check`, with paths relative to the root and `/` separators."""
    found = hf_check.check(root, cfg=cfg or Config())
    return [e.replace(str(root) + "\\", "").replace(str(root) + "/", "").replace("\\", "/")
            for e in found]


def only(root: Path, fragment: str) -> None:
    """Assert that `check` yields exactly one error, containing `fragment`."""
    found = errors_of(root)
    assert len(found) == 1 and fragment in found[0], found


# ---------------------------------------------------------------- valid structures


def test_check_valid_structure(tmp_path: Path) -> None:
    assert errors_of(build_tree(tmp_path)) == []


def test_check_missing_root(tmp_path: Path) -> None:
    assert "root does not exist" in hf_check.check(tmp_path / "missing")[0]


def test_check_accepts_valid_lock_log_and_config(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a/topic-x", "M1")
    write(root / ".lock-log", "{}\n")
    write(root / "handoff.json", '{"language": "it"}')
    assert errors_of(root) == []


def test_check_config_only_allowed_at_the_root(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-a/handoff.json", "{}")
    only(root, "area-a/handoff.json: file not allowed at level 2")


def test_check_reports_invalid_config(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "handoff.json", '{"max_topics": 0, "colour": "red"}')
    found = errors_of(root)
    assert any("handoff.json: unknown key 'colour'" in e for e in found), found
    assert any("max_topics must be a positive integer" in e for e in found), found
    write(root / "handoff.json", "{not json")
    assert any("handoff.json: not valid JSON" in e for e in errors_of(root))


# ---------------------------------------------------------------- levels and names


def test_check_extra_file_at_root(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "notes.md", fm("n", "n", 1, "2026-09-23"))
    only(root, "notes.md: file not allowed at level 1")


def test_check_extra_file_at_level_2(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-a/readme.txt", "x")
    only(root, "area-a/readme.txt: file not allowed at level 2")


def test_check_non_md_file_at_level_3(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/data.json", "{}")
    only(root, "data.json: file not allowed at level 3")


def test_check_folder_at_level_3(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / "area-b/topic-z/sub").mkdir()
    only(root, "topic-z/sub: folder not allowed at level 3")


def test_check_missing_index(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / "INDEX.md").unlink()
    only(root, "INDEX.md: index missing")


def test_check_too_many_topics(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    for i in range(20):
        write(root / f"area-b/extra-{i:02d}/INDEX.md",
              fm("x", "x", 4, "2026-08-01") + f"{hf_index.START}\n{hf_index.END}\n")
    hf_index.reindex(root, root / "area-b", None)
    only(root, "area-b: 21 topics (max 20)")


def test_check_too_many_files_in_a_topic(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    topic = root / "area-b/topic-z"
    for i in range(50):
        write(topic / f"entry-{i:02d}.md", fm("v", "v", 4, "2026-08-01"))
    hf_index.reindex(root, topic, None)
    only(root, "topic-z: 52 .md files (max 51)")


@pytest.mark.parametrize("name", ["Upper", "with_underscore", "double--hyphen", "città"])
def test_check_non_kebab_names(tmp_path: Path, name: str) -> None:
    root = build_tree(tmp_path)
    write(root / f"area-b/topic-z/{name}.md", fm("v", "v", 4, "2026-08-01"))
    assert any(f"{name}.md: name is not kebab-case" in e for e in errors_of(root))
    (root / f"area-b/topic-z/{name}.md").unlink()
    (root / f"area-b/{name}").mkdir()
    assert any(f"area-b/{name}: name is not kebab-case" in e for e in errors_of(root))


def test_check_entry_too_long(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    path = root / "area-b/topic-z/eta.md"
    write(path, read_text(path) + "line\n" * 80)
    only(root, "eta.md: 88 lines (max 80)")


# ---------------------------------------------------------------- configured limits


def test_check_uses_configured_limits(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    cfg = Config(max_topics=1, max_entry_files=3, max_entry_lines=5, max_summary=10)
    found = errors_of(root, cfg)
    assert any("area-a: 2 topics (max 1)" in e for e in found), found
    assert any("topic-x: 4 .md files (max 3)" in e for e in found), found
    assert any("eta.md: 8 lines (max 5)" in e for e in found), found
    assert any("summary of 16 characters (max 10)" in e for e in found), found


def test_cli_check_reads_handoff_json(tmp_path: Path,
                                      capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    write(root / "handoff.json", '{"max_entry_lines": 5}')
    assert handoff.main(["--root", str(root), "check"]) == 1
    assert "(max 5)" in capsys.readouterr().out


# ---------------------------------------------------------------- frontmatter


@pytest.mark.parametrize(("frontmatter", "message"), [
    ("", "frontmatter missing"),
    ("---\ntitle: T\n", "frontmatter missing"),
    (fm("T", "S", 6, "2026-08-01"), "invalid rank"),
    (fm("T", "S", 0, "2026-08-01"), "invalid rank"),
    (fm("T", "S", "one", "2026-08-01"), "invalid rank"),
    (fm("T", "S", 4, "01/08/2026"), "updated is not an ISO date"),
    (fm("T", "S", 4, "2026-02-30"), "updated is not an ISO date"),
    (fm("T", "", 4, "2026-08-01"), "empty summary"),
    (fm("T", "x" * 161, 4, "2026-08-01"), "summary of 161 characters"),
    (fm("", "S", 4, "2026-08-01"), "empty title"),
    ("---\ntitle: T\nsummary: S\nrank: 4\n---\n", "missing frontmatter key: 'updated'"),
    (fm("T", "S", 4, "2026-08-01").replace("---\n", "---\nauthor: x\n", 1),
     "unknown frontmatter key"),
    (fm("T", "S", 4, "2026-08-01").replace("---\n", "---\nrank: 4\n", 1), "repeated"),
    (fm("T", "S", 4, "2026-08-01").replace("---\n", "---\nno colon here\n", 1),
     "without ':'"),
])
def test_check_invalid_frontmatter(tmp_path: Path, frontmatter: str, message: str) -> None:
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/eta.md", frontmatter + "\nText.\n")
    found = errors_of(root)
    assert any("eta.md" in e and message in e for e in found), found


def test_frontmatter_with_quotes_and_160_char_summary() -> None:
    meta, problems = parse_frontmatter(fm('"Title: with colon"', "x" * 160, 1, "2026-09-23"))
    assert problems == [] and meta is not None and meta.title == "Title: with colon"


def test_check_level_2_index_frontmatter(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-a/INDEX.md"
    write(index, read_text(index).replace("rank: 1", "rank: 9"))
    assert any("area-a/INDEX.md: invalid rank" in e for e in errors_of(root))


# ---------------------------------------------------------------- indexes


def test_check_entry_missing_from_table(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/theta.md", fm("Theta", "New.", 4, "2026-08-01"))
    only(root, "entry missing from the table: theta.md")


def test_check_ghost_row(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    (root / "area-a/topic-x/alpha.md").unlink()
    found = errors_of(root)
    assert any("ghost row in the table: alpha.md" in e for e in found), found


def test_check_repeated_row(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-b/topic-z/INDEX.md"
    row = "| 4 | [Eta](eta.md) | Summary of eta. | 2026-08-01 |"
    write(index, read_text(index).replace(row, f"{row}\n{row}"))
    only(root, "repeated row in the table: eta.md")


def test_check_wrong_order(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-a/topic-x/INDEX.md"
    text = read_text(index)
    beta = "| 1 | [Beta](beta.md) | Summary of beta. | 2026-09-21 |"
    gamma = "| 1 | [Gamma](gamma.md) | Summary of gamma. | 2026-09-19 |"
    write(index, text.replace(beta, "@@").replace(gamma, beta).replace("@@", gamma))
    only(root, "wrong table order")


@pytest.mark.parametrize(("change", "message"), [
    ({"summary": "Changed."}, "summary of eta.md differs"),
    ({"rank": 3}, "rank of eta.md is 4, in the file 3"),
    ({"updated": "2026-08-02"}, "date of eta.md is 2026-08-01"),
    ({"title": "Eta new"}, "row of eta.md differs from the generated one"),
])
def test_check_row_out_of_sync(tmp_path: Path, change: dict[str, object],
                               message: str) -> None:
    root = build_tree(tmp_path)
    values: dict[str, object] = {"title": "Eta", "summary": "Summary of eta.",
                                 "rank": 4, "updated": "2026-08-01", **change}
    write(root / "area-b/topic-z/eta.md", fm(**values) + "\nText.\n")  # type: ignore[arg-type]
    assert any(message in e for e in errors_of(root)), errors_of(root)


def test_check_markers_missing_or_malformed(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-b/INDEX.md"
    text = read_text(index)
    write(index, text[: text.index(hf_index.START)])
    only(root, "table markers missing")
    write(index, text.replace(hf_index.END, ""))
    only(root, "table markers repeated, unpaired or swapped")


def test_check_folder_rank_inconsistent(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-a/topic-y/INDEX.md"
    write(index, read_text(index).replace("rank: 3", "rank: 2"))
    found = errors_of(root)
    assert any("topic-y/INDEX.md: folder rank 2, expected 3" in e for e in found), found


def test_check_folder_date_inconsistent(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    index = root / "area-b/INDEX.md"
    write(index, read_text(index).replace("updated: 2026-08-01", "updated: 2026-09-01"))
    found = errors_of(root)
    assert any("area-b/INDEX.md: updated 2026-09-01, expected 2026-08-01" in e
               for e in found), found


def test_check_follows_a_change_level_by_level(tmp_path: Path) -> None:
    """An entry promoted to rank 1 makes its topic inconsistent; after the topic's
    reindex, the area; after the area's, the root; finally the structure is valid."""
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/eta.md", fm("Eta", "Summary of eta.", 1, "2026-08-01"))
    steps = [("area-b/topic-z", "topic-z/INDEX.md: folder rank 4, expected 1"),
             ("area-b", "area-b/INDEX.md: folder rank 4, expected 1"),
             (".", "INDEX.md: rank of area-b/INDEX.md is 4, in the file 1")]
    for folder, message in steps:
        assert any(message in e for e in errors_of(root)), errors_of(root)
        hf_index.reindex(root, (root / folder).resolve(), None)
    assert errors_of(root) == []


# ---------------------------------------------------------------- links and locks


@pytest.mark.parametrize(("link", "broken"), [
    ("[a](beta.md)", False),
    ("[a](../topic-y/delta.md#section)", False),
    ("[a](../../area-b/INDEX.md)", False),
    ("[a](../../../outside/exists.md)", False),
    ("[a](<beta.md>)", False),
    ("![img](beta.md \"title\")", False),
    ("[a](https://example.org/x.md)", False),
    ("[a](mailto:x@example.org)", False),
    ("[a](#anchor)", False),
    ("`[a](in-code.md)`", False),
    ("```\n[a](in-block.md)\n```", False),
    ("[a](missing.md)", True),
    ("[a](../../../outside/missing.md)", True),
    ("[a](../topic-y/missing.md#x)", True),
    ("[ref]: missing-ref.md", True),
])
def test_check_relative_links(tmp_path: Path, link: str, broken: bool) -> None:
    root = build_tree(tmp_path)
    write(tmp_path / "outside/exists.md", "x")
    path = root / "area-a/topic-x/alpha.md"
    write(path, read_text(path) + f"\n{link}\n")
    found = [e for e in errors_of(root) if "broken link" in e]
    assert bool(found) == broken, found


def test_check_broken_link_in_an_index(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    write(root / "INDEX.md", read_text(root / "INDEX.md") + "\nSee [x](nothing.md).\n")
    only(root, "INDEX.md: broken link: nothing.md")


def test_check_expired_lock(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    hf_lock.acquire(root, root / "area-a", "M1", ttl=60,
                    now=hf_lock.utcnow() - timedelta(minutes=5))
    only(root, "area-a/.lock: expired lock")


def test_cli_check_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    assert handoff.main(["--root", str(root), "check"]) == 0
    assert "structure valid" in capsys.readouterr().out
    write(root / "area-a/stray.txt", "x")
    assert handoff.main(["--root", str(root), "check"]) == 1
    assert handoff.main(["--root", str(root), "check", "--warn-only"]) == 0
    assert "1 errors" in capsys.readouterr().out


# ---------------------------------------------------------------- list


def test_list_sorts_by_rank_and_filters(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    entries, warnings = hf_report.list_entries(root)
    assert warnings == [] and [e.path for e in entries] == [
        "area-a/topic-x/beta.md", "area-a/topic-x/gamma.md", "area-a/topic-x/alpha.md",
        "area-a/topic-y/delta.md", "area-b/topic-z/eta.md"]
    first, _ = hf_report.list_entries(root, max_rank=1)
    assert [e.rank for e in first] == [1, 1]
    area_b, _ = hf_report.list_entries(root, area="area-b")
    assert [(e.path, e.summary) for e in area_b] == [("area-b/topic-z/eta.md",
                                                      "Summary of eta.")]


def test_list_unknown_area(tmp_path: Path) -> None:
    with pytest.raises(HandoffError) as exc:
        hf_report.list_entries(build_tree(tmp_path), area="none")
    assert exc.value.code == 2


def test_list_warns_on_invalid_entry(tmp_path: Path,
                                     capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    write(root / "area-b/topic-z/broken.md", "no frontmatter\n")
    assert handoff.main(["--root", str(root), "list", "--max-rank", "1"]) == 0
    captured = capsys.readouterr()
    assert "broken.md" in captured.err
    assert captured.out.splitlines() == [
        "1  area-a/topic-x/beta.md  Summary of beta.",
        "1  area-a/topic-x/gamma.md  Summary of gamma."]


# ---------------------------------------------------------------- stats


def test_stats_levels_bootstrap_and_comparison(tmp_path: Path,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    write(tmp_path / "archive/old/notes.md", "x" * 7000)
    stats = hf_report.compute_stats(root, tmp_path / "archive" / "old")
    assert stats.levels[1].files == 1 and stats.levels[2].files == 2
    assert stats.levels[3].files == 3 + 5
    assert stats.total.files == 11
    size = sum(p.stat().st_size for p in (root / "INDEX.md", root / "area-a/INDEX.md",
                                          root / "area-b/INDEX.md",
                                          root / "area-a/topic-x/beta.md",
                                          root / "area-a/topic-x/gamma.md"))
    assert stats.bootstrap.files == 5 and stats.bootstrap.size == size
    assert stats.bootstrap.tokens == round(size / 3.5)
    assert stats.legacy_size == 7000
    legacy = str(tmp_path / "archive" / "old")
    assert handoff.main(["--root", str(root), "stats", "--legacy", legacy]) == 0
    out = capsys.readouterr().out
    assert "bootstrap (root + area indexes + rank <= 1): 5 files" in out
    assert "legacy handoff" in out and "~2000 tokens" in out


def test_stats_legacy_from_config_and_bootstrap_rank(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = build_tree(tmp_path)
    write(tmp_path / "old/notes.md", "y" * 350)
    write(root / "handoff.json", '{"legacy": "../old", "bootstrap_max_rank": 2}')
    assert handoff.main(["--root", str(root), "stats"]) == 0
    out = capsys.readouterr().out
    assert "rank <= 2): 6 files" in out and "legacy handoff" in out and "~100 tokens" in out


def test_stats_without_legacy(tmp_path: Path) -> None:
    root = build_tree(tmp_path)
    stats = hf_report.compute_stats(root, tmp_path / "missing")
    lines = hf_report.format_stats(stats, None)
    assert stats.legacy_size is None and not any("legacy" in x for x in lines)
