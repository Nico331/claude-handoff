# claude-handoff

A Claude Code plugin that gives a project a **handoff**: the memory a fresh
session needs and cannot deduce from the code (rules, decisions, current state,
procedures, open items), organised in three levels with ranked entries,
generated indexes and folder locks, so that each session reads a small
bootstrap instead of every note, and several agents can update it at the same
time.

## Why

Project notes grow by accumulation. A flat handoff that every session must read
in full eventually costs tens of thousands of tokens before any work starts,
and nobody knows any more which parts matter.

This plugin was extracted from a real project where the flat handoff had reached
**134 KB** in six files. After migrating it to this structure:

| | flat handoff | three-level handoff |
|---|---|---|
| read at every session start | 134 KB (all of it) | **41.6 KB** (root + area indexes + 20 rank-1 entries), **31%** |
| accuracy on a 20-question test, two fresh agents of the same model | 20/20 | **20/20** |

A fact-by-fact audit of ~460 facts after the migration found 2 missing and 4
altered facts, which were fixed (three of the four were already wrong in the old
notes). These numbers come from one project; yours will differ, and `stats`
measures them.

## How it works

```
.claude/handoff/
├── INDEX.md               level 1: purpose, reading order, table of areas
├── handoff.json           optional configuration
├── rules/                 level 2: an area (names are yours to choose)
│   ├── INDEX.md           table of topics, generated
│   └── cluster/           level 3: a topic (at most 20 per area)
│       ├── INDEX.md       table of entries, generated
│       └── shared-namespaces.md   an entry: one topic, < 80 lines
└── state/ decisions/ procedures/ open/ history/
```

Every entry carries a frontmatter:

```yaml
---
title: Shared namespaces
summary: Namespaces owned by other teams; never deploy or delete there.
rank: 1
updated: 2026-09-23
---
```

- **Rank** says who must read it (table below). A folder's rank is the minimum of
  its children, computed for you.
- **Indexes** are tables generated from the children's frontmatter between
  `<!-- handoff:index:start -->` and `<!-- handoff:index:end -->`, so an index
  can never lie about what is below it. Hand-written text around them is kept.
- **Bootstrap**: root index, area indexes, every rank-1 entry; then only what the
  task touches, chosen from the summaries.
- **Writes** go through a per-folder lock, one lock at a time, reindexing upward
  to the root, then `check`.

### Rank

| rank | meaning | who reads it |
|---|---|---|
| 1 | critical: getting it wrong causes damage (rules, where credentials live, things not to touch, decisions awaited from the user) | every session |
| 2 | current state, frequently used procedures | when the task touches the area |
| 3 | useful detail | on demand |
| 4 | history, context, closed items still referenced | rarely |
| 5 | archive | never, except when searching |

Closed items are lowered to rank 5 or deleted, never struck through. One fact
lives in one place. Never secrets.

## Install

```
/plugin marketplace add Nico331/claude-handoff
/plugin install claude-handoff@claude-handoff
```

Then run `/reload-plugins` (or restart Claude Code) if the install summary asks
for it. Nothing else to install: the plugin ships a native `handoff` binary for
Windows x64 (also run by Windows on Arm), Linux x86_64 and aarch64 (static, any
distribution) and macOS (Intel and Apple Silicon). The hooks start it directly,
without a shell, so they work in Git Bash, PowerShell or no shell at all. On any
other platform `bin/handoff` falls back to the Python version in `scripts/`
(Python 3.10+, standard library only).

To try it without installing: `claude --plugin-dir /path/to/claude-handoff`.

## Quick start

In your project:

```
/claude-handoff:init                  # or: /claude-handoff:init rules,state,decisions
```

This creates `.claude/handoff/` with the default areas (`rules`, `state`,
`decisions`, `procedures`, `open`, `history`), a `handoff.json`, and a seed topic
`rules/handoff/` explaining the protocol. The handoff is written in English by
default; for another language pass it at creation (`handoff init --language
it`) or set it later with `/claude-handoff:language it`. Then just work: at every session start
the hook injects the reading protocol and the rank-1 summaries, and at every
prompt it reminds Claude to record what changed. The `handoff` skill holds the
full rules and is loaded when Claude needs to write.

Add the lock files to your `.gitignore`:

```gitignore
.claude/handoff/**/.lock
.claude/handoff/**/.lock.steal
```

`.lock-log` (the record of stolen and forced locks) can be committed or ignored,
as you prefer.

Have existing notes? Run `/claude-handoff:migrate <path>`.

## Commands

Slash commands (namespaced by the plugin):

| command | what it does |
|---|---|
| `/claude-handoff:init [areas]` | scaffold the handoff; idempotent, never overwrites |
| `/claude-handoff:check` | validate and propose fixes |
| `/claude-handoff:migrate <path>` | multi-agent migration of existing notes, with evaluation |
| `/claude-handoff:language [code]` | show or set the language the handoff is written in; existing entries are translated only on request |
| `/claude-handoff:handoff` | the skill itself (also loaded automatically) |

The tool behind them, usable directly: `handoff` is on the PATH of Claude's Bash
tool while the plugin is enabled, otherwise run `<plugin>/bin/handoff`.

```
handoff [--root DIR] <command> [options]
```

The session-start hook prints the exact command line for your machine.

| command | what it does |
|---|---|
| `init [--areas a,b] [--owner N] [--no-seed] [--language CODE]` | create root, areas and `handoff.json` (with `language` = CODE when the file is new); only missing files; reindexes the folders it touched and their ancestors |
| `lock <folder> --owner N [--ttl S] [--steal-stale]` | take the folder lock atomically; `--steal-stale` breaks an expired lock and logs it |
| `unlock <folder> --owner N [--force]` | release your lock; `--force` releases someone else's and logs it |
| `status` | locks present, with age; expired ones marked `EXPIRED` |
| `reindex <folder> --owner N` | regenerate the folder's table and its rank/date; requires your lock (`--no-lock` for repairs and tests) |
| `reindex --all --owner N` | every folder, topics then areas then root, one lock at a time |
| `check [--warn-only]` | validate levels, limits, names, frontmatter, line counts, tables, folder ranks, relative links, expired locks, `handoff.json` |
| `list [--max-rank N] [--area A]` | entries by rank; `list --max-rank 1` is the bootstrap list |
| `stats [--legacy DIR]` | bytes and lines per level, bootstrap cost in bytes and estimated tokens, comparison with an old handoff |
| `language [CODE]` | print the content language (`en` by default), or write `language` into `handoff.json` keeping the other keys; atomic write |

`<folder>` is relative to the root: `.` is the root, `state/cluster` a topic.

### Exit codes

| code | meaning |
|---|---|
| 0 | success (also `check --warn-only` with errors) |
| 1 | content errors: `check` failed, invalid frontmatter or markers in `reindex`, `unlock` with no lock |
| 2 | usage error: bad arguments, missing folder, outside the root or deeper than level 3, unknown area, invalid `handoff.json` or language code, no handoff yet |
| 3 | folder locked: valid lock of someone else, expired lock not broken, race lost; in `reindex`, your own lock expired |
| 4 | lock owned by someone else (or missing, for `reindex`) |

## Lock protocol

A lock is the file `<folder>/.lock`, created with `O_CREAT|O_EXCL` and holding
owner, pid, host, time and lifetime (default 15 minutes). To change
`state/cluster/access.md`:

```bash
H=handoff    # or <plugin>/bin/handoff
$H lock    state/cluster --owner agent-1
#   edit state/cluster/access.md (update its frontmatter)
$H reindex state/cluster --owner agent-1
$H unlock  state/cluster --owner agent-1
$H lock    state --owner agent-1 && $H reindex state --owner agent-1 && $H unlock state --owner agent-1
$H lock    .     --owner agent-1 && $H reindex .     --owner agent-1 && $H unlock .     --owner agent-1
$H check
```

- Edit a file only while holding the lock of its folder.
- **Never hold two locks at once**: release the child before taking the parent.
  No circular wait is possible, so no deadlock.
- Exit code 3 means someone else is writing: wait and retry.
- An expired lock never disappears by itself. Break it only with
  `--steal-stale`; the act goes to `<root>/.lock-log` with the previous owner.
  Thieves queue on `<folder>/.lock.steal`, created the same way, so of two
  agents stealing the same lock at once exactly one wins, and the lock is
  removed only if it is still the expired one.
- `reindex --all` performs the whole upward walk with one lock at a time and
  releases each lock even when a step fails.

## Hooks

| event | with a handoff | without a handoff |
|---|---|---|
| `SessionStart` | reading protocol + tool command + content language + every entry with rank <= `bootstrap_max_rank` and its summary (at most 60 listed) + the cost of the bootstrap read against `bootstrap_budget_tokens`, with a warning when over | one line suggesting `/claude-handoff:init` |
| `UserPromptSubmit` | reminder: record changes in the same action, with the lock protocol, in the content language | nothing |

The hook always exits 0, prints ASCII-only JSON (`hookSpecificOutput.additionalContext`),
tolerates malformed files and an invalid `handoff.json` (it falls back to the
defaults and says so). It finds the project through `CLAUDE_PROJECT_DIR`, or the
`cwd` of the hook input. The hooks are declared in exec form (`command` plus
`args`): Windows starts `bin/handoff.exe`, macOS and Linux run `bin/handoff`, a
`sh` launcher that picks the binary for the machine. Run without arguments, the
binary reads the event from the hook input, so a Claude Code that ignores `args`
still gets the right text.

## Configuration

`<root>/handoff.json`, every key optional (unknown keys are an error):

| key | default | meaning |
|---|---|---|
| `max_topics` | 20 | topics per area |
| `max_entry_files` | 51 | `.md` files per topic, `INDEX.md` included |
| `max_entry_lines` | 80 | lines per entry |
| `max_summary` | 160 | characters per summary |
| `bootstrap_max_rank` | 1 | entries read at every session start (1-5) |
| `bootstrap_budget_tokens` | 50000 | most estimated tokens (bytes / 3.5) the bootstrap read may cost: `check` fails, `stats` shows the share used, and the SessionStart hook warns when it is exceeded |
| `lock_ttl_seconds` | 900 | default lock lifetime |
| `language` | `"en"` | language the handoff content is written in (entries, titles, summaries, hand-written index text): a lowercase tag such as `"en"`, `"it"`, `"de"`, `"pt-br"`. The hook text is in that language when translated (English, Italian; `it-ch` uses Italian), in English otherwise. Set it with `/claude-handoff:language` |
| `inject_summaries` | `true` | whether SessionStart lists the bootstrap entries |
| `default_areas` | `["rules", "state", "decisions", "procedures", "open", "history"]` | areas created by `init` |
| `legacy` | `null` | old handoff folder for `stats` to compare with, relative to the root |

The root is found in this order: `--root`; the `CLAUDE_HANDOFF_ROOT` environment
variable; the `root` key of `<project>/.claude/handoff.json` (for example
`{"root": "docs/handoff"}`), which is how the hooks learn about a custom root;
the default `.claude/handoff`.

## Limitations

- **Locks are advisory.** Nothing prevents a tool or a person from editing a file
  without the lock. `check` catches index drift, not a lost concurrent edit.
- Lock atomicity relies on `O_CREAT|O_EXCL`; it holds on local file systems and
  NTFS, not necessarily on old network file systems.
- The hook asks Claude to read the bootstrap; it cannot force it. It injects the
  summaries, not the full rank-1 files.
- The frontmatter is a strict subset of YAML: four keys, one-line values.
- The handoff content can be in any language, but the hook text is translated
  only into English and Italian; tool messages are English. Changing the
  language does not translate existing entries.
- Token counts in `stats` are an estimate (bytes / 3.5).
- The plugin carries the binaries of every platform (about 6 MB); each machine
  runs one. Other platforms need Python 3.10+ for the fallback.
- A lock can still be lost if the owner of an *expired* lock releases it in the
  very instant a thief is breaking it: expiry means the owner is presumed dead.
- With the plugin enabled for your user, every project without a handoff gets
  the one-line `init` hint at session start. Disable the plugin per project if
  you do not want it.
- `commands/` uses the flat command format, which the docs now describe as the
  older equivalent of `skills/`; it is still supported.

## Development

```
python -m pytest            # Python suite in tests/, needs pytest
cd rust && cargo test      # Rust unit tests
claude plugin validate .
```

The binaries in `bin/` are built from `rust/` with `cargo zigbuild` for
`x86_64-pc-windows-gnu`, `x86_64-unknown-linux-musl`, `aarch64-unknown-linux-musl`
and `universal2-apple-darwin` (see `.github/workflows/build.yml`; locally, the
`ghcr.io/rust-cross/cargo-zigbuild` image has everything). The Rust tool must
behave exactly like the Python one: `HANDOFF_BIN=<binary> python -m pytest
tests/test_parity.py` runs both on the same trees (every `check` rule, locks,
reindex, init, language, hook, usage errors and random trees) and compares exit
codes, output and the files left on disk. `python tests/bench.py <binary>`
compares their speed.

`tests/` covers every command and error path on trees built in temporary
folders: concurrent locks with threads and processes, concurrent stealing of an
expired lock, `reindex` leaving hand-written text alone, one violation per
`check` rule, configuration and root resolution, `init` idempotence, `reindex
--all` holding one lock at a time, and the hook (no handoff, malformed files,
ASCII JSON, Italian texts, internal errors), and the `language` command
(show, set preserving the other keys, invalid codes and files left untouched).

## License

MIT, see [LICENSE](LICENSE).
