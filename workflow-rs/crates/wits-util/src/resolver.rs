//! A small, layered lookup for "where does this setting come from?".
//!
//! Settings like the encryption password can legitimately live in several
//! places: an environment variable (handy for CI), or git config (handy for a
//! checkout you return to). We want a single, predictable order of precedence
//! and — importantly — no bootstrap loop, since the thing reading config here
//! is the same machinery that would otherwise need config to know where to
//! look.
//!
//! The one subtlety worth calling out is the *context* fallback. A repository
//! can keep several independent sets of secrets (the `default` set, a `prod`
//! set, and so on). When a non-default context is active we deliberately do
//! NOT fall back to the bare, context-less key: doing so would silently hand a
//! `prod` operation the `default` password, which is exactly the kind of
//! cross-context credential bleed that leads to encrypting data under the
//! wrong key. The bare key is only consulted for the default context.

use crate::git::Repository;

/// Which layer answered a lookup. Surfaced so a `status` view can tell the
/// user *why* it sees a particular value, which is usually the actual question
/// when secrets misbehave.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ConfigSource {
    Env,
    Git,
    Default,
}

impl std::fmt::Display for ConfigSource {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Env => "env",
            Self::Git => "git",
            Self::Default => "default",
        })
    }
}

pub struct ResolvedValue {
    pub value: String,
    pub source: ConfigSource,
}

/// Resolves keys for one namespace (`prefix`) and one `context`.
///
/// The same logical key maps to an env var (`PREFIX_[CONTEXT_]KEY`, upper-cased)
/// and a git config key (`prefix.[context.]key`, lower-cased); env always wins
/// because it is the more deliberate, ephemeral override.
pub struct Resolver<'repo> {
    repo: Option<&'repo Repository>,
    prefix: String,
    context: Option<String>,
}

impl<'repo> Resolver<'repo> {
    pub fn new(
        repo: Option<&'repo Repository>,
        prefix: impl Into<String>,
        context: Option<impl Into<String>>,
    ) -> Self {
        Self {
            repo,
            prefix: prefix.into(),
            context: context.map(Into::into),
        }
    }

    pub fn get(&self, key: &str) -> Option<ResolvedValue> {
        // Anything other than "default" is treated as an isolated context that
        // must not borrow the bare key — see the module note on credential bleed.
        let allow_bare = self.context.as_deref().is_none_or(|c| c == "default");

        if let Some(ctx) = &self.context {
            if let Some(value) = self.get_env(Some(ctx), key) {
                return Some(ResolvedValue {
                    value,
                    source: ConfigSource::Env,
                });
            }
        }
        if allow_bare {
            if let Some(value) = self.get_env(None, key) {
                return Some(ResolvedValue {
                    value,
                    source: ConfigSource::Env,
                });
            }
        }

        if let Some(repo) = self.repo {
            if let Some(ctx) = &self.context {
                if let Some(value) = self.get_git(repo, Some(ctx), key) {
                    return Some(ResolvedValue {
                        value,
                        source: ConfigSource::Git,
                    });
                }
            }
            if allow_bare {
                if let Some(value) = self.get_git(repo, None, key) {
                    return Some(ResolvedValue {
                        value,
                        source: ConfigSource::Git,
                    });
                }
            }
        }

        None
    }

    pub fn get_or_default(&self, key: &str, default_val: &str) -> ResolvedValue {
        self.get(key).unwrap_or_else(|| ResolvedValue {
            value: default_val.to_owned(),
            source: ConfigSource::Default,
        })
    }

    fn get_env(&self, ctx: Option<&str>, key: &str) -> Option<String> {
        let name = match ctx {
            Some(c) => format!("{}_{}_{}", self.prefix, c, key),
            None => format!("{}_{}", self.prefix, key),
        };
        std::env::var(name.to_uppercase()).ok()
    }

    fn get_git(&self, repo: &Repository, ctx: Option<&str>, key: &str) -> Option<String> {
        let name = match ctx {
            Some(c) => format!("{}.{}.{}", self.prefix, c, key),
            None => format!("{}.{}", self.prefix, key),
        };
        repo.get_config(&name.to_lowercase()).ok().flatten()
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::process::Command;

    #[test]
    fn falls_back_to_caller_default() {
        let resolver = Resolver::new(None::<&Repository>, "wftest", None::<String>);
        let resolved = resolver.get_or_default("missing", "fallback");
        assert_eq!(resolved.value, "fallback");
        assert_eq!(resolved.source, ConfigSource::Default);
    }

    #[test]
    fn reads_environment() {
        let resolver = Resolver::new(None::<&Repository>, "wftest", None::<String>);
        std::env::set_var("WFTEST_MYKEY", "env_value");
        let resolved = resolver.get("mykey");
        std::env::remove_var("WFTEST_MYKEY");
        let resolved = resolved.expect("env var should resolve");
        assert_eq!(resolved.value, "env_value");
        assert_eq!(resolved.source, ConfigSource::Env);
    }

    #[test]
    fn context_specific_key_wins_over_bare() {
        let resolver = Resolver::new(None::<&Repository>, "wftest2", Some("prod"));
        std::env::set_var("WFTEST2_MYKEY", "bare");
        std::env::set_var("WFTEST2_PROD_MYKEY", "scoped");
        let resolved = resolver.get("mykey");
        std::env::remove_var("WFTEST2_MYKEY");
        std::env::remove_var("WFTEST2_PROD_MYKEY");
        assert_eq!(resolved.unwrap().value, "scoped");
    }

    #[test]
    fn non_default_context_does_not_borrow_bare_key() {
        let resolver = Resolver::new(None::<&Repository>, "wftest3", Some("prod"));
        std::env::set_var("WFTEST3_MYKEY", "bare");
        let resolved = resolver.get("mykey");
        std::env::remove_var("WFTEST3_MYKEY");
        assert!(resolved.is_none());
    }

    // The git layer is the half that needs a real repository, so it earns its
    // own test: with no matching env var, a value set in git config must come
    // back tagged as coming from git.
    #[test]
    fn reads_git_config_when_env_is_absent() {
        let dir = tempfile::tempdir().unwrap();
        // force_run: tests share the global dry-run flag and run in parallel, so
        // these setup commands must execute even if another test has it toggled on.
        Command::new("git")
            .args(["init"])
            .current_dir(dir.path())
            .force_run()
            .exec()
            .unwrap();
        Command::new("git")
            .args(["config", "wfgittest.password", "from-git"])
            .current_dir(dir.path())
            .force_run()
            .exec()
            .unwrap();

        let repo = Repository::new(dir.path());
        let resolver = Resolver::new(Some(&repo), "wfgittest", Some("default"));
        let resolved = resolver.get("password").expect("git config should resolve");
        assert_eq!(resolved.value, "from-git");
        assert_eq!(resolved.source, ConfigSource::Git);
    }
}
