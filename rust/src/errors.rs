//! The error that stops a command, and the exit codes of the tool.

use std::fmt;
use std::path::Path;

/// Exit code: content errors or failed `check`.
pub const CONTENT: i32 = 1;

/// Exit code: usage error.
pub const USAGE: i32 = 2;

/// Exit code: the folder is already locked (by someone else or by an expired lock).
pub const BUSY: i32 = 3;

/// Exit code: the caller is not the owner of the lock.
pub const NOT_OWNER: i32 = 4;

/// Usage or content error that stops a command.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct HandoffError {
    /// Text for the user, including the path concerned.
    pub message: String,
    /// Exit code for the process (1 content, 2 usage, 3 busy, 4 not owner).
    pub code: i32,
}

impl HandoffError {
    /// Error with an explicit exit code.
    pub fn new(message: impl Into<String>, code: i32) -> Self {
        Self { message: message.into(), code }
    }

    /// Content error (exit code 1).
    pub fn content(message: impl Into<String>) -> Self {
        Self::new(message, CONTENT)
    }

    /// A file-system failure on `path`, reported as a content error.
    pub fn io(path: &Path, err: &std::io::Error) -> Self {
        Self::content(format!("{}: {}", crate::py::show(path), err))
    }
}

impl fmt::Display for HandoffError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.message)
    }
}

/// Result of every fallible operation of the tool.
pub type Result<T> = std::result::Result<T, HandoffError>;
