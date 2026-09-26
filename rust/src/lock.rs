//! Folder locks of the handoff.
//!
//! A lock is the file `<folder>/.lock`, created with `create_new` (`O_CREAT|O_EXCL`,
//! atomic on POSIX file systems and on NTFS), holding owner, pid, host, acquisition
//! time and lifetime. An expired lock does not vanish by itself: it is broken only
//! with `--steal-stale`, and the act is appended to `<root>/.lock-log`, like every
//! forced release. Thieves queue on `<folder>/.lock.steal`, created the same way, so
//! two of them can never both break a lock and both believe they hold it. The file format is the one of the Python tool, so both can share a
//! handoff.

use crate::errors::{HandoffError, Result, BUSY, NOT_OWNER, USAGE};
use crate::py;
use crate::sysinfo;
use crate::timeutil;
use crate::tree::{LOCK_LOG_NAME, LOCK_NAME};
use serde_json::{Map, Value};
use std::fs;
use std::io::{self, ErrorKind, Write};
use std::path::{Path, PathBuf};

/// Default lock lifetime in seconds (15 minutes), overridable in `handoff.json`.
pub const DEFAULT_TTL: i64 = 900;

/// Name of the guard a thief holds while breaking an expired lock.
pub const STEAL_NAME: &str = ".lock.steal";

/// Age in seconds after which a steal guard is considered abandoned.
const STEAL_GRACE: i64 = 60;

/// Content of a `.lock` file.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct LockInfo {
    /// Name declared by the holder (e.g. `agent-1`).
    pub owner: String,
    /// Pid of the process that took it.
    pub pid: i64,
    /// Machine name.
    pub host: String,
    /// ISO 8601 instant in UTC.
    pub acquired_at: String,
    /// Lifetime in seconds.
    pub ttl_seconds: i64,
}

impl LockInfo {
    /// Age of the lock at `now` (whole seconds), in microseconds.
    fn age_micros(&self, now: i64) -> i64 {
        now * 1_000_000 - timeutil::parse_aware(&self.acquired_at).unwrap_or(now * 1_000_000)
    }

    /// True when the age exceeds the declared lifetime.
    pub fn expired(&self, now: i64) -> bool {
        i128::from(self.age_micros(now)) > i128::from(self.ttl_seconds) * 1_000_000
    }

    /// The five fields as a JSON object, in file order.
    fn to_value(&self) -> Value {
        let mut map = Map::new();
        map.insert("owner".into(), self.owner.clone().into());
        map.insert("pid".into(), self.pid.into());
        map.insert("host".into(), self.host.clone().into());
        map.insert("acquired_at".into(), self.acquired_at.clone().into());
        map.insert("ttl_seconds".into(), self.ttl_seconds.into());
        Value::Object(map)
    }
}

/// A failure while contending for a lock: a verdict for the user, or a file-system
/// error still to be classified.
enum Failure {
    Verdict(HandoffError),
    Io(io::Error),
}

impl From<io::Error> for Failure {
    fn from(err: io::Error) -> Self {
        Failure::Io(err)
    }
}

impl From<HandoffError> for Failure {
    fn from(err: HandoffError) -> Self {
        Failure::Verdict(err)
    }
}

/// `int(value)` of Python for the values a lock file may hold.
fn py_int(value: &Value) -> Option<i64> {
    match value {
        Value::Bool(flag) => Some(i64::from(*flag)),
        Value::Number(n) => n.as_i64().or_else(|| {
            n.as_f64().filter(|f| f.is_finite() && f.abs() < 9.2e18).map(|f| f.trunc() as i64)
        }),
        Value::String(text) => {
            let text = py::strip(text);
            let (sign, digits) = match text.strip_prefix('-') {
                Some(rest) => (-1, rest),
                None => (1, text.strip_prefix('+').unwrap_or(text)),
            };
            let valid = !digits.is_empty() && !digits.starts_with('_') && !digits.ends_with('_')
                && !digits.contains("__") && digits.bytes().all(|b| b.is_ascii_digit() || b == b'_');
            if !valid {
                return None;
            }
            digits.replace('_', "").parse::<i64>().ok().map(|n| sign * n)
        }
        _ => None,
    }
}

/// The lock described by the bytes of a lock file, when they describe a valid one.
fn parse_lock(raw: &[u8]) -> Option<LockInfo> {
    let data: Value = serde_json::from_str(std::str::from_utf8(raw).ok()?).ok()?;
    let map = data.as_object()?;
    let info = LockInfo {
        owner: py::str_value(map.get("owner")?),
        pid: py_int(map.get("pid")?)?,
        host: py::str_value(map.get("host")?),
        acquired_at: py::str_value(map.get("acquired_at")?),
        ttl_seconds: py_int(map.get("ttl_seconds")?)?,
    };
    timeutil::parse_aware(&info.acquired_at)?;
    Some(info)
}

/// Read a lock file: `None` when it does not exist. An unreadable lock (broken JSON,
/// missing fields) becomes a lock owned by `?`, dated at the file's modification time
/// with the default lifetime, so that it can only be broken with `--steal-stale`.
pub fn read_lock(path: &Path) -> io::Result<Option<LockInfo>> {
    let raw = match fs::read(path) {
        Ok(raw) => raw,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    if let Some(info) = parse_lock(&raw) {
        return Ok(Some(info));
    }
    let modified = match fs::metadata(path).and_then(|m| m.modified()) {
        Ok(modified) => modified,
        Err(err) if err.kind() == ErrorKind::NotFound => return Ok(None),
        Err(err) => return Err(err),
    };
    Ok(Some(LockInfo {
        owner: "?".into(),
        pid: 0,
        host: "?".into(),
        acquired_at: timeutil::iso_utc(timeutil::system_seconds(modified)),
        ttl_seconds: DEFAULT_TTL,
    }))
}

/// One-line description: who holds the lock, since when, whether it expired.
pub fn describe(info: &LockInfo, now: i64) -> String {
    let seconds = info.age_micros(now).max(0) / 1_000_000;
    let state = if info.expired(now) { " EXPIRED" } else { "" };
    format!("owner={} pid={} host={} since {} (age {}m{:02}s, ttl {}s){state}", info.owner,
            info.pid, info.host, info.acquired_at, seconds / 60, seconds % 60, info.ttl_seconds)
}

/// Append one JSON line to `<root>/.lock-log` (`steal-stale` or `force-unlock`).
pub fn append_log(root: &Path, action: &str, folder: &Path, actor: &str,
                  previous: Option<&LockInfo>, now: i64) -> io::Result<()> {
    let relative = py::relative_posix(folder, root);
    let mut record = Map::new();
    record.insert("at".into(), timeutil::iso_utc(now).into());
    record.insert("action".into(), action.into());
    record.insert("actor".into(), actor.into());
    record.insert("folder".into(), if relative.is_empty() { ".".into() } else { relative.into() });
    record.insert("previous".into(), previous.map_or(Value::Null, LockInfo::to_value));
    let line = py::dumps(&Value::Object(record), None, false) + "\n";
    let mut log = fs::OpenOptions::new().create(true).append(true).open(root.join(LOCK_LOG_NAME))?;
    log.write_all(line.as_bytes())
}

/// Take the lock of a folder (already resolved).
///
/// Code 2 for invalid arguments; code 3 when the folder is locked (valid lock, expired
/// lock without `steal_stale`, or a lost race).
pub fn acquire(root: &Path, folder: &Path, owner: &str, ttl: i64, steal_stale: bool,
               now: Option<i64>) -> Result<LockInfo> {
    if py::strip(owner).is_empty() {
        return Err(HandoffError::new("--owner must not be empty", USAGE));
    }
    if ttl <= 0 {
        return Err(HandoffError::new("--ttl must be positive", USAGE));
    }
    let now = now.unwrap_or_else(timeutil::now_seconds);
    let path = folder.join(LOCK_NAME);
    let info = LockInfo {
        owner: owner.to_string(),
        pid: i64::from(std::process::id()),
        host: sysinfo::hostname(),
        acquired_at: timeutil::iso_utc(now),
        ttl_seconds: ttl,
    };
    match contend(root, folder, &path, info, steal_stale, now) {
        Ok(info) => Ok(info),
        Err(Failure::Verdict(err)) => Err(err),
        // Windows: the file is open or being deleted by another process.
        Err(Failure::Io(err)) if err.kind() == ErrorKind::PermissionDenied => Err(HandoffError::new(
            format!("{}: lock contended by another process, retry", py::show(folder)), BUSY)),
        Err(Failure::Io(err)) => Err(HandoffError::io(&path, &err)),
    }
}

/// The acquisition attempt proper; see `acquire`.
fn contend(root: &Path, folder: &Path, path: &Path, info: LockInfo, steal_stale: bool, now: i64)
           -> std::result::Result<LockInfo, Failure> {
    let shown = py::show(folder);
    let busy = |message: String| Failure::Verdict(HandoffError::new(message, BUSY));
    if create_exclusive(path, &info)? {
        return Ok(info);
    }
    let Some(current) = read_lock(path)? else {
        // Released between the two steps: one more try, no more.
        if create_exclusive(path, &info)? {
            return Ok(info);
        }
        return Err(busy(format!("{shown}: lock just taken by another process")));
    };
    if !current.expired(now) {
        return Err(busy(format!("{shown}: locked by {}", describe(&current, now))));
    }
    if !steal_stale {
        return Err(busy(format!("{shown}: expired lock of {}; use --steal-stale to break it",
                                describe(&current, now))));
    }
    // Thieves queue on a second exclusive file: only its holder may break the lock.
    let guard = folder.join(STEAL_NAME);
    if !create_guard(&guard)? {
        clear_abandoned_guard(&guard, now);
        return Err(busy(format!("{shown}: another process is breaking the expired lock, retry")));
    }
    let outcome = steal(root, folder, path, &info, &current, now);
    let _ = fs::remove_file(&guard);
    outcome
}

/// Break the expired lock `expected` and take the folder, holding the steal guard.
///
/// The lock is removed only if it is still `expected`: a lock released meanwhile is
/// simply taken, a lock replaced meanwhile is left alone.
fn steal(root: &Path, folder: &Path, path: &Path, info: &LockInfo, expected: &LockInfo, now: i64)
         -> std::result::Result<LockInfo, Failure> {
    let shown = py::show(folder);
    let busy = |message: String| Failure::Verdict(HandoffError::new(message, BUSY));
    match read_lock(path)? {
        None => {}
        Some(present) if present != *expected => {
            return Err(busy(format!("{shown}: the expired lock changed, retry")));
        }
        Some(_) => {
            match fs::remove_file(path) {
                Ok(()) => {}
                Err(err) if err.kind() == ErrorKind::NotFound => {}
                Err(err) => return Err(err.into()),
            }
            append_log(root, "steal-stale", folder, &info.owner, Some(expected), now)?;
        }
    }
    if create_exclusive(path, info)? {
        return Ok(info.clone());
    }
    Err(busy(format!("{shown}: lock taken by another process after the steal")))
}

/// Create the steal guard only if it does not exist; true on success.
fn create_guard(guard: &Path) -> io::Result<bool> {
    match fs::OpenOptions::new().write(true).create_new(true).open(guard) {
        Ok(mut file) => {
            let _ = write!(file, "{}", std::process::id());
            Ok(true)
        }
        Err(err) if err.kind() == ErrorKind::AlreadyExists => Ok(false),
        Err(err) => Err(err),
    }
}

/// Remove a steal guard left by a thief that died inside the steal (older than
/// `STEAL_GRACE` seconds); a live thief holds it for a few milliseconds only.
fn clear_abandoned_guard(guard: &Path, now: i64) {
    let modified = fs::metadata(guard).and_then(|m| m.modified()).map(timeutil::system_seconds);
    if matches!(modified, Ok(at) if now - at > STEAL_GRACE) {
        let _ = fs::remove_file(guard);
    }
}

/// Create the lock file only if it does not exist; true on success.
fn create_exclusive(path: &Path, info: &LockInfo) -> io::Result<bool> {
    let mut file = match fs::OpenOptions::new().write(true).create_new(true).open(path) {
        Ok(file) => file,
        Err(err) if err.kind() == ErrorKind::AlreadyExists => return Ok(false),
        Err(err) => return Err(err),
    };
    file.write_all(py::dumps(&info.to_value(), None, false).as_bytes())?;
    Ok(true)
}

/// Release the lock of a folder: code 1 when there is no lock, code 4 when the owner
/// differs and `force` is not set (a forced release is logged).
pub fn release(root: &Path, folder: &Path, owner: &str, force: bool, now: Option<i64>)
               -> Result<LockInfo> {
    let now = now.unwrap_or_else(timeutil::now_seconds);
    let path = folder.join(LOCK_NAME);
    let current = read_lock(&path).map_err(|e| HandoffError::io(&path, &e))?;
    let Some(current) = current else {
        return Err(HandoffError::content(format!("{}: no lock to release", py::show(folder))));
    };
    if current.owner != owner {
        if !force {
            return Err(HandoffError::new(format!("{}: the lock belongs to {}, not {}",
                py::show(folder), py::repr_str(&current.owner), py::repr_str(owner)), NOT_OWNER));
        }
        append_log(root, "force-unlock", folder, owner, Some(&current), now)
            .map_err(|e| HandoffError::io(&root.join(LOCK_LOG_NAME), &e))?;
    }
    match fs::remove_file(&path) {
        Ok(()) => Ok(current),
        Err(err) if err.kind() == ErrorKind::NotFound => Ok(current), // released concurrently
        Err(err) => Err(HandoffError::io(&path, &err)),
    }
}

/// Check that `owner` holds a valid lock on the folder: code 4 when the lock is
/// missing or someone else's; code 3 when it is the caller's but expired.
pub fn require_held(folder: &Path, owner: &str, now: Option<i64>) -> Result<LockInfo> {
    let now = now.unwrap_or_else(timeutil::now_seconds);
    let path = folder.join(LOCK_NAME);
    let shown = py::show(folder);
    let current = read_lock(&path).map_err(|e| HandoffError::io(&path, &e))?;
    let Some(current) = current else {
        return Err(HandoffError::new(
            format!("{shown}: the folder lock is required (handoff lock)"), NOT_OWNER));
    };
    if current.owner != owner {
        return Err(HandoffError::new(format!("{shown}: the lock belongs to {}, not {}",
            py::repr_str(&current.owner), py::repr_str(owner)), NOT_OWNER));
    }
    if current.expired(now) {
        return Err(HandoffError::new(format!("{shown}: your lock expired, take it again"), BUSY));
    }
    Ok(current)
}

/// Every lock under the root, the root included, sorted by path.
pub fn find_locks(root: &Path) -> Result<Vec<(PathBuf, LockInfo)>> {
    let mut found = Vec::new();
    for path in py::rglob(root, |name| py::glob_eq(name, LOCK_NAME)) {
        let info = read_lock(&path).map_err(|e| HandoffError::io(&path, &e))?;
        if let Some(info) = info {
            if path.is_file() {
                found.push((path.parent().unwrap_or(root).to_path_buf(), info));
            }
        }
    }
    Ok(found)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Arc, Barrier};

    fn temp_folder() -> PathBuf {
        let folder = std::env::temp_dir().join(format!("handoff-test-{}", py::unique_hex()));
        fs::create_dir_all(&folder).unwrap();
        folder
    }

    #[test]
    fn lock_file_matches_python_format() {
        let info = LockInfo { owner: "a".into(), pid: 7, host: "h".into(),
            acquired_at: "2026-09-23T12:00:00+00:00".into(), ttl_seconds: 900 };
        assert_eq!(py::dumps(&info.to_value(), None, false),
                   "{\"owner\": \"a\", \"pid\": 7, \"host\": \"h\", \
                    \"acquired_at\": \"2026-09-23T12:00:00+00:00\", \"ttl_seconds\": 900}");
        assert_eq!(parse_lock(py::dumps(&info.to_value(), None, false).as_bytes()), Some(info));
        assert_eq!(parse_lock(b"{\"owner\": \"a\", \"pid\": \"7\", \"host\": 1, \
            \"acquired_at\": \"2026-09-23T12:00:00+00:00\", \"ttl_seconds\": 9.5}")
            .map(|i| (i.pid, i.host, i.ttl_seconds)), Some((7, "1".into(), 9)));
        assert!(parse_lock(b"{\"owner\": \"a\", \"pid\": 1, \"host\": \"h\", \
            \"acquired_at\": \"2026-09-23T12:00:00\", \"ttl_seconds\": 1}").is_none());
    }

    #[test]
    fn describe_matches_python() {
        let now = timeutil::parse_aware("2026-09-23T12:16:05+00:00").unwrap() / 1_000_000;
        let info = LockInfo { owner: "a".into(), pid: 7, host: "h".into(),
            acquired_at: "2026-09-23T12:00:00+00:00".into(), ttl_seconds: 900 };
        assert_eq!(describe(&info, now), "owner=a pid=7 host=h since 2026-09-23T12:00:00+00:00 \
                                          (age 16m05s, ttl 900s) EXPIRED");
    }

    #[test]
    fn exactly_one_thread_wins() {
        let folder = temp_folder();
        let barrier = Arc::new(Barrier::new(16));
        let handles: Vec<_> = (0..16).map(|i| {
            let (folder, barrier) = (folder.clone(), barrier.clone());
            std::thread::spawn(move || {
                barrier.wait();
                acquire(&folder, &folder, &format!("T{i}"), 60, false, None).is_ok()
            })
        }).collect();
        let winners = handles.into_iter().map(|h| h.join().unwrap()).filter(|w| *w).count();
        assert_eq!(winners, 1);
        fs::remove_dir_all(&folder).unwrap();
    }

    #[test]
    fn exactly_one_thief_wins() {
        let folder = temp_folder();
        let old = timeutil::now_seconds() - 3600;
        let stale = LockInfo { owner: "dead".into(), pid: 1, host: "h".into(),
            acquired_at: timeutil::iso_utc(old), ttl_seconds: 60 };
        assert!(create_exclusive(&folder.join(LOCK_NAME), &stale).unwrap());
        let barrier = Arc::new(Barrier::new(8));
        let handles: Vec<_> = (0..8).map(|i| {
            let (folder, barrier) = (folder.clone(), barrier.clone());
            std::thread::spawn(move || {
                barrier.wait();
                acquire(&folder, &folder, &format!("L{i}"), 60, true, None).is_ok()
            })
        }).collect();
        let winners = handles.into_iter().map(|h| h.join().unwrap()).filter(|w| *w).count();
        assert_eq!(winners, 1);
        let log = fs::read_to_string(folder.join(LOCK_LOG_NAME)).unwrap();
        assert_eq!(log.lines().count(), 1);
        assert!(!folder.join(STEAL_NAME).exists());
        fs::remove_dir_all(&folder).unwrap();
    }

    #[test]
    fn release_and_require_held() {
        let folder = temp_folder();
        acquire(&folder, &folder, "a", 60, false, None).unwrap();
        assert_eq!(require_held(&folder, "b", None).unwrap_err().code, NOT_OWNER);
        assert_eq!(release(&folder, &folder, "b", false, None).unwrap_err().code, NOT_OWNER);
        release(&folder, &folder, "b", true, None).unwrap();
        assert_eq!(release(&folder, &folder, "a", false, None).unwrap_err().code, 1);
        assert!(fs::read_to_string(folder.join(LOCK_LOG_NAME)).unwrap().contains("force-unlock"));
        fs::remove_dir_all(&folder).unwrap();
    }
}
