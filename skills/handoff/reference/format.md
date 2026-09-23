# Handoff format reference

## Levels

| level | folder | may contain | limit (default) |
|---|---|---|---|
| 1 | root | `INDEX.md`, `handoff.json`, `.lock`, `.lock-log`, area folders | no limit on areas |
| 2 | area | `INDEX.md`, `.lock`, topic folders | `max_topics` = 20 |
| 3 | topic | `INDEX.md`, `.lock`, entry `.md` files, no folders | `max_entry_files` = 51 `.md` files, `INDEX.md` included |

- Folder and entry names: kebab-case, ASCII lowercase letters and digits
  (`cluster-access`, `mfa-reset`), never spaces, underscores or accents.
- An entry is at most `max_entry_lines` = 80 lines, frontmatter included.
- The root `INDEX.md` may have frontmatter (recommended; `init` writes one);
  area and topic indexes must.

## Frontmatter

Exactly four keys, one per line, no nesting:

| key | rule |
|---|---|
| `title` | non-empty; shown as the link text in the parent table |
| `summary` | one line, 1 to `max_summary` = 160 characters; it is what readers use to decide whether to open the file, so say what is *in* it, not that it exists |
| `rank` | integer 1-5 |
| `updated` | ISO date `YYYY-MM-DD`; set it to today when you change the content |

Values may be wrapped in single or double quotes (useful when a title contains
`: `). For indexes, `reindex` overwrites `rank` (minimum of the children) and
`updated` (most recent child); you only write `title` and `summary`.

## Index tables

```markdown
<!-- handoff:index:start -->
| rank | entry | summary | updated |
|---|---|---|---|
| 1 | [How to deploy](how-to-deploy.md) | ... | 2026-09-23 |
<!-- handoff:index:end -->
```

Rows are sorted by rank, then by name. `check` fails when a row is missing,
extra, duplicated, out of order, or differs from the child's frontmatter, so an
index can never lie about its content. If the markers are absent, `reindex`
appends them at the end of the file.

## Choosing a rank

Ask: *what happens if a fresh session does not know this?*

| rank | the answer | examples |
|---|---|---|
| 1 | it can cause damage or waste the user's trust | "never touch namespace X, it belongs to another team"; "the user decides releases, never deploy on your own"; "API keys live in the vault at path P, never in `.env`"; "decision awaited from the user: pick A or B before continuing the migration" |
| 2 | it will redo or misjudge today's work | current version deployed and where; the test command that actually works; state of the ongoing refactor |
| 3 | it will look it up when needed | the full list of environment variables of a service; a procedure used twice a year |
| 4 | it only matters to understand the past | why the old queue was replaced; a closed incident still cited by another entry |
| 5 | nothing, it is kept for searches | an obsolete procedure; a closed open-item |

Guidelines:

- **Rank 1 must stay small.** In the reference project, 20 rank-1 entries plus
  the indexes were 41.6 KB. If rank 1 grows past a few dozen entries, demote.
- The same fact at rank 1 in two areas is a duplicate: keep one, link from the
  other at rank 2 or lower.
- An open item is rank 1 only when it blocks on the user or when ignoring it
  causes damage; otherwise 2 or 3.
- Closing an item: lower to 5 (or delete) and say so in your session notes.

## handoff.json

Optional, at the root. Every key is optional; unknown keys are an error.

| key | type | default | meaning |
|---|---|---|---|
| `max_topics` | int > 0 | 20 | topics per area |
| `max_entry_files` | int > 0 | 51 | `.md` files per topic, `INDEX.md` included |
| `max_entry_lines` | int > 0 | 80 | lines per entry |
| `max_summary` | int > 0 | 160 | characters per summary |
| `bootstrap_max_rank` | 1-5 | 1 | entries read at every session start |
| `lock_ttl_seconds` | int > 0 | 900 | default lock lifetime |
| `language` | `en`, `it` | `en` | language of the text the hooks inject |
| `inject_summaries` | bool | true | SessionStart lists the bootstrap entries with summaries |
| `default_areas` | list of names | rules, state, decisions, procedures, open, history | areas `init` creates |
| `legacy` | path or null | null | old handoff for `stats` to compare with, relative to the root |

The root itself is chosen by `--root`, then `CLAUDE_HANDOFF_ROOT`, then the
`root` key of `<project>/.claude/handoff.json`, then `.claude/handoff`.
