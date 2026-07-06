//! The data model: what a project *is*, parsed from TOML but not yet resolved.
//!
//! These types stay deliberately close to the file on disk. Templated fields
//! (`build_dir`, an `[environment]` map, …) are kept as *raw* strings and
//! `toml::Value`s here; turning them into concrete paths and command lines is
//! [`super::resolve`]'s job, and only once a [`Profile`] is supplied. Keeping the
//! two apart is the whole reason a read-only `info` never has to run a build
//! planner.
//!
//! One thing is inferred rather than declared: a repo's *kind*. A path that is
//! nested under `repos.main` and carries its own `main_branch` is a submodule; a
//! nested path without one is a subtree; anything else is standalone. Declaring
//! it would just be a fourth thing to keep consistent with the other three.

use std::collections::BTreeMap;

use serde::de::{self, Deserializer, SeqAccess, Visitor};
use serde::Deserialize;

/// A whole config file, parsed. Every section is optional so one file may carry
/// a project, toolchains, and an org at once (§10.2). Unknown keys are rejected
/// so a typo like `[toolchian]` fails loudly instead of being silently ignored.
#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct RawFile {
    pub project: Option<RawProject>,
    #[serde(default)]
    pub repos: BTreeMap<String, RawRepo>,
    pub org: Option<RawOrg>,
    #[serde(default)]
    pub toolchains: BTreeMap<String, RawToolchain>,
}

#[derive(Debug, Deserialize, Default)]
#[serde(deny_unknown_fields)]
pub struct RawProject {
    pub org: Option<String>,
    pub focus: Option<String>,
    pub build_system: Option<String>,
    pub toolchain: Option<String>,
    pub generator: Option<String>,
    pub build_dir: Option<String>,
    pub install_dir: Option<String>,
    #[serde(default)]
    pub default_presets: Vec<String>,
    #[serde(default)]
    pub environment: BTreeMap<String, toml::Value>,
    #[serde(default)]
    pub definitions: BTreeMap<String, toml::Value>,
    #[serde(default)]
    pub extra_config_args: Vec<String>,
    #[serde(default)]
    pub extra_build_args: Vec<String>,
    #[serde(default)]
    pub extra_install_args: Vec<String>,
    #[serde(default)]
    pub presets: BTreeMap<String, RawPreset>,
}

#[derive(Debug, Deserialize, Default, Clone)]
#[serde(deny_unknown_fields)]
pub struct RawRepo {
    pub path: String,
    pub main_branch: Option<String>,
    pub anchor: Option<String>,
    pub branch_strategy: Option<String>,
    pub worktree_dir: Option<String>,
    #[serde(default)]
    pub remotes: RawRemotes,
    /// Phase name (`pre_update`, `update`, `post_clone`, …) → command string.
    #[serde(default)]
    pub hooks: BTreeMap<String, String>,
    #[serde(default)]
    pub presets: BTreeMap<String, RawPreset>,
}

#[derive(Debug, Deserialize, Default, Clone)]
#[serde(deny_unknown_fields)]
pub struct RawRemotes {
    pub origin: Option<String>,
    pub upstream: Option<String>,
    #[serde(default)]
    pub mirrors: Vec<String>,
}

#[derive(Debug, Deserialize, Default, Clone)]
#[serde(deny_unknown_fields)]
pub struct RawPreset {
    #[serde(default)]
    pub extends: StringList,
    pub applies_when: Option<BTreeMap<String, toml::Value>>,
    #[serde(default)]
    pub environment: BTreeMap<String, toml::Value>,
    #[serde(default)]
    pub definitions: BTreeMap<String, toml::Value>,
    #[serde(default)]
    pub extra_config_args: Vec<String>,
    #[serde(default)]
    pub extra_build_args: Vec<String>,
    #[serde(default)]
    pub extra_install_args: Vec<String>,
}

#[derive(Debug, Deserialize, Default, Clone)]
#[serde(deny_unknown_fields)]
pub struct RawToolchain {
    pub cc: Option<String>,
    pub cxx: Option<String>,
    pub rustc: Option<String>,
    pub ar: Option<String>,
    pub nm: Option<String>,
    pub ranlib: Option<String>,
    pub strip: Option<String>,
    pub linker: Option<String>,
    pub launcher: Option<String>,
    #[serde(default)]
    pub c_flags: Vec<String>,
    #[serde(default)]
    pub cxx_flags: Vec<String>,
    #[serde(default)]
    pub link_flags: Vec<String>,
    #[serde(default)]
    pub supports: Vec<String>,
    #[serde(default)]
    pub environment: BTreeMap<String, toml::Value>,
    #[serde(default)]
    pub definitions: BTreeMap<String, toml::Value>,
}

#[derive(Debug, Deserialize, Default, Clone)]
#[serde(deny_unknown_fields)]
pub struct RawOrg {
    pub name: String,
    #[serde(default)]
    pub presets: BTreeMap<String, RawPreset>,
}

/// A field that may be written as a single string or a list of strings
/// (`extends = "base"` or `extends = ["a", "b"]`).
#[derive(Debug, Default, Clone)]
pub struct StringList(pub Vec<String>);

impl<'de> Deserialize<'de> for StringList {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        struct V;
        impl<'de> Visitor<'de> for V {
            type Value = StringList;
            fn expecting(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
                f.write_str("a string or a list of strings")
            }
            fn visit_str<E: de::Error>(self, s: &str) -> Result<StringList, E> {
                Ok(StringList(vec![s.to_owned()]))
            }
            fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<StringList, A::Error> {
                let mut out = Vec::new();
                while let Some(item) = seq.next_element::<String>()? {
                    out.push(item);
                }
                Ok(StringList(out))
            }
        }
        d.deserialize_any(V)
    }
}

/// standalone / submodule / subtree — inferred, never declared (module docs).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Kind {
    Standalone,
    Submodule,
    Subtree,
}

impl Kind {
    /// A subtree has no git of its own — it lives inside its anchor's checkout.
    pub fn has_own_git(self) -> bool {
        !matches!(self, Kind::Subtree)
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Kind::Standalone => "standalone",
            Kind::Submodule => "submodule",
            Kind::Subtree => "subtree",
        }
    }
}

/// Infer a repo's kind from its name, path, and whether it declares a main
/// branch. `repos.main` is always standalone; a nested (relative) path is a
/// submodule when it has its own `main_branch`, a subtree otherwise.
pub fn infer_kind(name: &str, repo: &RawRepo) -> Kind {
    if name == "main" || !is_nested(&repo.path) {
        Kind::Standalone
    } else if repo.main_branch.is_some() {
        Kind::Submodule
    } else {
        Kind::Subtree
    }
}

/// A path is "nested" (a subpath of `repos.main`) when it is relative — not
/// absolute and not `~`-rooted (shells usually expand `~`, but a quoted path
/// might reach us intact).
pub fn is_nested(path: &str) -> bool {
    !(path.starts_with('/') || path.starts_with('~') || std::path::Path::new(path).is_absolute())
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum BranchStrategy {
    #[default]
    InPlace,
    Worktree,
}

impl BranchStrategy {
    pub fn parse(s: Option<&str>) -> anyhow::Result<Self> {
        match s {
            None | Some("in-place") => Ok(BranchStrategy::InPlace),
            Some("worktree") => Ok(BranchStrategy::Worktree),
            Some(other) => {
                anyhow::bail!("unknown branch_strategy '{other}' (use in-place|worktree)")
            }
        }
    }
}

/// The axes that affect *resolution* (paths, identity). Built from CLI flags,
/// never from a file. Separated from [`BuildOptions`] on purpose: these change
/// what `build_dir`/`work.dir` resolve to, those change only the commands.
#[derive(Debug, Clone, Default)]
pub struct Profile {
    pub build_type: Option<String>,
    pub toolchain: Option<String>,
    pub generator: Option<String>,
    pub branch: Option<String>,
    pub presets: Vec<String>,
    /// `--focus` override; falls back to `project.focus`, then `"main"`.
    pub focus: Option<String>,
}

/// What a build *does*, not where it resolves to. Extra args are verbatim and
/// applied last, at the highest priority.
#[derive(Debug, Clone, Default)]
pub struct BuildOptions {
    pub mode: BuildMode,
    pub install: bool,
    pub target: Option<String>,
    pub extra_config_args: Vec<String>,
    pub extra_build_args: Vec<String>,
    pub extra_install_args: Vec<String>,
}

#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub enum BuildMode {
    #[default]
    Auto,
    ConfigOnly,
    BuildOnly,
    Reconfig,
    Uninstall,
}

/// A toolchain after selection: canonical fields plus verbatim pass-through
/// blocks. Backends translate the canonical fields into native form (§7); the
/// pass-through blocks are applied as-is.
#[derive(Debug, Clone, Default)]
pub struct Toolchain {
    pub name: String,
    pub cc: Option<String>,
    pub cxx: Option<String>,
    pub rustc: Option<String>,
    pub ar: Option<String>,
    pub nm: Option<String>,
    pub ranlib: Option<String>,
    pub strip: Option<String>,
    pub linker: Option<String>,
    pub launcher: Option<String>,
    pub c_flags: Vec<String>,
    pub cxx_flags: Vec<String>,
    pub link_flags: Vec<String>,
    pub environment: Vec<(String, String)>,
    pub definitions: Vec<(String, crate::core::template::Value)>,
}

/// The accumulated, resolved build configuration produced by the pipeline (§5).
/// `definitions` keep their type (bool/int/string) so a backend can spell each
/// one the way its tool expects.
#[derive(Debug, Clone, Default)]
pub struct LogicalConfig {
    pub environment: Vec<(String, String)>,
    pub definitions: Vec<(String, crate::core::template::Value)>,
    pub extra_config_args: Vec<String>,
    pub extra_build_args: Vec<String>,
    pub extra_install_args: Vec<String>,
}

impl LogicalConfig {
    /// Set an environment variable, replacing any earlier value for the key.
    /// Order is preserved by keeping the first insertion position.
    pub fn set_env(&mut self, key: impl Into<String>, value: impl Into<String>) {
        set_kv(&mut self.environment, key.into(), value.into());
    }

    #[allow(dead_code)] // part of the read-only query surface; used in tests
    pub fn env_entry(&self, key: &str) -> Option<&str> {
        self.environment
            .iter()
            .find(|(k, _)| k == key)
            .map(|(_, v)| v.as_str())
    }

    pub fn set_definition(&mut self, key: impl Into<String>, value: crate::core::template::Value) {
        set_kv(&mut self.definitions, key.into(), value);
    }

    pub fn has_definition(&self, key: &str) -> bool {
        self.definitions.iter().any(|(k, _)| k == key)
    }
}

fn set_kv<V>(list: &mut Vec<(String, V)>, key: String, value: V) {
    if let Some(slot) = list.iter_mut().find(|(k, _)| *k == key) {
        slot.1 = value;
    } else {
        list.push((key, value));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn kind_inference() {
        let sub = RawRepo {
            path: "subprojects/x".into(),
            main_branch: Some("develop".into()),
            ..Default::default()
        };
        let subtree = RawRepo {
            path: "src/gallium/lvp".into(),
            ..Default::default()
        };
        let standalone = RawRepo {
            path: "~/src/mesa".into(),
            main_branch: Some("main".into()),
            ..Default::default()
        };
        assert_eq!(infer_kind("inner", &sub), Kind::Submodule);
        assert_eq!(infer_kind("lvp", &subtree), Kind::Subtree);
        assert_eq!(infer_kind("side", &standalone), Kind::Standalone);
        // main is always standalone even with a relative path
        assert_eq!(infer_kind("main", &subtree), Kind::Standalone);
    }

    #[test]
    fn extends_accepts_string_or_list() {
        let one: RawPreset = toml::from_str(r#"extends = "base""#).unwrap();
        assert_eq!(one.extends.0, vec!["base"]);
        let many: RawPreset = toml::from_str(r#"extends = ["a", "b"]"#).unwrap();
        assert_eq!(many.extends.0, vec!["a", "b"]);
    }

    #[test]
    fn parses_a_project_file() {
        let file: RawFile = toml::from_str(
            r#"
            [project]
            focus = "main"
            build_system = "cmake"
            toolchain = "clang"
            build_dir = "{{work.dir}}/_build/{{build_type}}"

            [repos.main]
            path = "~/src/hello"
            main_branch = "main"
            [repos.main.remotes]
            origin = "git@github.com:me/hello.git"
            "#,
        )
        .unwrap();
        let project = file.project.unwrap();
        assert_eq!(project.build_system.as_deref(), Some("cmake"));
        assert_eq!(file.repos["main"].path, "~/src/hello");
        assert_eq!(
            file.repos["main"].remotes.origin.as_deref(),
            Some("git@github.com:me/hello.git")
        );
    }
}
