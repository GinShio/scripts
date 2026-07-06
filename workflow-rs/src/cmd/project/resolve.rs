//! The resolution pipeline (§5): turn a project + a [`Profile`] into concrete
//! paths and one accumulated [`LogicalConfig`], in a single left-to-right pass.
//!
//! The pass is strictly one-directional — toolchain → project → presets → CLI —
//! and no later layer can overwrite a toolchain's compiler identity, so nothing
//! is ever re-asserted or recomputed. Context values may reference each other
//! (`env.BIN` from `env.TOOLS`); the template engine resolves those lazily, so
//! the order entries appear in a map never matters.
//!
//! Everything here is pure: given the same inputs it produces the same plan, and
//! it touches neither git nor the filesystem (beyond turning `~` into an absolute
//! path). That is what lets `info` report without running a build.

use std::collections::BTreeMap;
use std::path::PathBuf;

use anyhow::{bail, Context, Result};

use crate::core::template::{Engine, Value};

use super::model::{
    infer_kind, BranchStrategy, LogicalConfig, Profile, RawPreset, RawToolchain, Toolchain,
};
use super::workspace::{ProjectData, Workspace};

/// A fully resolved build plan: everything `build` executes and `info` reports.
pub struct Plan {
    pub focus: String,
    /// The repo the build sources from (the focus's `anchor`, or the focus).
    pub build_repo: String,
    /// The nearest own-git repo from the focus — what carries branch identity and
    /// is switched to the target branch.
    pub identity_repo: Option<String>,
    pub strategy: BranchStrategy,
    pub branch_raw: String,
    pub branch_slug: String,
    pub build_type: String,
    pub generator: Option<String>,
    /// The resolved build system. Part of the read-only query surface (§11);
    /// `build` reads it from project config pre-planning (to pick a backend
    /// before it has a plan), so this copy is currently for consumers/`info`.
    #[allow(dead_code)]
    pub build_system: Option<String>,
    pub toolchain: Option<Toolchain>,
    pub work_dir: PathBuf,
    pub build_dir: Option<PathBuf>,
    pub install_dir: Option<PathBuf>,
    pub logical: LogicalConfig,
    /// The final context, so callers can resolve arbitrary templates or inspect.
    /// Part of the read-only query surface (§11); not consumed in-tree yet.
    #[allow(dead_code)]
    pub context: Value,
}

/// The one build-system responsibility the pipeline needs: translate a selected
/// [`Toolchain`]'s canonical fields into a backend's native env/definitions at
/// L0 (§5.4). This is the *only* seam between the read-only core and the build
/// systems — the core owns the trait, but the concrete backends that implement
/// it live entirely in `cmd::build` (§1.4). The core never names a backend, and
/// callers that only resolve *paths* (`context`, `info`) inject nothing.
pub trait ToolchainInjector {
    /// Merge the toolchain's native env/definitions into `cfg`. Runs at L0, so a
    /// later preset or CLI override of the same key wins.
    fn apply_toolchain(&self, tc: &Toolchain, cfg: &mut LogicalConfig);
}

/// Inputs that vary per invocation but are not part of the file model.
///
/// Deliberately *not* `build::BuildOptions`: the pipeline only ever needs the
/// verbatim L3 overrides (§5.5), not the build action's `mode`/`install`/
/// `target`, so callers that just resolve paths (`context`, `info --check`)
/// can leave those empty instead of fabricating a whole `BuildOptions`.
pub struct PlanInput<'a> {
    pub profile: &'a Profile,
    /// The target branch (from `--branch` or the caller's git read).
    pub branch: &'a str,
    /// Whether to inject the toolchain's env/definitions (skipped when trusting
    /// an already-configured build dir; §5.3). Selection still happens.
    pub inject_toolchain: bool,
    /// The build system's toolchain translator (§5.4). `None` for path-only
    /// resolves; L0 is skipped when it is absent even if `inject_toolchain`.
    pub injector: Option<&'a dyn ToolchainInjector>,
    /// L3 — verbatim overrides, applied last, at the highest priority.
    pub extra_config_args: &'a [String],
    pub extra_build_args: &'a [String],
    pub extra_install_args: &'a [String],
}

pub fn plan(ws: &Workspace, project: &ProjectData, input: &PlanInput<'_>) -> Result<Plan> {
    let profile = input.profile;
    let focus = project.focus_name(profile.focus.as_deref()).to_owned();
    if !project.repos.contains_key(&focus) {
        bail!(
            "focus repo '{focus}' is not defined in project '{}'",
            project.name
        );
    }
    let build_repo = anchor_of(project, &focus);
    if !project.repos.contains_key(&build_repo) {
        bail!("anchor repo '{build_repo}' (of focus '{focus}') is not defined");
    }
    let identity_repo = identity_repo(project, &focus);
    let strategy = BranchStrategy::parse(project.repos[&build_repo].branch_strategy.as_deref())?;

    let branch_raw = input.branch.to_owned();
    let branch_slug = slugify(&branch_raw);
    let build_type = profile
        .build_type
        .clone()
        .unwrap_or_else(|| "debug".to_owned());
    let generator = profile
        .generator
        .clone()
        .or_else(|| project.project.generator.clone());
    let build_system = project.project.build_system.clone();

    // Toolchain *selection* always happens (path templates depend on the name).
    let toolchain = select_toolchain(ws, project, profile)?;

    // --- Base context ------------------------------------------------------
    let mut ctx = Ctx::new();
    ctx.set("project.name", Value::str(&project.name));
    ctx.set(
        "project.org",
        Value::str(project.org.clone().unwrap_or_default()),
    );
    ctx.set("project.focus", Value::str(&focus));
    for name in project.repos.keys() {
        ctx.set(&format!("repos.{name}"), repo_value(project, name));
    }
    ctx.root.insert_path("repo", repo_value(project, &focus));
    ctx.set("branch.raw", Value::str(&branch_raw));
    ctx.set("branch.slug", Value::str(&branch_slug));
    ctx.set("build_type", Value::str(&build_type));
    if let Some(gen) = &generator {
        ctx.set("generator", Value::str(gen));
    }
    ctx.set("system", system_facts());
    // Process environment as the env.* base.
    let mut env_map = BTreeMap::new();
    for (k, v) in std::env::vars() {
        env_map.insert(k, Value::Str(v));
    }
    ctx.root.insert_path("env", Value::Map(env_map));

    // Resolve the toolchain against the base context and expose it as toolchain.*.
    let toolchain = match toolchain {
        Some((name, raw)) => Some(resolve_toolchain(&mut ctx, name, &raw)?),
        None => None,
    };

    // --- Paths -------------------------------------------------------------
    let work_dir = resolve_work_dir(project, &ctx, &build_repo, strategy)?;
    ctx.set("work.dir", Value::str(work_dir.display().to_string()));

    let build_dir = match &project.project.build_dir {
        Some(tpl) => Some(PathBuf::from(ctx.render(tpl)?)),
        None => None,
    };
    let install_dir = match &project.project.install_dir {
        Some(tpl) => Some(PathBuf::from(ctx.render(tpl)?)),
        None => None,
    };

    // --- Pipeline ----------------------------------------------------------
    let mut logical = LogicalConfig::default();

    // L0 — toolchain injection. The build system's translator (§5.4) is supplied
    // by the caller; a path-only resolve has none, and simply skips this layer.
    if input.inject_toolchain {
        if let (Some(tc), Some(inj)) = (&toolchain, input.injector) {
            inj.apply_toolchain(tc, &mut logical);
            fold_env(&mut ctx, &logical);
        }
    }

    // L1 — project config.
    apply_env_map(
        &mut ctx,
        &mut logical,
        "project.environment",
        &project.project.environment,
    )?;
    apply_def_map(
        &mut ctx,
        &mut logical,
        "project.definitions",
        &project.project.definitions,
    )?;
    resolve_args(
        &ctx,
        &project.project.extra_config_args,
        &mut logical.extra_config_args,
    )?;
    resolve_args(
        &ctx,
        &project.project.extra_build_args,
        &mut logical.extra_build_args,
    )?;
    resolve_args(
        &ctx,
        &project.project.extra_install_args,
        &mut logical.extra_install_args,
    )?;

    // L2 — presets.
    let names = applied_presets(
        ws,
        project,
        &focus,
        profile,
        &toolchain,
        &build_type,
        &generator,
    );
    for name in &names {
        let mut seen = Vec::new();
        resolve_preset_into(&mut ctx, &mut logical, ws, project, &focus, name, &mut seen)?;
    }

    // L3 — CLI extra args (verbatim, highest priority).
    logical
        .extra_config_args
        .extend(input.extra_config_args.iter().cloned());
    logical
        .extra_build_args
        .extend(input.extra_build_args.iter().cloned());
    logical
        .extra_install_args
        .extend(input.extra_install_args.iter().cloned());

    Ok(Plan {
        focus,
        build_repo,
        identity_repo,
        strategy,
        branch_raw,
        branch_slug,
        build_type,
        generator,
        build_system,
        toolchain,
        work_dir,
        build_dir,
        install_dir,
        logical,
        context: ctx.root,
    })
}

// --- focus / anchor / identity ------------------------------------------------

/// The repo the build sources from: the focus's `anchor`, or the focus itself.
pub fn anchor_of(project: &ProjectData, focus: &str) -> String {
    project
        .repos
        .get(focus)
        .and_then(|r| r.anchor.clone())
        .unwrap_or_else(|| focus.to_owned())
}

/// The nearest own-git repo starting from the focus: the focus itself if it has
/// its own git, otherwise its anchor (a subtree shares its anchor's git).
pub fn identity_repo(project: &ProjectData, focus: &str) -> Option<String> {
    let mut name = focus.to_owned();
    for _ in 0..project.repos.len() + 1 {
        let repo = project.repos.get(&name)?;
        if infer_kind(&name, repo).has_own_git() {
            return Some(name);
        }
        name = repo.anchor.clone()?;
    }
    None
}

// --- toolchain ----------------------------------------------------------------

/// Select the toolchain by name via the chain: env → `--toolchain` → the
/// project's `toolchain` field. (Env wins, per the codebase's "env is the
/// deliberate override" rule.) Returns the name and its raw definition.
fn select_toolchain(
    ws: &Workspace,
    project: &ProjectData,
    profile: &Profile,
) -> Result<Option<(String, RawToolchain)>> {
    let name = std::env::var("WITS_PROJECT_TOOLCHAIN")
        .ok()
        .filter(|s| !s.is_empty())
        .or_else(|| profile.toolchain.clone())
        .or_else(|| project.project.toolchain.clone());
    let Some(name) = name else {
        return Ok(None);
    };
    let raw =
        ws.toolchains().get(&name).cloned().with_context(|| {
            format!("unknown toolchain '{name}' (none is declared by that name)")
        })?;
    Ok(Some((name, raw)))
}

fn resolve_toolchain(ctx: &mut Ctx, name: String, raw: &RawToolchain) -> Result<Toolchain> {
    let opt = |ctx: &Ctx, s: &Option<String>| -> Result<Option<String>> {
        match s {
            Some(v) => Ok(Some(ctx.render(v)?)),
            None => Ok(None),
        }
    };
    let list = |ctx: &Ctx, xs: &[String]| -> Result<Vec<String>> {
        xs.iter().map(|x| ctx.render(x)).collect()
    };
    let environment = raw
        .environment
        .iter()
        .map(|(k, v)| Ok((k.clone(), ctx.render_value(&v.clone().into())?)))
        .collect::<Result<Vec<_>>>()?;
    let definitions = raw
        .definitions
        .iter()
        .map(|(k, v)| Ok((k.clone(), ctx.engine().resolve(&v.clone().into())?)))
        .collect::<Result<Vec<_>>>()?;

    let tc = Toolchain {
        cc: opt(ctx, &raw.cc)?,
        cxx: opt(ctx, &raw.cxx)?,
        rustc: opt(ctx, &raw.rustc)?,
        ar: opt(ctx, &raw.ar)?,
        nm: opt(ctx, &raw.nm)?,
        ranlib: opt(ctx, &raw.ranlib)?,
        strip: opt(ctx, &raw.strip)?,
        linker: opt(ctx, &raw.linker)?,
        launcher: opt(ctx, &raw.launcher)?,
        c_flags: list(ctx, &raw.c_flags)?,
        cxx_flags: list(ctx, &raw.cxx_flags)?,
        link_flags: list(ctx, &raw.link_flags)?,
        environment,
        definitions,
        name: name.clone(),
    };

    // Expose toolchain.* so config can reference {{toolchain.cc}} etc.
    let mut m = BTreeMap::new();
    m.insert("name".into(), Value::str(&name));
    let put = |m: &mut BTreeMap<String, Value>, k: &str, v: &Option<String>| {
        m.insert(k.into(), Value::str(v.clone().unwrap_or_default()));
    };
    put(&mut m, "cc", &tc.cc);
    put(&mut m, "cxx", &tc.cxx);
    put(&mut m, "rustc", &tc.rustc);
    put(&mut m, "ar", &tc.ar);
    put(&mut m, "nm", &tc.nm);
    put(&mut m, "ranlib", &tc.ranlib);
    put(&mut m, "strip", &tc.strip);
    put(&mut m, "linker", &tc.linker);
    put(&mut m, "launcher", &tc.launcher);
    m.insert("c_flags".into(), Value::str(tc.c_flags.join(" ")));
    m.insert("cxx_flags".into(), Value::str(tc.cxx_flags.join(" ")));
    m.insert("link_flags".into(), Value::str(tc.link_flags.join(" ")));
    ctx.root.insert_path("toolchain", Value::Map(m));

    Ok(tc)
}

// --- paths --------------------------------------------------------------------

fn resolve_work_dir(
    project: &ProjectData,
    ctx: &Ctx,
    build_repo: &str,
    strategy: BranchStrategy,
) -> Result<PathBuf> {
    match strategy {
        BranchStrategy::InPlace => project
            .repo_abs_path(build_repo)
            .with_context(|| format!("cannot resolve path of repo '{build_repo}'")),
        BranchStrategy::Worktree => {
            let tpl = project.repos[build_repo]
                .worktree_dir
                .as_deref()
                .with_context(|| {
                    format!("repo '{build_repo}' uses worktree strategy but has no worktree_dir")
                })?;
            // worktree_dir is a field of the build repo, so `repo` = build repo here.
            let mut scoped = ctx.clone();
            scoped
                .root
                .insert_path("repo", repo_value(project, build_repo));
            Ok(PathBuf::from(scoped.render(tpl)?))
        }
    }
}

// --- pipeline layers ----------------------------------------------------------

/// Fold the accumulated environment into the context's `env.*` so later layers
/// can reference values earlier ones produced (`{{env.CC}}`, …).
fn fold_env(ctx: &mut Ctx, logical: &LogicalConfig) {
    for (k, v) in &logical.environment {
        ctx.set_env(k, v.clone());
    }
}

fn apply_env_map(
    ctx: &mut Ctx,
    logical: &mut LogicalConfig,
    ns: &str,
    raw: &BTreeMap<String, toml::Value>,
) -> Result<()> {
    if raw.is_empty() {
        return Ok(());
    }
    // Overlay the raw entries under both the namespace and env.* so entries may
    // reference each other in any order, then resolve each.
    for (k, v) in raw {
        let val: Value = v.clone().into();
        ctx.set(&format!("{ns}.{k}"), val.clone());
        ctx.set_env(k, template_string(&val));
    }
    let engine = ctx.engine();
    let mut resolved = Vec::new();
    for k in raw.keys() {
        let value = engine.get(&format!("env.{k}"))?;
        resolved.push((k.clone(), value_to_string(&value)));
    }
    for (k, v) in resolved {
        logical.set_env(&k, v.clone());
        ctx.set_env(&k, v);
    }
    Ok(())
}

fn apply_def_map(
    ctx: &mut Ctx,
    logical: &mut LogicalConfig,
    ns: &str,
    raw: &BTreeMap<String, toml::Value>,
) -> Result<()> {
    if raw.is_empty() {
        return Ok(());
    }
    for (k, v) in raw {
        ctx.set(&format!("{ns}.{k}"), v.clone().into());
    }
    let engine = ctx.engine();
    for k in raw.keys() {
        let value = engine.get(&format!("{ns}.{k}"))?;
        logical.set_definition(k, value);
    }
    Ok(())
}

fn resolve_args(ctx: &Ctx, raw: &[String], out: &mut Vec<String>) -> Result<()> {
    for arg in raw {
        out.push(ctx.render(arg)?);
    }
    Ok(())
}

// --- presets ------------------------------------------------------------------

/// The ordered, de-duplicated list of presets to apply: `default_presets`, then
/// `applies_when` matches, then CLI `--preset`; last occurrence wins position.
fn applied_presets(
    ws: &Workspace,
    project: &ProjectData,
    focus: &str,
    profile: &Profile,
    toolchain: &Option<Toolchain>,
    build_type: &str,
    generator: &Option<String>,
) -> Vec<String> {
    let mut ordered: Vec<String> = project.project.default_presets.clone();

    // Auto-applied: any candidate whose merged applies_when matches.
    let axes = MatchAxes {
        build_type,
        toolchain: toolchain.as_ref().map(|t| t.name.as_str()),
        os: std::env::consts::OS,
        arch: std::env::consts::ARCH,
        generator: generator.as_deref(),
    };
    for name in candidate_preset_names(ws, project, focus) {
        if let Some(p) = effective_preset(ws, project, focus, &name) {
            if let Some(cond) = &p.applies_when {
                if axes.matches(cond) {
                    ordered.push(name);
                }
            }
        }
    }

    ordered.extend(profile.presets.iter().cloned());

    // De-duplicate keeping the last position.
    let mut seen = std::collections::HashSet::new();
    let mut out = Vec::new();
    for name in ordered.into_iter().rev() {
        if seen.insert(name.clone()) {
            out.push(name);
        }
    }
    out.reverse();
    out
}

fn candidate_preset_names(ws: &Workspace, project: &ProjectData, focus: &str) -> Vec<String> {
    let mut names = std::collections::BTreeSet::new();
    if let Some(org) = &project.org {
        if let Some(m) = ws.org_presets(org) {
            names.extend(m.keys().cloned());
        }
    }
    names.extend(project.project.presets.keys().cloned());
    if let Some(repo) = project.repos.get(focus) {
        names.extend(repo.presets.keys().cloned());
    }
    names.into_iter().collect()
}

/// Merge the same-named preset across org → project → repo (maps: nearest wins;
/// lists/extends/applies_when: nearest non-empty wins). A qualified `org/preset`
/// reference reaches one org's presets directly, without merging.
fn effective_preset(
    ws: &Workspace,
    project: &ProjectData,
    focus: &str,
    name: &str,
) -> Option<RawPreset> {
    if let Some((org, base)) = name.split_once('/') {
        return ws.org_presets(org).and_then(|m| m.get(base)).cloned();
    }
    let mut layers: Vec<&RawPreset> = Vec::new();
    if let Some(org) = &project.org {
        if let Some(p) = ws.org_presets(org).and_then(|m| m.get(name)) {
            layers.push(p);
        }
    }
    if let Some(p) = project.project.presets.get(name) {
        layers.push(p);
    }
    if let Some(p) = project.repos.get(focus).and_then(|r| r.presets.get(name)) {
        layers.push(p);
    }
    if layers.is_empty() {
        return None;
    }
    let mut merged = RawPreset::default();
    for layer in layers {
        for (k, v) in &layer.environment {
            merged.environment.insert(k.clone(), v.clone());
        }
        for (k, v) in &layer.definitions {
            merged.definitions.insert(k.clone(), v.clone());
        }
        if !layer.extends.0.is_empty() {
            merged.extends = layer.extends.clone();
        }
        if layer.applies_when.is_some() {
            merged.applies_when = layer.applies_when.clone();
        }
        if !layer.extra_config_args.is_empty() {
            merged.extra_config_args = layer.extra_config_args.clone();
        }
        if !layer.extra_build_args.is_empty() {
            merged.extra_build_args = layer.extra_build_args.clone();
        }
        if !layer.extra_install_args.is_empty() {
            merged.extra_install_args = layer.extra_install_args.clone();
        }
    }
    Some(merged)
}

fn resolve_preset_into(
    ctx: &mut Ctx,
    logical: &mut LogicalConfig,
    ws: &Workspace,
    project: &ProjectData,
    focus: &str,
    name: &str,
    seen: &mut Vec<String>,
) -> Result<()> {
    if seen.iter().any(|n| n == name) {
        seen.push(name.to_owned());
        bail!("circular preset inheritance: {}", seen.join(" -> "));
    }
    let preset = effective_preset(ws, project, focus, name)
        .with_context(|| format!("unknown preset '{name}'"))?;
    seen.push(name.to_owned());
    for parent in &preset.extends.0 {
        resolve_preset_into(ctx, logical, ws, project, focus, parent, seen)?;
    }
    seen.pop();

    apply_env_map(
        ctx,
        logical,
        &format!("preset.{name}.environment"),
        &preset.environment,
    )?;
    apply_def_map(
        ctx,
        logical,
        &format!("preset.{name}.definitions"),
        &preset.definitions,
    )?;
    // Preset lists replace what earlier layers set (they are the nearest-level
    // contribution for this preset); different presets still accumulate in order.
    resolve_replace(
        ctx,
        &preset.extra_config_args,
        &mut logical.extra_config_args,
    )?;
    resolve_replace(ctx, &preset.extra_build_args, &mut logical.extra_build_args)?;
    resolve_replace(
        ctx,
        &preset.extra_install_args,
        &mut logical.extra_install_args,
    )?;
    Ok(())
}

fn resolve_replace(ctx: &Ctx, raw: &[String], out: &mut Vec<String>) -> Result<()> {
    for arg in raw {
        let rendered = ctx.render(arg)?;
        if !out.contains(&rendered) {
            out.push(rendered);
        }
    }
    Ok(())
}

struct MatchAxes<'a> {
    build_type: &'a str,
    toolchain: Option<&'a str>,
    os: &'a str,
    arch: &'a str,
    generator: Option<&'a str>,
}

impl MatchAxes<'_> {
    fn matches(&self, cond: &BTreeMap<String, toml::Value>) -> bool {
        cond.iter().all(|(key, want)| {
            let actual = match key.as_str() {
                "build_type" => Some(self.build_type),
                "toolchain" => self.toolchain,
                "os" => Some(self.os),
                "arch" => Some(self.arch),
                "generator" => self.generator,
                _ => return false, // unknown match key never matches
            };
            let Some(actual) = actual else { return false };
            match want {
                toml::Value::String(s) => s == actual,
                toml::Value::Array(items) => items
                    .iter()
                    .any(|i| i.as_str().is_some_and(|s| s == actual)),
                _ => false,
            }
        })
    }
}

// --- context helper -----------------------------------------------------------

/// A mutable template context plus rendering helpers. Cloning is cheap enough for
/// this scale, and each render gets a fresh memoising [`Engine`].
#[derive(Clone)]
struct Ctx {
    root: Value,
}

impl Ctx {
    fn new() -> Self {
        Ctx {
            root: Value::Map(BTreeMap::new()),
        }
    }

    fn engine(&self) -> Engine {
        Engine::new(self.root.clone())
    }

    fn set(&mut self, path: &str, value: Value) {
        self.root.insert_path(path, value);
    }

    fn set_env(&mut self, key: &str, value: String) {
        self.root
            .insert_path(&format!("env.{key}"), Value::Str(value));
    }

    /// Render a template string to a plain string (embedded scalars stringified).
    fn render(&self, s: &str) -> Result<String> {
        Ok(value_to_string(&self.engine().resolve_str(s)?))
    }

    /// Resolve a value, returning it as a string (for env values).
    fn render_value(&self, v: &Value) -> Result<String> {
        Ok(value_to_string(&self.engine().resolve(v)?))
    }
}

// --- misc ---------------------------------------------------------------------

fn value_to_string(v: &Value) -> String {
    match v {
        Value::Str(s) => s.clone(),
        Value::Int(n) => n.to_string(),
        Value::Float(f) => f.to_string(),
        Value::Bool(b) => b.to_string(),
        Value::List(items) => items
            .iter()
            .map(value_to_string)
            .collect::<Vec<_>>()
            .join(" "),
        Value::Map(_) => String::new(),
    }
}

fn template_string(v: &Value) -> String {
    // Raw form for overlaying into env.* before resolution: strings stay as-is
    // (they may be templates), scalars stringify.
    value_to_string(v)
}

/// A context sufficient to resolve a repo-scoped template (a hook, a
/// `worktree_dir`): `project.*`, every `repos.<name>.*`, `repo.*` = `repo_name`,
/// plus `system.*` and `env.*`. No Profile is needed, so this is safe for
/// `update`/`context` which do not build a full plan.
pub fn context_for_repo(project: &ProjectData, repo_name: &str) -> Value {
    let mut root = Value::Map(BTreeMap::new());
    root.insert_path("project.name", Value::str(&project.name));
    root.insert_path(
        "project.org",
        Value::str(project.org.clone().unwrap_or_default()),
    );
    root.insert_path("project.focus", Value::str(project.focus_name(None)));
    for name in project.repos.keys() {
        root.insert_path(&format!("repos.{name}"), repo_value(project, name));
    }
    root.insert_path("repo", repo_value(project, repo_name));
    root.insert_path("system", system_facts());
    let mut env_map = BTreeMap::new();
    for (k, v) in std::env::vars() {
        env_map.insert(k, Value::Str(v));
    }
    root.insert_path("env", Value::Map(env_map));
    root
}

pub fn repo_value(project: &ProjectData, name: &str) -> Value {
    let repo = &project.repos[name];
    let kind = infer_kind(name, repo);
    let abs = project
        .repo_abs_path(name)
        .map(|p| p.display().to_string())
        .unwrap_or_default();
    let origin = repo.remotes.origin.clone().unwrap_or_default();
    let upstream = repo
        .remotes
        .upstream
        .clone()
        .unwrap_or_else(|| origin.clone());
    let mut m = BTreeMap::new();
    m.insert("name".into(), Value::str(name));
    m.insert("path".into(), Value::str(abs));
    m.insert("kind".into(), Value::str(kind.as_str()));
    m.insert(
        "main_branch".into(),
        Value::str(repo.main_branch.clone().unwrap_or_default()),
    );
    m.insert(
        "anchor".into(),
        Value::str(repo.anchor.clone().unwrap_or_default()),
    );
    m.insert("origin".into(), Value::str(origin));
    m.insert("upstream".into(), Value::str(upstream));
    m.insert(
        "mirrors".into(),
        Value::List(repo.remotes.mirrors.iter().map(Value::str).collect()),
    );
    Value::Map(m)
}

/// System facts for `system.*` — best-effort; missing pieces simply resolve to 0.
pub fn system_facts() -> Value {
    let cpu = std::thread::available_parallelism()
        .map(|n| n.get() as i64)
        .unwrap_or(1);
    let mem_gb = read_total_memory_gb().unwrap_or(0);
    let mut memory = BTreeMap::new();
    memory.insert("total_gb".into(), Value::Int(mem_gb));
    let mut cpu_map = BTreeMap::new();
    cpu_map.insert("count".into(), Value::Int(cpu));
    Value::map([
        ("os", Value::str(std::env::consts::OS)),
        ("arch", Value::str(std::env::consts::ARCH)),
        ("memory", Value::Map(memory)),
        ("cpu", Value::Map(cpu_map)),
    ])
}

/// Total physical memory in GiB, best-effort. `std` offers no cross-platform
/// memory query (only `available_parallelism` for CPUs), so this is per-OS: a
/// `/proc` read on Linux, a `sysctl` on the BSDs/macOS, and `None` elsewhere —
/// which simply resolves `system.memory.total_gb` to 0.
fn read_total_memory_gb() -> Option<i64> {
    #[cfg(target_os = "linux")]
    {
        let text = std::fs::read_to_string("/proc/meminfo").ok()?;
        let line = text.lines().find(|l| l.starts_with("MemTotal:"))?;
        let kb: i64 = line
            .trim_start_matches("MemTotal:")
            .trim()
            .trim_end_matches("kB")
            .trim()
            .parse()
            .ok()?;
        Some((kb / (1024 * 1024)).max(1))
    }
    #[cfg(any(target_os = "macos", target_os = "freebsd", target_os = "openbsd"))]
    {
        let key = if cfg!(target_os = "macos") {
            "hw.memsize"
        } else {
            "hw.physmem"
        };
        let out = crate::core::process::Command::new("sysctl")
            .args(["-n", key])
            .force_run()
            .exec()
            .ok()?;
        let bytes: i64 = out.stdout_trimmed().parse().ok()?;
        Some((bytes / (1024 * 1024 * 1024)).max(1))
    }
    #[cfg(not(any(
        target_os = "linux",
        target_os = "macos",
        target_os = "freebsd",
        target_os = "openbsd"
    )))]
    {
        None
    }
}

/// Filesystem-safe branch slug: every character outside `[A-Za-z0-9._-]` → `_`.
pub fn slugify(branch: &str) -> String {
    branch
        .chars()
        .map(|c| {
            if c.is_ascii_alphanumeric() || matches!(c, '.' | '_' | '-') {
                c
            } else {
                '_'
            }
        })
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::cmd::project::model::Profile;

    fn ws_with(body: &str, stem: &str) -> (tempfile::TempDir, Workspace) {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(format!("{stem}.toml")), body).unwrap();
        let ws = Workspace::load_from(dir.path()).unwrap();
        (dir, ws)
    }

    /// A stand-in for a real backend, so the pipeline can be tested without any
    /// build-system dependency: it echoes the toolchain's `cc` into a definition
    /// (the way a backend would) so we can assert L0 ran. Real per-backend
    /// translation is tested in `cmd::build`.
    struct MockInjector;
    impl ToolchainInjector for MockInjector {
        fn apply_toolchain(&self, tc: &Toolchain, cfg: &mut LogicalConfig) {
            if let Some(cc) = &tc.cc {
                cfg.set_definition("MOCK_CC", Value::Str(cc.clone()));
            }
        }
    }

    #[test]
    fn resolves_paths_and_injects_toolchain() {
        let body = r#"
            [project]
            build_system = "cmake"
            toolchain = "clang"
            build_dir = "{{work.dir}}/_build/{{toolchain.name}}/{{build_type}}"

            [repos.main]
            path = "/src/hello"
            main_branch = "main"

            [toolchains.clang]
            cc = "clang"
            cxx = "clang++"
        "#;
        let (_d, ws) = ws_with(body, "hello");
        let project = ws.project("hello").unwrap();
        let profile = Profile {
            build_type: Some("release".into()),
            ..Default::default()
        };
        let injector = MockInjector;
        let input = PlanInput {
            profile: &profile,
            branch: "main",
            inject_toolchain: true,
            injector: Some(&injector),
            extra_config_args: &[],
            extra_build_args: &[],
            extra_install_args: &[],
        };
        let plan = plan(&ws, project, &input).unwrap();
        assert_eq!(plan.work_dir, PathBuf::from("/src/hello"));
        assert_eq!(
            plan.build_dir.unwrap(),
            PathBuf::from("/src/hello/_build/clang/release")
        );
        // The injector ran at L0: the selected toolchain's `cc` was translated
        // into the backend-shaped definition the mock emits.
        assert!(plan.logical.definitions.iter().any(|(k, _)| k == "MOCK_CC"));
    }

    #[test]
    fn no_injector_skips_l0_but_still_resolves_paths() {
        let body = r#"
            [project]
            build_system = "cmake"
            toolchain = "clang"
            build_dir = "{{work.dir}}/b"

            [repos.main]
            path = "/src/hello"
            main_branch = "main"

            [toolchains.clang]
            cc = "clang"
        "#;
        let (_d, ws) = ws_with(body, "hello");
        let project = ws.project("hello").unwrap();
        let profile = Profile::default();
        let input = PlanInput {
            profile: &profile,
            branch: "main",
            inject_toolchain: true, // requested, but no injector supplied
            injector: None,
            extra_config_args: &[],
            extra_build_args: &[],
            extra_install_args: &[],
        };
        let plan = plan(&ws, project, &input).unwrap();
        // Toolchain *selection* still happened (paths need the name)…
        assert_eq!(plan.toolchain.as_ref().unwrap().name, "clang");
        // …but with no injector, L0 emitted nothing.
        assert!(plan.logical.definitions.is_empty());
    }

    #[test]
    fn preset_applies_when_and_override() {
        let body = r#"
            [project]
            build_system = "cmake"
            default_presets = ["warn"]
            build_dir = "{{work.dir}}/b"

            [repos.main]
            path = "/src/x"
            main_branch = "main"

            [project.presets.warn]
            definitions = { WERROR = true }

            [project.presets.dbg]
            applies_when = { build_type = "debug" }
            definitions = { ASSERTS = true }
        "#;
        let (_d, ws) = ws_with(body, "x");
        let project = ws.project("x").unwrap();
        let profile = Profile::default();
        let input = PlanInput {
            profile: &profile,
            branch: "main",
            inject_toolchain: false,
            injector: None,
            extra_config_args: &[],
            extra_build_args: &[],
            extra_install_args: &[],
        };
        let plan = plan(&ws, project, &input).unwrap();
        assert!(plan.logical.has_definition("WERROR"));
        assert!(plan.logical.has_definition("ASSERTS")); // auto-applied for debug
    }
}
