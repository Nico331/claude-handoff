//! Session hooks of the claude-handoff plugin (`handoff hook <event>`).
//!
//! Called by `hooks/hooks.json` at two moments:
//!
//! - `session-start`: injects the read protocol of the handoff and, unless
//!   `inject_summaries` is false, the list of bootstrap entries (rank <=
//!   `bootstrap_max_rank`) with their summaries, so the session knows what is critical
//!   before opening a file;
//! - `prompt` (UserPromptSubmit): reminds, at every prompt, to decide whether the
//!   handoff must be updated, with the lock protocol.
//!
//! Both texts state the language the handoff content is written in. The texts exist in
//! English and Italian; any other language gets English (a regional tag such as
//! `it-ch` first tries its base language). Without a handoff, session start prints a
//! one-line hint and each prompt prints nothing.
//!
//! The hook must never break a session: it always exits 0, prints ASCII-only JSON,
//! tolerates malformed files and falls back to a minimal message on any failure.

use crate::config::{self, Config};
use crate::py;
use serde_json::{Map, Value};
use std::io::{IsTerminal, Read};
use std::path::{Path, PathBuf};

/// Most bootstrap entries listed in the injected text; the rest are counted.
const MAX_LISTED: usize = 60;

/// Injected texts of one language; `{root}`, `{tool}`, `{rank}`, `{language}`,
/// `{items}` and `{count}` are filled in.
struct Texts {
    start: &'static str,
    listed: &'static str,
    none: &'static str,
    more: &'static str,
    unlisted: &'static str,
    budget: &'static str,
    over: &'static str,
    prompt: &'static str,
    absent: &'static str,
}

const EN: Texts = Texts {
    start: "HANDOFF PROTOCOL (claude-handoff plugin): this project keeps its memory in \
        {root}/. Before any other action read, in order: {root}/INDEX.md, then the \
        INDEX.md of every area, then ALL entries with rank <= {rank} (listed below \
        with their summary), then the rank <= 2 entries of the topics today's task \
        touches; the rest on demand, starting from the summaries in the indexes. \
        Tool: {tool} <command> (list --max-rank {rank}, check, lock, reindex, \
        unlock, status). Write handoff content (entries, summaries, index text) \
        in language '{language}'.",
    listed: "Entries with rank <= {rank}:\n{items}",
    none: "  (no entry with rank <= {rank} found)",
    more: "  ... and {count} more: run `{tool} list --max-rank {rank}`",
    unlisted: "List them with: {tool} list --max-rank {rank}",
    budget: "Bootstrap read: ~{tokens} tokens ({files} files) of a {budget}-token \
        budget (bootstrap_budget_tokens in handoff.json).",
    over: "OVER BUDGET: read the indexes and only the rank <= {rank} entries whose \
        summary concerns today's task; in this session demote the least critical \
        rank <= {rank} entries (lock protocol) until the bootstrap fits the budget.",
    prompt: "HANDOFF REMINDER: for this prompt and every action that follows, decide \
        whether it changes a fact recorded in {root}/ or adds one a future session \
        needs. If so, update it in the same action, never at the end, with the lock \
        protocol: {tool} lock <folder> --owner <name> -> edit -> reindex -> unlock \
        -> same on the parent, up to the root; never hold two locks; then check. \
        One fact in one place; closed items go to rank 5 or are deleted, never \
        struck through; never secrets. Write handoff content (entries, summaries, \
        index text) in language '{language}'.",
    absent: "claude-handoff: no handoff at {root}/. To create one, run /claude-handoff:init.",
};

const IT: Texts = Texts {
    start: "PROTOCOLLO DELL'HANDOFF (plugin claude-handoff): la memoria del progetto sta \
        in {root}/. Prima di qualunque altra azione leggere, in ordine: \
        {root}/INDEX.md, poi l'INDEX.md di ogni area, poi TUTTE le voci con rank <= \
        {rank} (elencate sotto con il sommario), poi le voci rank <= 2 degli \
        argomenti del giorno; il resto su bisogno, partendo dai sommari negli \
        indici. Strumento: {tool} <comando> (list --max-rank {rank}, check, lock, \
        reindex, unlock, status). Scrivere il contenuto dell'handoff (voci, \
        sommari, testo degli indici) in lingua '{language}'.",
    listed: "Voci con rank <= {rank}:\n{items}",
    none: "  (nessuna voce con rank <= {rank} trovata)",
    more: "  ... e altre {count}: `{tool} list --max-rank {rank}`",
    unlisted: "Elenco: {tool} list --max-rank {rank}",
    budget: "Lettura di avvio: ~{tokens} token ({files} file) su un budget di {budget} \
        (bootstrap_budget_tokens in handoff.json).",
    over: "OLTRE IL BUDGET: leggere gli indici e solo le voci rank <= {rank} il cui \
        sommario riguarda il compito di oggi; in questa sessione abbassare di rank le \
        voci rank <= {rank} meno critiche (protocollo dei lock) finche' la lettura \
        rientra nel budget.",
    prompt: "PROMEMORIA DELL'HANDOFF: per questo prompt e per ogni azione che ne segue, \
        valutare se cambia un fatto scritto in {root}/ o ne aggiunge uno che una \
        sessione futura deve sapere. Se si', aggiornarlo nella stessa azione, mai in \
        coda, con il protocollo dei lock: {tool} lock <cartella> --owner <nome> -> \
        modifica -> reindex -> unlock -> genitore, fino alla radice; mai due lock \
        insieme; poi check. Un fatto in un solo posto; voci chiuse a rank 5 o \
        cancellate, mai barrate; mai segreti nell'handoff. Scrivere il contenuto \
        dell'handoff (voci, sommari, testo degli indici) in lingua '{language}'.",
    absent: "claude-handoff: nessun handoff in {root}/. Per crearne uno: /claude-handoff:init.",
};

/// Hook texts for a content language: its own, else its base tag's, else English.
fn hook_texts(language: &str) -> &'static Texts {
    for candidate in [language, language.split('-').next().unwrap_or(language)] {
        match candidate {
            "en" => return &EN,
            "it" => return &IT,
            _ => {}
        }
    }
    &EN
}

/// Fill the `{name}` placeholders of a template in one pass, like `str.format`: text
/// coming from a value is never substituted again.
fn fill(template: &str, values: &[(&str, &str)]) -> String {
    let mut text = String::with_capacity(template.len() + 128);
    let mut rest = template;
    while let Some(open) = rest.find('{') {
        text.push_str(&rest[..open]);
        let after = &rest[open + 1..];
        let value = after.find('}').and_then(|close| {
            values.iter().find(|(name, _)| *name == &after[..close]).map(|(_, v)| (close, *v))
        });
        match value {
            Some((close, value)) => {
                text.push_str(value);
                rest = &after[close + 1..];
            }
            None => {
                text.push('{');
                rest = after;
            }
        }
    }
    text.push_str(rest);
    text
}

/// Leniently read the frontmatter keys of a Markdown file: empty when the file has no
/// frontmatter or cannot be read. A reminder must never fail because of a bad file.
fn read_frontmatter(bytes: Option<&Vec<u8>>) -> Vec<(String, String)> {
    let Some(Ok(text)) = bytes.map(|b| std::str::from_utf8(b)) else { return Vec::new() };
    let lines = py::splitlines(text);
    if lines.is_empty() || py::strip(lines[0]) != "---" {
        return Vec::new();
    }
    let mut values: Vec<(String, String)> = Vec::new();
    for line in &lines[1..] {
        if py::strip(line) == "---" {
            return values;
        }
        if let Some((key, value)) = line.split_once(':') {
            let value = crate::tree::unquote(py::strip(value)).to_string();
            let key = py::strip(key).to_string();
            match values.iter_mut().find(|(k, _)| *k == key) {
                Some(slot) => slot.1 = value,
                None => values.push((key, value)),
            }
        }
    }
    Vec::new() // never closed: not frontmatter
}

/// Level-3 entries with rank <= `max_rank`, sorted by rank then path: `(path relative
/// to the root, summary)` pairs; malformed entries are skipped.
fn bootstrap_entries(root: &Path, max_rank: u64) -> Vec<(String, String)> {
    let folders = |folder: &Path| -> Vec<PathBuf> {
        py::scan_dir(folder).unwrap_or_default().into_iter().filter(|i| i.is_dir).map(|i| i.path).collect()
    };
    let mut candidates = Vec::new();
    for area in folders(root) {
        for topic in folders(&area) {
            candidates.extend(py::list_dir(&topic).unwrap_or_default().into_iter().filter(|path| {
                let name = py::name_of(path);
                py::glob_md(&name) && name != crate::tree::INDEX_NAME
            }));
        }
    }
    crate::tree::prefetch(&candidates);
    let mut found: Vec<(u64, String, String)> = Vec::new();
    for path in &candidates {
        let bytes = crate::tree::read_bytes(path).ok();
        let fields = read_frontmatter(bytes.as_ref());
        let get = |key: &str| fields.iter().find(|(k, _)| k == key).map(|(_, v)| v.clone());
        let rank_text = get("rank").unwrap_or_default();
        if rank_text.is_empty() || !rank_text.bytes().all(|b| b.is_ascii_digit()) {
            continue;
        }
        let rank = rank_text.parse::<u64>().unwrap_or(u64::MAX);
        if (1..=max_rank).contains(&rank) {
            found.push((rank, py::relative_posix(path, root), get("summary").unwrap_or_default()));
        }
    }
    found.sort();
    found.into_iter().map(|(_, rel, summary)| (rel, summary)).collect()
}

/// Project directory: `CLAUDE_PROJECT_DIR`, else the `cwd` of the hook input.
fn project_dir(stdin_text: Option<&str>) -> PathBuf {
    if let Some(dir) = std::env::var_os("CLAUDE_PROJECT_DIR").filter(|d| !d.is_empty()) {
        return PathBuf::from(dir);
    }
    if let Some(text) = stdin_text {
        if let Ok(Value::Object(data)) = serde_json::from_str::<Value>(text) {
            if let Some(Value::String(cwd)) = data.get("cwd") {
                return PathBuf::from(cwd);
            }
        }
    }
    std::env::current_dir().unwrap_or_default()
}

/// Root as shown in the text: relative to the project when inside it.
fn display_root(root: &Path, project: &Path) -> String {
    let project = py::resolve(project);
    if root == project {
        ".".into()
    } else if py::is_below(root, &project) {
        py::relative_posix(root, &project)
    } else {
        py::as_posix(root)
    }
}

/// The command line of this tool, as the injected text quotes it.
pub fn tool_path() -> String {
    std::env::current_exe().map(|p| py::as_posix(&p)).unwrap_or_else(|_| "handoff".into())
}

/// Text to inject for `event`, or `None` to stay silent.
fn build_message(event: &str, project: &Path) -> Option<String> {
    let root = config::resolve_root(None, Some(project));
    let shown = display_root(&root, project);
    if !root.is_dir() {
        return (event == "session-start").then(|| fill(EN.absent, &[("root", &shown)]));
    }
    let problems = config::config_problems(&root);
    let cfg = if problems.is_empty() { config::load_config(&root).unwrap_or_default() } else { Config::default() };
    let texts = hook_texts(&cfg.language);
    let tool = format!("\"{}\" --root \"{shown}\"", tool_path());
    let rank = cfg.bootstrap_max_rank.to_string();
    let base = [("root", shown.as_str()), ("tool", tool.as_str()), ("rank", rank.as_str()),
                ("language", cfg.language.as_str())];
    if event != "session-start" {
        return Some(fill(texts.prompt, &base));
    }
    let mut parts = vec![fill(texts.start, &base)];
    if !problems.is_empty() {
        parts.push(format!("(handoff.json ignored: {})", problems.join("; ")));
    }
    if cfg.inject_summaries {
        let entries = bootstrap_entries(&root, cfg.bootstrap_max_rank);
        let mut items: Vec<String> = entries.iter().take(MAX_LISTED)
            .map(|(rel, summary)| format!("  - {shown}/{rel}: {summary}")).collect();
        if entries.len() > MAX_LISTED {
            let count = (entries.len() - MAX_LISTED).to_string();
            items.push(fill(texts.more, &[("count", &count), ("tool", &tool), ("rank", &rank)]));
        }
        let listing = if items.is_empty() { fill(texts.none, &[("rank", &rank)]) } else { items.join("\n") };
        parts.push(fill(texts.listed, &[("rank", &rank), ("items", &listing)]));
    } else {
        parts.push(fill(texts.unlisted, &[("tool", &tool), ("rank", &rank)]));
    }
    // The budget line is optional: a failure here never breaks the hook.
    if let Ok(cost) = crate::report::bootstrap_cost(&root, &cfg) {
        let (tokens, files) = (cost.tokens().to_string(), cost.files.to_string());
        let budget = cfg.bootstrap_budget_tokens.to_string();
        parts.push(fill(texts.budget, &[("tokens", &tokens), ("files", &files), ("budget", &budget)]));
        if crate::report::over_budget(&cost, &cfg) {
            parts.push(fill(texts.over, &[("rank", &rank)]));
        }
    }
    Some(parts.join("\n"))
}

/// ASCII JSON understood by Claude Code as additional context.
pub fn payload(event: &str, text: &str) -> String {
    let name = if event == "session-start" { "SessionStart" } else { "UserPromptSubmit" };
    let mut specific = Map::new();
    specific.insert("hookEventName".into(), name.into());
    specific.insert("additionalContext".into(), text.into());
    let mut outer = Map::new();
    outer.insert("hookSpecificOutput".into(), Value::Object(specific));
    outer.insert("suppressOutput".into(), true.into());
    py::dumps(&Value::Object(outer), None, true)
}

/// Hook input from stdin, or `None` on a terminal or when unreadable.
pub fn stdin_text() -> Option<String> {
    let stdin = std::io::stdin();
    if stdin.is_terminal() {
        return None;
    }
    let mut text = String::new();
    stdin.lock().read_to_string(&mut text).ok().map(|_| text)
}

/// The event named by the hook input (`hook_event_name`), spelled as on the command
/// line; `None` when the input is not the input of one of our two hooks.
pub fn event_of_input(text: &str) -> Option<&'static str> {
    let Ok(Value::Object(data)) = serde_json::from_str::<Value>(text) else { return None };
    match data.get("hook_event_name").and_then(Value::as_str)? {
        "SessionStart" => Some("session-start"),
        "UserPromptSubmit" => Some("prompt"),
        _ => None,
    }
}

/// Entry point of `handoff hook [event]`: prints the payload, if any. Always exit 0.
///
/// Without `event` the hook input decides (`hook_event_name`), else `prompt`; `input`
/// is the hook input when the caller already read it from stdin.
pub fn run(event: Option<&str>, input: Option<String>) -> i32 {
    std::panic::set_hook(Box::new(|_| {}));
    let project_given = std::env::var_os("CLAUDE_PROJECT_DIR").is_some_and(|d| !d.is_empty());
    let input = match input {
        Some(text) => Some(text),
        None if event.is_none() || !project_given => stdin_text(),
        None => None,
    };
    let event = event.or_else(|| input.as_deref().and_then(event_of_input)).unwrap_or("prompt");
    let outcome = std::panic::catch_unwind(|| {
        build_message(event, &project_dir(if project_given { None } else { input.as_deref() }))
    });
    let text = outcome.unwrap_or_else(|_| {
        Some(format!("claude-handoff: the hook could not read the handoff (internal error); \
                      run \"{}\" check.", tool_path()))
    });
    if let Some(text) = text {
        use std::io::Write;
        let mut out = std::io::stdout().lock();
        let _ = out.write_all(payload(event, &text).as_bytes());
        let _ = out.flush();
    }
    0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn payload_is_ascii_like_python() {
        let text = payload("session-start", "caff\u{e8} \"x\"\n");
        assert_eq!(text, "{\"hookSpecificOutput\": {\"hookEventName\": \"SessionStart\", \
                          \"additionalContext\": \"caff\\u00e8 \\\"x\\\"\\n\"}, \"suppressOutput\": true}");
        assert!(payload("prompt", "x").contains("UserPromptSubmit"));
    }

    #[test]
    fn event_comes_from_the_hook_input() {
        assert_eq!(event_of_input(r#"{"hook_event_name": "SessionStart", "cwd": "/x"}"#),
                   Some("session-start"));
        assert_eq!(event_of_input(r#"{"hook_event_name": "UserPromptSubmit"}"#), Some("prompt"));
        assert_eq!(event_of_input(r#"{"hook_event_name": "Stop"}"#), None);
        assert_eq!(event_of_input("not json"), None);
    }

    #[test]
    fn fill_substitutes_once() {
        assert_eq!(fill("{root} and {tool} {x}", &[("root", "{tool}"), ("tool", "T")]),
                   "{tool} and T {x}");
    }

    #[test]
    fn texts_fall_back_to_base_language_then_english() {
        assert!(hook_texts("it-ch").start.starts_with("PROTOCOLLO"));
        assert!(hook_texts("de").start.starts_with("HANDOFF PROTOCOL"));
    }
}
