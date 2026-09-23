---
description: Create the three-level handoff in this project (idempotent, never overwrites existing files)
argument-hint: "[comma-separated areas, e.g. rules,state,decisions]"
disable-model-invocation: true
---

Set up the claude-handoff project memory in the current project.

1. Run, from the project directory (use `python` instead of `python3` on Windows
   if `python3` is not available):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" init --owner init
   ```

   The user's arguments were: "$ARGUMENTS". If they name areas, append
   `--areas <names, comma-separated, no spaces>` to the command; otherwise add
   nothing (the defaults are rules, state, decisions, procedures, open, history,
   or `default_areas` of an existing `handoff.json`). Area names must be
   kebab-case.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" check`; it must print
   `structure valid`.
3. Tell the user, briefly: where the root is, which areas exist, that
   `handoff.json` holds the limits and the hook language (`"language": "it"` for
   Italian), and that `.lock` files should be git-ignored (suggest adding
   `.claude/handoff/**/.lock` to `.gitignore`; do not edit it without asking).
4. If the project already has notes that should live in the handoff (a notes
   folder, a long memory section in `CLAUDE.md`, an older handoff), mention
   `/claude-handoff:migrate` instead of copying them now.

Do not overwrite or rewrite any existing handoff file: `init` only creates what is
missing and regenerates the index tables of the folders it touched.
