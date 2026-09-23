# Migrating existing notes into a handoff

This is the procedure that took a 134 KB, six-file flat handoff to a three-level
one whose bootstrap is 41.6 KB (31%), with the same 20/20 accuracy on a
20-question test. It relies on sub-agents with their own context: the
coordinator (you) plans, delegates, measures and fixes; it does not copy text
itself.

Throughout, `handoff.py` means
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py"` (`python` on Windows).

## 0. Ground rules

- **Copy, never replace.** Build the new structure next to the old source (e.g.
  `--root .claude/handoff-new`); the old source stays untouched until the user
  decides to switch, and afterwards it is **archived, never deleted**.
- Ask the user before switching the default root, and before changing any
  project rule file (such as `CLAUDE.md`) that tells sessions what to read.
- No secrets travel into the new structure: where a credential lives, never its
  value. If the old source contains secrets, report them to the user.

## 1. Inventory and area plan (coordinator)

1. Measure the source: files, bytes, and the rough topic list of each file.
2. Choose the areas (suggested: `rules`, `state`, `decisions`, `procedures`,
   `open`, `history`; rename freely) and, for each area, its likely topics.
3. Write the **plan** (a scratch file outside the repository is fine): for each
   area, one owner agent and the exact source sections it owns. Ownership must be
   **disjoint**: every source section belongs to exactly one area, so two agents
   never write the same fact. Note cross-area facts explicitly ("the deploy
   command lives in `procedures/deploy`; `state` links to it").
4. Scaffold: `handoff.py --root <new> init --areas <a,b,c>` (add `--no-seed` if
   the project already has its own rules about the handoff).

## 2. One agent per area, in parallel

Launch one sub-agent per area at the same time. Each prompt includes:

- the source files and sections it owns (and that it must not write elsewhere);
- the format rules: frontmatter with `title`, `summary` (<= 160 characters),
  `rank` (1-5 with the criteria of [format.md](format.md#choosing-a-rank)),
  `updated`; kebab-case names; one topic per file; <= 80 lines per entry;
  <= 20 topics per area; <= 51 files per topic;
- "lose nothing": every fact of its sections must land somewhere (code blocks and
  commands verbatim), or be listed in its report as deliberately dropped with
  the reason (for example, a fact that is struck through as closed goes to
  rank 5, not to the bin);
- the lock protocol for its own area: `lock <area>/<topic> --owner <agent>`,
  write, `reindex`, `unlock`, then the area; it must **not** reindex the root;
- a final report: topics created, entries per rank, facts it was unsure about,
  items dropped.

## 3. Assemble (coordinator)

1. Write the hand-written part of the root `INDEX.md`: purpose, reading order,
   rank legend, write protocol (the `init` template is a good start).
2. `handoff.py --root <new> reindex --all --owner coordinator` and
   `handoff.py --root <new> check` until it prints `structure valid`.
3. **Deduplicate rank 1**: `handoff.py --root <new> list --max-rank 1`. The same
   rule at rank 1 in several areas is a duplicate: keep one, demote the others to
   2 and link. Do this **one lock at a time** (in the reference migration the
   coordinator held seven sibling locks at once to save time; it could not
   deadlock, but it broke the rule, so don't).
4. `handoff.py --root <new> stats --legacy <old>`: note the bootstrap bytes and
   the percentage of the old source.

## 4. Evaluate

Prepare **N questions** (20 is a good number) from the *old* source, with the
expected answers, covering every area and some rank-3/4 details. Keep the
question file **outside the repository** so no agent can read the answers.

Launch, in parallel, two **fresh agents of the same model**:

- **A (old)**: answers the questions using only the old source;
- **B (new)**: answers them using only the new structure, following the read
  protocol (root, area indexes, rank 1, then on demand).

Both must **log every file they read** with its size, so you can compare bytes
read, not only accuracy. Score both against the expected answers.

In parallel, launch one **completeness audit** agent: it walks the old source
fact by fact and, for each fact, finds where it lives in the new structure;
it reports facts missing, facts altered (a changed number, date, name or
condition is a serious error), duplicates, and rank choices it disagrees with.
In the reference migration it checked ~460 facts and found 2 missing and 4
seriously altered; three of the four were already wrong in the old source, so
also report those back to the user.

## 5. Fix

Give the audit findings to one **fix agent** that must use the lock protocol
for every change (one lock at a time, `reindex` up to the root, `check` at the
end). Fix errors that were already in the old source in the old source too, if
it is still the source in use. Re-run `stats`.

## 6. Switch (only with the user's go-ahead)

1. Move the old source to an archive folder (e.g. `.claude/archive/handoff-v1/`)
   with `git mv`; never delete it.
2. Move the new root to its final place (default `.claude/handoff/`) and set
   `"legacy"` in `handoff.json` to the archive, so `stats` keeps comparing.
3. Update any project rule that tells sessions what to read at startup.
4. Add `.lock` files to `.gitignore` (see the README).
5. Final `check`, `stats`, and a short note to the user with the measured result.
