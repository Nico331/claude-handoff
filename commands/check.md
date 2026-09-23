---
description: Validate the project handoff (structure, frontmatter, generated indexes, links, locks) and fix what it reports
argument-hint: "[--warn-only]"
---

Validate the claude-handoff project memory.

1. Run from the project directory (`python` on Windows if `python3` is missing):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" check $ARGUMENTS
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" status
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" stats
   ```

2. If `check` prints `structure valid`, report that together with the bootstrap
   size from `stats` and any lock from `status`. Stop.
3. Otherwise group the errors and propose fixes. Typical causes:
   - *entry missing from the table / ghost row / rank or date of X is ...*: an
     entry changed without `reindex`; run `reindex --all --owner <you>`.
   - *N lines (max 80)*: the entry covers two topics; split it.
   - *summary of N characters*: shorten the summary, keep the facts in the body.
   - *name is not kebab-case / file not allowed at level N*: rename or move.
   - *broken link*: fix the target or remove the link.
   - *expired lock*: a writer died; ask the user before `lock --steal-stale`.
4. Apply fixes only with the lock protocol of the `handoff` skill (one lock at a
   time, `reindex` up to the root), then run `check` again until it is valid.
   Ask before deleting any entry.
