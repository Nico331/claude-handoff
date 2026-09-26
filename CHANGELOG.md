# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] - 2026-09-26

### Changed

- The tool and the hooks are a native binary written in Rust (`rust/`), shipped
  in `bin/` for Windows x64, Linux x86_64 and aarch64 and macOS (universal):
  Python is no longer needed. Same commands, options, messages, exit codes and
  file formats as the Python tool, `bootstrap_budget_tokens` of 0.2.1 included;
  the Python tool stays in `scripts/` as the fallback for other platforms and as
  the reference of `tests/test_parity.py`.
- The hooks run in exec form (`command` + `args`, no shell), so they also work on
  Windows without Git Bash. Without arguments the binary reads the event from
  the hook input.
- Commands, skill and docs call `handoff` (on the PATH of the Bash tool) or
  `${CLAUDE_PLUGIN_ROOT}/bin/handoff`.

### Fixed

- `lock --steal-stale`: several processes stealing the same expired lock could
  all believe they held it (about one round in five with 8 processes on Linux),
  and some crashed with `FileNotFoundError`. Thieves now queue on
  `<folder>/.lock.steal`; the lock is removed only if it is still the expired
  one. Fixed in both the Rust and the Python tool; `check` accepts the guard
  file.

### Added

- `tests/test_parity.py` (Python vs Rust on the same trees) and `tests/bench.py`.
## [0.2.1] - 2026-09-26

### Added

- `bootstrap_budget_tokens` in `handoff.json` (default 50000): the most estimated
  tokens (bytes / 3.5) the bootstrap read (root index, area indexes, entries with
  rank <= `bootstrap_max_rank`) may cost. `check` fails when it is exceeded, `stats`
  shows the share of the budget used, and the SessionStart hook reports the size
  and, when over budget, tells the session to read selectively and to demote
  entries until the bootstrap fits.

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
