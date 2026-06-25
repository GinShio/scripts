//! CLI plumbing: global option types, the configuration `Resolver`, and the
//! command registry.
//!
//! # Global options
//!
//! [`GlobalOptions`] carries the three flags that affect every subcommand:
//!
//! ```text
//! wf [-v] [-n] [-c <PATH>] <subcommand> [subcommand-args…]
//! ```
//!
//! # Configuration Resolver
//!
//! [`Resolver`] implements the layered configuration priority chain used by
//! `wf crypt` and any other subcommand that needs to read a keyed setting from
//! multiple sources without a bootstrap loop:
//!
//! | Priority | Source | Example key format |
//! |---|---|---|
//! | 1 (highest) | Explicit CLI argument | — |
//! | 2 | Environment variable (with context) | `TRANSCRYPT_PROD_PASSWORD` |
//! | 3 | Environment variable (no context) | `TRANSCRYPT_PASSWORD` |
//! | 4 | Git config (with context) | `transcrypt.prod.password` |
//! | 5 | Git config (no context) | `transcrypt.password` |
//! | 6 (lowest) | Default value supplied by caller | — |
//!
//! Steps 3 and 5 (fallback without context) are only attempted when the active
//! context is `"default"`.  This prevents unexpected credential leakage
//! between non-default contexts.

use std::collections::HashMap;

use crate::core::git::Repository;

// ---------------------------------------------------------------------------
// GlobalOptions
// ---------------------------------------------------------------------------

/// Options that apply to every subcommand, set via top-level flags.
#[derive(Debug, Clone, Default)]
pub struct GlobalOptions {
    /// `-v` / `--verbose`: Enable debug logging.
    pub verbose: bool,
    /// `-n` / `--dry-run`: Print actions without executing them.
    pub dry_run: bool,
    /// `-c` / `--config`: Explicit path to a TOML configuration file.
    pub config_path: Option<String>,
}

// ---------------------------------------------------------------------------
// ConfigSource
// ---------------------------------------------------------------------------

/// Identifies which layer of the priority chain produced a resolved value.
///
/// Useful for `status` subcommands that want to show the user where each
/// configuration value comes from.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfigSource {
    /// Came from an explicit CLI argument.
    Cli,
    /// Came from an environment variable.
    Env,
    /// Came from `git config`.
    Git,
    /// Came from a TOML file.
    Toml,
    /// No source found; the caller's default was used.
    Default,
}

impl std::fmt::Display for ConfigSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Cli => f.write_str("cli"),
            Self::Env => f.write_str("env"),
            Self::Git => f.write_str("git"),
            Self::Toml => f.write_str("toml"),
            Self::Default => f.write_str("default"),
        }
    }
}

// ---------------------------------------------------------------------------
// ResolvedValue
// ---------------------------------------------------------------------------

/// A configuration value together with the source it was read from.
#[derive(Debug, Clone)]
pub struct ResolvedValue {
    pub value: String,
    pub source: ConfigSource,
}

// ---------------------------------------------------------------------------
// Resolver
// ---------------------------------------------------------------------------

/// Hierarchical, context-aware configuration resolver.
///
/// `prefix` is the namespace string (e.g. `"transcrypt"`), used to construct
/// both environment variable names and git config keys:
///
/// - Environment: `<PREFIX>_[<CONTEXT>_]<KEY>` (uppercased)
/// - Git config: `<prefix>[.<context>].<key>` (lowercased)
///
/// `context` is optional (e.g. `"prod"` for a non-default transcrypt context).
/// When `context` is `None` or `Some("default")`, the resolver also tries
/// the bare key without any context segment.
pub struct Resolver<'repo> {
    /// Git repository used for `git config --get` queries (optional).
    repo: Option<&'repo Repository>,
    /// Namespace prefix, e.g. `"transcrypt"`.
    prefix: String,
    /// Active context, e.g. `Some("prod")` or `None`.
    context: Option<String>,
    /// Values explicitly injected from CLI arguments.
    cli_args: HashMap<String, String>,
}

impl<'repo> Resolver<'repo> {
    /// Creates a new resolver.
    ///
    /// - `repo`: optional reference to the current git repository.
    /// - `prefix`: the namespace, e.g. `"transcrypt"`.
    /// - `context`: the active context, or `None` for the default context.
    pub fn new(
        repo: Option<&'repo Repository>,
        prefix: impl Into<String>,
        context: Option<impl Into<String>>,
    ) -> Self {
        Self {
            repo,
            prefix: prefix.into(),
            context: context.map(Into::into),
            cli_args: HashMap::new(),
        }
    }

    /// Registers an explicit CLI override.
    ///
    /// CLI values are returned at the highest priority, before any environment
    /// or git config lookup.
    pub fn set_cli_arg(&mut self, key: impl Into<String>, value: impl Into<String>) {
        self.cli_args.insert(key.into(), value.into());
    }

    /// Resolves `key` through the priority chain.
    ///
    /// Returns `None` when none of the sources provide a value.
    pub fn get(&self, key: &str) -> Option<ResolvedValue> {
        // 1. CLI arguments
        if let Some(val) = self.cli_args.get(key) {
            return Some(ResolvedValue {
                value: val.clone(),
                source: ConfigSource::Cli,
            });
        }

        let is_default_ctx = self.context.as_deref().map_or(true, |c| c == "default");

        // 2. Environment variable with context
        if let Some(ctx) = &self.context {
            if let Some(val) = self.get_env(Some(ctx), key) {
                return Some(ResolvedValue {
                    value: val,
                    source: ConfigSource::Env,
                });
            }
        }

        // 3. Environment variable without context (only for default context)
        if is_default_ctx || self.context.is_none() {
            if let Some(val) = self.get_env(None, key) {
                return Some(ResolvedValue {
                    value: val,
                    source: ConfigSource::Env,
                });
            }
        }

        // 4. Git config with context
        if let Some(repo) = self.repo {
            if let Some(ctx) = &self.context {
                if let Some(val) = self.get_git(repo, Some(ctx), key) {
                    return Some(ResolvedValue {
                        value: val,
                        source: ConfigSource::Git,
                    });
                }
            }

            // 5. Git config without context (only for default context)
            if is_default_ctx || self.context.is_none() {
                if let Some(val) = self.get_git(repo, None, key) {
                    return Some(ResolvedValue {
                        value: val,
                        source: ConfigSource::Git,
                    });
                }
            }
        }

        None
    }

    /// Resolves `key` and falls back to `default_val` when not found.
    pub fn get_or_default<'a>(&self, key: &str, default_val: &'a str) -> ResolvedValue {
        self.get(key).unwrap_or_else(|| ResolvedValue {
            value: default_val.to_owned(),
            source: ConfigSource::Default,
        })
    }

    // -----------------------------------------------------------------------
    // Internal helpers
    // -----------------------------------------------------------------------

    /// Reads environment variable `<PREFIX>_[<CTX>_]<KEY>` (all uppercase).
    fn get_env(&self, ctx: Option<&str>, key: &str) -> Option<String> {
        let raw = match ctx {
            Some(c) => format!("{}_{}_{}", self.prefix, c, key),
            None => format!("{}_{}", self.prefix, key),
        };
        let env_key = raw.to_uppercase();
        std::env::var(&env_key).ok()
    }

    /// Reads git config key `<prefix>[.<ctx>].<key>` (all lowercase).
    fn get_git(&self, repo: &Repository, ctx: Option<&str>, key: &str) -> Option<String> {
        let raw = match ctx {
            Some(c) => format!("{}.{}.{}", self.prefix, c, key),
            None => format!("{}.{}", self.prefix, key),
        };
        let git_key = raw.to_lowercase();
        repo.get_config(&git_key).ok().flatten()
    }
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cli_arg_has_highest_priority() {
        let mut resolver = Resolver::new(None::<&Repository>, "test", None::<String>);
        assert!(resolver.get("key").is_none());

        resolver.set_cli_arg("key", "cli_value");
        let res = resolver.get("key").unwrap();
        assert_eq!(res.value, "cli_value");
        assert_eq!(res.source, ConfigSource::Cli);
    }

    #[test]
    fn get_or_default_returns_default_when_not_found() {
        let resolver = Resolver::new(None::<&Repository>, "test", None::<String>);
        let res = resolver.get_or_default("missing", "fallback");
        assert_eq!(res.value, "fallback");
        assert_eq!(res.source, ConfigSource::Default);
    }

    #[test]
    fn env_var_is_resolved() {
        let resolver = Resolver::new(None::<&Repository>, "wftest", None::<String>);
        std::env::set_var("WFTEST_MYKEY", "env_value");
        let res = resolver.get("mykey");
        std::env::remove_var("WFTEST_MYKEY");
        assert!(res.is_some());
        let rv = res.unwrap();
        assert_eq!(rv.value, "env_value");
        assert_eq!(rv.source, ConfigSource::Env);
    }

    #[test]
    fn context_env_var_takes_priority_over_bare() {
        let resolver = Resolver::new(None::<&Repository>, "wftest2", Some("prod"));
        std::env::set_var("WFTEST2_MYKEY", "bare");
        std::env::set_var("WFTEST2_PROD_MYKEY", "with_ctx");
        let res = resolver.get("mykey");
        std::env::remove_var("WFTEST2_MYKEY");
        std::env::remove_var("WFTEST2_PROD_MYKEY");
        let rv = res.unwrap();
        assert_eq!(rv.value, "with_ctx");
    }
}
