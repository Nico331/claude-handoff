# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.0] - 2026-09-24

### Added

- Command `language [CODE]` of `scripts/handoff.py`: prints the language the
  handoff content is written in, or writes it into `handoff.json` keeping every
  other key and its order (atomic write; the file is created if absent).
- Slash command `/claude-handoff:language [code]`; existing entries are
  translated only on request, with the lock protocol.
- `init --language CODE` writes the language into a new `handoff.json`.

### Changed

- `language` in `handoff.json` now means the language of the handoff content
  (entries, titles, summaries, hand-written index text), default `en`. Any
  lowercase language tag is accepted (`en`, `it`, `de`, `pt-br`); invalid tags
  are rejected with a clear message.
- Both hook texts state the content language. The hook text stays available in
  English and Italian; other languages get English, and a regional tag falls
  back to its base language (`it-ch` uses Italian).

## [0.1.0] - 2026-09-23

### Added

- Tool `scripts/handoff.py` (Python 3.10+, standard library only) with the
  commands `init`, `lock`, `unlock`, `status`, `reindex` (single folder or
  `--all`, bottom-up, one lock at a time), `check`, `list` and `stats`.
- Three-level structure: root, areas (names chosen by the project), topics
  (at most 20 per area) and entries (at most 51 files per topic, 80 lines each),
  with `title`/`summary`/`rank`/`updated` frontmatter and generated index tables.
- Atomic per-folder locks (`O_CREAT|O_EXCL`) with owner, pid, host and lifetime;
  stale locks broken only with `--steal-stale`, forced releases and steals
  logged in `.lock-log`.
- Optional `handoff.json` configuration (limits, bootstrap rank, lock lifetime,
  hook language `en`/`it`, summary injection, default areas, legacy folder) and
  root resolution through `--root`, `CLAUDE_HANDOFF_ROOT` or
  `.claude/handoff.json`.
- `SessionStart` and `UserPromptSubmit` hooks injecting the reading protocol,
  the bootstrap entries and the update reminder; a one-line `init` hint in
  projects without a handoff.
- Skill `handoff` with format and migration references; commands
  `/claude-handoff:init`, `/claude-handoff:check`, `/claude-handoff:migrate`.
- Single-plugin marketplace manifest.
