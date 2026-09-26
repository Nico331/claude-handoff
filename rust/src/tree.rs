//! Model of the three-level handoff: names, frontmatter and the children of a folder.
//!
//! Frontmatter is parsed by hand: one `key: value` per line between two `---` lines,
//! with the four keys `title`, `summary`, `rank` and `updated`. Nothing here writes to
//! disk except `write_text_preserving`, used by `reindex`.

use crate::config::Config;
use crate::errors::{HandoffError, Result, USAGE};
use crate::py;
use crate::timeutil::Date;
use std::collections::HashMap;
use std::fs;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// Name of the index, the only `.md` file allowed at levels 1 and 2.
pub const INDEX_NAME: &str = "INDEX.md";

/// Name of the lock file inside the folder being modified.
pub const LOCK_NAME: &str = ".lock";

/// Append-only log of stolen and force-released locks, at the root.
pub const LOCK_LOG_NAME: &str = ".lock-log";

/// Frontmatter keys: all required, and the only ones allowed.
pub const KEYS: [&str; 4] = ["title", "summary", "rank", "updated"];

/// kebab-case name: ASCII lowercase letters and digits separated by single hyphens.
pub fn is_kebab(name: &str) -> bool {
    !name.is_empty()
        && name.split('-').all(|part| {
            !part.is_empty() && part.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit())
        })
}

/// Valid frontmatter of an entry or an index.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Meta {
    /// Short title, not empty.
    pub title: String,
    /// One line, 1 to `max_summary` characters.
    pub summary: String,
    /// Importance, 1 to 5 (1 = critical).
    pub rank: u8,
    /// Date of the last update.
    pub updated: Date,
}

/// A child of a folder as it appears in the folder's index table.
#[derive(Debug, Clone)]
pub struct Child {
    /// Sort name (file stem or sub-folder name).
    pub name: String,
    /// Relative link target (`entry.md` or `topic/INDEX.md`).
    pub link: String,
    /// Absolute path of the file carrying the frontmatter.
    #[allow(dead_code)]
    pub path: PathBuf,
    /// Frontmatter of the child.
    pub meta: Meta,
}

/// Files read ahead by a read-only command; `read_bytes` and `read_text` serve them.
static PREFETCHED: OnceLock<HashMap<PathBuf, Vec<u8>>> = OnceLock::new();

/// Read `paths` in parallel and keep their bytes for the rest of the process. Only for
/// commands that never write: a prefetched file is not read from disk again.
pub fn prefetch(paths: &[PathBuf]) {
    if PREFETCHED.get().is_none() {
        let _ = PREFETCHED.set(py::read_parallel(paths));
    }
}

/// Bytes of a file, from the prefetched ones when available.
pub fn read_bytes(path: &Path) -> std::io::Result<Vec<u8>> {
    match PREFETCHED.get().and_then(|files| files.get(path)) {
        Some(bytes) => Ok(bytes.clone()),
        None => fs::read(path),
    }
}

/// Read a UTF-8 file, normalising line endings to `\n`.
pub fn read_text(path: &Path) -> Result<String> {
    let bytes = read_bytes(path).map_err(|e| HandoffError::io(path, &e))?;
    let text = String::from_utf8(bytes).map_err(|e| {
        HandoffError::content(format!("{}: not UTF-8 text ({e})", py::show(path)))
    })?;
    Ok(if text.contains('\r') { text.replace("\r\n", "\n") } else { text })
}

/// Write `data` to a new temporary file in `folder` and move it over `path`.
pub fn write_atomic(path: &Path, data: &[u8], prefix: &str, suffix: &str) -> std::io::Result<()> {
    let folder = path.parent().unwrap_or(Path::new("."));
    let temp = loop {
        let candidate = folder.join(format!("{prefix}{}{suffix}", &py::unique_hex()[..8]));
        match fs::OpenOptions::new().write(true).create_new(true).open(&candidate) {
            Ok(mut file) => {
                let written = file.write_all(data);
                drop(file);
                if let Err(err) = written {
                    let _ = fs::remove_file(&candidate);
                    return Err(err);
                }
                break candidate;
            }
            Err(err) if err.kind() == std::io::ErrorKind::AlreadyExists => continue,
            Err(err) => return Err(err),
        }
    };
    fs::rename(&temp, path).inspect_err(|_| {
        let _ = fs::remove_file(&temp);
    })
}

/// Atomically write `text`, keeping the line-ending style of the existing file (an
/// existing CRLF file stays CRLF).
pub fn write_text_preserving(path: &Path, text: &str) -> Result<()> {
    let crlf = path.exists()
        && fs::read(path).map(|b| b.windows(2).any(|w| w == b"\r\n")).unwrap_or(false);
    let data = if crlf { text.replace('\n', "\r\n") } else { text.to_string() };
    write_atomic(path, data.as_bytes(), ".tmp-", ".md").map_err(|e| HandoffError::io(path, &e))
}

/// Inner lines of the frontmatter block and the index of its closing line; `None` when
/// the text does not start with `---` or the block is not closed.
pub fn split_frontmatter(text: &str) -> Option<(Vec<&str>, usize)> {
    let lines: Vec<&str> = text.split('\n').collect();
    if py::strip(lines[0]) != "---" {
        return None;
    }
    (1..lines.len())
        .find(|&i| py::strip(lines[i]) == "---")
        .map(|close| (lines[1..close].to_vec(), close))
}

/// Strip one pair of matching single or double quotes around a value.
pub fn unquote(value: &str) -> &str {
    let bytes = value.as_bytes();
    if bytes.len() >= 2 && bytes[0] == bytes[bytes.len() - 1] && matches!(bytes[0], b'"' | b'\'') {
        &value[1..value.len() - 1]
    } else {
        value
    }
}

/// Read and validate the frontmatter: `(Some(meta), [])` or `(None, problems)`.
pub fn parse_frontmatter(text: &str, cfg: &Config) -> (Option<Meta>, Vec<String>) {
    let Some((inner, _)) = split_frontmatter(text) else {
        return (None, vec!["frontmatter missing or not closed by '---'".into()]);
    };
    let mut raw: Vec<(&str, String)> = Vec::new();
    let mut errors = Vec::new();
    for line in inner {
        if py::strip(line).is_empty() {
            continue;
        }
        let Some((key, value)) = line.split_once(':') else {
            errors.push(format!("frontmatter line without ':': {}", py::repr_str(py::strip(line))));
            continue;
        };
        let key = py::strip(key);
        if !KEYS.contains(&key) {
            errors.push(format!("unknown frontmatter key: {}", py::repr_str(key)));
        } else if raw.iter().any(|(k, _)| *k == key) {
            errors.push(format!("repeated frontmatter key: {}", py::repr_str(key)));
        } else {
            raw.push((key, unquote(py::strip(value)).to_string()));
        }
    }
    for key in KEYS {
        if !raw.iter().any(|(k, _)| *k == key) {
            errors.push(format!("missing frontmatter key: {}", py::repr_str(key)));
        }
    }
    if !errors.is_empty() {
        return (None, errors);
    }
    let get = |key: &str| raw.iter().find(|(k, _)| *k == key).map(|(_, v)| v.clone()).unwrap_or_default();
    validate_meta(get("title"), get("summary"), get("rank"), get("updated"), cfg)
}

/// Convert and check the four raw frontmatter values.
fn validate_meta(title: String, summary: String, rank: String, updated: String, cfg: &Config)
                 -> (Option<Meta>, Vec<String>) {
    let mut errors = Vec::new();
    if title.is_empty() {
        errors.push("empty title".to_string());
    }
    let length = summary.chars().count() as u64;
    if summary.is_empty() {
        errors.push("empty summary".into());
    } else if length > cfg.max_summary {
        errors.push(format!("summary of {length} characters (max {})", cfg.max_summary));
    }
    let number = if !rank.is_empty() && rank.bytes().all(|b| b.is_ascii_digit()) {
        rank.parse::<u64>().ok().filter(|n| (1..=5).contains(n))
    } else {
        None
    };
    if number.is_none() {
        errors.push(format!("invalid rank: {} (allowed 1-5)", py::repr_str(&rank)));
    }
    let date = Date::parse(&updated);
    if date.is_none() {
        errors.push(format!("updated is not an ISO date: {}", py::repr_str(&updated)));
    }
    if !errors.is_empty() {
        return (None, errors);
    }
    let meta = Meta {
        title,
        summary,
        rank: number.unwrap_or(1) as u8,
        updated: date.unwrap_or(Date::MIN),
    };
    (Some(meta), Vec::new())
}

/// Rewrite only the `rank` and `updated` values of the frontmatter.
pub fn rewrite_frontmatter(text: &str, rank: u8, updated: Date) -> Result<String> {
    let Some((_, close)) = split_frontmatter(text) else {
        return Err(HandoffError::content("frontmatter missing: cannot update rank and date"));
    };
    let mut lines: Vec<String> = text.split('\n').map(str::to_string).collect();
    for line in lines.iter_mut().take(close).skip(1) {
        let key = py::strip(line.split(':').next().unwrap_or("")).to_string();
        if key == "rank" {
            *line = format!("rank: {rank}");
        } else if key == "updated" {
            *line = format!("updated: {}", updated.iso());
        }
    }
    Ok(lines.join("\n"))
}

/// Level of a folder: 1 the root, 2 an area, 3 a topic (deeper is > 3).
pub fn level_of(root: &Path, folder: &Path) -> usize {
    folder.strip_prefix(root).map(|rel| rel.components().count()).unwrap_or(0) + 1
}

/// Turn the `<folder>` command-line argument into an absolute path (code 2 when
/// missing, not a folder, outside the root, or deeper than level 3).
pub fn resolve_folder(root: &Path, name: &str) -> Result<PathBuf> {
    let root = py::resolve(root);
    let candidate = Path::new(name);
    let folder = py::resolve(&if candidate.is_absolute() { candidate.to_path_buf() } else { root.join(candidate) });
    if folder != root && !py::is_below(&folder, &root) {
        return Err(HandoffError::new(format!("{name}: outside the root {}", py::show(&root)), USAGE));
    }
    if !folder.is_dir() {
        return Err(HandoffError::new(format!("{name}: no such folder"), USAGE));
    }
    if level_of(&root, &folder) > 3 {
        return Err(HandoffError::new(format!("{name}: deeper than level 3"), USAGE));
    }
    Ok(folder)
}

/// Non-hidden sub-folders, sorted by name.
pub fn subdirs(folder: &Path) -> Result<Vec<PathBuf>> {
    let items = py::scan_dir(folder).map_err(|e| HandoffError::io(folder, &e))?;
    Ok(items
        .into_iter()
        .filter(|item| item.is_dir && !py::name_of(&item.path).starts_with('.'))
        .map(|item| item.path)
        .collect())
}

/// Entries of a folder matching `*.md` other than INDEX.md, sorted.
pub fn md_children(folder: &Path) -> Result<Vec<PathBuf>> {
    let entries = py::list_dir(folder).map_err(|e| HandoffError::io(folder, &e))?;
    Ok(entries
        .into_iter()
        .filter(|p| {
            let name = py::name_of(p);
            py::glob_md(&name) && name != INDEX_NAME
        })
        .collect())
}

/// Children a folder's index must list, without reading them: sub-folders (through
/// their INDEX.md) at levels 1 and 2, `.md` files other than INDEX.md at level 3.
///
/// Returns `(name, relative link, file carrying the frontmatter)` triples, by name.
pub fn child_sources(root: &Path, folder: &Path) -> Result<Vec<(String, String, PathBuf)>> {
    if level_of(root, folder) >= 3 {
        return Ok(md_children(folder)?
            .into_iter()
            .map(|p| {
                let name = py::name_of(&p);
                (py::stem(&name).to_string(), name, p)
            })
            .collect());
    }
    Ok(subdirs(folder)?
        .into_iter()
        .map(|d| {
            let name = py::name_of(&d);
            let link = format!("{name}/{INDEX_NAME}");
            let index = d.join(INDEX_NAME);
            (name, link, index)
        })
        .collect())
}

/// Children of a folder with their frontmatter: `(valid children sorted by rank then
/// name, errors)`; a child without an index or with invalid frontmatter only appears
/// among the errors.
pub fn read_children(root: &Path, folder: &Path, cfg: &Config) -> Result<(Vec<Child>, Vec<String>)> {
    let mut children = Vec::new();
    let mut errors = Vec::new();
    for (name, link, path) in child_sources(root, folder)? {
        if !path.is_file() {
            errors.push(format!("{}: index missing", py::show(&path)));
            continue;
        }
        let (meta, problems) = parse_frontmatter(&read_text(&path)?, cfg);
        match meta {
            Some(meta) => children.push(Child { name, link, path, meta }),
            None => errors.extend(problems.iter().map(|p| format!("{}: {p}", py::show(&path)))),
        }
    }
    children.sort_by(|a, b| (a.meta.rank, &a.name).cmp(&(b.meta.rank, &b.name)));
    Ok((children, errors))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn fm(rank: &str, updated: &str) -> String {
        format!("---\ntitle: T\nsummary: S\nrank: {rank}\nupdated: {updated}\n---\nbody\n")
    }

    #[test]
    fn kebab_names() {
        assert!(is_kebab("a-1-b"));
        for bad in ["", "A", "a--b", "-a", "a-", "a_b", "caffè"] {
            assert!(!is_kebab(bad), "{bad}");
        }
    }

    #[test]
    fn frontmatter_valid_and_quoted() {
        let text = "---\ntitle: \"Quoted\"\nsummary: 'One: two'\nrank: 01\nupdated: 2026-09-23\n---\n";
        let (meta, problems) = parse_frontmatter(text, &Config::default());
        assert!(problems.is_empty());
        let meta = meta.unwrap();
        assert_eq!((meta.title.as_str(), meta.summary.as_str(), meta.rank), ("Quoted", "One: two", 1));
    }

    #[test]
    fn frontmatter_problems_match_python() {
        let cfg = Config::default();
        assert_eq!(parse_frontmatter("no", &cfg).1, vec!["frontmatter missing or not closed by '---'"]);
        assert_eq!(parse_frontmatter("---\ntitle: a\n", &cfg).1,
                   vec!["frontmatter missing or not closed by '---'"]);
        let text = "---\ntitle: a\nbogus\nextra: 1\ntitle: b\n---\n";
        assert_eq!(parse_frontmatter(text, &cfg).1, vec![
            "frontmatter line without ':': 'bogus'", "unknown frontmatter key: 'extra'",
            "repeated frontmatter key: 'title'", "missing frontmatter key: 'summary'",
            "missing frontmatter key: 'rank'", "missing frontmatter key: 'updated'"]);
        assert_eq!(parse_frontmatter(&fm("6", "2026-02-30"), &cfg).1, vec![
            "invalid rank: '6' (allowed 1-5)", "updated is not an ISO date: '2026-02-30'"]);
        let long = format!("---\ntitle: t\nsummary: {}\nrank: 1\nupdated: 2026-01-01\n---\n", "é".repeat(161));
        assert_eq!(parse_frontmatter(&long, &cfg).1, vec!["summary of 161 characters (max 160)"]);
    }

    #[test]
    fn rewrite_touches_only_rank_and_date() {
        let text = fm("3", "2020-01-01");
        let out = rewrite_frontmatter(&text, 1, Date::parse("2026-09-23").unwrap()).unwrap();
        assert_eq!(out, "---\ntitle: T\nsummary: S\nrank: 1\nupdated: 2026-09-23\n---\nbody\n");
        assert!(rewrite_frontmatter("plain", 1, Date::MIN).is_err());
    }
}
