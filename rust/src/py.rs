//! Behaviour borrowed from Python, where the tool was first written.
//!
//! The Rust tool prints the same text and sorts in the same order as the Python one,
//! so the helpers here reproduce `str.strip`, `str.splitlines`, `repr`, `json.dumps`,
//! `Path` ordering and printing, `Path.resolve` and `pathlib` globbing.

use serde_json::Value;
use std::fs;
use std::io;
use std::path::{Component, Path, PathBuf};

/// `str.isspace` for one character (Unicode white space plus U+001C..U+001F).
pub fn is_space(c: char) -> bool {
    c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c)
}

/// `str.strip()`.
pub fn strip(s: &str) -> &str {
    s.trim_matches(is_space)
}

/// `str.splitlines()`: every Unicode line boundary, `\r\n` counting as one.
pub fn splitlines(s: &str) -> Vec<&str> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut chars = s.char_indices().peekable();
    while let Some((i, c)) = chars.next() {
        let boundary = matches!(c, '\n' | '\r' | '\u{b}' | '\u{c}' | '\u{1c}' | '\u{1d}'
            | '\u{1e}' | '\u{85}' | '\u{2028}' | '\u{2029}');
        if boundary {
            lines.push(&s[start..i]);
            let mut end = i + c.len_utf8();
            if c == '\r' {
                if let Some(&(_, '\n')) = chars.peek() {
                    chars.next();
                    end += 1;
                }
            }
            start = end;
        }
    }
    if start < s.len() {
        lines.push(&s[start..]);
    }
    lines
}

/// `len(bytes.splitlines())`: only `\n`, `\r` and `\r\n` end a line.
pub fn bytes_line_count(data: &[u8]) -> usize {
    let (mut count, mut i, mut start) = (0, 0, 0);
    while i < data.len() {
        match data[i] {
            b'\n' => {
                count += 1;
                i += 1;
                start = i;
            }
            b'\r' => {
                count += 1;
                i += 1;
                if i < data.len() && data[i] == b'\n' {
                    i += 1;
                }
                start = i;
            }
            _ => i += 1,
        }
    }
    if start < data.len() {
        count += 1;
    }
    count
}

/// True for the characters `str.isprintable` rejects (besides the ASCII controls).
fn non_printable(c: char) -> bool {
    let n = c as u32;
    matches!(n, 0x80..=0xa0 | 0xad | 0x600..=0x605 | 0x61c | 0x6dd | 0x70f | 0x1680
        | 0x180e | 0x2000..=0x200f | 0x2028..=0x202f | 0x205f..=0x2064 | 0x2066..=0x206f
        | 0x3000 | 0xd800..=0xf8ff | 0xfeff | 0xfff9..=0xfffb | 0xf0000..=0x10ffff)
}

/// `repr(str)`: quotes chosen like Python, controls and invisible characters escaped.
pub fn repr_str(s: &str) -> String {
    let quote = if s.contains('\'') && !s.contains('"') { '"' } else { '\'' };
    let mut out = String::with_capacity(s.len() + 2);
    out.push(quote);
    for c in s.chars() {
        match c {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if (c as u32) < 0x20 || c as u32 == 0x7f => {
                out.push_str(&format!("\\x{:02x}", c as u32))
            }
            c if non_printable(c) => {
                let n = c as u32;
                if n <= 0xff {
                    out.push_str(&format!("\\x{n:02x}"));
                } else if n <= 0xffff {
                    out.push_str(&format!("\\u{n:04x}"));
                } else {
                    out.push_str(&format!("\\U{n:08x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push(quote);
    out
}

/// `repr(float)`: shortest round-trip digits, scientific outside 1e-4 <= |x| < 1e16.
pub fn repr_float(x: f64) -> String {
    if x.is_nan() {
        return "nan".into();
    }
    if x.is_infinite() {
        return if x > 0.0 { "inf".into() } else { "-inf".into() };
    }
    let sci = format!("{x:e}");
    let (mantissa, exponent) = sci.split_once('e').unwrap_or((&sci, "0"));
    let exponent: i32 = exponent.parse().unwrap_or(0);
    if x != 0.0 && !(-4..16).contains(&exponent) {
        let sign = if exponent < 0 { '-' } else { '+' };
        return format!("{mantissa}e{sign}{:02}", exponent.abs());
    }
    let fixed = format!("{x}");
    if fixed.contains('.') { fixed } else { format!("{fixed}.0") }
}

/// `repr()` of a decoded JSON value, as Python prints it.
pub fn repr_value(value: &Value) -> String {
    match value {
        Value::Null => "None".into(),
        Value::Bool(true) => "True".into(),
        Value::Bool(false) => "False".into(),
        Value::Number(n) => number_text(n),
        Value::String(s) => repr_str(s),
        Value::Array(items) => {
            let inner: Vec<String> = items.iter().map(repr_value).collect();
            format!("[{}]", inner.join(", "))
        }
        Value::Object(map) => {
            let inner: Vec<String> =
                map.iter().map(|(k, v)| format!("{}: {}", repr_str(k), repr_value(v))).collect();
            format!("{{{}}}", inner.join(", "))
        }
    }
}

/// `str()` of a decoded JSON value.
pub fn str_value(value: &Value) -> String {
    match value {
        Value::String(s) => s.clone(),
        other => repr_value(other),
    }
}

/// A JSON number as Python prints it (`int` or `float`).
fn number_text(n: &serde_json::Number) -> String {
    if let Some(i) = n.as_i64() {
        i.to_string()
    } else if let Some(u) = n.as_u64() {
        u.to_string()
    } else {
        repr_float(n.as_f64().unwrap_or(f64::NAN))
    }
}

/// `json.dumps(value, indent=indent, ensure_ascii=ensure_ascii)` with Python's
/// default separators (`", "` and `": "` without indent, `","` and `": "` with it).
pub fn dumps(value: &Value, indent: Option<usize>, ensure_ascii: bool) -> String {
    let mut out = String::new();
    write_json(&mut out, value, indent, 0, ensure_ascii);
    out
}

fn write_json(out: &mut String, value: &Value, indent: Option<usize>, depth: usize,
              ascii: bool) {
    let newline = |out: &mut String, level: usize| {
        if let Some(width) = indent {
            out.push('\n');
            out.push_str(&" ".repeat(width * level));
        }
    };
    let separator = if indent.is_some() { "," } else { ", " };
    match value {
        Value::Null => out.push_str("null"),
        Value::Bool(b) => out.push_str(if *b { "true" } else { "false" }),
        Value::Number(n) => out.push_str(&number_text(n)),
        Value::String(s) => write_json_string(out, s, ascii),
        Value::Array(items) => {
            if items.is_empty() {
                out.push_str("[]");
                return;
            }
            out.push('[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push_str(separator);
                }
                newline(out, depth + 1);
                write_json(out, item, indent, depth + 1, ascii);
            }
            newline(out, depth);
            out.push(']');
        }
        Value::Object(map) => {
            if map.is_empty() {
                out.push_str("{}");
                return;
            }
            out.push('{');
            for (i, (key, item)) in map.iter().enumerate() {
                if i > 0 {
                    out.push_str(separator);
                }
                newline(out, depth + 1);
                write_json_string(out, key, ascii);
                out.push_str(": ");
                write_json(out, item, indent, depth + 1, ascii);
            }
            newline(out, depth);
            out.push('}');
        }
    }
}

fn write_json_string(out: &mut String, s: &str, ascii: bool) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c if ascii && (c as u32) > 0x7e => {
                let mut units = [0u16; 2];
                for unit in c.encode_utf16(&mut units) {
                    out.push_str(&format!("\\u{unit:04x}"));
                }
            }
            c => out.push(c),
        }
    }
    out.push('"');
}

/// Ordering key of a path, like `PurePath` comparison: component by component,
/// ignoring case on Windows.
pub fn path_key(p: &Path) -> Vec<String> {
    p.components()
        .map(|c| {
            let text = c.as_os_str().to_string_lossy();
            if cfg!(windows) { text.to_lowercase() } else { text.into_owned() }
        })
        .collect()
}

/// `sorted(paths)`.
pub fn sort_paths(paths: &mut [PathBuf]) {
    paths.sort_by_cached_key(|p| path_key(p));
}

/// `str(Path(text))`: separators normalised (`\` on Windows), empty and `.` parts dropped.
pub fn show_str(text: &str) -> String {
    let (seps, sep): (&[char], char) = if cfg!(windows) { (&['/', '\\'], '\\') } else { (&['/'], '/') };
    if text.is_empty() {
        return ".".into();
    }
    let mut out = String::new();
    let mut rest = text;
    if cfg!(windows) {
        if rest.starts_with("\\\\") || rest.starts_with("//") {
            return text.replace('/', "\\");
        }
        if rest.len() >= 2 && rest.as_bytes()[1] == b':' && rest.is_char_boundary(2) {
            out.push_str(&rest[..2]);
            rest = &rest[2..];
        }
    }
    if rest.starts_with(seps) {
        out.push(sep);
    }
    let parts: Vec<&str> = rest.split(seps).filter(|p| !p.is_empty() && *p != ".").collect();
    out.push_str(&parts.join(&sep.to_string()));
    if out.is_empty() { ".".into() } else { out }
}

/// `str(path)`.
pub fn show(p: &Path) -> String {
    show_str(&p.to_string_lossy())
}

/// `path.as_posix()`.
pub fn as_posix(p: &Path) -> String {
    let text = show(p);
    if cfg!(windows) { text.replace('\\', "/") } else { text }
}

/// `path.relative_to(root).as_posix()`, empty when `path` is `root`.
pub fn relative_posix(path: &Path, root: &Path) -> String {
    match path.strip_prefix(root) {
        Ok(rel) => rel
            .components()
            .map(|c| c.as_os_str().to_string_lossy().into_owned())
            .collect::<Vec<_>>()
            .join("/"),
        Err(_) => as_posix(path),
    }
}

/// `fs::canonicalize` without the `\\?\` prefix Windows adds.
fn canonical(p: &Path) -> io::Result<PathBuf> {
    let resolved = fs::canonicalize(p)?;
    if cfg!(windows) {
        let text = resolved.to_string_lossy();
        if let Some(rest) = text.strip_prefix(r"\\?\UNC\") {
            return Ok(PathBuf::from(format!(r"\\{rest}")));
        }
        if let Some(rest) = text.strip_prefix(r"\\?\") {
            return Ok(PathBuf::from(rest));
        }
    }
    Ok(resolved)
}

/// `Path.resolve()` (non-strict): absolute, symbolic links and `..` resolved on the
/// part that exists, the rest appended.
pub fn resolve(p: &Path) -> PathBuf {
    let absolute = if p.is_absolute() {
        p.to_path_buf()
    } else {
        std::env::current_dir().unwrap_or_default().join(p)
    };
    if let Ok(resolved) = canonical(&absolute) {
        return resolved;
    }
    let mut out = PathBuf::new();
    let mut exists = true;
    for component in absolute.components() {
        match component {
            Component::Prefix(_) | Component::RootDir => out.push(component.as_os_str()),
            Component::CurDir => {}
            Component::ParentDir => {
                out.pop();
            }
            Component::Normal(name) => {
                out.push(name);
                if exists {
                    match canonical(&out) {
                        Ok(resolved) => out = resolved,
                        Err(_) => exists = false,
                    }
                }
            }
        }
    }
    out
}

/// True when `path` is strictly below `root` (`root in path.parents`).
pub fn is_below(path: &Path, root: &Path) -> bool {
    path != root && path.starts_with(root)
}

/// Name comparison as `pathlib` globbing does it: case-insensitive on Windows.
pub fn glob_eq(name: &str, literal: &str) -> bool {
    if cfg!(windows) { name.to_lowercase() == literal.to_lowercase() } else { name == literal }
}

/// Whether a file name matches the glob `*.md`.
pub fn glob_md(name: &str) -> bool {
    if cfg!(windows) { name.to_lowercase().ends_with(".md") } else { name.ends_with(".md") }
}

/// `PurePath.suffix` of a file name.
pub fn suffix(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[i..],
        _ => "",
    }
}

/// `PurePath.stem` of a file name.
pub fn stem(name: &str) -> &str {
    match name.rfind('.') {
        Some(i) if i > 0 && i < name.len() - 1 => &name[..i],
        _ => name,
    }
}

/// File name of a path as text.
pub fn name_of(p: &Path) -> String {
    p.file_name().map(|n| n.to_string_lossy().into_owned()).unwrap_or_default()
}

/// An entry of a folder and its kind, as `Path.is_dir()` and `Path.is_file()` see it
/// (symbolic links followed).
#[derive(Debug, Clone)]
pub struct DirItem {
    pub path: PathBuf,
    pub is_dir: bool,
    pub is_file: bool,
}

/// Kind of a listed entry: from the listing itself, which costs no extra system call
/// except for symbolic links.
fn kind(entry: &fs::DirEntry) -> (bool, bool, bool) {
    match entry.file_type() {
        Ok(t) if !t.is_symlink() => (t.is_dir(), t.is_file(), false),
        _ => match fs::metadata(entry.path()) {
            Ok(m) => (m.is_dir(), m.is_file(), true),
            Err(_) => (false, false, true),
        },
    }
}

/// Entries of a folder with their kind, sorted like `sorted(folder.iterdir())`.
pub fn scan_dir(folder: &Path) -> io::Result<Vec<DirItem>> {
    let mut items = Vec::new();
    for entry in fs::read_dir(folder)? {
        let entry = entry?;
        let (is_dir, is_file, _) = kind(&entry);
        items.push(DirItem { path: entry.path(), is_dir, is_file });
    }
    items.sort_by_cached_key(|item| path_key(&item.path));
    Ok(items)
}

/// Entries of a folder (`sorted(folder.iterdir())`).
pub fn list_dir(folder: &Path) -> io::Result<Vec<PathBuf>> {
    Ok(scan_dir(folder)?.into_iter().map(|item| item.path).collect())
}

/// `sorted(root.rglob(pattern))` for a name predicate: every match under `root`,
/// `root` included, without following symbolic links to folders.
pub fn rglob(root: &Path, matches: impl Fn(&str) -> bool) -> Vec<PathBuf> {
    let mut found = Vec::new();
    let mut pending = vec![root.to_path_buf()];
    while let Some(folder) = pending.pop() {
        let Ok(entries) = fs::read_dir(&folder) else { continue };
        for entry in entries.flatten() {
            let path = entry.path();
            if matches(&entry.file_name().to_string_lossy()) {
                found.push(path.clone());
            }
            let (is_dir, _, link) = kind(&entry);
            if is_dir && !link {
                pending.push(path);
            }
        }
    }
    sort_paths(&mut found);
    found
}

/// Read many files at once with a few threads: on file systems where opening a file
/// is slow (Windows, network drives) this is several times faster than one by one.
/// Files that cannot be read are left out; reading them again reports the error.
pub fn read_parallel(paths: &[PathBuf]) -> std::collections::HashMap<PathBuf, Vec<u8>> {
    let workers = std::thread::available_parallelism().map_or(4, |n| n.get()).clamp(1, 16);
    let read = |part: &[PathBuf]| -> Vec<(PathBuf, Vec<u8>)> {
        part.iter().filter_map(|p| fs::read(p).ok().map(|bytes| (p.clone(), bytes))).collect()
    };
    if paths.len() < 32 || workers == 1 {
        return read(paths).into_iter().collect();
    }
    let chunk = paths.len().div_ceil(workers);
    std::thread::scope(|scope| {
        let handles: Vec<_> = paths.chunks(chunk).map(|part| scope.spawn(move || read(part))).collect();
        handles.into_iter().flat_map(|h| h.join().unwrap_or_default()).collect()
    })
}

/// A name no other process will pick: 32 hexadecimal digits, like `uuid4().hex`.
pub fn unique_hex() -> String {
    use std::collections::hash_map::RandomState;
    use std::hash::{BuildHasher, Hasher};
    use std::sync::atomic::{AtomicU64, Ordering};
    static COUNTER: AtomicU64 = AtomicU64::new(0);
    let nanos = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    let mut words = [0u64; 2];
    for (i, word) in words.iter_mut().enumerate() {
        let mut hasher = RandomState::new().build_hasher();
        hasher.write_u128(nanos);
        hasher.write_u32(std::process::id());
        hasher.write_u64(COUNTER.fetch_add(1, Ordering::Relaxed));
        hasher.write_usize(i);
        *word = hasher.finish();
    }
    format!("{:016x}{:016x}", words[0], words[1])
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn strip_and_splitlines_follow_python() {
        assert_eq!(strip("\u{1c} a \u{a0}"), "a");
        assert_eq!(splitlines("a\r\nb\rc\u{2028}d\n"), vec!["a", "b", "c", "d"]);
        assert_eq!(splitlines(""), Vec::<&str>::new());
        assert_eq!(bytes_line_count(b"a\r\nb\x0bc\n"), 2);
        assert_eq!(bytes_line_count(b"a\n\nb"), 3);
    }

    #[test]
    fn repr_matches_python() {
        assert_eq!(repr_str("abc"), "'abc'");
        assert_eq!(repr_str("it's"), "\"it's\"");
        assert_eq!(repr_str("a'b\"c"), "'a\\'b\"c'");
        assert_eq!(repr_str("tab\there\u{1}"), "'tab\\there\\x01'");
        assert_eq!(repr_str("caff\u{e8}"), "'caff\u{e8}'");
        assert_eq!(repr_value(&json!([1, "x", null, true, 1.5])), "[1, 'x', None, True, 1.5]");
        assert_eq!(repr_float(1e16), "1e+16");
        assert_eq!(repr_float(0.0001), "0.0001");
        assert_eq!(repr_float(1e-5), "1e-05");
        assert_eq!(repr_float(2.0), "2.0");
    }

    #[test]
    fn dumps_matches_python() {
        let value = json!({"a": [1, "\u{e8}"], "b": {}, "c": [], "d": null});
        assert_eq!(dumps(&value, None, true),
                   "{\"a\": [1, \"\\u00e8\"], \"b\": {}, \"c\": [], \"d\": null}");
        assert_eq!(dumps(&value, Some(2), false),
                   "{\n  \"a\": [\n    1,\n    \"\u{e8}\"\n  ],\n  \"b\": {},\n  \"c\": [],\n  \"d\": null\n}");
        assert_eq!(dumps(&json!("\u{1f600}\u{7f}"), None, true), "\"\\ud83d\\ude00\\u007f\"");
    }

    #[test]
    fn suffix_and_stem_follow_pathlib() {
        assert_eq!((suffix("a.md"), stem("a.md")), (".md", "a"));
        assert_eq!((suffix(".md"), stem(".md")), ("", ".md"));
        assert_eq!((suffix("a.b.md"), stem("a.b.md")), (".md", "a.b"));
        assert_eq!((suffix("a."), stem("a.")), ("", "a."));
    }
}
