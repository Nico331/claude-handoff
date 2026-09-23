---
name: handoff
description: >-
  Read and maintain the project's three-level handoff (ranked, indexed,
  lock-protected project memory, by default in .claude/handoff/). Use at the
  start of a session to bootstrap from it; whenever a user decision, a new rule,
  a discovered procedure, a changed number or state, or an open item opened or
  closed must be recorded for future sessions; when several agents write project
  notes at the same time; when a handoff check fails or a lock blocks a write;
  when the user asks to set up, restructure or migrate project notes, a
  CLAUDE.md-style memory or a flat handoff folder into this structure; and when
  asked how much context the project memory costs.
---

# The handoff

A handoff is the project memory a fresh session needs and cannot deduce from the
code: rules, decisions, current state, procedures, open items. It is **state, not
a diary**: facts are updated in place.

Tool (Python 3.10+, standard library only), run from the project directory:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" <command>    # `python` on Windows
```

Below, `handoff.py` stands for that command line. The session-start hook also
prints the exact command for this machine. The root is `.claude/handoff/` unless
`--root`, `CLAUDE_HANDOFF_ROOT` or `.claude/handoff.json` (`{"root": "..."}`) say
otherwise.

## Shape

```
<root>/
├── INDEX.md            level 1: root index (plus optional handoff.json)
└── <area>/             level 2: areas, names chosen by the project
    ├── INDEX.md
    └── <topic>/        at most 20 topics per area
        ├── INDEX.md
        └── <entry>.md  at most 51 .md files per topic, INDEX.md included
```

Every entry and every area/topic index starts with:

```yaml
---
title: Short title
summary: One line, at most 160 characters, saying what the reader will find.
rank: 2
updated: 2026-09-23
---
```

Entries: kebab-case names, **one topic per file**, under 80 lines (aim for 20-50).
Index tables between `<!-- handoff:index:start -->` and `<!-- handoff:index:end -->`
are **generated** by `reindex`; never edit them by hand. Hand-written text around
them is kept. Limits are configurable in `<root>/handoff.json`; see
[reference/format.md](reference/format.md).

## Rank

| rank | meaning | who reads it |
|---|---|---|
| 1 | critical: getting it wrong causes damage (rules, credentials' whereabouts, things not to touch, decisions awaited from the user) | **every** session |
| 2 | current state and frequently used procedures | when the task touches the area |
| 3 | useful detail | on demand |
| 4 | history, context, closed items still referenced | rarely |
| 5 | archive | never, except when searching |

A folder's rank is the minimum of its children (computed by `reindex`). Rank 1 is
the fixed cost of every session: keep it short. Examples and edge cases are in
[reference/format.md](reference/format.md#choosing-a-rank).

## Read (bootstrap)

1. `<root>/INDEX.md`;
2. the `INDEX.md` of every area;
3. every rank-1 entry (`handoff.py list --max-rank 1`; the session-start hook
   already lists them with their summaries, but read the files, not only the
   summaries, when a summary is not enough to act safely);
4. for today's task: the rank <= 2 entries of the topics it touches, then the
   rest on demand, choosing from the summaries in the indexes.

Do not read the whole handoff "just in case": that is what the structure avoids.

## Write

**When:** at every prompt and after every action, ask: does this change a fact
written in the handoff, or add one a future session must know? If so, update it
**in the same action**, not at the end of the session.

**Protocol** (one lock at a time, bottom-up), e.g. for `state/cluster/access.md`:

```bash
handoff.py lock    state/cluster --owner agent-1
#   edit state/cluster/access.md; set `updated:` to today; adjust rank/summary
handoff.py reindex state/cluster --owner agent-1
handoff.py unlock  state/cluster --owner agent-1
handoff.py lock    state --owner agent-1 && handoff.py reindex state --owner agent-1 && handoff.py unlock state --owner agent-1
handoff.py lock    . --owner agent-1     && handoff.py reindex . --owner agent-1     && handoff.py unlock . --owner agent-1
handoff.py check
```

- Only edit a file while holding the lock of **its** folder.
- **Never hold two locks at once**: release the child before taking the parent.
  This makes deadlocks impossible. `reindex --all --owner <name>` does the whole
  bottom-up walk for you, one lock at a time.
- Exit code 3 from `lock`: someone else is writing; wait and retry. An expired
  lock is broken only with `lock --steal-stale` (logged in `<root>/.lock-log`),
  never by deleting the file. `unlock --force` is for a coordinator and is logged.
- If `reindex` fails, fix the file it names, still holding the lock, and rerun.
- Finish with `check`; it must print `structure valid`.

**Content rules:**

- **One fact in one place.** If it is already in a README, an ADR, the code or
  another entry, link to it instead of copying.
- **Never secrets.** Record where a credential lives and its state, never its value.
- **Closed items are not struck through**: lower them to rank 5 or delete the
  entry. A struck-through line still costs every reader.
- New topic: create `<area>/<topic>/INDEX.md` with frontmatter and a line of
  purpose, add the entries, then `reindex` topic, area, root. New area: prefer
  `handoff.py init --areas <existing>,<new>` (idempotent).
- An entry over 80 lines is two topics: split it and link them.

## Commands

| command | purpose |
|---|---|
| `init [--areas a,b] [--no-seed]` | create root, areas, `handoff.json`; idempotent, never overwrites |
| `lock <folder> --owner N [--ttl S] [--steal-stale]` | take a folder lock |
| `unlock <folder> --owner N [--force]` | release it |
| `status` | locks present, expired ones marked |
| `reindex <folder> --owner N` / `reindex --all --owner N` | regenerate index tables and folder ranks |
| `check [--warn-only]` | validate everything |
| `list [--max-rank N] [--area A]` | entries by rank |
| `stats [--legacy DIR]` | bytes per level, bootstrap cost, comparison with an old handoff |

Exit codes: 0 ok, 1 content error or failed check, 2 usage error, 3 folder locked,
4 lock owned by someone else.

## Migrating existing notes

To turn a flat notes folder, a long `CLAUDE.md` memory section or an older
handoff into this structure, follow
[reference/migration.md](reference/migration.md) (also available as
`/claude-handoff:migrate`). It is a multi-agent procedure with a measured
evaluation; do not improvise a single-pass copy.
