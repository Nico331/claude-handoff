//! Read-only commands `list` and `stats`.
//!
//! `list` is what an agent uses at bootstrap (`list --max-rank 1`); `stats` measures
//! what that bootstrap costs against the whole structure and, optionally, against the
//! old flat handoff it replaced.

use crate::config::Config;
use crate::errors::{HandoffError, Result, USAGE};
use crate::py;
use crate::tree;
use std::fs;
use std::path::{Path, PathBuf};

/// Rough bytes-per-token estimate, used for the bootstrap cost.
pub const BYTES_PER_TOKEN: f64 = 3.5;

/// A level-3 entry as `list` prints it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Entry {
    /// Path relative to the root, with `/`.
    pub path: String,
    /// Declared rank.
    pub rank: u8,
    /// Declared summary.
    pub summary: String,
}

/// Level-3 entry files (INDEX.md excluded), in path order; code 2 when `area` does not
/// exist.
pub fn entry_files(root: &Path, area: Option<&str>) -> Result<Vec<PathBuf>> {
    let mut areas = tree::subdirs(root)?;
    if let Some(area) = area {
        areas.retain(|a| py::name_of(a) == area);
        if areas.is_empty() {
            return Err(HandoffError::new(format!("no such area: {area}"), USAGE));
        }
    }
    let mut files = Vec::new();
    for area in &areas {
        for topic in tree::subdirs(area)? {
            files.extend(tree::md_children(&topic)?);
        }
    }
    Ok(files)
}

/// Entries sorted by rank, then path, and warnings: an entry with invalid frontmatter
/// cannot be ranked, so it is left out and produces a warning instead.
pub fn list_entries(root: &Path, max_rank: Option<u64>, area: Option<&str>, cfg: &Config)
                    -> Result<(Vec<Entry>, Vec<String>)> {
    let mut entries = Vec::new();
    let mut warnings = Vec::new();
    let files = entry_files(root, area)?;
    tree::prefetch(&files);
    for path in files {
        let (meta, problems) = tree::parse_frontmatter(&tree::read_text(&path)?, cfg);
        let Some(meta) = meta else {
            warnings.push(format!("{}: invalid frontmatter, entry skipped ({})", py::show(&path),
                                  problems.first().map(String::as_str).unwrap_or("")));
            continue;
        };
        if max_rank.is_none_or(|max| u64::from(meta.rank) <= max) {
            entries.push(Entry { path: py::relative_posix(&path, root), rank: meta.rank,
                                 summary: meta.summary });
        }
    }
    entries.sort_by(|a, b| (a.rank, &a.path).cmp(&(b.rank, &b.path)));
    Ok((entries, warnings))
}

/// Sum of files, bytes and lines.
#[derive(Debug, Default, Clone, Copy)]
pub struct Tally {
    pub files: u64,
    pub size: u64,
    pub lines: u64,
}

impl Tally {
    /// Add one file to the sum.
    fn add(&mut self, path: &Path) -> Result<()> {
        let data = tree::read_bytes(path).map_err(|e| HandoffError::io(path, &e))?;
        self.files += 1;
        self.size += data.len() as u64;
        self.lines += py::bytes_line_count(&data) as u64;
        Ok(())
    }

    /// Estimated tokens: bytes / 3.5.
    pub fn tokens(&self) -> u64 {
        (self.size as f64 / BYTES_PER_TOKEN).round() as u64
    }
}

/// Measures of the structure.
#[derive(Debug, Default)]
pub struct Stats {
    /// Sums per level (1, 2, 3).
    pub levels: [Tally; 3],
    /// Sum of every `.md`.
    pub total: Tally,
    /// Root + area indexes + entries with rank <= the bootstrap rank.
    pub bootstrap: Tally,
    /// Bytes of the old handoff, `None` when not compared.
    pub legacy_size: Option<u64>,
}

/// Measure the structure and the bootstrap cost, and the old handoff when it exists.
pub fn compute_stats(root: &Path, legacy: Option<&Path>, cfg: &Config) -> Result<Stats> {
    let mut stats = Stats::default();
    let markdown = py::rglob(root, py::glob_md);
    tree::prefetch(&markdown);
    for path in markdown {
        let depth = path.parent().map_or(0, |p| p.strip_prefix(root).map_or(0, |r| r.components().count()));
        let level = (depth + 1).min(3);
        stats.levels[level - 1].add(&path)?;
        stats.total.add(&path)?;
        if level < 3 {
            stats.bootstrap.add(&path)?;
        }
    }
    let (entries, _) = list_entries(root, Some(cfg.bootstrap_max_rank), None, cfg)?;
    for entry in entries {
        let path: PathBuf = std::iter::once(root.as_os_str())
            .chain(Path::new(&entry.path).iter())
            .collect();
        stats.bootstrap.add(&path)?;
    }
    if let Some(legacy) = legacy.filter(|l| l.is_dir()) {
        let files = py::rglob(legacy, |_| true);
        stats.legacy_size = Some(files.iter().filter(|p| p.is_file())
            .map(|p| fs::metadata(p).map(|m| m.len()).unwrap_or(0)).sum());
    }
    Ok(stats)
}

/// Percentage with one decimal, `-` when the whole is zero.
fn percent(part: u64, whole: u64) -> String {
    if whole == 0 { "-".into() } else { format!("{:.1}%", 100.0 * part as f64 / whole as f64) }
}

/// Text lines of `stats`; `legacy` is the path as the user gave it.
pub fn format_stats(stats: &Stats, legacy: Option<&str>, cfg: &Config) -> Vec<String> {
    let mut lines: Vec<String> = stats.levels.iter().enumerate()
        .map(|(i, t)| format!("level {}: {} files, {} bytes, {} lines", i + 1, t.files, t.size, t.lines))
        .collect();
    let (total, boot) = (&stats.total, &stats.bootstrap);
    lines.push(format!("total: {} files, {} bytes, {} lines, ~{} tokens", total.files, total.size,
                       total.lines, total.tokens()));
    lines.push(format!("bootstrap (root + area indexes + rank <= {}): {} files, {} bytes, ~{} tokens, \
                        {} of the total", cfg.bootstrap_max_rank, boot.files, boot.size, boot.tokens(),
                       percent(boot.size, total.size)));
    if let Some(size) = stats.legacy_size {
        let tokens = (size as f64 / BYTES_PER_TOKEN).round() as u64;
        lines.push(format!("legacy handoff ({}): {size} bytes, ~{tokens} tokens; the bootstrap is {} of it",
                           legacy.unwrap_or(""), percent(boot.size, size)));
    }
    lines
}
