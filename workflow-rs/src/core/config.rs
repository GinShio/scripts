//! Locating and enumerating a tool's config *tree*.
//!
//! Where [`super::resolver`] resolves a single *setting* by precedence (an env
//! var over a git-config value), this module answers the coarser question:
//! *where does this tool keep its config directory, and what `*.toml` files live
//! under it?* The env -> XDG -> HOME search and the recursive scan are generic
//! OS-convention plumbing with no domain knowledge, so they sit on the floor
//! rather than inside any one command. A subsystem supplies its own [`Root`]
//! naming, then routes or merges the discovered files however it likes — this
//! layer only finds them.

use std::path::{Path, PathBuf};

use anyhow::{bail, Result};

/// How a tool names its config directory across the standard search locations.
pub struct Root<'a> {
    /// An absolute override read from this environment variable; when set it
    /// must point at an existing directory.
    pub env: &'a str,
    /// The path under `$XDG_CONFIG_HOME`.
    pub xdg: &'a str,
    /// The path under `$HOME`.
    pub home: &'a str,
}

/// Resolve the single config root: `$<env>` (which must exist if set), then the
/// first existing of `$XDG_CONFIG_HOME/<xdg>` and `$HOME/<home>`.
pub fn resolve_root(spec: &Root<'_>) -> Result<PathBuf> {
    if let Some(env) = std::env::var_os(spec.env) {
        let path = PathBuf::from(env);
        if !path.is_dir() {
            bail!(
                "{} points at {} which is not a directory",
                spec.env,
                path.display()
            );
        }
        return Ok(path);
    }

    let mut candidates = Vec::new();
    if let Some(xdg) = std::env::var_os("XDG_CONFIG_HOME") {
        candidates.push(PathBuf::from(xdg).join(spec.xdg));
    }
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(PathBuf::from(home).join(spec.home));
    }
    for candidate in &candidates {
        if candidate.is_dir() {
            return Ok(candidate.clone());
        }
    }
    bail!(
        "no config root found (looked for {})",
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    );
}

/// Every `*.toml` under `root`, recursively, in sorted order. A `root` that does
/// not exist yields an empty list rather than an error, so a tool with no config
/// installed simply sees nothing to load.
pub fn discover_toml(root: &Path) -> std::io::Result<Vec<PathBuf>> {
    let mut out = Vec::new();
    scan(root, &mut out)?;
    out.sort();
    Ok(out)
}

fn scan(dir: &Path, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(dir)? {
        let path = entry?.path();
        if path.is_dir() {
            scan(&path, out)?;
        } else if path.extension().and_then(|e| e.to_str()) == Some("toml") {
            out.push(path);
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn discovers_toml_recursively_and_sorted() {
        let dir = tempfile::tempdir().unwrap();
        let root = dir.path();
        std::fs::create_dir_all(root.join("a/b")).unwrap();
        std::fs::write(root.join("z.toml"), "").unwrap();
        std::fs::write(root.join("a/m.toml"), "").unwrap();
        std::fs::write(root.join("a/b/c.toml"), "").unwrap();
        std::fs::write(root.join("a/skip.txt"), "").unwrap();

        let found = discover_toml(root).unwrap();

        // Recursive (nested c.toml found), extension-filtered (skip.txt absent),
        // and returned in sorted path order.
        assert_eq!(
            found,
            vec![
                root.join("a/b/c.toml"),
                root.join("a/m.toml"),
                root.join("z.toml"),
            ]
        );
    }

    #[test]
    fn missing_root_is_empty_not_an_error() {
        let dir = tempfile::tempdir().unwrap();
        let absent = dir.path().join("does-not-exist");
        assert!(discover_toml(&absent).unwrap().is_empty());
    }
}
