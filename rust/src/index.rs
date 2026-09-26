//! Generated index tables, `reindex` and `reindex --all`.
//!
//! The table sits between two markers and is regenerated from the children's
//! frontmatter; every line outside the markers is hand-written and never touched.
//! `reindex` also rewrites `rank` (minimum of the children) and `updated` (maximum of
//! the children) in the index's own frontmatter.

use crate::config::Config;
use crate::errors::{HandoffError, Result};
use crate::lock;
use crate::py;
use crate::timeutil::Date;
use crate::tree::{self, Child, INDEX_NAME};
use regex::Regex;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// Start marker of the generated table.
pub const START: &str = "<!-- handoff:index:start -->";

/// End marker of the generated table.
pub const END: &str = "<!-- handoff:index:end -->";

/// Header and separator of the table.
pub const HEADER: [&str; 2] = ["| rank | entry | summary | updated |", "|---|---|---|---|"];

fn link_pattern() -> &'static Regex {
    static LINK: OnceLock<Regex> = OnceLock::new();
    LINK.get_or_init(|| Regex::new(r"^\[(.*)\]\(([^()\s]+)\)$").expect("valid pattern"))
}

/// A row of an index table, as read from the file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Row {
    /// Text of the rank cell.
    pub rank: String,
    /// Link target of the entry cell (empty when the cell is not a link).
    pub link: String,
    /// Summary, unescaped.
    pub summary: String,
    /// Text of the updated cell.
    pub updated: String,
    /// The original line, for exact comparisons.
    pub line: String,
}

/// Make text safe inside a table cell: `|` becomes `\|`.
fn escape(text: &str) -> String {
    text.replace('|', "\\|")
}

/// Table row for a child: rank, title linked to the child, summary, date.
pub fn render_row(child: &Child) -> String {
    let title = escape(&child.meta.title).replace(']', "\\]");
    format!("| {} | [{title}]({}) | {} | {} |", child.meta.rank, child.link,
            escape(&child.meta.summary), child.meta.updated.iso())
}

/// Full table (header included) for children already sorted.
pub fn render_table(children: &[Child]) -> Vec<String> {
    HEADER.iter().map(|h| h.to_string()).chain(children.iter().map(render_row)).collect()
}

/// Positions of the markers, `None` when both are missing; an error when a marker is
/// repeated, only one is present, or the two are swapped.
fn marker_positions(lines: &[&str]) -> Result<Option<(usize, usize)>> {
    let starts: Vec<usize> = (0..lines.len()).filter(|&i| py::strip(lines[i]) == START).collect();
    let ends: Vec<usize> = (0..lines.len()).filter(|&i| py::strip(lines[i]) == END).collect();
    if starts.is_empty() && ends.is_empty() {
        return Ok(None);
    }
    if starts.len() != 1 || ends.len() != 1 || ends[0] < starts[0] {
        return Err(HandoffError::content("table markers repeated, unpaired or swapped"));
    }
    Ok(Some((starts[0], ends[0])))
}

/// Replace what lies between the markers with `table`; when the markers are missing
/// they are appended with the table.
pub fn splice(text: &str, table: &[String]) -> Result<String> {
    let lines: Vec<&str> = text.split('\n').collect();
    let Some((start, end)) = marker_positions(&lines)? else {
        let body = text.trim_end_matches('\n');
        return Ok(format!("{body}\n\n{START}\n{}\n{END}\n", table.join("\n")));
    };
    let mut out: Vec<&str> = lines[..=start].to_vec();
    out.extend(table.iter().map(String::as_str));
    out.extend_from_slice(&lines[end..]);
    Ok(out.join("\n"))
}

/// Split a row on `|` not preceded by a backslash.
fn split_cells(text: &str) -> Vec<&str> {
    let mut cells = Vec::new();
    let mut start = 0;
    let mut previous = None;
    for (i, c) in text.char_indices() {
        if c == '|' && previous != Some('\\') {
            cells.push(&text[start..i]);
            start = i + 1;
        }
        previous = Some(c);
    }
    cells.push(&text[start..]);
    cells
}

/// Data rows of the table between the markers (header and separator excluded);
/// `None` when the markers are missing.
pub fn parse_rows(text: &str) -> Result<Option<Vec<Row>>> {
    let lines: Vec<&str> = text.split('\n').collect();
    let Some((start, end)) = marker_positions(&lines)? else {
        return Ok(None);
    };
    let mut rows = Vec::new();
    for line in &lines[start + 1..end] {
        let stripped = py::strip(line);
        if stripped.is_empty() || HEADER.contains(&stripped) {
            continue;
        }
        let mut cells: Vec<String> =
            split_cells(stripped.trim_matches('|')).into_iter().map(|c| py::strip(c).to_string()).collect();
        while cells.len() < 4 {
            cells.push(String::new());
        }
        let link = link_pattern()
            .captures(&cells[1])
            .and_then(|m| m.get(2))
            .map(|m| m.as_str().to_string())
            .unwrap_or_default();
        rows.push(Row {
            rank: cells[0].clone(),
            link,
            summary: cells[2].replace("\\|", "|"),
            updated: cells[3].clone(),
            line: line.to_string(),
        });
    }
    Ok(Some(rows))
}

/// Rank (minimum) and date (maximum) the folder must carry; `None` when empty.
pub fn expected_folder_meta(children: &[Child]) -> Option<(u8, Date)> {
    let rank = children.iter().map(|c| c.meta.rank).min()?;
    let updated = children.iter().map(|c| c.meta.updated).max()?;
    Some((rank, updated))
}

/// Regenerate the index table of one folder and the folder's rank and date.
///
/// `owner` must hold the folder lock; `None` skips the check (`--no-lock`). Returns
/// true when INDEX.md changed; it is rewritten atomically, and only when it changes.
pub fn reindex(root: &Path, folder: &Path, owner: Option<&str>, now: Option<i64>, cfg: &Config)
               -> Result<bool> {
    if let Some(owner) = owner {
        lock::require_held(folder, owner, now)?;
    }
    let index = folder.join(INDEX_NAME);
    if !index.is_file() {
        return Err(HandoffError::content(format!("{}: index missing", py::show(&index))));
    }
    let (children, errors) = tree::read_children(root, folder, cfg)?;
    if !errors.is_empty() {
        return Err(HandoffError::content(errors.join("\n")));
    }
    let original = tree::read_text(&index)?;
    let mut text = splice(&original, &render_table(&children))
        .map_err(|e| HandoffError::content(format!("{}: {}", py::show(&index), e.message)))?;
    if tree::level_of(root, folder) >= 2 {
        let (_, problems) = tree::parse_frontmatter(&text, cfg);
        if !problems.is_empty() {
            let lines: Vec<String> = problems.iter().map(|p| format!("{}: {p}", py::show(&index))).collect();
            return Err(HandoffError::content(lines.join("\n")));
        }
    }
    if let Some((rank, updated)) = expected_folder_meta(&children) {
        if tree::split_frontmatter(&text).is_some() {
            text = tree::rewrite_frontmatter(&text, rank, updated)?;
        }
    }
    if text == original {
        return Ok(false);
    }
    tree::write_text_preserving(&index, &text)?;
    Ok(true)
}

/// Every folder of the handoff that has an index to maintain, deepest first: topics,
/// then areas, then the root; within a level, by path.
pub fn bottom_up(root: &Path) -> Result<Vec<PathBuf>> {
    let areas = tree::subdirs(root)?;
    let mut folders = Vec::new();
    for area in &areas {
        folders.extend(tree::subdirs(area)?);
    }
    folders.extend(areas);
    folders.push(root.to_path_buf());
    Ok(folders)
}

/// Reindex folders bottom-up, holding exactly one lock at a time.
///
/// For each folder: take its lock (never stealing), reindex, release; the release
/// happens even on failure. Returns the folders whose INDEX.md changed; the first
/// failure stops the walk, and folders processed before it keep their new index.
pub fn reindex_all(root: &Path, owner: &str, cfg: &Config, folders: Option<Vec<PathBuf>>,
                   mut report: impl FnMut(&Path, bool)) -> Result<Vec<PathBuf>> {
    let folders = match folders {
        Some(folders) => folders,
        None => bottom_up(root)?,
    };
    let mut changed = Vec::new();
    for folder in folders {
        lock::acquire(root, &folder, owner, cfg.lock_ttl_seconds as i64, false, None)?;
        let outcome = reindex(root, &folder, Some(owner), None, cfg);
        let released = lock::release(root, &folder, owner, false, None);
        let did_change = outcome?;
        released?;
        if did_change {
            changed.push(folder.clone());
        }
        report(&folder, did_change);
    }
    Ok(changed)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn splice_appends_or_replaces() {
        let table = vec!["| a |".to_string()];
        assert_eq!(splice("text\n\n", &table).unwrap(), format!("text\n\n{START}\n| a |\n{END}\n"));
        let text = format!("x\n{START}\nold\n{END}\ny");
        assert_eq!(splice(&text, &table).unwrap(), format!("x\n{START}\n| a |\n{END}\ny"));
        assert!(splice(&format!("{END}\n{START}"), &table).is_err());
        assert!(splice(&format!("{START}\n{START}\n{END}"), &table).is_err());
    }

    #[test]
    fn rows_are_parsed_like_python() {
        let text = format!("{START}\n{}\n{}\n| 2 | [T\\|x](a.md) | S \\| s | 2026-01-01 |\n\
                            | 3 | plain |\n\n{END}", HEADER[0], HEADER[1]);
        let rows = parse_rows(&text).unwrap().unwrap();
        assert_eq!(rows.len(), 2);
        assert_eq!((rows[0].rank.as_str(), rows[0].link.as_str()), ("2", "a.md"));
        assert_eq!((rows[0].summary.as_str(), rows[0].updated.as_str()), ("S | s", "2026-01-01"));
        assert_eq!((rows[1].link.as_str(), rows[1].summary.as_str()), ("", ""));
        assert!(parse_rows("no markers").unwrap().is_none());
    }
}
