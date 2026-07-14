//! Loading the project registry: find the config root, scan it, and route each
//! file's sections into projects, toolchains, and orgs.
//!
//! Configuration is content-addressed (§10): files live anywhere under one root
//! and declare what they are by their sections, so loading is "read every
//! `*.toml`, look at what's inside, file it accordingly". A project's *name* is
//! its file stem; its *org* is explicit (`project.org`). The same `(org, name)`
//! twice is a conflict, not a silent override — cross-file layering of one
//! project was a foot-gun we don't reproduce.
//!
//! `repo.path` is a template resolved against a Profile-free context (`project.name`,
//! `project.org`, `env.*`, `system.*`; no `repos.*` to avoid circularity). This
//! lets paths like `~/Projects/{{project.org}}/{{project.name}}` work. Because
//! no Profile is required, [`Workspace::project_for_path`] — the reverse lookup
//! `wits stack` and git hooks lean on — stays answerable without a Profile.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

use crate::template::{Engine, Value};

use super::model::{
    infer_kind, is_nested, Kind, RawFile, RawPreset, RawProject, RawRepo, RawToolchain,
};

/// Org-level data retained after loading: a referenceable palette of shared
/// values plus the org's declared presets. The palette is exposed under
/// `org.environment.*` / `org.definitions.*` in template contexts, but is NOT
/// automatically merged into any build's logical config — templates must
/// reference it explicitly (e.g. `{{org.environment.MY_VAR}}`).
pub struct OrgData {
    pub environment: std::collections::BTreeMap<String, toml::Value>,
    pub definitions: std::collections::BTreeMap<String, toml::Value>,
    pub presets: std::collections::BTreeMap<String, RawPreset>,
}

/// One project as loaded from disk (still raw / unresolved).
pub struct ProjectData {
    pub name: String,
    pub org: Option<String>,
    pub source: PathBuf,
    pub project: RawProject,
    pub repos: BTreeMap<String, RawRepo>,
}

impl ProjectData {
    /// The canonical key: `org/name`, or the bare name when unscoped.
    pub fn key(&self) -> String {
        match &self.org {
            Some(org) => format!("{org}/{}", self.name),
            None => self.name.clone(),
        }
    }

    /// The focus repo's name: `--focus` override → `project.focus` → `"main"`.
    pub fn focus_name<'a>(&'a self, override_focus: Option<&'a str>) -> &'a str {
        override_focus
            .or(self.project.focus.as_deref())
            .unwrap_or("main")
    }

    pub fn kind_of(&self, repo_name: &str) -> Option<Kind> {
        self.repos.get(repo_name).map(|r| infer_kind(repo_name, r))
    }

    /// The on-disk location of a repo. `path` is resolved as a template against
    /// a Profile-free context (`project.name`, `project.org`, `env.*`, `system.*`),
    /// then `~` is expanded. Nested (relative) paths are joined under `repos.main`.
    /// Returns an error if the template is malformed or resolves to a non-string.
    pub fn repo_abs_path(&self, repo_name: &str) -> Result<PathBuf> {
        let repo = self
            .repos
            .get(repo_name)
            .with_context(|| format!("repo '{repo_name}' not found"))?;
        let tpl = &repo.path;
        let rendered = render_path_template(tpl, &self.name, self.org.as_deref())
            .with_context(|| format!("resolving path template for repo '{repo_name}': {tpl:?}"))?;
        if is_nested(&rendered) && repo_name != "main" {
            let main = self.repos.get("main").with_context(|| {
                format!("repo '{repo_name}' has a nested path but 'main' is not declared")
            })?;
            let main_tpl = &main.path;
            let main_rendered = render_path_template(main_tpl, &self.name, self.org.as_deref())
                .with_context(|| {
                    format!("resolving path template for repo 'main': {main_tpl:?}")
                })?;
            Ok(expand_tilde(&main_rendered).join(&rendered))
        } else {
            Ok(expand_tilde(&rendered))
        }
    }
}

pub struct Workspace {
    /// Keyed by `org/name` (or bare `name`).
    projects: BTreeMap<String, ProjectData>,
    /// Bare name → the keys that carry it, for ambiguity detection.
    by_name: BTreeMap<String, Vec<String>>,
    toolchains: BTreeMap<String, RawToolchain>,
    orgs: BTreeMap<String, OrgData>,
}

impl Workspace {
    pub fn toolchains(&self) -> &BTreeMap<String, RawToolchain> {
        &self.toolchains
    }

    pub fn org_presets(&self, org: &str) -> Option<&BTreeMap<String, RawPreset>> {
        self.orgs.get(org).map(|d| &d.presets)
    }

    pub fn org_base(&self, org: &str) -> Option<&OrgData> {
        self.orgs.get(org)
    }

    pub fn projects(&self) -> impl Iterator<Item = &ProjectData> {
        self.projects.values()
    }

    /// Load the registry from the resolved config root (§10.1).
    pub fn load() -> Result<Self> {
        let root = crate::config::resolve_root(&CONFIG_ROOT)?;
        Self::load_from(&root)
    }

    pub fn load_from(root: &Path) -> Result<Self> {
        let mut ws = Workspace {
            projects: BTreeMap::new(),
            by_name: BTreeMap::new(),
            toolchains: BTreeMap::new(),
            orgs: BTreeMap::new(),
        };

        let files = crate::config::discover_toml(root)
            .with_context(|| format!("scanning config root {}", root.display()))?;

        for file in &files {
            ws.ingest(file)
                .with_context(|| format!("loading {}", file.display()))?;
        }
        Ok(ws)
    }

    fn ingest(&mut self, path: &Path) -> Result<()> {
        let text = std::fs::read_to_string(path)?;
        let raw: RawFile = toml::from_str(&text)?;

        // Toolchains and orgs are additive registries; a repeated *name* is a
        // conflict (spreading distinct entries across files is the additive part).
        for (name, tc) in raw.toolchains {
            if self.toolchains.insert(name.clone(), tc).is_some() {
                bail!("toolchain '{name}' is defined more than once");
            }
        }
        if let Some(org) = raw.org {
            let data = OrgData {
                environment: org.environment,
                definitions: org.definitions,
                presets: org.presets,
            };
            if self.orgs.insert(org.name.clone(), data).is_some() {
                bail!("org '{}' is declared more than once", org.name);
            }
        }

        if let Some(project) = raw.project {
            let name = path
                .file_stem()
                .and_then(|s| s.to_str())
                .context("project file has no usable stem")?
                .to_owned();
            if !raw.repos.contains_key("main") {
                bail!("project '{name}' has no [repos.main] (it is a required root)");
            }
            let data = ProjectData {
                org: project.org.clone(),
                name: name.clone(),
                source: path.to_path_buf(),
                project,
                repos: raw.repos,
            };
            let key = data.key();
            if self.projects.contains_key(&key) {
                bail!("project '{key}' is defined in more than one file");
            }
            self.by_name.entry(name).or_default().push(key.clone());
            self.projects.insert(key, data);
        }

        Ok(())
    }

    /// Resolve a name reference (`name` or `org/name`) to a project.
    pub fn project(&self, reference: &str) -> Result<&ProjectData> {
        if let Some(p) = self.projects.get(reference) {
            return Ok(p);
        }
        if reference.contains('/') {
            bail!("no project '{reference}'{}", self.available());
        }
        match self.by_name.get(reference).map(Vec::as_slice) {
            Some([only]) => Ok(&self.projects[only]),
            Some(many) if many.len() > 1 => bail!(
                "project '{reference}' is ambiguous across orgs ({}); qualify it as org/name",
                many.join(", ")
            ),
            _ => bail!("no project '{reference}'{}", self.available()),
        }
    }

    /// The project that owns `path` — the one whose repo checkout is the deepest
    /// prefix of `path`. This is the reverse lookup consumers need to answer
    /// "which project am I standing in?".
    pub fn project_for_path(&self, path: &Path) -> Option<&ProjectData> {
        let query = std::fs::canonicalize(path).unwrap_or_else(|_| path.to_path_buf());
        let mut best: Option<(&ProjectData, usize)> = None;
        for project in self.projects.values() {
            for repo_name in project.repos.keys() {
                let Ok(repo_path) = project.repo_abs_path(repo_name) else {
                    continue;
                };
                let repo_path = std::fs::canonicalize(&repo_path).unwrap_or(repo_path);
                if query.starts_with(&repo_path) {
                    let depth = repo_path.components().count();
                    if best.is_none_or(|(_, d)| depth > d) {
                        best = Some((project, depth));
                    }
                }
            }
        }
        best.map(|(p, _)| p)
    }

    fn available(&self) -> String {
        if self.projects.is_empty() {
            ". No projects are configured.".to_string()
        } else {
            format!(
                ". Available: {}",
                self.projects.keys().cloned().collect::<Vec<_>>().join(", ")
            )
        }
    }
}

/// Where `project` keeps its config tree (§10.1): `$WITS_PROJECT_CONFIG`, then
/// `$XDG_CONFIG_HOME/wits/project`, then `$HOME/.wits/project`.
const CONFIG_ROOT: crate::config::Root<'static> = crate::config::Root {
    env: "WITS_PROJECT_CONFIG",
    xdg: "wits/project",
    home: ".wits/project",
};

/// Classify a CLI positional as a filesystem path rather than a name: `.`/`..`
/// or a leading `.`, `/`, or `~` (§1). Everything else is a name.
pub fn looks_like_path(token: &str) -> bool {
    token == "."
        || token == ".."
        || token.starts_with('.')
        || token.starts_with('/')
        || token.starts_with('~')
}

/// Render a `repo.path` template against the shared Profile-free path context
/// (`project.name`, `project.org`, `system.*`, `env.*`; no `repos.*`, which would
/// be circular). Built by [`super::context::path_context`] so this exact same
/// namespace backs `repo_abs_path` here and any other path resolve — they can't
/// drift apart.
fn render_path_template(
    tpl: &str,
    project_name: &str,
    project_org: Option<&str>,
) -> Result<String> {
    let root = super::context::path_context(project_name, project_org);
    let engine = Engine::new(root);
    match engine.resolve_str(tpl)? {
        Value::Str(s) => Ok(s),
        other => anyhow::bail!("path template {tpl:?} resolved to a non-string: {other:?}"),
    }
}

pub fn expand_tilde(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, rel: &str, body: &str) {
        let path = dir.join(rel);
        std::fs::create_dir_all(path.parent().unwrap()).unwrap();
        std::fs::write(path, body).unwrap();
    }

    #[test]
    fn loads_and_resolves_by_name_and_org() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "mesa/lavapipe.toml",
            r#"
            [project]
            org = "mesa"
            [repos.main]
            path = "~/src/mesa"
            main_branch = "main"
            "#,
        );
        write(
            dir.path(),
            "hello.toml",
            r#"
            [project]
            [repos.main]
            path = "/tmp/hello"
            main_branch = "main"
            "#,
        );
        let ws = Workspace::load_from(dir.path()).unwrap();
        assert_eq!(ws.projects().count(), 2);
        assert_eq!(ws.project("hello").unwrap().name, "hello");
        // bare name resolves through the org
        assert_eq!(ws.project("lavapipe").unwrap().org.as_deref(), Some("mesa"));
        assert_eq!(ws.project("mesa/lavapipe").unwrap().name, "lavapipe");
        assert!(ws.project("nope").is_err());
    }

    #[test]
    fn duplicate_project_is_rejected() {
        let dir = tempfile::tempdir().unwrap();
        let body = r#"
            [project]
            [repos.main]
            path = "/tmp/x"
            main_branch = "main"
            "#;
        write(dir.path(), "a/x.toml", body);
        write(dir.path(), "b/x.toml", body);
        assert!(Workspace::load_from(dir.path()).is_err());
    }

    #[test]
    fn project_for_path_finds_owner() {
        let dir = tempfile::tempdir().unwrap();
        let checkout = dir.path().join("checkout");
        std::fs::create_dir_all(checkout.join("src/sub")).unwrap();
        write(
            dir.path(),
            "proj.toml",
            &format!(
                r#"
                [project]
                [repos.main]
                path = "{}"
                main_branch = "main"
                "#,
                checkout.display()
            ),
        );
        let ws = Workspace::load_from(dir.path()).unwrap();
        let found = ws.project_for_path(&checkout.join("src/sub")).unwrap();
        assert_eq!(found.name, "proj");
        assert!(ws.project_for_path(Path::new("/nowhere")).is_none());
    }

    #[test]
    fn path_template_resolves_project_org_and_name() {
        let dir = tempfile::tempdir().unwrap();
        let base = dir.path();
        // Use an env var the template can reference without touching $HOME.
        let key = "WITS_TEST_PATH_BASE_TEMPLATE";
        std::env::set_var(key, base.to_str().unwrap());
        let checkout = base.join("acme").join("myproj");
        std::fs::create_dir_all(&checkout).unwrap();
        write(
            base,
            "myproj.toml",
            r#"
            [project]
            org = "acme"
            [repos.main]
            path = "{{env.WITS_TEST_PATH_BASE_TEMPLATE}}/{{project.org}}/{{project.name}}"
            main_branch = "main"
            "#,
        );
        let ws = Workspace::load_from(base).unwrap();
        let project = ws.project("acme/myproj").unwrap();
        let abs = project.repo_abs_path("main").unwrap();
        assert_eq!(abs, checkout);
        // project_for_path must resolve an inner path via the templated path.
        let found = ws.project_for_path(&checkout.join("src")).unwrap();
        assert_eq!(found.name, "myproj");
        std::env::remove_var(key);
    }

    #[test]
    fn malformed_path_template_is_hard_error() {
        let dir = tempfile::tempdir().unwrap();
        write(
            dir.path(),
            "bad.toml",
            r#"
            [project]
            [repos.main]
            path = "{{no.such.var}}"
            main_branch = "main"
            "#,
        );
        let ws = Workspace::load_from(dir.path()).unwrap();
        let project = ws.project("bad").unwrap();
        assert!(project.repo_abs_path("main").is_err());
    }

    #[test]
    fn path_classifier() {
        assert!(looks_like_path("."));
        assert!(looks_like_path("./sub"));
        assert!(looks_like_path("/abs"));
        assert!(looks_like_path("~/x"));
        assert!(!looks_like_path("hello"));
        assert!(!looks_like_path("mesa/lavapipe"));
    }
}
