//! The `language` command: read or set the language the handoff content is written in.
//!
//! The language lives in the `language` key of `<root>/handoff.json`. Setting it
//! rewrites only that key: every other key keeps its value and its position, and a
//! missing file is created holding just `{"language": ...}`. The write is atomic
//! (temporary file in the root, then a rename), so a reader never sees half a file.
//! Existing entries are never translated here: that is a content change, done on
//! request with the lock protocol.

use crate::config::{language_problem_str, read_json, validate, CONFIG_NAME, DEFAULT_LANGUAGE};
use crate::errors::{HandoffError, Result, USAGE};
use crate::py;
use crate::tree::write_atomic;
use serde_json::{Map, Value};
use std::path::Path;

/// Decoded `<root>/handoff.json` with its key order, or `None` when absent; code 2 when
/// it is not valid JSON or its keys are not valid.
fn read_raw(root: &Path) -> Result<Option<Map<String, Value>>> {
    let path = root.join(CONFIG_NAME);
    if !path.is_file() {
        return Ok(None);
    }
    let data = read_json(&path).map_err(|reason| {
        HandoffError::new(format!("{}: not valid JSON ({reason})", py::show(&path)), USAGE)
    })?;
    if let Err(problems) = validate(&data) {
        return Err(HandoffError::new(format!("{}: {}", py::show(&path), problems.join("; ")), USAGE));
    }
    match data {
        Value::Object(map) => Ok(Some(map)),
        _ => Ok(None),
    }
}

/// Language configured for the handoff at `root`, and whether it is explicit (false
/// when the default applies because the file or its `language` key is absent).
pub fn current_language(root: &Path) -> Result<(String, bool)> {
    match read_raw(root)?.and_then(|data| data.get("language").cloned()) {
        Some(value) => Ok((py::str_value(&value), true)),
        None => Ok((DEFAULT_LANGUAGE.to_string(), false)),
    }
}

/// Write `code` as the `language` of `<root>/handoff.json`.
///
/// Returns `(previous, changed)`: `previous` is the language set before (`None` when
/// the default applied); nothing is written, and `changed` is false, when the file
/// already says `code`.
pub fn set_language(root: &Path, code: &str) -> Result<(Option<String>, bool)> {
    if !root.is_dir() {
        return Err(HandoffError::new(format!("{}: no handoff here (run init first)", py::show(root)),
                                     USAGE));
    }
    if let Some(problem) = language_problem_str(code) {
        return Err(HandoffError::new(problem, USAGE));
    }
    let mut data = read_raw(root)?.unwrap_or_default();
    let previous = data.get("language").map(py::str_value);
    if previous.as_deref() == Some(code) {
        return Ok((previous, false));
    }
    // An existing key keeps its position, a new one goes last.
    data.insert("language".into(), Value::String(code.to_string()));
    let path = root.join(CONFIG_NAME);
    let text = py::dumps(&Value::Object(data), Some(2), false) + "\n";
    write_atomic(&path, text.as_bytes(), &format!(".{CONFIG_NAME}."), ".tmp")
        .map_err(|e| HandoffError::io(&path, &e))?;
    Ok((previous, true))
}
