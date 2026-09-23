---
description: Migrate a flat notes folder, CLAUDE.md memory or older handoff into the three-level handoff, with multi-agent evaluation
argument-hint: "<path of the existing notes or handoff>"
disable-model-invocation: true
---

Migrate the existing project notes at `$ARGUMENTS` into a three-level handoff.

Read and follow `${CLAUDE_PLUGIN_ROOT}/skills/handoff/reference/migration.md`
step by step; the format rules are in
`${CLAUDE_PLUGIN_ROOT}/skills/handoff/reference/format.md`. The tool is
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py"` (`python` on Windows).

In short, you are the **coordinator**:

1. Inventory `$ARGUMENTS` and write an area plan with **disjoint ownership**:
   every source section belongs to exactly one area.
2. Scaffold the new structure **next to** the old one
   (`--root .claude/handoff-new init --areas ...`); never modify the source.
3. Launch **one sub-agent per area in parallel**; each writes entries with
   frontmatter (title, summary <= 160, rank 1-5, updated), one topic per file,
   <= 80 lines, using the lock protocol, and reports what it dropped.
4. Write the root `INDEX.md`, run `reindex --all --owner coordinator` and
   `check`; deduplicate rank-1 facts one lock at a time; run `stats --legacy`.
5. **Evaluate**: prepare N questions with answers from the old source and keep
   them outside the repository; two fresh agents of the same model answer them,
   one from the old source and one from the new structure following the read
   protocol, both logging every file read with its size; in parallel one agent
   audits completeness fact by fact.
6. Give the audit findings to a **fix agent** that must use the lock protocol.
7. Report to the user: bytes of the old source vs the new bootstrap, accuracy of
   both agents, audit findings (including errors already present in the old
   source), and what remains. **Switch only with the user's go-ahead**, archiving
   the old source (never deleting it).

If `$ARGUMENTS` is empty, ask the user which notes to migrate before starting.
