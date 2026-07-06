//! Build systems — the tool's one extension axis.
//!
//! A new build system is a new [`Backend`] impl plus a line in [`for_system`];
//! the core and the resolver know nothing about any concrete backend. A backend
//! does exactly three things (§7): translate the *canonical* toolchain vocabulary
//! into its native form, emit the ordered command steps for a build mode, and
//! detect prior configuration. The definition→argv *spelling* (`-DK:TYPE=V` vs
//! `-Dk=v`) is private to each backend, never leaked outward.

mod cargo;
mod cmake;
mod meson;

use std::path::{Path, PathBuf};

use crate::core::template::Value;

use super::model::{BuildMode, LogicalConfig, Toolchain};

/// One command to run. Env is applied by the executor from the resolved
/// [`LogicalConfig`], so a step is just "what to run, and where".
#[derive(Debug, Clone)]
pub struct Step {
    pub description: String,
    pub program: String,
    pub args: Vec<String>,
    pub cwd: PathBuf,
}

impl Step {
    fn new(
        description: impl Into<String>,
        program: impl Into<String>,
        args: Vec<String>,
        cwd: &Path,
    ) -> Self {
        Step {
            description: description.into(),
            program: program.into(),
            args,
            cwd: cwd.to_path_buf(),
        }
    }
}

/// Everything a backend needs to emit steps. Assembled by the build action from
/// the resolved plan.
pub struct EmitContext<'a> {
    pub source_dir: &'a Path,
    pub build_dir: &'a Path,
    pub install_dir: Option<&'a Path>,
    pub build_type: &'a str,
    pub generator: Option<&'a str>,
    pub target: Option<&'a str>,
    pub logical: &'a LogicalConfig,
    pub mode: BuildMode,
    pub install: bool,
}

pub trait Backend {
    fn name(&self) -> &str;

    /// Translate the toolchain's canonical fields into this backend's native
    /// env/definitions, merging into `cfg`. Runs at pipeline L0, so a later
    /// preset or CLI override of the same key wins.
    fn apply_toolchain(&self, tc: &Toolchain, cfg: &mut LogicalConfig);

    /// The ordered command steps for the requested mode.
    fn steps(&self, ctx: &EmitContext<'_>) -> anyhow::Result<Vec<Step>>;

    /// Whether `build_dir` already holds a configured build.
    fn is_configured(&self, build_dir: &Path) -> bool;
}

/// The backend for a `build_system` name, or `None` if unsupported.
pub fn for_system(name: &str) -> Option<Box<dyn Backend>> {
    match name {
        "cmake" => Some(Box::new(cmake::Cmake)),
        "meson" => Some(Box::new(meson::Meson)),
        "cargo" => Some(Box::new(cargo::Cargo)),
        _ => None,
    }
}

/// Set the universal, tool-agnostic environment variables every backend honours.
/// A launcher is *not* folded in here — that is each backend's native concern
/// (cmake a definition, meson a `CC` prefix, cargo `RUSTC_WRAPPER`).
fn set_universal_env(tc: &Toolchain, cfg: &mut LogicalConfig) {
    let pairs = [
        ("CC", &tc.cc),
        ("CXX", &tc.cxx),
        ("RUSTC", &tc.rustc),
        ("AR", &tc.ar),
        ("NM", &tc.nm),
        ("RANLIB", &tc.ranlib),
        ("STRIP", &tc.strip),
    ];
    for (key, value) in pairs {
        if let Some(v) = value {
            cfg.set_env(key, v.clone());
        }
    }
    if !tc.c_flags.is_empty() {
        cfg.set_env("CFLAGS", tc.c_flags.join(" "));
    }
    if !tc.cxx_flags.is_empty() {
        cfg.set_env("CXXFLAGS", tc.cxx_flags.join(" "));
    }
    if !tc.link_flags.is_empty() {
        cfg.set_env("LDFLAGS", tc.link_flags.join(" "));
    }
}

/// Apply the toolchain's verbatim pass-through blocks. Done after the derived
/// translation so an explicitly-declared toolchain env/definition wins over the
/// value derived from a canonical field.
fn apply_passthrough(tc: &Toolchain, cfg: &mut LogicalConfig) {
    for (k, v) in &tc.environment {
        cfg.set_env(k.clone(), v.clone());
    }
    for (k, v) in &tc.definitions {
        cfg.set_definition(k.clone(), v.clone());
    }
}

/// Render a definition value for a cmake `-D` flag (`KEY:TYPE=VALUE`).
fn cmake_definition(key: &str, value: &Value) -> String {
    match value {
        Value::Bool(b) => format!("{key}:BOOL={}", if *b { "ON" } else { "OFF" }),
        Value::Int(n) => format!("{key}:STRING={n}"),
        Value::Float(f) => format!("{key}:STRING={f}"),
        Value::Str(s) => format!("{key}:STRING={s}"),
        _ => format!("{key}:STRING="),
    }
}

/// Render a definition value for a meson `-D` option (`key=value`).
fn meson_definition(key: &str, value: &Value) -> String {
    let v = match value {
        Value::Bool(b) => b.to_string(),
        Value::Int(n) => n.to_string(),
        Value::Float(f) => f.to_string(),
        Value::Str(s) => s.clone(),
        _ => String::new(),
    };
    format!("{key}={v}")
}
