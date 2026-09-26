//! Configuration of a handoff: where its root is and which limits apply.
//!
//! The configuration lives in `<root>/handoff.json` and is optional: every key has a
//! default. Because the file lives *inside* the root, the root itself is found first,
//! in this order:
//!
//! 1. the `--root` command-line option;
//! 2. the `CLAUDE_HANDOFF_ROOT` environment variable;
//! 3. the `root` key of the optional project pointer `<project>/.claude/handoff.json`;
//! 4. the default `<project>/.claude/handoff`.

use crate::errors::{HandoffError, Result, USAGE};
use crate::py;
use regex::Regex;
use serde_json::{Map, Value};
use std::path::{Path, PathBuf};
use std::sync::OnceLock;

/// Name of the configuration file at the handoff root.
pub const CONFIG_NAME: &str = "handoff.json";

/// Environment variable that overrides the root.
pub const ENV_ROOT: &str = "CLAUDE_HANDOFF_ROOT";

/// Area names `init` creates when neither `--areas` nor the config says otherwise.
pub const DEFAULT_AREAS: [&str; 6] = ["rules", "state", "decisions", "procedures", "open", "history"];

/// Language of the handoff content when `handoff.json` does not set one.
pub const DEFAULT_LANGUAGE: &str = "en";

/// Keys of `handoff.json`, in the order `init` writes them.
const FIELDS: [&str; 10] = ["max_topics", "max_entry_files", "max_entry_lines", "max_summary",
    "bootstrap_max_rank", "lock_ttl_seconds", "language", "inject_summaries", "default_areas",
    "legacy"];

/// Keys that must hold an integer > 0.
const POSITIVE_INTS: [&str; 5] =
    ["max_topics", "max_entry_files", "max_entry_lines", "max_summary", "lock_ttl_seconds"];

/// Validated configuration of one handoff.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Config {
    /// Most topic folders in one area (level 2).
    pub max_topics: u64,
    /// Most `.md` files in one topic, its INDEX.md included.
    pub max_entry_files: u64,
    /// Most lines in one entry.
    pub max_entry_lines: u64,
    /// Most characters in a `summary`.
    pub max_summary: u64,
    /// Entries with rank <= this are read at every session start.
    pub bootstrap_max_rank: u64,
    /// Default lifetime of a lock.
    pub lock_ttl_seconds: u64,
    /// Language the handoff content is written in; also selects the hook text.
    pub language: String,
    /// Whether the SessionStart hook lists the bootstrap entries.
    pub inject_summaries: bool,
    /// Areas `init` creates by default.
    pub default_areas: Vec<String>,
    /// Old flat handoff for `stats` to compare with, relative to the root.
    pub legacy: Option<String>,
}

impl Default for Config {
    fn default() -> Self {
        Config {
            max_topics: 20,
            max_entry_files: 51,
            max_entry_lines: 80,
            max_summary: 160,
            bootstrap_max_rank: 1,
            lock_ttl_seconds: 900,
            language: DEFAULT_LANGUAGE.into(),
            inject_summaries: true,
            default_areas: DEFAULT_AREAS.iter().map(|a| a.to_string()).collect(),
            legacy: None,
        }
    }
}

fn language_tag() -> &'static Regex {
    static TAG: OnceLock<Regex> = OnceLock::new();
    TAG.get_or_init(|| Regex::new(r"^[a-z]{2,3}(?:-[a-z0-9]{2,8})*$").expect("valid pattern"))
}

/// Why `value` is not an acceptable language tag; `None` for `en`, `it`, `pt-br`...
pub fn language_problem(value: &Value) -> Option<String> {
    if let Value::String(tag) = value {
        if language_tag().is_match(tag) {
            return None;
        }
    }
    Some(format!("language must be a lowercase language tag such as en, it, de or pt-br \
                  (got {})", py::repr_value(value)))
}

/// `language_problem` for a tag typed on the command line.
pub fn language_problem_str(tag: &str) -> Option<String> {
    language_problem(&Value::String(tag.to_string()))
}

/// A JSON integer > 0 (booleans and floats excluded).
fn positive(value: &Value) -> Option<u64> {
    value.as_u64().filter(|n| *n > 0)
}

/// Turn the decoded JSON of `handoff.json` into a `Config`, or list its problems.
pub fn validate(data: &Value) -> std::result::Result<Config, Vec<String>> {
    let Value::Object(map) = data else {
        return Err(vec!["must be a JSON object".into()]);
    };
    let mut unknown: Vec<&String> = map.keys().filter(|k| !FIELDS.contains(&k.as_str())).collect();
    unknown.sort();
    let mut problems: Vec<String> =
        unknown.iter().map(|k| format!("unknown key {}", py::repr_str(k))).collect();
    let mut config = Config::default();
    for key in POSITIVE_INTS {
        if let Some(value) = map.get(key) {
            match positive(value) {
                Some(number) => match key {
                    "max_topics" => config.max_topics = number,
                    "max_entry_files" => config.max_entry_files = number,
                    "max_entry_lines" => config.max_entry_lines = number,
                    "max_summary" => config.max_summary = number,
                    _ => config.lock_ttl_seconds = number,
                },
                None => problems.push(format!("{key} must be a positive integer")),
            }
        }
    }
    if let Some(value) = map.get("bootstrap_max_rank") {
        match value.as_u64().filter(|n| (1..=5).contains(n)) {
            Some(rank) => config.bootstrap_max_rank = rank,
            None => problems.push("bootstrap_max_rank must be an integer from 1 to 5".into()),
        }
    }
    if let Some(value) = map.get("language") {
        match language_problem(value) {
            None => config.language = value.as_str().unwrap_or(DEFAULT_LANGUAGE).to_string(),
            Some(problem) => problems.push(problem),
        }
    }
    if let Some(value) = map.get("inject_summaries") {
        match value {
            Value::Bool(flag) => config.inject_summaries = *flag,
            _ => problems.push("inject_summaries must be true or false".into()),
        }
    }
    if let Some(value) = map.get("default_areas") {
        match value {
            Value::Array(items) if !items.is_empty()
                && items.iter().all(|a| a.as_str().is_some_and(|s| !s.is_empty())) =>
            {
                config.default_areas =
                    items.iter().filter_map(|a| a.as_str().map(str::to_string)).collect();
            }
            _ => problems.push("default_areas must be a non-empty list of names".into()),
        }
    }
    if let Some(value) = map.get("legacy") {
        match value {
            Value::Null => config.legacy = None,
            Value::String(path) if !path.is_empty() => config.legacy = Some(path.clone()),
            _ => problems.push("legacy must be a path string or null".into()),
        }
    }
    if problems.is_empty() { Ok(config) } else { Err(problems) }
}

/// Decode a JSON file; the error text says why it is not valid JSON.
pub fn read_json(path: &Path) -> std::result::Result<Value, String> {
    let bytes = std::fs::read(path).map_err(|e| e.to_string())?;
    let text = String::from_utf8(bytes).map_err(|e| format!("not UTF-8: {e}"))?;
    serde_json::from_str(&text).map_err(|e| e.to_string())
}

/// Problems of `<root>/handoff.json`, empty if it is absent or valid.
pub fn config_problems(root: &Path) -> Vec<String> {
    let path = root.join(CONFIG_NAME);
    if !path.is_file() {
        return Vec::new();
    }
    match read_json(&path) {
        Err(reason) => vec![format!("not valid JSON ({reason})")],
        Ok(data) => validate(&data).err().unwrap_or_default(),
    }
}

/// Read `<root>/handoff.json`: the defaults when absent, code 2 when invalid.
pub fn load_config(root: &Path) -> Result<Config> {
    let problems = config_problems(root);
    if !problems.is_empty() {
        return Err(HandoffError::new(
            format!("{}: {}", py::show(&root.join(CONFIG_NAME)), problems.join("; ")), USAGE));
    }
    let path = root.join(CONFIG_NAME);
    if !path.is_file() {
        return Ok(Config::default());
    }
    Ok(read_json(&path).ok().and_then(|data| validate(&data).ok()).unwrap_or_default())
}

/// Pretty JSON of a configuration, as `init` writes it.
pub fn config_json(config: &Config) -> String {
    let mut map = Map::new();
    map.insert("max_topics".into(), config.max_topics.into());
    map.insert("max_entry_files".into(), config.max_entry_files.into());
    map.insert("max_entry_lines".into(), config.max_entry_lines.into());
    map.insert("max_summary".into(), config.max_summary.into());
    map.insert("bootstrap_max_rank".into(), config.bootstrap_max_rank.into());
    map.insert("lock_ttl_seconds".into(), config.lock_ttl_seconds.into());
    map.insert("language".into(), config.language.clone().into());
    map.insert("inject_summaries".into(), config.inject_summaries.into());
    map.insert("default_areas".into(), config.default_areas.clone().into());
    map.insert("legacy".into(), config.legacy.clone().map_or(Value::Null, Value::String));
    py::dumps(&Value::Object(map), Some(2), true) + "\n"
}

/// Find the handoff root (see the module documentation for the order).
///
/// `project` defaults to the current directory. The result is absolute and may not
/// exist yet.
pub fn resolve_root(cli_root: Option<&Path>, project: Option<&Path>) -> PathBuf {
    let project = py::resolve(project.unwrap_or(Path::new(".")));
    if let Some(root) = cli_root {
        return py::resolve(&project.join(root));
    }
    if let Some(root) = std::env::var_os(ENV_ROOT).filter(|v| !v.is_empty()) {
        return py::resolve(&project.join(root));
    }
    let pointer = project.join(".claude").join(CONFIG_NAME);
    if pointer.is_file() {
        // A broken pointer falls back to the default; `check` shows the root used.
        if let Ok(Value::Object(data)) = read_json(&pointer) {
            if let Some(Value::String(root)) = data.get("root") {
                if !root.is_empty() {
                    return py::resolve(&project.join(root));
                }
            }
        }
    }
    py::resolve(&project.join(".claude").join("handoff"))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn validate_reports_every_problem() {
        let problems = validate(&json!({"zeta": 1, "alpha": 2, "max_topics": 0,
            "bootstrap_max_rank": 6, "language": "EN", "inject_summaries": 1,
            "default_areas": [], "legacy": ""})).unwrap_err();
        assert_eq!(problems, vec![
            "unknown key 'alpha'", "unknown key 'zeta'", "max_topics must be a positive integer",
            "bootstrap_max_rank must be an integer from 1 to 5",
            "language must be a lowercase language tag such as en, it, de or pt-br (got 'EN')",
            "inject_summaries must be true or false",
            "default_areas must be a non-empty list of names",
            "legacy must be a path string or null"]);
        assert_eq!(validate(&json!([])).unwrap_err(), vec!["must be a JSON object"]);
        assert!(validate(&json!({"max_topics": true})).is_err());
        assert!(validate(&json!({"max_topics": 2.0})).is_err());
    }

    #[test]
    fn validate_keeps_the_values() {
        let config = validate(&json!({"max_topics": 3, "language": "pt-br", "legacy": "old"}))
            .unwrap();
        assert_eq!((config.max_topics, config.language.as_str()), (3, "pt-br"));
        assert_eq!(config.legacy.as_deref(), Some("old"));
    }

    #[test]
    fn config_json_matches_python() {
        let text = config_json(&Config::default());
        assert!(text.starts_with("{\n  \"max_topics\": 20,\n  \"max_entry_files\": 51,"));
        assert!(text.ends_with("  \"legacy\": null\n}\n"));
        assert!(text.contains("\"default_areas\": [\n    \"rules\",\n"));
    }
}
