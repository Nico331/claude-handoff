//! Command-line tool of the three-level handoff (claude-handoff plugin).
//!
//! Usage, from the project directory:
//!
//! ```text
//! handoff [--root DIR] <command> [options]
//! handoff hook <session-start|prompt>
//! ```
//!
//! Commands: init, lock, unlock, status, reindex, check, list, stats, language, and the
//! session hook. Exit codes: 0 success; 1 content errors or failed `check`; 2 usage
//! error; 3 folder already locked (or expired lock not broken); 4 lock owned by someone
//! else. `hook` always exits 0.

mod check;
mod config;
mod errors;
mod hook;
mod index;
mod init;
mod language;
mod lock;
mod py;
mod report;
mod sysinfo;
mod timeutil;
mod tree;

use config::{Config, CONFIG_NAME, DEFAULT_LANGUAGE};
use errors::{HandoffError, Result, USAGE};
use std::collections::HashMap;
use std::io::Write;
use std::path::{Path, PathBuf};
use tree::INDEX_NAME;

const PROG: &str = "handoff";

const EXIT_CODES: &str =
    "exit: 0 ok, 1 content errors/check failed, 2 usage error, 3 folder locked, 4 lock owned by someone else";

/// One option of a command: long name, whether it takes a value, help text.
struct Opt {
    name: &'static str,
    value: Option<&'static str>,
    help: &'static str,
}

/// One command: name, positionals (name, required, help), options, help text.
struct Command {
    name: &'static str,
    help: &'static str,
    positionals: &'static [(&'static str, bool, &'static str)],
    options: &'static [Opt],
}

const COMMANDS: &[Command] = &[
    Command { name: "init", help: "create the root, the areas and handoff.json (idempotent, never overwrites)",
        positionals: &[], options: &[
            Opt { name: "areas", value: Some("AREAS"), help: "comma-separated area names (default: rules,state,decisions,procedures,open,history or default_areas of handoff.json)" },
            Opt { name: "owner", value: Some("OWNER"), help: "lock owner for the final reindex (default: init)" },
            Opt { name: "no-seed", value: None, help: "do not create the seed topic explaining the handoff" },
            Opt { name: "language", value: Some("CODE"), help: "content language written into a new handoff.json (default: en; ignored if handoff.json exists)" },
        ] },
    Command { name: "lock", help: "take the lock of a folder (relative to the root)",
        positionals: &[("<folder>", true, "folder relative to the root; . is the root")], options: &[
            Opt { name: "owner", value: Some("OWNER"), help: "name of the lock holder (required)" },
            Opt { name: "ttl", value: Some("TTL"), help: "lifetime in seconds (default: lock_ttl_seconds, 900)" },
            Opt { name: "steal-stale", value: None, help: "break an expired lock, logging it in .lock-log" },
        ] },
    Command { name: "unlock", help: "release the lock of a folder",
        positionals: &[("<folder>", true, "folder relative to the root")], options: &[
            Opt { name: "owner", value: Some("OWNER"), help: "who releases (required)" },
            Opt { name: "force", value: None, help: "release someone else's lock (coordinator); logged" },
        ] },
    Command { name: "status", help: "list the locks present, with age, expired ones marked",
        positionals: &[], options: &[] },
    Command { name: "reindex", help: "regenerate the INDEX.md table and the folder rank",
        positionals: &[("<folder>", false, "folder relative to the root")], options: &[
            Opt { name: "owner", value: Some("OWNER"), help: "holder of the folder lock" },
            Opt { name: "all", value: None, help: "every folder bottom-up, taking one lock at a time" },
            Opt { name: "no-lock", value: None, help: "skip the lock check (tests and repairs only)" },
        ] },
    Command { name: "check", help: "validate structure, frontmatter, indexes, links, locks and handoff.json",
        positionals: &[], options: &[
            Opt { name: "warn-only", value: None, help: "print the errors but exit 0" },
        ] },
    Command { name: "list", help: "list entries by rank (bootstrap: --max-rank 1)",
        positionals: &[], options: &[
            Opt { name: "max-rank", value: Some("MAX_RANK"), help: "only rank <= N" },
            Opt { name: "area", value: Some("AREA"), help: "only the entries of one area" },
        ] },
    Command { name: "stats", help: "bytes and lines per level and bootstrap cost",
        positionals: &[], options: &[
            Opt { name: "legacy", value: Some("LEGACY"), help: "old flat handoff folder to compare with (default: 'legacy' of handoff.json, if set)" },
        ] },
    Command { name: "language", help: "show the language the handoff content is written in, or set it",
        positionals: &[("CODE", false, "new language tag, e.g. en, it, de, pt-br (omit to print the current one)")],
        options: &[] },
    Command { name: "hook", help: "session hook for Claude Code: session-start or prompt (always exits 0)",
        positionals: &[("EVENT", false, "session-start or prompt (default)")], options: &[] },
];

/// Arguments of one command after parsing.
struct Args {
    root: Option<PathBuf>,
    values: HashMap<&'static str, String>,
    flags: Vec<&'static str>,
    positionals: Vec<String>,
}

impl Args {
    fn value(&self, name: &str) -> Option<&str> {
        self.values.get(name).map(String::as_str)
    }

    fn flag(&self, name: &str) -> bool {
        self.flags.contains(&name)
    }

    fn positional(&self, index: usize) -> Option<&str> {
        self.positionals.get(index).map(String::as_str)
    }
}

/// What the command line asks for.
enum Parsed {
    Run(&'static Command, Args),
    Help(String),
}

fn usage_line(command: Option<&Command>) -> String {
    match command {
        None => format!("usage: {PROG} [-h] [--root ROOT] <command> ..."),
        Some(c) => {
            let mut parts = vec![format!("usage: {PROG} {} [-h]", c.name)];
            for option in c.options {
                match option.value {
                    Some(meta) => parts.push(format!("[--{} {meta}]", option.name)),
                    None => parts.push(format!("[--{}]", option.name)),
                }
            }
            for (name, required, _) in c.positionals {
                parts.push(if *required { name.to_string() } else { format!("[{name}]") });
            }
            parts.join(" ")
        }
    }
}

fn help_text(command: Option<&Command>) -> String {
    let mut text = usage_line(command) + "\n\n";
    match command {
        None => {
            text.push_str("Three-level project handoff: init, locks, indexes, checks, stats.\n\n\
                           commands:\n");
            for c in COMMANDS {
                text.push_str(&format!("  {:<10} {}\n", c.name, c.help));
            }
            text.push_str("\noptions:\n  -h, --help   show this help message and exit\n  \
                           --root ROOT  handoff root (default: $CLAUDE_HANDOFF_ROOT, then the 'root' of \
                           .claude/handoff.json, then .claude/handoff)\n");
        }
        Some(c) => {
            text.push_str(&format!("{}\n", c.help));
            if !c.positionals.is_empty() {
                text.push_str("\npositional arguments:\n");
                for (name, _, help) in c.positionals {
                    text.push_str(&format!("  {name:<18} {help}\n"));
                }
            }
            text.push_str("\noptions:\n  -h, --help         show this help message and exit\n");
            for option in c.options {
                let label = match option.value {
                    Some(meta) => format!("--{} {meta}", option.name),
                    None => format!("--{}", option.name),
                };
                text.push_str(&format!("  {label:<18} {}\n", option.help));
            }
        }
    }
    text + "\n" + EXIT_CODES + "\n"
}

/// A usage error, formatted like argparse (usage line, then `prog: error: ...`).
fn usage_error(command: Option<&Command>, message: &str) -> HandoffError {
    HandoffError::new(format!("{}\n{PROG}: error: {message}", usage_line(command)), USAGE)
}

/// Resolve `--name` against the known long options: exact, else a unique prefix.
fn match_option<'a>(given: &str, names: &[&'a str]) -> std::result::Result<Option<&'a str>, String> {
    if let Some(exact) = names.iter().find(|n| **n == given) {
        return Ok(Some(exact));
    }
    let candidates: Vec<&&str> = names.iter().filter(|n| n.starts_with(given)).collect();
    match candidates.len() {
        0 => Ok(None),
        1 => Ok(Some(candidates[0])),
        _ => {
            let listed: Vec<String> = candidates.iter().map(|n| format!("--{n}")).collect();
            Err(format!("ambiguous option: --{given} could match {}", listed.join(", ")))
        }
    }
}

/// argparse treats `-5` as a value, `-x` as an option.
fn looks_like_option(arg: &str) -> bool {
    arg.starts_with('-') && arg.len() > 1 && arg[1..].parse::<f64>().is_err()
}

fn parse_args(argv: &[String]) -> Result<Parsed> {
    let mut root = None;
    let mut i = 0;
    while i < argv.len() && looks_like_option(&argv[i]) {
        let arg = &argv[i];
        if arg == "-h" {
            return Ok(Parsed::Help(help_text(None)));
        }
        let Some(long) = arg.strip_prefix("--") else {
            return Err(usage_error(None, &format!("unrecognized arguments: {arg}")));
        };
        let (name, inline) = match long.split_once('=') {
            Some((name, value)) => (name, Some(value.to_string())),
            None => (long, None),
        };
        match match_option(name, &["root", "help"]).map_err(|m| usage_error(None, &m))? {
            Some("help") => return Ok(Parsed::Help(help_text(None))),
            Some(_) => {
                let value = match inline {
                    Some(value) => value,
                    None => {
                        i += 1;
                        match argv.get(i) {
                            Some(v) if !looks_like_option(v) => v.clone(),
                            _ => return Err(usage_error(None, "argument --root: expected one argument")),
                        }
                    }
                };
                root = Some(PathBuf::from(value));
            }
            None => return Err(usage_error(None, &format!("unrecognized arguments: {arg}"))),
        }
        i += 1;
    }
    let Some(name) = argv.get(i) else {
        return Err(usage_error(None, "the following arguments are required: <command>"));
    };
    let Some(command) = COMMANDS.iter().find(|c| c.name == name) else {
        let choices: Vec<String> = COMMANDS.iter().map(|c| format!("'{}'", c.name)).collect();
        return Err(usage_error(None, &format!("argument <command>: invalid choice: {} (choose from {})",
                                              py::repr_str(name), choices.join(", "))));
    };
    let mut args = Args { root, values: HashMap::new(), flags: Vec::new(), positionals: Vec::new() };
    if command.name == "hook" {
        args.positionals = argv[i + 1..].to_vec();
        return Ok(Parsed::Run(command, args));
    }
    let names: Vec<&str> = command.options.iter().map(|o| o.name).chain(["help"]).collect();
    let mut unrecognized: Vec<String> = Vec::new();
    let mut only_positionals = false;
    i += 1;
    while i < argv.len() {
        let arg = &argv[i];
        i += 1;
        if !only_positionals && arg == "--" {
            only_positionals = true;
            continue;
        }
        if only_positionals || !looks_like_option(arg) {
            if args.positionals.len() < command.positionals.len() {
                args.positionals.push(arg.clone());
            } else {
                unrecognized.push(arg.clone());
            }
            continue;
        }
        if arg == "-h" {
            return Ok(Parsed::Help(help_text(Some(command))));
        }
        let Some(long) = arg.strip_prefix("--") else {
            unrecognized.push(arg.clone());
            continue;
        };
        let (name, inline) = match long.split_once('=') {
            Some((name, value)) => (name, Some(value.to_string())),
            None => (long, None),
        };
        let Some(found) = match_option(name, &names).map_err(|m| usage_error(Some(command), &m))? else {
            unrecognized.push(arg.clone());
            continue;
        };
        if found == "help" {
            return Ok(Parsed::Help(help_text(Some(command))));
        }
        let option = command.options.iter().find(|o| o.name == found).expect("known option");
        if option.value.is_none() {
            if let Some(value) = inline {
                return Err(usage_error(Some(command),
                    &format!("argument --{found}: ignored explicit argument {}", py::repr_str(&value))));
            }
            if !args.flags.contains(&option.name) {
                args.flags.push(option.name);
            }
            continue;
        }
        let value = match inline {
            Some(value) => value,
            None => match argv.get(i) {
                Some(v) if !looks_like_option(v) => {
                    i += 1;
                    v.clone()
                }
                _ => return Err(usage_error(Some(command), &format!("argument --{found}: expected one argument"))),
            },
        };
        args.values.insert(option.name, value);
    }
    if !unrecognized.is_empty() {
        return Err(usage_error(None, &format!("unrecognized arguments: {}", unrecognized.join(" "))));
    }
    let mut missing: Vec<String> = Vec::new();
    if matches!(command.name, "lock" | "unlock") && !args.values.contains_key("owner") {
        missing.push("--owner".into());
    }
    for (index, (name, required, _)) in command.positionals.iter().enumerate() {
        if *required && args.positionals.len() <= index {
            missing.push(name.to_string());
        }
    }
    if !missing.is_empty() {
        // argparse lists positionals before options.
        missing.sort_by_key(|m| m.starts_with("--"));
        return Err(usage_error(Some(command),
                               &format!("the following arguments are required: {}", missing.join(", "))));
    }
    for name in ["ttl", "max-rank"] {
        if let Some(value) = args.value(name) {
            let valid = py::strip(value).parse::<i64>().map(|n| n > 0).unwrap_or(false);
            if !valid {
                return Err(usage_error(Some(command), &format!("argument --{name}: must be a positive integer")));
            }
        }
    }
    Ok(Parsed::Run(command, args))
}

fn out(line: &str) {
    let mut stdout = std::io::stdout().lock();
    let _ = writeln!(stdout, "{line}");
}

fn err(line: &str) {
    let mut stderr = std::io::stderr().lock();
    let _ = writeln!(stderr, "{line}");
}

fn positive(args: &Args, name: &str) -> Option<u64> {
    args.value(name).and_then(|v| py::strip(v).parse::<u64>().ok())
}

/// Resolved root and configuration of the handoff the command works on.
fn context(args: &Args) -> Result<(PathBuf, Config)> {
    let root = config::resolve_root(args.root.as_deref(), None);
    let cfg = config::load_config(&root)?;
    Ok((root, cfg))
}

/// Like `context`, but the root must exist (code 2 otherwise).
fn existing_root(args: &Args) -> Result<(PathBuf, Config)> {
    let (root, cfg) = context(args)?;
    if !root.is_dir() {
        return Err(HandoffError::new(format!("{}: no handoff here (run init first)", py::show(&root)), USAGE));
    }
    Ok((root, cfg))
}

fn cmd_init(args: &Args) -> Result<i32> {
    let (root, mut cfg) = context(args)?;
    let areas = args.value("areas").map(|list| {
        list.split(',').map(py::strip).filter(|a| !a.is_empty()).map(str::to_string).collect::<Vec<_>>()
    });
    if let Some(language) = args.value("language") {
        if let Some(problem) = config::language_problem_str(language) {
            return Err(HandoffError::new(problem, USAGE));
        }
        if root.join(CONFIG_NAME).exists() {
            out(&format!("{} exists: --language ignored (use the language command to change it)",
                         py::show(&root.join(CONFIG_NAME))));
        } else {
            cfg.language = language.to_string();
        }
    }
    let owner = args.value("owner").unwrap_or("init");
    let result = init::init(&root, areas, owner, &cfg, !args.flag("no-seed"), sysinfo::local_today())?;
    for path in &result.created {
        out(&format!("created: {}", py::show(path)));
    }
    for folder in &result.reindexed {
        out(&format!("reindexed: {}", py::show(&folder.join(INDEX_NAME))));
    }
    if result.created.is_empty() {
        out(&format!("{}: already initialised, nothing created", py::show(&root)));
    }
    Ok(0)
}

fn cmd_lock(args: &Args) -> Result<i32> {
    let (root, cfg) = existing_root(args)?;
    let folder = tree::resolve_folder(&root, args.positional(0).unwrap_or("."))?;
    let ttl = positive(args, "ttl").unwrap_or(cfg.lock_ttl_seconds);
    let info = lock::acquire(&root, &folder, args.value("owner").unwrap_or(""), ttl as i64,
                             args.flag("steal-stale"), None)?;
    out(&format!("locked: {} ({}, ttl {}s)", py::show(&folder), info.owner, info.ttl_seconds));
    Ok(0)
}

fn cmd_unlock(args: &Args) -> Result<i32> {
    let (root, _) = existing_root(args)?;
    let folder = tree::resolve_folder(&root, args.positional(0).unwrap_or("."))?;
    let owner = args.value("owner").unwrap_or("");
    let info = lock::release(&root, &folder, owner, args.flag("force"), None)?;
    let forced = if info.owner != owner { " (forced, logged)" } else { "" };
    out(&format!("unlocked: {} (was {}){forced}", py::show(&folder), info.owner));
    Ok(0)
}

fn cmd_status(args: &Args) -> Result<i32> {
    let (root, _) = existing_root(args)?;
    let now = timeutil::now_seconds();
    let locks = lock::find_locks(&root)?;
    if locks.is_empty() {
        out("no locks");
    }
    for (folder, info) in locks {
        let name = py::relative_posix(&folder, &root);
        out(&format!("{}: {}", if name.is_empty() { "." } else { &name }, lock::describe(&info, now)));
    }
    Ok(0)
}

fn cmd_reindex(args: &Args) -> Result<i32> {
    let (root, cfg) = existing_root(args)?;
    let owner = args.value("owner");
    if args.flag("all") {
        if args.positional(0).is_some() || args.flag("no-lock") {
            return Err(HandoffError::new("reindex --all takes no folder and no --no-lock", USAGE));
        }
        let Some(owner) = owner.filter(|o| !o.is_empty()) else {
            return Err(HandoffError::new("reindex --all requires --owner", USAGE));
        };
        let changed = index::reindex_all(&root, owner, &cfg, None, |folder, did| {
            let state = if did { "updated" } else { "already aligned" };
            out(&format!("{}: {state}", py::show(&folder.join(INDEX_NAME))));
        })?;
        out(&format!("{} indexes updated", changed.len()));
        return Ok(0);
    }
    let Some(name) = args.positional(0).filter(|n| !n.is_empty()) else {
        return Err(HandoffError::new("reindex needs a <folder> or --all", USAGE));
    };
    let no_lock = args.flag("no-lock");
    if !no_lock && owner.is_none_or(str::is_empty) {
        return Err(HandoffError::new("reindex requires --owner (the lock holder) or --no-lock", USAGE));
    }
    let folder = tree::resolve_folder(&root, name)?;
    let changed = index::reindex(&root, &folder, if no_lock { None } else { owner }, None, &cfg)?;
    let state = if changed { "updated" } else { "already aligned" };
    out(&format!("{}: {state}", py::show(&folder.join(INDEX_NAME))));
    Ok(0)
}

fn cmd_check(args: &Args) -> Result<i32> {
    let (root, _) = context(args)?;
    let cfg = config::load_config(&root).unwrap_or_default();
    let errors = check::check(&root, None, &cfg)?;
    for line in &errors {
        out(line);
    }
    out(&if errors.is_empty() { "structure valid".to_string() } else { format!("{} errors", errors.len()) });
    Ok(if !errors.is_empty() && !args.flag("warn-only") { 1 } else { 0 })
}

fn cmd_list(args: &Args) -> Result<i32> {
    let (root, cfg) = existing_root(args)?;
    let (entries, warnings) = report::list_entries(&root, positive(args, "max-rank"), args.value("area"), &cfg)?;
    for warning in warnings {
        err(&warning);
    }
    for entry in entries {
        out(&format!("{}  {}  {}", entry.rank, entry.path, entry.summary));
    }
    Ok(0)
}

fn cmd_stats(args: &Args) -> Result<i32> {
    let (root, cfg) = existing_root(args)?;
    let (legacy, shown): (Option<PathBuf>, Option<String>) = match (args.value("legacy"), &cfg.legacy) {
        (Some(given), _) => (Some(PathBuf::from(given)), Some(py::show_str(given))),
        (None, Some(configured)) => {
            let path = root.join(configured);
            let shown = if Path::new(configured).is_absolute() {
                py::show_str(configured)
            } else {
                py::show_str(&format!("{}{}{configured}", py::show(&root), std::path::MAIN_SEPARATOR))
            };
            (Some(path), Some(shown))
        }
        (None, None) => (None, None),
    };
    let stats = report::compute_stats(&root, legacy.as_deref(), &cfg)?;
    for line in report::format_stats(&stats, shown.as_deref(), &cfg) {
        out(&line);
    }
    Ok(0)
}

fn cmd_language(args: &Args) -> Result<i32> {
    let (root, _) = existing_root(args)?;
    let Some(code) = args.positional(0) else {
        let (language, explicit) = language::current_language(&root)?;
        out(&format!("language: {language}{}", if explicit { "" } else { " (default)" }));
        return Ok(0);
    };
    let (previous, changed) = language::set_language(&root, code)?;
    if !changed {
        out(&format!("language: {code} (unchanged)"));
    } else {
        let was = previous.unwrap_or_else(|| format!("{DEFAULT_LANGUAGE}, the default"));
        out(&format!("language: {code} (was {was})"));
    }
    Ok(0)
}

fn run(argv: &[String]) -> i32 {
    if argv.is_empty() {
        // Called as a hook without arguments (a Claude Code that ignores `args`): the
        // hook input on stdin names the event.
        if let Some(input) = hook::stdin_text() {
            if hook::event_of_input(&input).is_some() {
                return hook::run(None, Some(input));
            }
        }
    }
    let parsed = match parse_args(argv) {
        Ok(parsed) => parsed,
        Err(error) => {
            err(&error.message);
            return error.code;
        }
    };
    let (command, args) = match parsed {
        Parsed::Help(text) => {
            print!("{text}");
            return 0;
        }
        Parsed::Run(command, args) => (command, args),
    };
    if command.name == "hook" {
        return hook::run(args.positional(0), None);
    }
    let outcome = match command.name {
        "init" => cmd_init(&args),
        "lock" => cmd_lock(&args),
        "unlock" => cmd_unlock(&args),
        "status" => cmd_status(&args),
        "reindex" => cmd_reindex(&args),
        "check" => cmd_check(&args),
        "list" => cmd_list(&args),
        "stats" => cmd_stats(&args),
        _ => cmd_language(&args),
    };
    match outcome {
        Ok(code) => code,
        Err(error) => {
            err(&error.message);
            error.code
        }
    }
}

fn main() {
    let argv: Vec<String> = std::env::args_os().skip(1).map(|a| a.to_string_lossy().into_owned()).collect();
    let code = run(&argv);
    let _ = std::io::stdout().flush();
    std::process::exit(code);
}
