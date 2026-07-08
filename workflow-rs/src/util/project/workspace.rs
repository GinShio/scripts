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
//! `repo.path` is treated as a literal location (with `~` expanded), not a
//! template. The clone destination is a fixed fact; only build *outputs*
//! (`build_dir`, `worktree_dir`) are templated. That keeps [`Workspace::project_for_path`]
//! — the reverse lookup `wf stack` and git hooks lean on — answerable without a
//! Profile.

use std::collections::BTreeMap;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};

use super::model::{
    infer_kind, is_nested, Kind, RawFile, RawPreset, RawProject, RawRepo, RawToolchain,
};

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

    /// The on-disk location of a repo: absolute paths as-is, a nested (relative)
    /// path joined under `repos.main`. `~` is expanded.
    pub fn repo_abs_path(&self, repo_name: &str) -> Option<PathBuf> {
        let repo = self.repos.get(repo_name)?;
        if is_nested(&repo.path) && repo_name != "main" {
            let main = self.repos.get("main")?;
            Some(expand_tilde(&main.path).join(&repo.path))
        } else {
            Some(expand_tilde(&repo.path))
        }
    }
}

pub struct Workspace {
    /// Keyed by `org/name` (or bare `name`).
    projects: BTreeMap<String, ProjectData>,
    /// Bare name → the keys that carry it, for ambiguity detection.
    by_name: BTreeMap<String, Vec<String>>,
    toolchains: BTreeMap<String, RawToolchain>,
    /// Org name → its presets.
    orgs: BTreeMap<String, BTreeMap<String, RawPreset>>,
}

impl Workspace {
    pub fn toolchains(&self) -> &BTreeMap<String, RawToolchain> {
        &self.toolchains
    }

    pub fn org_presets(&self, org: &str) -> Option<&BTreeMap<String, RawPreset>> {
        self.orgs.get(org)
    }

    pub fn projects(&self) -> impl Iterator<Item = &ProjectData> {
        self.projects.values()
    }

    /// Load the registry from the resolved config root (§10.1).
    pub fn load() -> Result<Self> {
        let root = resolve_config_root()?;
        Self::load_from(&root)
    }

    pub fn load_from(root: &Path) -> Result<Self> {
        let mut ws = Workspace {
            projects: BTreeMap::new(),
            by_name: BTreeMap::new(),
            toolchains: BTreeMap::new(),
            orgs: BTreeMap::new(),
        };

        let mut files = Vec::new();
        scan_toml(root, &mut files)
            .with_context(|| format!("scanning config root {}", root.display()))?;
        files.sort();

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
            if self.orgs.insert(org.name.clone(), org.presets).is_some() {
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
                let Some(repo_path) = project.repo_abs_path(repo_name) else {
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

/// Resolve the single config root: `$WITS_PROJECT_CONFIG` (must exist if set),
/// then the first existing of `$XDG_CONFIG_HOME/wits/project` and
/// `$HOME/.wits/project`.
pub fn resolve_config_root() -> Result<PathBuf> {
    if let Some(env) = std::env::var_os("WITS_PROJECT_CONFIG") {
        let path = PathBuf::from(env);
        if !path.is_dir() {
            bail!(
                "WITS_PROJECT_CONFIG points at {} which is not a directory",
                path.display()
            );
        }
        return Ok(path);
    }

    let mut candidates = Vec::new();
    if let Some(xdg) = std::env::var_os("XDG_CONFIG_HOME") {
        candidates.push(PathBuf::from(xdg).join("wits").join("project"));
    }
    if let Some(home) = std::env::var_os("HOME") {
        candidates.push(PathBuf::from(home).join(".wits").join("project"));
    }
    for candidate in &candidates {
        if candidate.is_dir() {
            return Ok(candidate.clone());
        }
    }
    bail!(
        "no project config root found (looked for {})",
        candidates
            .iter()
            .map(|p| p.display().to_string())
            .collect::<Vec<_>>()
            .join(", ")
    );
}

/// Classify a CLI positional as a filesystem path rather than a name: `.`/`..`
/// or a leading `.`, `/`, or `~` (§1). Everything else is a name.
pub fn looks_like_path(token: &str) -> bool {
    token == "."
        || token == ".."
        || token.starts_with('.')
        || token.starts_with('/')
        || token.starts_with('~')
}

pub fn expand_tilde(path: &str) -> PathBuf {
    if let Some(rest) = path.strip_prefix("~/") {
        if let Some(home) = std::env::var_os("HOME") {
            return PathBuf::from(home).join(rest);
        }
    }
    PathBuf::from(path)
}

fn scan_toml(dir: &Path, out: &mut Vec<PathBuf>) -> std::io::Result<()> {
    if !dir.is_dir() {
        return Ok(());
    }
    for entry in std::fs::read_dir(dir)? {
        let path = entry?.path();
        if path.is_dir() {
            scan_toml(&path, out)?;
        } else if path.extension().and_then(|e| e.to_str()) == Some("toml") {
            out.push(path);
        }
    }
    Ok(())
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
    fn path_classifier() {
        assert!(looks_like_path("."));
        assert!(looks_like_path("./sub"));
        assert!(looks_like_path("/abs"));
        assert!(looks_like_path("~/x"));
        assert!(!looks_like_path("hello"));
        assert!(!looks_like_path("mesa/lavapipe"));
    }
}
