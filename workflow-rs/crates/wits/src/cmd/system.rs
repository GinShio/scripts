//! `wits system` — print detected host facts for shell.
//!
//! The same `system.*` tree wits resolves in project templates (see
//! [`wits_util::system`]), exposed one value per line so a hook or setup script
//! reads it directly: `cores=$(wits system cpu.count)`. A dotted path addresses
//! one fact; a leaf prints its bare value, an intermediate node prints its
//! subtree (one `path value` per line), and no argument prints the whole tree.
//! There is deliberately no `--json`: the consumers are shell, and this is a
//! flat set of scalars.

use anyhow::{anyhow, Result};
use clap::Args;

use wits_util::system;
use wits_util::template::Value;

#[derive(Debug, Args)]
pub struct SystemArgs {
    /// A dotted fact path (`cpu.count`, `os.kernel.major`, `gpu.list`, …). Omit
    /// to print the whole tree.
    pub path: Option<String>,
}

pub fn run(args: &SystemArgs) -> Result<()> {
    let facts = system::facts();
    let node = match &args.path {
        None => &facts,
        Some(path) => get(&facts, path).ok_or_else(|| anyhow!("unknown system fact '{path}'"))?,
    };

    match node {
        // A subtree: flatten to `relative.path value` lines (sorted, stable).
        Value::Map(_) => print_subtree(node, ""),
        // A list leaf, queried directly: one element per line, the shell-friendly
        // form (`for g in $(wits system gpu.list)`).
        Value::List(items) => {
            for item in items {
                println!("{}", scalar(item));
            }
        }
        // A scalar leaf: the bare value.
        scalar => println!("{}", self::scalar(scalar)),
    }
    Ok(())
}

/// Navigate a dotted path into the facts tree, `None` if any segment is missing
/// or descends through a non-map.
fn get<'a>(v: &'a Value, path: &str) -> Option<&'a Value> {
    let mut cur = v;
    for part in path.split('.') {
        match cur {
            Value::Map(map) => cur = map.get(part)?,
            _ => return None,
        }
    }
    Some(cur)
}

/// Print every leaf under `v` as `dotted.key value`, keys relative to `prefix`.
/// A list is space-joined on its key's line (the per-element form is reserved
/// for a list queried directly).
fn print_subtree(v: &Value, prefix: &str) {
    match v {
        Value::Map(map) => {
            for (key, child) in map {
                let path = if prefix.is_empty() {
                    key.clone()
                } else {
                    format!("{prefix}.{key}")
                };
                print_subtree(child, &path);
            }
        }
        other => println!("{prefix} {}", scalar(other)),
    }
}

/// A fact rendered as a single string; a list is space-joined.
fn scalar(v: &Value) -> String {
    match v {
        Value::Str(s) => s.clone(),
        Value::Int(n) => n.to_string(),
        Value::Float(f) => f.to_string(),
        Value::Bool(b) => b.to_string(),
        Value::List(items) => items.iter().map(scalar).collect::<Vec<_>>().join(" "),
        Value::Map(_) => String::new(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tree() -> Value {
        Value::map([
            (
                "cpu",
                Value::map([("count", Value::Int(8)), ("vendor", Value::str("amd"))]),
            ),
            (
                "gpu",
                Value::map([(
                    "list",
                    Value::List(vec![Value::str("amd"), Value::str("intel")]),
                )]),
            ),
            ("os", Value::str("linux")),
        ])
    }

    #[test]
    fn get_navigates_dotted_paths() {
        let t = tree();
        assert!(matches!(get(&t, "cpu.count"), Some(Value::Int(8))));
        assert!(matches!(get(&t, "cpu"), Some(Value::Map(_))));
        assert!(matches!(get(&t, "gpu.list"), Some(Value::List(_))));
        // A missing key, and descending *through* a scalar, both miss.
        assert!(get(&t, "cpu.nonesuch").is_none());
        assert!(get(&t, "os.name").is_none());
    }

    #[test]
    fn scalar_renders_leaves_and_joins_lists() {
        assert_eq!(scalar(&Value::Int(8)), "8");
        assert_eq!(scalar(&Value::Bool(true)), "true");
        assert_eq!(scalar(&Value::str("amd")), "amd");
        assert_eq!(
            scalar(&Value::List(vec![Value::str("amd"), Value::str("intel")])),
            "amd intel"
        );
    }
}
