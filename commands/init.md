---
description: Create the three-level handoff in this project (idempotent, never overwrites existing files)
argument-hint: "[comma-separated areas, e.g. rules,state,decisions] [language, e.g. it]"
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
   kebab-case. If they name a language for the handoff ("in Italian", `it`),
   append `--language <lowercase tag>`; otherwise the handoff is in English.
2. Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" check`; it must print
   `structure valid`.
3. Tell the user, briefly: where the root is, which areas exist, that
   `handoff.json` holds the limits and the language the handoff is written in
   (English unless chosen; `/claude-handoff:language <code>` changes it), and
   that `.lock` files should be git-ignored (suggest adding
   `.claude/handoff/**/.lock` to `.gitignore`; do not edit it without asking).
   If a language other than English was chosen, the index and seed text `init`
   just wrote is in English: offer to translate it, with the lock protocol.
4. If the project already has notes that should live in the handoff (a notes
   folder, a long memory section in `CLAUDE.md`, an older handoff), mention
   `/claude-handoff:migrate` instead of copying them now.

Do not overwrite or rewrite any existing handoff file: `init` only creates what is
missing and regenerates the index tables of the folders it touched.
