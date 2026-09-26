//! The `init` command: scaffold a handoff root, its areas and `handoff.json`.
//!
//! `init` is idempotent and never overwrites a file: it only creates what is missing.
//! Afterwards it regenerates the index tables of the folders it touched and of their
//! ancestors, bottom-up and one lock at a time; hand-written text in an existing index
//! is left alone (only the generated table and the folder's rank/date change).

use crate::config::{config_json, Config, CONFIG_NAME};
use crate::errors::{HandoffError, Result, USAGE};
use crate::index;
use crate::py;
use crate::timeutil::Date;
use crate::tree::{self, INDEX_NAME};
use std::fs;
use std::path::{Path, PathBuf};

/// Topic created in the first area of a brand-new handoff, explaining the handoff.
pub const SEED_TOPIC: &str = "handoff";

/// Summary of a suggested default area; other areas get a generic one.
fn area_summary(name: &str) -> String {
    match name {
        "rules" => "Rules every session must know beyond the project docs: preferences, things \
                    not to touch, conventions.".into(),
        "state" => "What exists today: components, numbers, environments, access, known gaps \
                    and discrepancies.".into(),
        "decisions" => "Decisions in force: what was decided, when, why, and where it is \
                        formalised.".into(),
        "procedures" => "How things are done here: exact commands and the traps that cost \
                         time.".into(),
        "open" => "Open items by topic; decisions awaited from the user at rank 1; closed items \
                   archived at rank 5.".into(),
        "history" => "Past context: previous systems, closed work, lessons learned.".into(),
        other => format!("Topics of the {other} area."),
    }
}

/// What `init` did.
#[derive(Debug, Default)]
pub struct InitResult {
    /// Files and folders created, in creation order.
    pub created: Vec<PathBuf>,
    /// Folders whose INDEX.md table changed.
    pub reindexed: Vec<PathBuf>,
}

/// Frontmatter block with the four keys.
fn frontmatter(title: &str, summary: &str, rank: u8, today: Date) -> String {
    format!("---\ntitle: {title}\nsummary: {summary}\nrank: {rank}\nupdated: {}\n---\n", today.iso())
}

/// Hand-written part of a new root INDEX.md.
pub fn root_index(today: Date) -> String {
    frontmatter("Project handoff",
                "Consolidated project memory in three levels; start here and read in rank order.",
                1, today) + "
# Project handoff

Current state of the project, not a diary. It holds what cannot be deduced from the
code and the docs, and links to them instead of copying them.

**Read at session start, in this order:** this file, then the `INDEX.md` of every
area, then every rank-1 entry (`handoff list --max-rank 1`), then, for today's
task, the rank <= 2 entries of the topics it touches; the rest on demand, starting
from the summaries in the indexes.

**Rank:** 1 critical (always read) - 2 current state and frequent procedures -
3 useful detail - 4 history - 5 archive. A folder is worth the minimum of what it
contains.

**Write:** `handoff lock <folder> --owner <name>`, edit, `reindex`, `unlock`,
then the same on the parent, up to the root. Never hold two locks at once. Then
`handoff check`. One fact in one place; never secrets; closed items go to rank 5
or are deleted, never struck through.
"
}

/// `str.capitalize()` for an ASCII kebab-case name with hyphens made spaces.
fn area_title(name: &str) -> String {
    let words = name.replace('-', " ");
    let mut chars = words.chars();
    match chars.next() {
        Some(first) => first.to_uppercase().chain(chars.flat_map(char::to_lowercase)).collect(),
        None => String::new(),
    }
}

/// Hand-written part of a new area INDEX.md.
pub fn area_index(name: &str, today: Date) -> String {
    let title = area_title(name);
    let summary = area_summary(name);
    frontmatter(&title, &summary, 3, today) + &format!("
# {title}

{summary}

Local rules of this area, if any, go here in a few hand-written lines.
")
}

/// The seed topic of a brand-new handoff: file name and content.
pub fn seed_files(today: Date) -> Vec<(&'static str, String)> {
    vec![
        (INDEX_NAME, frontmatter("The handoff itself",
            "How this handoff is read and written: bootstrap order, lock protocol, entry format.",
            1, today) + "
# The handoff itself

Rules of this handoff. The claude-handoff plugin injects a short version at every
session start; these entries are the reference.
"),
        ("how-to-use.md", frontmatter("How to read and update the handoff",
            "Read root, area indexes and rank-1 entries first; update in the same action with \
             one lock at a time; one fact in one place; no secrets.", 1, today) + "
# How to read and update the handoff

## Read (every session)

1. `INDEX.md` of the root;
2. `INDEX.md` of every area;
3. every rank-1 entry, wherever it is (`handoff list --max-rank 1`);
4. for today's task: the rank <= 2 entries of the topics it touches, then the rest
   on demand, starting from the summaries in the indexes.

## When to write

At every prompt and after every action: does this change a fact written here, or
add one a future session must know? If so, update it **in the same action**, not
at the end. Typical triggers: a user decision, a new rule, a component added or
removed, a procedure discovered, an open item opened or closed, a number changed.

## How to write

- Hold the lock of the folder of the file you edit:
  `handoff lock <area>/<topic> --owner <name>`.
- Edit the entry and its frontmatter (`updated` = today).
- `handoff reindex <area>/<topic> --owner <name>`, then `unlock`.
- Same three steps on `<area>`, then on `.` (the root).
- **Never hold two locks at once**; release the child before taking the parent.
- `handoff check` at the end must print `structure valid`.
- Exit code 3 from `lock` means someone else is writing: wait and retry. Break an
  expired lock only with `--steal-stale`, never by deleting the file.

## What to write

- One fact in **one place**; if it is already in a README, an ADR or the code,
  link to it instead.
- **Never secrets**: say where a credential lives and its state, never its value.
- A closed item is **not struck through**: lower it to rank 5 or delete it.
- Write in the language set in `handoff.json` (`handoff language` prints it),
  whatever the language of the conversation.
"),
        ("entry-format.md", frontmatter("Shape of folders, entries and indexes",
            "Three levels, at most 20 topics per area and 51 files per topic; frontmatter \
             title, summary, rank, updated; entries under 80 lines.", 2, today) + "
# Shape of folders, entries and indexes

- Level 1 (root): only `INDEX.md`, `handoff.json` and folders.
- Level 2 (area): only `INDEX.md` and at most 20 topic folders.
- Level 3 (topic): only `.md` files, at most 51 including `INDEX.md`.
- Names in kebab-case, ASCII, no spaces.
- **One entry, one topic**, under 80 lines (aim for 20-50). Longer: split it.

Every entry and every area/topic `INDEX.md` starts with:

```yaml
---
title: Short title
summary: One line, at most 160 characters, saying what is here.
rank: 2
updated: 2026-01-31
---
```

The table between the `handoff:index` markers of each `INDEX.md` is generated by
`handoff reindex`; never edit it by hand. A folder's rank is the minimum of its
children's; keep rank 1 short, it is the fixed cost of every session.
"),
    ]
}

/// Write `text` to `path` only if `path` does not exist yet.
fn create(path: &Path, text: &str, result: &mut InitResult) -> Result<()> {
    if path.exists() {
        return Ok(());
    }
    fs::write(path, text).map_err(|e| HandoffError::io(path, &e))?;
    result.created.push(path.to_path_buf());
    Ok(())
}

/// Create a folder if missing, recording it.
fn mkdir(path: &Path, result: &mut InitResult) -> Result<()> {
    if path.is_dir() {
        return Ok(());
    }
    fs::create_dir_all(path).map_err(|e| HandoffError::io(path, &e))?;
    result.created.push(path.to_path_buf());
    Ok(())
}

/// Scaffold a handoff (see the module documentation).
///
/// `areas` defaults to `cfg.default_areas`; `seed` creates the seed topic when the root
/// index is created now; `today` is the date written in new frontmatter. Code 2 when
/// an area name is not kebab-case, 3 when a folder to reindex is locked.
pub fn init(root: &Path, areas: Option<Vec<String>>, owner: &str, cfg: &Config, seed: bool,
            today: Date) -> Result<InitResult> {
    let names = areas.filter(|a| !a.is_empty()).unwrap_or_else(|| cfg.default_areas.clone());
    for name in &names {
        if !tree::is_kebab(name) {
            return Err(HandoffError::new(format!("area name is not kebab-case: {}", py::repr_str(name)),
                                         USAGE));
        }
    }
    let mut result = InitResult::default();
    mkdir(root, &mut result)?;
    let written = Config { default_areas: names.clone(), ..cfg.clone() };
    create(&root.join(CONFIG_NAME), &config_json(&written), &mut result)?;
    let fresh = !root.join(INDEX_NAME).exists();
    create(&root.join(INDEX_NAME), &root_index(today), &mut result)?;
    for name in &names {
        mkdir(&root.join(name), &mut result)?;
        create(&root.join(name).join(INDEX_NAME), &area_index(name, today), &mut result)?;
    }
    if fresh && seed {
        let topic = root.join(&names[0]).join(SEED_TOPIC);
        mkdir(&topic, &mut result)?;
        for (file_name, text) in seed_files(today) {
            create(&topic.join(file_name), &text, &mut result)?;
        }
    }
    let mut folders: Vec<PathBuf> = Vec::new();
    for path in &result.created {
        let touched = if path.is_dir() { path.as_path() } else { path.parent().unwrap_or(root) };
        for folder in touched.ancestors() {
            if (folder == root || py::is_below(folder, root)) && !folders.iter().any(|f| f == folder) {
                folders.push(folder.to_path_buf());
            }
        }
    }
    folders.sort_by_cached_key(|p| (std::cmp::Reverse(tree::level_of(root, p)), py::as_posix(p)));
    result.reindexed = index::reindex_all(root, owner, cfg, Some(folders), |_, _| {})?;
    Ok(result)
}
