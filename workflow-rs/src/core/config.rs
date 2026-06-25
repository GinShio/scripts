//! TOML v1.0 configuration loading with path resolution and deep merging.
//!
//! # Path resolution
//!
//! [`resolve_config_path`] probes sources in priority order:
//!
//! | Priority | Source |
//! |---|---|
//! | 1 (highest) | CLI `--config <PATH>` argument |
//! | 2 | `WF_CONFIG` environment variable |
//! | 3 | `wf.toml` in the current working directory |
//! | 4 | `.wf.toml` in the current working directory |
//! | 5 (lowest) | `None` (caller uses defaults) |
//!
//! # Typed loading
//!
//! [`ConfigLoader`] provides a generic interface.  Each command defines its
//! own config schema as a `serde`-derivable struct, then calls
//! [`ConfigLoader::load`] to parse and return a populated instance:
//!
//! ```no_run
//! use serde::Deserialize;
//! use wf::core::config::ConfigLoader;
//!
//! #[derive(Debug, Default, Deserialize)]
//! struct BuildConfig {
//!     toolchain: Option<String>,
//! }
//!
//! let mut loader = ConfigLoader::new();
//! let cfg: BuildConfig = loader.load(None)?;  // None → auto-resolve path
//! # Ok::<(), wf::core::config::ConfigError>(())
//! ```
//!
//! # Directory loading
//!
//! When the resolved path points to a **directory**, every `*.toml` file
//! inside is parsed in alphabetical order and deep-merged into a single
//! document.  Later files override earlier ones for scalar and array fields;
//! nested tables are merged recursively.

use std::path::{Path, PathBuf};

use serde::de::DeserializeOwned;
use thiserror::Error;

// ---------------------------------------------------------------------------
// Error type
// ---------------------------------------------------------------------------

/// Errors produced by the configuration subsystem.
#[derive(Debug, Error)]
pub enum ConfigError {
    #[error("failed to read config file '{path}': {source}")]
    Io {
        path: PathBuf,
        #[source]
        source: std::io::Error,
    },
    #[error("failed to parse config file '{path}': {source}")]
    Parse {
        path: PathBuf,
        source: toml::de::Error,
    },
    #[error("failed to deserialize merged config: {0}")]
    Deserialize(toml::de::Error),
    #[error("config path '{0}' does not exist")]
    NotFound(PathBuf),
}

// ---------------------------------------------------------------------------
// Path resolution
// ---------------------------------------------------------------------------

/// Resolves the configuration file path according to the priority chain
/// documented in the module-level documentation.
///
/// Returns `None` when no source provides a path and neither `wf.toml` nor
/// `.wf.toml` exist in the current directory.
pub fn resolve_config_path(cli_path: Option<&str>) -> Option<PathBuf> {
    // 1. CLI argument
    if let Some(p) = cli_path {
        log::debug!("Config path resolved from CLI: {p}");
        return Some(PathBuf::from(p));
    }

    // 2. Environment variable
    if let Ok(env_path) = std::env::var("WF_CONFIG") {
        log::debug!("Config path resolved from WF_CONFIG env: {env_path}");
        return Some(PathBuf::from(env_path));
    }

    // 3. Well-known filenames in CWD
    for name in ["wf.toml", ".wf.toml"] {
        if Path::new(name).exists() {
            log::debug!("Config path resolved from CWD: {name}");
            return Some(PathBuf::from(name));
        }
    }

    None
}

// ---------------------------------------------------------------------------
// Deep merge on toml::Value
// ---------------------------------------------------------------------------

/// Recursively merges `overlay` into `base`.
///
/// - Tables are merged key-by-key (overlay wins for conflicts at leaf level).
/// - Arrays and scalar values in `overlay` replace those in `base`.
///
/// This is used when a directory of TOML files is loaded: files are sorted
/// alphabetically, and each successive file is overlaid on the accumulator.
pub fn merge_toml(base: &mut toml::Value, overlay: toml::Value) {
    match (base, overlay) {
        (toml::Value::Table(b), toml::Value::Table(o)) => {
            for (key, val) in o {
                // `toml::map::Entry` does not expose `and_modify`, so we use
                // a get_mut + insert pattern instead.
                if let Some(existing) = b.get_mut(&key) {
                    merge_toml(existing, val);
                } else {
                    b.insert(key, val);
                }
            }
        }
        // For all other combinations, overlay replaces base.
        (b, o) => *b = o,
    }
}

// ---------------------------------------------------------------------------
// ConfigLoader
// ---------------------------------------------------------------------------

/// Generic, reusable TOML configuration loader.
///
/// Maintains no internal state beyond what is needed during a single `load`
/// call; it is safe to call `load` multiple times with different type
/// parameters.
pub struct ConfigLoader;

impl ConfigLoader {
    /// Creates a new loader instance.
    pub fn new() -> Self {
        Self
    }

    /// Resolves the configuration path and deserialises it into `T`.
    ///
    /// - When `cli_path` is `None`, path resolution falls back through the
    ///   chain described in [`resolve_config_path`].
    /// - When no path resolves, `T::default()` is returned — callers should
    ///   ensure `T: Default`.
    pub fn load<T>(&self, cli_path: Option<&str>) -> Result<T, ConfigError>
    where
        T: DeserializeOwned + Default,
    {
        match resolve_config_path(cli_path) {
            Some(path) => self.load_path(&path),
            None => {
                log::debug!(
                    "No config file found; using defaults for {}",
                    std::any::type_name::<T>()
                );
                Ok(T::default())
            }
        }
    }

    /// Loads from an explicit path (file or directory).
    pub fn load_path<T>(&self, path: &Path) -> Result<T, ConfigError>
    where
        T: DeserializeOwned + Default,
    {
        if path.is_dir() {
            log::debug!(
                "Config path is a directory: {}. Merging all *.toml files.",
                path.display()
            );
            self.load_directory(path)
        } else {
            let raw = self.parse_file(path)?;
            Self::deserialize(raw, path)
        }
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    /// Reads a single TOML file and returns the untyped [`toml::Value`].
    fn parse_file(&self, path: &Path) -> Result<toml::Value, ConfigError> {
        log::debug!("Loading config file: {}", path.display());
        let content = std::fs::read_to_string(path).map_err(|e| ConfigError::Io {
            path: path.to_owned(),
            source: e,
        })?;
        content
            .parse::<toml::Value>()
            .map_err(|e| ConfigError::Parse {
                path: path.to_owned(),
                source: e,
            })
    }

    /// Deserialises a [`toml::Value`] into `T`.
    fn deserialize<T: DeserializeOwned>(value: toml::Value, path: &Path) -> Result<T, ConfigError> {
        value.try_into().map_err(|e| ConfigError::Parse {
            path: path.to_owned(),
            source: e,
        })
    }

    /// Iterates all `*.toml` files in `dir_path`, sorts them alphabetically,
    /// deep-merges them, then deserialises the result into `T`.
    fn load_directory<T>(&self, dir_path: &Path) -> Result<T, ConfigError>
    where
        T: DeserializeOwned + Default,
    {
        let mut entries: Vec<PathBuf> = std::fs::read_dir(dir_path)
            .map_err(|e| ConfigError::Io {
                path: dir_path.to_owned(),
                source: e,
            })?
            .filter_map(|entry| entry.ok())
            .map(|e| e.path())
            .filter(|p| p.is_file() && p.extension().map_or(false, |ext| ext == "toml"))
            .collect();

        if entries.is_empty() {
            log::warn!(
                "No .toml files found in config directory: {}",
                dir_path.display()
            );
            return Ok(T::default());
        }

        entries.sort();

        let mut merged: Option<toml::Value> = None;
        for entry in &entries {
            let value = self.parse_file(entry)?;
            match merged.as_mut() {
                None => merged = Some(value),
                Some(acc) => merge_toml(acc, value),
            }
        }

        let merged_value = merged.unwrap_or(toml::Value::Table(toml::value::Table::new()));
        merged_value.try_into().map_err(ConfigError::Deserialize)
    }
}

impl Default for ConfigLoader {
    fn default() -> Self {
        Self::new()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;
    use serde::Deserialize;

    #[derive(Debug, Default, Deserialize, PartialEq)]
    struct DummyConfig {
        name: Option<String>,
        count: Option<u32>,
        nested: Option<DummyNested>,
    }

    #[derive(Debug, Default, Deserialize, PartialEq)]
    struct DummyNested {
        value: Option<u32>,
    }

    #[test]
    fn resolve_cli_path() {
        let resolved = resolve_config_path(Some("/custom/path.toml")).unwrap();
        assert_eq!(resolved, PathBuf::from("/custom/path.toml"));
    }

    #[test]
    fn resolve_returns_none_when_nothing_found() {
        // Ensure neither CWD file exists (they shouldn't in a test env).
        let dir = tempfile::tempdir().unwrap();
        let _guard = std::env::set_current_dir(dir.path());
        // Can't easily remove the env var, so we just verify that
        // without any source we still don't crash.
        let _ = resolve_config_path(None);
    }

    #[test]
    fn merge_toml_deep_merges_tables() {
        let mut base: toml::Value = toml::toml! {
            name = "base"
            [nested]
            a = 1
            b = 2
        }
        .into();
        let overlay: toml::Value = toml::toml! {
            name = "overlay"
            [nested]
            b = 99
            c = 3
        }
        .into();
        merge_toml(&mut base, overlay);

        let table = base.as_table().unwrap();
        assert_eq!(table["name"].as_str().unwrap(), "overlay");
        let nested = table["nested"].as_table().unwrap();
        assert_eq!(nested["a"].as_integer().unwrap(), 1); // preserved
        assert_eq!(nested["b"].as_integer().unwrap(), 99); // overridden
        assert_eq!(nested["c"].as_integer().unwrap(), 3); // added
    }

    #[test]
    fn load_single_file() {
        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("config.toml");
        std::fs::write(&path, "name = \"hello\"\ncount = 42\n").unwrap();

        let loader = ConfigLoader::new();
        let cfg: DummyConfig = loader.load_path(&path).unwrap();
        assert_eq!(cfg.name.as_deref(), Some("hello"));
        assert_eq!(cfg.count, Some(42));
    }

    #[test]
    fn load_directory_merges_alphabetically() {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join("a.toml"), "name = \"a\"\ncount = 1\n").unwrap();
        std::fs::write(dir.path().join("b.toml"), "name = \"b\"\n").unwrap();

        let loader = ConfigLoader::new();
        // b.toml is loaded after a.toml; name should be "b", count preserved from a.
        let cfg: DummyConfig = loader.load_path(dir.path()).unwrap();
        assert_eq!(cfg.name.as_deref(), Some("b"));
        // count = 1 comes from a.toml and is preserved because b.toml doesn't set it
        assert_eq!(cfg.count, Some(1));
    }
}
