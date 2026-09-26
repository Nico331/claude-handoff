//! The `check` command: validation of the whole structure.
//!
//! Every defect becomes a `path: message` line. The check never modifies anything and
//! does not stop at the first error: a structure with three defects yields three lines.

use crate::config::{config_problems, Config, CONFIG_NAME};
use crate::errors::Result;
use crate::index::{expected_folder_meta, parse_rows, render_row, Row};
use crate::lock;
use crate::py;
use crate::timeutil;
use crate::tree::{self, Child, INDEX_NAME, LOCK_LOG_NAME, LOCK_NAME};
use regex::Regex;
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

struct Patterns {
    fence: Regex,
    inline_code: Regex,
    inline_link: Regex,
    ref_link: Regex,
    scheme: Regex,
}

fn patterns() -> &'static Patterns {
    static PATTERNS: OnceLock<Patterns> = OnceLock::new();
    PATTERNS.get_or_init(|| Patterns {
        fence: Regex::new(r"^\s*(```|~~~)").expect("valid pattern"),
        inline_code: Regex::new(r"`+[^`]*`+").expect("valid pattern"),
        inline_link: Regex::new(r#"!?\[[^\]]*\]\(\s*(<[^>]*>|[^)\s]+)(?:\s+"[^"]*")?\s*\)"#)
            .expect("valid pattern"),
        ref_link: Regex::new(r"^\s{0,3}\[[^\]]+\]:\s*(<[^>]*>|\S+)").expect("valid pattern"),
        scheme: Regex::new(r"^[A-Za-z][A-Za-z0-9+.-]*:").expect("valid pattern"),
    })
}

/// Validate the structure under `root`: the errors, one per line, in path order;
/// empty when everything is valid. `now` is the instant used for lock expiry.
pub fn check(root: &Path, now: Option<i64>, cfg: &Config) -> Result<Vec<String>> {
    if !root.is_dir() {
        return Ok(vec![format!("{}: root does not exist (run init)", py::show(root))]);
    }
    let now = now.unwrap_or_else(timeutil::now_seconds);
    let markdown = py::rglob(root, py::glob_md);
    tree::prefetch(&markdown);
    let config = py::show(&root.join(CONFIG_NAME));
    let mut errors: Vec<String> = config_problems(root).iter().map(|p| format!("{config}: {p}")).collect();
    check_folder(root, root, &mut errors, cfg)?;
    let mut exists = HashMap::new();
    for path in markdown {
        errors.extend(check_links(&path, &mut exists)?);
    }
    for (folder, info) in lock::find_locks(root)? {
        if info.expired(now) {
            errors.push(format!("{}: expired lock, {}", py::show(&folder.join(LOCK_NAME)),
                                lock::describe(&info, now)));
        }
    }
    Ok(errors)
}

/// Check one folder and recurse into the allowed sub-folders.
fn check_folder(root: &Path, folder: &Path, errors: &mut Vec<String>, cfg: &Config) -> Result<()> {
    let level = tree::level_of(root, folder);
    let items = py::scan_dir(folder).map_err(|e| crate::errors::HandoffError::io(folder, &e))?;
    let dirs: Vec<&PathBuf> = items.iter().filter(|i| i.is_dir).map(|i| &i.path).collect();
    let files: Vec<&PathBuf> = items.iter().filter(|i| i.is_file).map(|i| &i.path).collect();
    let mut allowed = vec![LOCK_NAME, lock::STEAL_NAME, INDEX_NAME];
    if level == 1 {
        allowed.extend([LOCK_LOG_NAME, CONFIG_NAME]);
    }
    let index = folder.join(INDEX_NAME);
    if !index.is_file() {
        errors.push(format!("{}: index missing", py::show(&index)));
    }
    for path in &files {
        let name = py::name_of(path);
        if allowed.contains(&name.as_str()) {
            continue;
        }
        if level == 3 && py::suffix(&name) == ".md" {
            if !tree::is_kebab(py::stem(&name)) {
                errors.push(format!("{}: name is not kebab-case", py::show(path)));
            }
            continue;
        }
        let place = if level == 3 { "only .md files".to_string() } else { format!("only {INDEX_NAME} and folders") };
        errors.push(format!("{}: file not allowed at level {level} ({place})", py::show(path)));
    }
    if level == 3 {
        errors.extend(dirs.iter().map(|d| format!("{}: folder not allowed at level 3", py::show(d))));
        let md_count = files.iter().filter(|p| py::suffix(&py::name_of(p)) == ".md").count() as u64;
        if md_count > cfg.max_entry_files {
            errors.push(format!("{}: {md_count} .md files (max {})", py::show(folder), cfg.max_entry_files));
        }
        for path in &files {
            let name = py::name_of(path);
            if py::suffix(&name) == ".md" && name != INDEX_NAME {
                check_entry(path, errors, cfg)?;
            }
        }
    } else {
        if level == 2 && dirs.len() as u64 > cfg.max_topics {
            errors.push(format!("{}: {} topics (max {})", py::show(folder), dirs.len(), cfg.max_topics));
        }
        for sub in &dirs {
            if !tree::is_kebab(&py::name_of(sub)) {
                errors.push(format!("{}: name is not kebab-case", py::show(sub)));
                continue;
            }
            check_folder(root, sub, errors, cfg)?;
        }
    }
    if index.is_file() {
        check_index(root, folder, level, errors, cfg)?;
    }
    Ok(())
}

/// Frontmatter and length of a level-3 entry.
fn check_entry(path: &Path, errors: &mut Vec<String>, cfg: &Config) -> Result<()> {
    let text = tree::read_text(path)?;
    let (_, problems) = tree::parse_frontmatter(&text, cfg);
    errors.extend(problems.iter().map(|p| format!("{}: {p}", py::show(path))));
    let lines = py::splitlines(&text).len() as u64;
    if lines > cfg.max_entry_lines {
        errors.push(format!("{}: {lines} lines (max {}); split it", py::show(path), cfg.max_entry_lines));
    }
    Ok(())
}

/// Frontmatter, table and rank of one INDEX.md.
fn check_index(root: &Path, folder: &Path, level: usize, errors: &mut Vec<String>, cfg: &Config)
               -> Result<()> {
    let index = folder.join(INDEX_NAME);
    let shown = py::show(&index);
    let text = tree::read_text(&index)?;
    let (meta, problems) = tree::parse_frontmatter(&text, cfg);
    if level >= 2 || tree::split_frontmatter(&text).is_some() {
        // The root may go without frontmatter.
        errors.extend(problems.iter().map(|p| format!("{shown}: {p}")));
    }
    let (children, _) = tree::read_children(root, folder, cfg)?;
    match parse_rows(&text) {
        Err(err) => {
            errors.push(format!("{shown}: {}", err.message));
            return Ok(());
        }
        Ok(None) => errors.push(format!("{shown}: table markers missing (run reindex)")),
        Ok(Some(rows)) => compare_rows(&shown, folder, root, &rows, &children, errors)?,
    }
    if let (Some(meta), Some((rank, updated))) = (meta, expected_folder_meta(&children)) {
        if meta.rank != rank {
            errors.push(format!("{shown}: folder rank {}, expected {rank} (minimum of the children)",
                                meta.rank));
        }
        if meta.updated != updated {
            errors.push(format!("{shown}: updated {}, expected {updated} (most recent child)",
                                meta.updated));
        }
    }
    Ok(())
}

/// Compare the table rows with the real children: missing children, ghost or
/// duplicate rows, wrong order, and rows whose rank, summary, date or title differ from
/// the child's frontmatter.
fn compare_rows(index: &str, folder: &Path, root: &Path, rows: &[Row], children: &[Child],
                errors: &mut Vec<String>) -> Result<()> {
    let known: Vec<String> = tree::child_sources(root, folder)?.into_iter().map(|(_, link, _)| link).collect();
    let mut seen: Vec<&str> = Vec::new();
    for row in rows {
        if !known.contains(&row.link) {
            let label = if row.link.is_empty() { py::repr_str(py::strip(&row.line)) } else { row.link.clone() };
            errors.push(format!("{index}: ghost row in the table: {label}"));
            continue;
        }
        if seen.contains(&row.link.as_str()) {
            errors.push(format!("{index}: repeated row in the table: {}", row.link));
            continue;
        }
        seen.push(&row.link);
        if let Some(child) = children.iter().find(|c| c.link == row.link) {
            compare_row(index, row, child, errors);
        }
    }
    errors.extend(children.iter().filter(|c| !seen.contains(&c.link.as_str()))
        .map(|c| format!("{index}: entry missing from the table: {}", c.link)));
    let listed: Vec<&str> = seen.iter().copied().filter(|l| children.iter().any(|c| c.link == *l)).collect();
    let wanted: Vec<&str> = children.iter().map(|c| c.link.as_str()).filter(|l| listed.contains(l)).collect();
    if listed != wanted {
        errors.push(format!("{index}: wrong table order (by rank, then name): expected {}",
                            wanted.join(", ")));
    }
    Ok(())
}

/// Differences between one table row and the child's frontmatter.
fn compare_row(index: &str, row: &Row, child: &Child, errors: &mut Vec<String>) {
    if py::strip(&row.line) == render_row(child) {
        return;
    }
    let meta = &child.meta;
    let before = errors.len();
    if row.summary != meta.summary {
        errors.push(format!("{index}: summary of {} differs from the file", child.link));
    }
    if row.rank != meta.rank.to_string() {
        errors.push(format!("{index}: rank of {} is {}, in the file {}", child.link, row.rank, meta.rank));
    }
    if row.updated != meta.updated.iso() {
        errors.push(format!("{index}: date of {} is {}, in the file {}", child.link, row.updated,
                            meta.updated));
    }
    if errors.len() == before {
        errors.push(format!("{index}: row of {} differs from the generated one (title or format)",
                            child.link));
    }
}

/// Targets of the Markdown links in the text (inline links, images and reference
/// definitions), code blocks and spans excluded, in order.
pub fn link_targets(text: &str) -> Vec<String> {
    let p = patterns();
    let mut targets = Vec::new();
    let mut fenced = false;
    for line in text.split('\n') {
        // The cheap substring tests skip the patterns on lines they cannot match.
        if (line.contains("```") || line.contains("~~~")) && p.fence.is_match(line) {
            fenced = !fenced;
            continue;
        }
        if fenced {
            continue;
        }
        let line = if line.contains('`') { p.inline_code.replace_all(line, "") } else { line.into() };
        if line.contains("](") {
            targets.extend(p.inline_link.captures_iter(&line).filter_map(|m| m.get(1))
                .map(|m| m.as_str().to_string()));
        }
        if line.contains("]:") {
            if let Some(target) = p.ref_link.captures(&line).and_then(|m| m.get(1)) {
                targets.push(target.as_str().to_string());
            }
        }
    }
    targets
}

/// `urllib.parse.unquote`: `%XX` sequences decoded as UTF-8, invalid bytes replaced.
fn percent_decode(text: &str) -> String {
    let bytes = text.as_bytes();
    let mut out = Vec::with_capacity(bytes.len());
    let mut i = 0;
    while i < bytes.len() {
        if bytes[i] == b'%' && i + 2 < bytes.len() {
            let hex = std::str::from_utf8(&bytes[i + 1..i + 3]).ok().and_then(|h| u8::from_str_radix(h, 16).ok());
            if let Some(byte) = hex {
                out.push(byte);
                i += 3;
                continue;
            }
        }
        out.push(bytes[i]);
        i += 1;
    }
    String::from_utf8_lossy(&out).into_owned()
}

/// Relative links of the file that do not resolve on disk.
///
/// URLs with a scheme (`https:`, `mailto:`), pure anchors (`#x`) and absolute paths are
/// ignored; fragment and query are dropped before resolving. Links leaving the handoff
/// towards the rest of the repository are checked like any other. `exists` remembers
/// the targets already looked up: many links share a target, and the disk does not
/// change during a check.
fn check_links(path: &Path, exists: &mut HashMap<PathBuf, bool>) -> Result<Vec<String>> {
    if !path.is_file() {
        return Ok(Vec::new());
    }
    let mut errors = Vec::new();
    let folder = path.parent().unwrap_or(Path::new("."));
    for raw in link_targets(&tree::read_text(path)?) {
        let target = if raw.len() >= 2 && raw.starts_with('<') && raw.ends_with('>') { &raw[1..raw.len() - 1] } else { raw.as_str() };
        if target.is_empty() || target.starts_with(['#', '/']) || patterns().scheme.is_match(target) {
            continue;
        }
        let without_fragment = target.split('#').next().unwrap_or("");
        let relative = percent_decode(without_fragment.split('?').next().unwrap_or(""));
        if relative.is_empty() {
            continue;
        }
        let target = folder.join(&relative);
        let found = match exists.get(&target) {
            Some(found) => *found,
            None => {
                let found = target.exists();
                exists.insert(target, found);
                found
            }
        };
        if !found {
            errors.push(format!("{}: broken link: {raw}", py::show(path)));
        }
    }
    Ok(errors)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn link_targets_skip_code() {
        let text = "a [x](one.md) `[y](no.md)` ![i](<two words.png> \"t\")\n```\n[z](no2.md)\n```\n\
                    [ref]: three.md\n";
        assert_eq!(link_targets(text), vec!["one.md", "<two words.png>", "three.md"]);
    }

    #[test]
    fn percent_decoding() {
        assert_eq!(percent_decode("a%20b%zz%"), "a b%zz%");
        assert_eq!(percent_decode("caf%C3%A9"), "café");
    }
}
