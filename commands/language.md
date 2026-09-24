---
description: Show or set the language the project handoff is written in (default English)
argument-hint: "[language code, e.g. en, it, de, pt-br]"
disable-model-invocation: true
---

Show or change the language of the claude-handoff project memory: the language
entries, summaries and hand-written index text are written in.

1. Run, from the project directory (use `python` instead of `python3` on Windows
   if `python3` is not available):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT}/scripts/handoff.py" language $ARGUMENTS
   ```

   The user's arguments were: "$ARGUMENTS". Without arguments the command prints
   the current language (`en` when `handoff.json` does not set one). With a code
   it writes `language` into `<root>/handoff.json`, keeping every other key. The
   code is a lowercase language tag (`en`, `it`, `de`, `pt-br`); if the user wrote
   a language name ("Italian", "Deutsch"), use its tag instead.
2. Report the result in one line. On exit code 2, show the error: no handoff yet
   (suggest `/claude-handoff:init`), an invalid code, or an invalid
   `handoff.json` (suggest `/claude-handoff:check`); nothing was written.
3. After a change, tell the user:
   - from the next prompt, new and updated handoff content is written in the new
     language; the hook text itself is available in English and Italian, other
     languages get the English text;
   - **existing entries are not translated automatically.** Offer to translate
     them, and do it only if the user asks, with the lock protocol of the
     `handoff` skill (one lock at a time, `reindex` up to the root, then `check`),
     keeping file names, ranks and facts unchanged.
