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

use std::path::PathBuf;

use anyhow::{bail, Context, Result};

use crate::template::Value;

use super::context::{self, apply_def_map, apply_env_map, fold_env, resolve_args, Ctx};
use super::model::{infer_kind, BranchStrategy, BuildSystem, LogicalConfig, Profile, Toolchain};
use super::presets::{applied_presets, resolve_preset_into};
use super::toolchain::{resolve_toolchain, select_toolchain};
use super::workspace::{ProjectData, Workspace};

// The context construction, preset, toolchain, and host-fact machinery lives in
// sibling modules; this file is just the pipeline that composes them. The public
// context builders are re-exported so existing `resolve::…` call sites (and the
// `build_system` backends) don't have to learn the new module layout.
pub use super::context::{context_for_repo, path_context, path_slug, repo_value, system_facts};

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
    pub build_system: Option<BuildSystem>,
    pub toolchain: Option<Toolchain>,
    pub work_dir: PathBuf,
    /// Where the backend configures from: the build repo's `source_dir` template,
    /// or `work_dir` when unset. Distinct from `work_dir`, which stays the
    /// checkout root that carries branch identity and anchors path templates.
    pub source_dir: PathBuf,
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
/// it live entirely in `crate::build_system` (§1.4). The core never names
/// a backend, and
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
    let branch_slug = path_slug(&branch_raw);
    let build_type = profile
        .build_type
        .clone()
        .unwrap_or_else(|| "debug".to_owned());
    let generator = profile
        .generator
        .clone()
        .or_else(|| project.project.generator.clone());
    let build_system = project.project.build_system;

    // Toolchain *selection* always happens (path templates depend on the name).
    let toolchain = select_toolchain(ws, project, profile)?;

    // --- Base context ------------------------------------------------------
    // The shared base (project.*, repos.*, repo.* = focus, org palette, system.*,
    // env.*) is built in `context`; the pipeline layers the Profile-specific
    // bindings on top.
    let mut ctx = context::plan_base(ws, project, &focus);
    ctx.set("branch.raw", Value::str(&branch_raw));
    ctx.set("branch.slug", Value::str(&branch_slug));
    ctx.set("build_type", Value::str(&build_type));
    if let Some(gen) = &generator {
        ctx.set("generator", Value::str(gen));
    }

    // Resolve the toolchain against the base context and expose it as toolchain.*.
    let toolchain = match toolchain {
        Some((name, raw)) => Some(resolve_toolchain(&mut ctx, name, &raw)?),
        None => None,
    };

    // --- Paths -------------------------------------------------------------
    let work_dir = resolve_work_dir(project, &ctx, &build_repo, strategy)?;
    ctx.set("work.dir", Value::str(work_dir.display().to_string()));

    // The configure source: the build repo's `source_dir` template, or the
    // checkout root when unset. It is not exposed as a context variable — build
    // outputs template off `work.dir`, not off the source.
    let source_dir = match &project.repos[&build_repo].source_dir {
        Some(tpl) => PathBuf::from(ctx.render(tpl)?),
        None => work_dir.clone(),
    };

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
        source_dir,
        build_dir,
        install_dir,
        logical,
        context: ctx.into_value(),
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
            scoped.set("repo", repo_value(project, build_repo));
            Ok(PathBuf::from(scoped.render(tpl)?))
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::project::model::Profile;

    fn ws_with(body: &str, stem: &str) -> (tempfile::TempDir, Workspace) {
        let dir = tempfile::tempdir().unwrap();
        std::fs::write(dir.path().join(format!("{stem}.toml")), body).unwrap();
        let ws = Workspace::load_from(dir.path()).unwrap();
        (dir, ws)
    }

    /// A stand-in for a real backend, so the pipeline can be tested without any
    /// build-system dependency: it echoes the toolchain's `cc` into a definition
    /// (the way a backend would) so we can assert L0 ran. Real per-backend
    /// translation is tested in `crate::build_system`.
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
    fn source_dir_defaults_to_work_dir_and_can_be_a_subdir() {
        let body = r#"
            [project]
            build_system = "cmake"
            build_dir = "{{work.dir}}/_build"

            [repos.main]
            path = "/src/hello"
            main_branch = "main"
            source_dir = "{{work.dir}}/subdir"

            [repos.other]
            path = "/src/other"
            main_branch = "main"
        "#;
        let (_d, ws) = ws_with(body, "hello");
        let project = ws.project("hello").unwrap();
        let base = Profile::default();
        let input = |profile: &Profile| -> Plan {
            plan(
                &ws,
                project,
                &PlanInput {
                    profile,
                    branch: "main",
                    inject_toolchain: false,
                    injector: None,
                    extra_config_args: &[],
                    extra_build_args: &[],
                    extra_install_args: &[],
                },
            )
            .unwrap()
        };
        // work.dir stays the checkout root; source_dir is the declared subdir.
        let plan = input(&base);
        assert_eq!(plan.work_dir, PathBuf::from("/src/hello"));
        assert_eq!(plan.source_dir, PathBuf::from("/src/hello/subdir"));
        // A repo without source_dir falls back to work.dir.
        let other = Profile {
            focus: Some("other".into()),
            ..Default::default()
        };
        let plan2 = input(&other);
        assert_eq!(plan2.source_dir, plan2.work_dir);
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
    fn org_palette_is_referenceable_not_auto_applied() {
        let body = r#"
            [org]
            name = "acme"
            [org.environment]
            SHARED_VAR = "from-org"
            UNUSED_VAR  = "never-used"
            [org.definitions]
            ORG_LEVEL = 42

            [project]
            org = "acme"
            build_dir = "{{work.dir}}/b"
            [project.environment]
            MY_ENV = "{{org.environment.SHARED_VAR}}"
            [project.definitions]
            MY_DEF = "{{org.definitions.ORG_LEVEL}}"

            [repos.main]
            path = "/src/x"
            main_branch = "main"
        "#;
        let (_d, ws) = ws_with(body, "x");
        let project = ws.project("acme/x").unwrap();
        let input = PlanInput {
            profile: &Profile::default(),
            branch: "main",
            inject_toolchain: false,
            injector: None,
            extra_config_args: &[],
            extra_build_args: &[],
            extra_install_args: &[],
        };
        let plan = plan(&ws, project, &input).unwrap();
        assert_eq!(plan.logical.env_entry("MY_ENV"), Some("from-org"));
        assert!(plan.logical.has_definition("MY_DEF"));
        // Unreferenced org entries must NOT appear in logical config.
        assert!(plan.logical.env_entry("UNUSED_VAR").is_none());
        assert!(plan.logical.env_entry("SHARED_VAR").is_none());
    }

    #[test]
    fn org_palette_referenceable_from_preset() {
        let body = r#"
            [org]
            name = "myorg"
            [org.environment]
            ORG_SETTING = "hello"

            [project]
            org = "myorg"
            default_presets = ["use-org"]
            build_dir = "{{work.dir}}/b"

            [project.presets.use-org]
            environment = { FROM_ORG = "{{org.environment.ORG_SETTING}}" }

            [repos.main]
            path = "/src/y"
            main_branch = "main"
        "#;
        let (_d, ws) = ws_with(body, "y");
        let project = ws.project("myorg/y").unwrap();
        let input = PlanInput {
            profile: &Profile::default(),
            branch: "main",
            inject_toolchain: false,
            injector: None,
            extra_config_args: &[],
            extra_build_args: &[],
            extra_install_args: &[],
        };
        let plan = plan(&ws, project, &input).unwrap();
        assert_eq!(plan.logical.env_entry("FROM_ORG"), Some("hello"));
        // ORG_SETTING itself was not referenced from project env directly.
        assert!(plan.logical.env_entry("ORG_SETTING").is_none());
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
