//! Deterministic project resolution tests (no live compositor).
//!
//! Covers the worker contract: path precedence FILE > CWD > GIT_ROOT >
//! LOGSEQ_PAGE > GIT_REMOTE, component-boundary descendants, longest-folder
//! wins, ties at the same strength are ambiguous (no weaker fallback),
//! exact Logseq page matching (derived label or display name, linked rows
//! only), remote form normalization, unknown/old-JSON handling, mapping
//! mutation/deletion/corruption fail-closed + invalidation, and project
//! semantic transitions.

use qs_agent_orchestrator::desktop_context::{
    classify_transition, ActivityKind, DesktopContext, FocusedWindow, ProjectContext,
    ResourceContext, Source, Workspace,
};
use qs_agent_orchestrator::project_context::{
    canonicalize_path_str, normalize_github_remote, ProjectResolver,
};
use std::path::{Path, PathBuf};

fn helper_script() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("scripts")
        .join("projects.py")
}

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-proj-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&p).unwrap();
    p
}

fn write_registry(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, body).unwrap();
}

fn reg_table(id: &str, name: &str, folder: &str, url: &str) -> String {
    let mut s = format!("[[projects]]\nid = \"{id}\"\nname = \"{name}\"\n");
    if !folder.is_empty() {
        s.push_str(&format!("local_folder = \"{folder}\"\n"));
    }
    if !url.is_empty() {
        s.push_str(&format!("github_url = \"{url}\"\n"));
    }
    s.push('\n');
    s
}

fn registry_doc(tables: &[String]) -> String {
    let mut s = "version = 1\n\n".to_string();
    for t in tables {
        s.push_str(t);
    }
    s
}

fn resolver_for(registry: &Path) -> ProjectResolver {
    ProjectResolver::with_paths(registry.to_path_buf(), helper_script())
}

fn res(adapter: &str, file: Option<&str>, cwd: Option<&str>, root: Option<&str>) -> ResourceContext {
    ResourceContext::new(adapter, file, cwd, root, None, None, None, None)
}

fn reg_table_page(id: &str, name: &str, folder: &str, url: &str, logseq_path: &str) -> String {
    let mut s = format!("[[projects]]\nid = \"{id}\"\nname = \"{name}\"\n");
    if !folder.is_empty() {
        s.push_str(&format!("local_folder = \"{folder}\"\n"));
    }
    if !url.is_empty() {
        s.push_str(&format!("github_url = \"{url}\"\n"));
    }
    if !logseq_path.is_empty() {
        s.push_str(&format!("logseq_path = \"{logseq_path}\"\n"));
    }
    s.push('\n');
    s
}

fn res_page(adapter: &str, page: &str) -> ResourceContext {
    ResourceContext::new(adapter, None, None, None, None, None, Some(page), None)
}

fn git_available() -> bool {
    std::process::Command::new("git")
        .arg("--version")
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .map(|s| s.success())
        .unwrap_or(false)
}

fn git(args: &[&str], dir: &Path) {
    let status = std::process::Command::new("git")
        .args(args)
        .current_dir(dir)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .status()
        .expect("git must spawn");
    assert!(status.success(), "git {args:?} failed in {}", dir.display());
}

/// Fresh real repo at `dir` (initialized, no remotes yet).
fn init_repo(dir: &Path) {
    std::fs::create_dir_all(dir).unwrap();
    git(&["init", "-q", "-b", "main"], dir);
}

/// Repo with one commit (needed for `git worktree add`).
fn init_repo_with_commit(dir: &Path) {
    init_repo(dir);
    git(&["config", "user.email", "t@t"], dir);
    git(&["config", "user.name", "t"], dir);
    std::fs::write(dir.join("f.md"), "hi").unwrap();
    git(&["add", "."], dir);
    git(&["commit", "-qm", "init"], dir);
}

const A_ID: &str = "11111111-1111-1111-1111-111111111111";
const B_ID: &str = "22222222-2222-2222-2222-222222222222";
const C_ID: &str = "33333333-3333-3333-3333-333333333333";

#[test]
fn file_descendant_boundary() {
    let base = tmpdir("boundary");
    let proj = base.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    let sibling = base.join("proj-suffix");
    std::fs::create_dir_all(&sibling).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "Alpha", &proj.to_string_lossy(), "")]),
    );
    let mut r = resolver_for(&reg);
    // Inside => match.
    let inside = proj.join("note.md").to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("neovim", Some(&inside), None, None), 1_000_000)
        .expect("descendant must match");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "file");
    // Sibling with shared prefix but component boundary => no match.
    let outside = sibling.join("note.md").to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&outside), None, None), 1_000_001),
        None,
        "component boundary must not match"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn nested_longest_folder_wins() {
    let base = tmpdir("nested");
    let outer = base.join("outer");
    let inner = outer.join("inner");
    std::fs::create_dir_all(&inner).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "Outer", &outer.to_string_lossy(), ""),
            reg_table(B_ID, "Inner", &inner.to_string_lossy(), ""),
        ]),
    );
    let mut r = resolver_for(&reg);
    let f = inner.join("a.md").to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("neovim", Some(&f), None, None), 2_000_000)
        .expect("nested must match");
    assert_eq!(hit.id, B_ID, "longest mapped folder must win");
    assert_eq!(hit.matched_by, "file");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn same_strength_tie_is_ambiguous_without_fallback() {
    // Two projects claim the SAME folder at FILE strength; CWD would match a
    // third project. The FILE tie is ambiguous => None, NOT the CWD fallback.
    let base = tmpdir("tie");
    let shared = base.join("shared");
    let other = base.join("other-cwd-only");
    std::fs::create_dir_all(&shared).unwrap();
    std::fs::create_dir_all(&other).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "A", &shared.to_string_lossy(), ""),
            reg_table(B_ID, "B", &shared.to_string_lossy(), ""),
            reg_table(C_ID, "C", &other.to_string_lossy(), ""),
        ]),
    );
    let mut r = resolver_for(&reg);
    let f = shared.join("x.md").to_string_lossy().to_string();
    let c = other.to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&f), Some(&c), None), 3_000_000),
        None,
        "same-strength tie must not fall back to weaker CWD"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn file_beats_cwd_on_conflict() {
    let base = tmpdir("filecwd");
    let fa = base.join("fa");
    let cb = base.join("cb");
    std::fs::create_dir_all(&fa).unwrap();
    std::fs::create_dir_all(&cb).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "A", &fa.to_string_lossy(), ""),
            reg_table(B_ID, "B", &cb.to_string_lossy(), ""),
        ]),
    );
    let mut r = resolver_for(&reg);
    let f = fa.join("x.md").to_string_lossy().to_string();
    let c = cb.to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("neovim", Some(&f), Some(&c), None), 4_000_000)
        .expect("file must win");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "file");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn precedence_cwd_then_git_root_then_remote() {
    let base = tmpdir("prec");
    let cwd_dir = base.join("cwdproj");
    let root_dir = base.join("rootproj");
    std::fs::create_dir_all(&cwd_dir).unwrap();
    std::fs::create_dir_all(&root_dir).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "Cwd", &cwd_dir.to_string_lossy(), ""),
            reg_table(B_ID, "Root", &root_dir.to_string_lossy(), ""),
            reg_table(C_ID, "Remote", "", "https://github.com/acme/remote"),
        ]),
    );
    let mut r = resolver_for(&reg);
    // CWD beats git_root.
    let hit = r
        .resolve_resource(
            &res(
                "kitty",
                None,
                Some(&cwd_dir.to_string_lossy()),
                Some(&root_dir.to_string_lossy()),
            ),
            5_000_000,
        )
        .expect("cwd must beat git_root");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "cwd");
    // git_root beats remote: root matches B even though repo remote maps to C.
    // Build a real repo dir whose remote points at C's URL but whose path is B.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let repo = root_dir.clone();
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/remote"], &repo);
    let hit2 = r
        .resolve_resource(
            &res(
                "kitty",
                None,
                None,
                Some(&repo.to_string_lossy()),
            ),
            5_000_100,
        )
        .expect("git_root must beat git_remote");
    assert_eq!(hit2.id, B_ID);
    assert_eq!(hit2.matched_by, "git_root");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn remote_forms_match_case_insensitive() {
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("remoteforms");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "git@github.com:acme/widget.git"], &repo);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(
            A_ID,
            "Web",
            "",
            "https://github.com/Acme/Widget",
        )]),
    );
    let mut r = resolver_for(&reg);
    // SSH scp form, different case, .git suffix.
    let root = repo.to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("kitty", None, None, Some(&root)), 6_000_000)
        .expect("scp form must normalize");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "git_remote");

    // www + https + trailing slash.
    git(&["remote", "set-url", "origin", "https://www.github.com/ACME/WIDGET/"], &repo);
    let hit2 = r
        .resolve_resource(&res("kitty", None, None, Some(&root)), 6_030_001)
        .expect("www/trailing slash must normalize");
    assert_eq!(hit2.id, A_ID);

    // Untrusted host => no association.
    git(&["remote", "set-url", "origin", "https://gitlab.com/acme/widget"], &repo);
    assert_eq!(
        r.resolve_resource(&res("kitty", None, None, Some(&root)), 6_060_002),
        None
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn explicit_git_remote_field_used_without_config() {
    let base = tmpdir("explicitremote");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "Web", "", "https://github.com/acme/widget")]),
    );
    let mut r = resolver_for(&reg);
    let with_remote = res("kitty", None, None, None)
        .with_git_remote(Some("git@github.com:acme/widget.git"));
    let hit = r
        .resolve_resource(&with_remote, 7_000_000)
        .expect("explicit git_remote must match");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "git_remote");
    // Malformed explicit remote (untrusted host) => None, never stored.
    let bad = res("kitty", None, None, None).with_git_remote(Some("https://evil.example/x"));
    assert!(bad.git_remote.is_none(), "untrusted remote must not be stored");
    assert_eq!(r.resolve_resource(&bad, 7_000_001), None);
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn conflicting_mapped_remotes_are_ambiguous() {
    // Two repo remotes claiming two DIFFERENT registered projects: no guess.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("multiremote");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/one"], &repo);
    git(&["remote", "add", "upstream", "https://github.com/acme/two"], &repo);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "One", "", "https://github.com/acme/one"),
            reg_table(B_ID, "Two", "", "https://github.com/acme/two"),
        ]),
    );
    let mut r = resolver_for(&reg);
    let root = repo.to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("kitty", None, None, Some(&root)), 8_000_000),
        None,
        "claims on two distinct projects must be ambiguous"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn fork_with_unmapped_upstream_matches_mapped_origin() {
    // Unique CLAIM wins even with unregistered remotes present: origin is
    // mapped, upstream points at an unregistered fork parent.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("forkupstream");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/mine"], &repo);
    git(&["remote", "add", "upstream", "https://github.com/someone-else/parent"], &repo);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "Mine", "", "https://github.com/acme/mine")]),
    );
    let mut r = resolver_for(&reg);
    let root = repo.to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("kitty", None, None, Some(&root)), 8_100_000)
        .expect("unique mapped claim must win despite unmapped upstream");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "git_remote");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn git_config_comments_and_quoting_resolve() {
    // Real git config semantics: trailing comments after the section header
    // and the url value must not break remote discovery (the old hand parser
    // lost these).
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("gitcomments");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/one"], &repo);
    std::fs::write(
        repo.join(".git/config"),
        std::fs::read_to_string(repo.join(".git/config")).unwrap()
            + "\n[remote \"upstream\"] # fork parent\n\turl = https://github.com/unmapped/parent # trailing\n",
    )
    .unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "One", "", "https://github.com/acme/one")]),
    );
    let mut r = resolver_for(&reg);
    let root = repo.to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("kitty", None, None, Some(&root)), 8_200_000)
        .expect("comments must not break git config discovery");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "git_remote");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn linked_worktree_remote_resolves_through_commondir() {
    // A linked worktree's `.git` is a pointer file; git resolves remotes via
    // the common dir. Matching must work from the worktree path.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("worktree");
    let origin_repo = base.join("origin-repo");
    init_repo_with_commit(&origin_repo);
    git(&["remote", "add", "origin", "https://github.com/acme/one"], &origin_repo);
    let wt_parent = base.join("wt-parent");
    std::fs::create_dir_all(&wt_parent).unwrap();
    git(&["worktree", "add", &*wt_parent.join("wt").to_string_lossy(), "HEAD"], &origin_repo);
    let wt = wt_parent.join("wt");
    assert!(wt.join(".git").is_file(), "linked worktree must use a gitdir pointer");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "One", "", "https://github.com/acme/one")]),
    );
    let mut r = resolver_for(&reg);
    let root = wt.to_string_lossy().to_string();
    let hit = r
        .resolve_resource(&res("kitty", None, None, Some(&root)), 8_300_000)
        .expect("worktree remote must resolve via commondir");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "git_remote");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn remote_config_change_within_ttl_re_resolves() {
    // Config/commondir metadata invalidation: a remote URL change takes
    // effect even inside the 30 s discovery TTL.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("remotechange");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/one"], &repo);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "One", "", "https://github.com/acme/one"),
            reg_table(B_ID, "Two", "", "https://github.com/acme/two"),
        ]),
    );
    let mut r = resolver_for(&reg);
    let root = repo.to_string_lossy().to_string();
    let t0 = 8_400_000;
    assert_eq!(
        r.resolve_resource(&res("kitty", None, None, Some(&root)), t0)
            .map(|p| p.id),
        Some(A_ID.to_string())
    );
    git(&["remote", "set-url", "origin", "https://github.com/acme/two"], &repo);
    assert_eq!(
        r.resolve_resource(&res("kitty", None, None, Some(&root)), t0 + 1_000)
            .map(|p| p.id),
        Some(B_ID.to_string()),
        "config change must invalidate inside the TTL"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn duplicate_github_url_across_projects_ambiguous() {
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("dupurl");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/shared"], &repo);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "A", "", "https://github.com/acme/shared"),
            reg_table(B_ID, "B", "", "https://github.com/acme/shared"),
        ]),
    );
    let mut r = resolver_for(&reg);
    let root = repo.to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("kitty", None, None, Some(&root)), 9_000_000),
        None,
        "two projects claiming one remote must be ambiguous"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn unknown_paths_match_nothing() {
    let base = tmpdir("unknown");
    let proj = base.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "A", &proj.to_string_lossy(), "")]),
    );
    let mut r = resolver_for(&reg);
    let elsewhere = base.join("elsewhere").join("x.md").to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&elsewhere), None, None), 10_000_000),
        None
    );
    // Titles never infer: a title mentioning the project matches nothing.
    let titled = ResourceContext::new(
        "zen-title",
        None,
        None,
        None,
        None,
        None,
        None,
        Some("Alpha project notes"),
    );
    assert_eq!(r.resolve_resource(&titled, 10_000_001), None);
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn old_json_reads_without_project_or_git_remote() {
    let legacy =
        r#"{"focused_window":{"id":"0x1","application":"kitty","title":"t"},"workspace":{"id":"1","name":"1"},"available":true,"source":"hyprland","observed_at_ms":123}"#;
    let ctx: DesktopContext = serde_json::from_str(legacy).expect("legacy must parse");
    assert_eq!(ctx.project, None);
    assert_eq!(ctx.resource, None);
    let legacy_res =
        r#"{"adapter":"kitty","cwd":"/tmp","title":"t"}"#;
    let rc: ResourceContext = serde_json::from_str(legacy_res).expect("legacy resource parses");
    assert_eq!(rc.git_remote, None);
    // Round-trip keeps new fields.
    let enriched = ctx.with_resource(Some(ResourceContext::new(
        "kitty",
        None,
        Some("/tmp"),
        None,
        None,
        None,
        None,
        None,
    )));
    assert_eq!(enriched.project, None, "with_resource starts project-free");
    let with_p = enriched.with_project(Some(ProjectContext::new(A_ID, "A", "cwd")));
    let json = serde_json::to_string(&with_p).unwrap();
    assert!(json.contains("\"project\""));
    let back: DesktopContext = serde_json::from_str(&json).unwrap();
    assert_eq!(back.project, with_p.project);
}

#[test]
fn mapping_mutation_deletion_corrupt_fail_closed() {
    let base = tmpdir("mutate");
    let pa = base.join("pa");
    let pb = base.join("pb-longer-name");
    std::fs::create_dir_all(&pa).unwrap();
    std::fs::create_dir_all(&pb).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "A", &pa.to_string_lossy(), "")]),
    );
    let mut r = resolver_for(&reg);
    let fa = pa.join("x.md").to_string_lossy().to_string();
    let fb = pb.join("x.md").to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&fa), None, None), 11_000_000)
            .map(|p| p.id),
        Some(A_ID.to_string())
    );
    // Mutate: A now points at pb. Same-length trick avoided: pb is longer, so
    // len+mtime differ and the cache must invalidate.
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "A", &pb.to_string_lossy(), "")]),
    );
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&fa), None, None), 11_000_100),
        None,
        "old folder must stop matching after mutation"
    );
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&fb), None, None), 11_000_101)
            .map(|p| p.id),
        Some(A_ID.to_string()),
        "new folder must match after mutation"
    );
    // Delete: missing registry reads as empty (fail closed, no stale).
    std::fs::remove_file(&reg).unwrap();
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&fb), None, None), 11_000_200),
        None,
        "deletion must clear, never serve stale"
    );
    // Corrupt: garbage TOML fails closed.
    write_registry(&reg, "version = 1\n[[projects]]\nbroken = [\n");
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&fb), None, None), 11_000_300),
        None,
        "corrupt registry must fail closed"
    );
    // Recovery: valid rewrite afterwards resolves again (retry TTL passed by
    // using a later clock + changed bytes).
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "A", &pb.to_string_lossy(), "")]),
    );
    assert_eq!(
        r.resolve_resource(&res("neovim", Some(&fb), None, None), 11_006_000)
            .map(|p| p.id),
        Some(A_ID.to_string()),
        "valid rewrite must recover"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn project_semantic_transitions_and_clearing() {
    // semantic_eq includes project; project-only diffs classify as context.
    let win = || FocusedWindow::new("0x1", "kitty", "t");
    let ws = || Workspace::new("1", "1");
    let rc = || {
        ResourceContext::new(
            "kitty",
            None,
            Some("/tmp"),
            None,
            None,
            None,
            None,
            None,
        )
    };
    let a = DesktopContext::available(Source::Hyprland, Some(win()), Some(ws()), 1)
        .with_resource(Some(rc()))
        .with_project(Some(ProjectContext::new(A_ID, "A", "cwd")));
    let b = DesktopContext::available(Source::Hyprland, Some(win()), Some(ws()), 2)
        .with_resource(Some(rc()))
        .with_project(Some(ProjectContext::new(B_ID, "B", "cwd")));
    assert!(!a.semantic_eq(&b), "different projects must differ");
    assert_eq!(
        classify_transition(Some(&a), &b),
        ActivityKind::Context,
        "project-only change must be context"
    );
    // with_resource clears a possibly-stale project.
    let stripped = b.clone().with_resource(None);
    assert_eq!(stripped.resource, None);
    assert_eq!(stripped.project, None, "stripped inputs must clear project");
    // Unavailable clears project.
    let un = DesktopContext::unavailable(Source::Hyprland, 3);
    assert_eq!(un.project, None);
    assert_eq!(
        classify_transition(Some(&a), &un),
        ActivityKind::Availability
    );
}

#[test]
fn resolver_enrich_clears_without_resource() {
    let base = tmpdir("enrichclear");
    let reg = base.join("projects.toml");
    write_registry(&reg, &registry_doc(&[]));
    let mut r = resolver_for(&reg);
    let win = FocusedWindow::new("0x1", "kitty", "t");
    let bare =
        DesktopContext::available(Source::Hyprland, Some(win.clone()), Some(Workspace::new("1", "1")), 1);
    assert_eq!(r.enrich(bare.clone(), 12_000_000).project, None);
    // A stale project on a resourceless snapshot is cleared, not retained.
    let stale = bare.with_project(Some(ProjectContext::new(A_ID, "A", "cwd")));
    assert_eq!(r.enrich(stale, 12_000_001).project, None);
    // Unavailable never carries one either.
    let un = DesktopContext::unavailable(Source::Hyprland, 12_000_002)
        .with_project(Some(ProjectContext::new(A_ID, "A", "cwd")));
    assert_eq!(r.enrich(un, 12_000_002).project, None);
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn tilde_and_symlink_canonicalization() {
    // "~/" expands via $HOME; symlinked dirs canonicalize to the same target.
    let home = std::env::var("HOME").unwrap_or_default();
    assert!(!home.is_empty(), "HOME must exist for this test");
    let proj = PathBuf::from(&home).join(format!(
        "qs-tilde-probe-{}",
        std::process::id()
    ));
    let _ = std::fs::remove_dir_all(&proj);
    std::fs::create_dir_all(&proj).unwrap();
    let rel = format!("~/{}", proj.file_name().unwrap().to_string_lossy());
    let canon_tilde = canonicalize_path_str(&rel).expect("tilde must expand");
    let canon_abs =
        canonicalize_path_str(&proj.to_string_lossy()).expect("abs must canonicalize");
    assert_eq!(canon_tilde, canon_abs);

    let base = tmpdir("symlink");
    let real = base.join("real");
    std::fs::create_dir_all(&real).unwrap();
    #[cfg(unix)]
    {
        let link = base.join("link");
        let _ = std::os::unix::fs::symlink(&real, &link);
        let reg = base.join("projects.toml");
        write_registry(
            &reg,
            &registry_doc(&[reg_table(A_ID, "A", &real.to_string_lossy(), "")]),
        );
        let mut r = resolver_for(&reg);
        let via_link = link.join("x.md").to_string_lossy().to_string();
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&via_link), None, None), 13_000_000)
                .map(|p| p.id),
            Some(A_ID.to_string()),
            "symlinked candidate must canonicalize to the mapped target"
        );
    }
    let _ = std::fs::remove_dir_all(&base);
    let _ = std::fs::remove_dir_all(&proj);
}

#[test]
fn remote_overflow_sixty_five_fails_closed_end_to_end() {
    // 65 valid remotes where the 65th claims a different project: discovery
    // must fail closed (None), never truncate to the first 64 and falsely
    // report a unique claim on the first project.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("remoteoverflow");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/first"], &repo);
    for i in 0..63u32 {
        git(
            &[
                "remote",
                "add",
                &format!("r{i:02}"),
                &format!("https://github.com/acme/unmapped{i:03}").as_str(),
            ],
            &repo,
        );
    }
    git(&["remote", "add", "zz-last", "https://github.com/acme/second"], &repo);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "First", "", "https://github.com/acme/first"),
            reg_table(B_ID, "Second", "", "https://github.com/acme/second"),
        ]),
    );
    let mut r = resolver_for(&reg);
    let root = repo.to_string_lossy().to_string();
    assert_eq!(
        r.resolve_resource(&res("kitty", None, None, Some(&root)), 18_000_000),
        None,
        "65 remotes must fail discovery closed, not match the first claim"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn misleading_git_dir_env_does_not_hijack_discovery() {
    // A hostile ambient GIT_DIR/GIT_WORK_TREE/GIT_CONFIG_* must not redirect
    // discovery: the probe child runs with poisoned env (parent env untouched
    // — no global mutation) and must still match the real repo.
    if !git_available() {
        eprintln!("git missing; skipping");
        return;
    }
    let base = tmpdir("githijack");
    let repo = base.join("repo");
    init_repo(&repo);
    git(&["remote", "add", "origin", "https://github.com/acme/real"], &repo);
    let evil = base.join("evil");
    init_repo(&evil);
    git(&["remote", "add", "origin", "https://github.com/evil/hijack"], &evil);
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "Real", "", "https://github.com/acme/real")]),
    );
    let exe = std::env::current_exe().expect("current test exe");
    let out = std::process::Command::new(exe)
        .arg("--exact")
        .arg("inner_misleading_git_dir_probe")
        .arg("--ignored")
        .arg("--nocapture")
        .env("QS_PROBE_REPO", &repo)
        .env("QS_PROBE_REG", &reg)
        .env("GIT_DIR", evil.join(".git"))
        .env("GIT_WORK_TREE", &evil)
        .env("GIT_CONFIG_COUNT", "1")
        .env("GIT_CONFIG_KEY_0", "remote.origin.url")
        .env("GIT_CONFIG_VALUE_0", "https://github.com/evil/env")
        .stdin(std::process::Stdio::null())
        .output()
        .expect("probe child must spawn");
    assert!(
        out.status.success(),
        "probe must pass under poisoned git env:\nstdout: {}\nstderr: {}",
        String::from_utf8_lossy(&out.stdout),
        String::from_utf8_lossy(&out.stderr)
    );
    let _ = std::fs::remove_dir_all(&base);
}

/// Subprocess probe for [`misleading_git_dir_env_does_not_hijack_discovery`]:
/// runs ONLY in a child test binary with poisoned `GIT_*` env (never in the
/// normal suite — hence `ignore`), resolving through the sanitized git
/// invocation. `QS_PROBE_REPO`/`QS_PROBE_REG` arrive via the child env.
#[test]
#[ignore]
fn inner_misleading_git_dir_probe() {
    assert!(
        std::env::var("GIT_DIR").is_ok(),
        "probe misconfigured: GIT_DIR must be poisoned"
    );
    let repo = std::env::var("QS_PROBE_REPO").expect("QS_PROBE_REPO");
    let reg = PathBuf::from(std::env::var("QS_PROBE_REG").expect("QS_PROBE_REG"));
    let mut r = resolver_for(&reg);
    let hit = r
        .resolve_resource(&res("kitty", None, None, Some(&repo)), 17_000_000)
        .expect("sanitized discovery must match the real repo despite GIT_DIR");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "git_remote");
}

#[test]
fn normalize_rejects_credentials_leak_and_bad_hosts() {
    // Credentials are stripped, never echoed.
    let n = normalize_github_remote("https://oauth-token123@github.com/acme/widget").unwrap();
    assert_eq!(n, "https://github.com/acme/widget");
    assert!(!n.contains("oauth"));
    assert!(normalize_github_remote("https://github.com.evil.example/acme/widget").is_none());
    assert!(normalize_github_remote("http://github.com/acme/widget").is_none());
    assert!(normalize_github_remote("git@gitlab.com:acme/widget.git").is_none());
    assert!(normalize_github_remote("https://github.com/ac me/widget").is_none());
}

#[test]
fn dotdot_after_symlink_resolves_target_not_lexical_parent() {
    // Regression: `..` must resolve AFTER symlinks. With
    // `<work>/link -> <repos>/B/subdir`, `<work>/link/../file` is
    // `<repos>/B/file`, never `<work>/file`.
    #[cfg(unix)]
    {
        let base = tmpdir("dotdot");
        let repos_b = base.join("repos").join("B");
        let subdir = repos_b.join("subdir");
        std::fs::create_dir_all(&subdir).unwrap();
        let work = base.join("work");
        std::fs::create_dir_all(&work).unwrap();
        std::os::unix::fs::symlink(&subdir, work.join("link")).unwrap();
        let got = canonicalize_path_str(
            &work.join("link").join("..").join("file").to_string_lossy(),
        )
        .expect("must canonicalize");
        let want = repos_b.join("file").to_string_lossy().to_string();
        assert_eq!(got, want, "symlink-aware .. must follow the target");
        // And resolution actually matches a mapping for the real location.
        let reg = base.join("projects.toml");
        write_registry(
            &reg,
            &registry_doc(&[reg_table(A_ID, "B", &repos_b.to_string_lossy(), "")]),
        );
        let mut r = resolver_for(&reg);
        let via = work.join("link").join("..").join("file").to_string_lossy().to_string();
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&via), None, None), 14_000_000)
                .map(|p| p.id),
            Some(A_ID.to_string())
        );
        // The lexical (wrong) parent must NOT match a project mapped there.
        let reg2 = base.join("projects2.toml");
        write_registry(
            &reg2,
            &registry_doc(&[reg_table(B_ID, "Work", &work.to_string_lossy(), "")]),
        );
        let mut r2 = resolver_for(&reg2);
        assert_eq!(
            r2.resolve_resource(&res("neovim", Some(&via), None, None), 14_000_001),
            None,
            "must not match the lexical parent across a symlink"
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}

#[test]
fn symlink_retarget_converges_without_registry_reload() {
    // Mapped-folder projections refresh lazily (short TTL): retargeting the
    // symlink behind an unchanged registry converges after the TTL — the old
    // location stops matching and the new one starts, with no Python respawn.
    #[cfg(unix)]
    {
        use qs_agent_orchestrator::project_context::FOLDER_TTL_MS;
        let base = tmpdir("retarget");
        let real_a = base.join("real-a");
        let real_b = base.join("real-b");
        std::fs::create_dir_all(&real_a).unwrap();
        std::fs::create_dir_all(&real_b).unwrap();
        let link = base.join("link");
        std::os::unix::fs::symlink(&real_a, &link).unwrap();
        let reg = base.join("projects.toml");
        write_registry(
            &reg,
            &registry_doc(&[reg_table(A_ID, "A", &link.to_string_lossy(), "")]),
        );
        let mut r = resolver_for(&reg);
        let t0 = 15_000_000;
        let fa = real_a.join("x.md").to_string_lossy().to_string();
        let fb = real_b.join("x.md").to_string_lossy().to_string();
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&fa), None, None), t0)
                .map(|p| p.id),
            Some(A_ID.to_string()),
            "pre-retarget target must match"
        );
        // Retarget while the registry file is untouched.
        std::fs::remove_file(&link).unwrap();
        std::os::unix::fs::symlink(&real_b, &link).unwrap();
        let t1 = t0 + FOLDER_TTL_MS as i64 + 1;
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&fa), None, None), t1),
            None,
            "old target must stop matching after the folder TTL"
        );
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&fb), None, None), t1 + 1)
                .map(|p| p.id),
            Some(A_ID.to_string()),
            "new target must match after the folder TTL"
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}

#[test]
fn logseq_page_matches_derived_label() {
    let base = tmpdir("logseqpage");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table_page(
            A_ID,
            "Asset-Prices",
            "",
            "",
            "pages/Asset-Prices.md",
        )]),
    );
    let mut r = resolver_for(&reg);
    let hit = r
        .resolve_resource(&res_page("logseq-title", "Asset-Prices"), 20_000_000)
        .expect("derived page label must match");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.name, "Asset-Prices");
    assert_eq!(hit.matched_by, "logseq_page");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn logseq_page_matches_display_name_despite_stale_path() {
    // Stale underscore `logseq_path` derives `Gender_Norm_ABM`, but the
    // registry display name mirrors the real page label, so it still matches.
    let base = tmpdir("logseqstale");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table_page(
            A_ID,
            "Gender Norm ABM",
            "",
            "",
            "pages/Gender_Norm_ABM.md",
        )]),
    );
    let mut r = resolver_for(&reg);
    let hit = r
        .resolve_resource(&res_page("logseq-title", "Gender Norm ABM"), 21_000_000)
        .expect("display name must keep stale paths matching");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "logseq_page");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn logseq_page_unknown_matches_nothing() {
    let base = tmpdir("logsequnknown");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table_page(
            A_ID,
            "Asset-Prices",
            "",
            "",
            "pages/Asset-Prices.md",
        )]),
    );
    let mut r = resolver_for(&reg);
    assert_eq!(
        r.resolve_resource(&res_page("logseq-title", "No-Such-Page"), 22_000_000),
        None
    );
    // Empty page carries no signal either.
    assert_eq!(
        r.resolve_resource(&res_page("logseq-title", ""), 22_000_001),
        None
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn logseq_page_tie_is_ambiguous_without_remote_fallback() {
    // Two distinct entries claim the same page; the remote fallback must
    // NOT rescue the tie even though a mapped remote is present.
    let base = tmpdir("logseqpagetie");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table_page(A_ID, "Shared", "", "", "pages/Shared.md"),
            reg_table_page(
                B_ID,
                "Shared",
                "",
                "https://github.com/acme/shared",
                "pages/Shared.md",
            ),
        ]),
    );
    let mut r = resolver_for(&reg);
    let claimed = ResourceContext::new(
        "logseq-title",
        None,
        None,
        None,
        None,
        None,
        Some("Shared"),
        None,
    )
    .with_git_remote(Some("https://github.com/acme/shared"));
    assert_eq!(
        r.resolve_resource(&claimed, 23_000_000),
        None,
        "page tie must not fall back to the remote level"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn linked_folder_cwd_beats_logseq_page() {
    // Entry A matches at CWD strength, entry B at page strength: the
    // stronger path level wins.
    let base = tmpdir("logseqcwdbeats");
    let folder_a = base.join("proj-a");
    std::fs::create_dir_all(&folder_a).unwrap();
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "A", &folder_a.to_string_lossy(), ""),
            reg_table_page(B_ID, "Bee", "", "", "pages/Bee.md"),
        ]),
    );
    let mut r = resolver_for(&reg);
    let both = ResourceContext::new(
        "logseq-title",
        None,
        Some(&folder_a.to_string_lossy()),
        None,
        None,
        None,
        Some("Bee"),
        None,
    );
    let hit = r
        .resolve_resource(&both, 24_000_000)
        .expect("cwd must beat the page level");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "cwd");
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn logseq_page_name_only_entry_matches_nothing() {
    // A name-only row (no `logseq_path`) never matches the page level,
    // even when its display name equals the focused page.
    let base = tmpdir("logseqnameonly");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "Asset-Prices", "", "")]),
    );
    let mut r = resolver_for(&reg);
    assert_eq!(
        r.resolve_resource(&res_page("logseq-title", "Asset-Prices"), 25_000_000),
        None,
        "name-only entries carry no page signal"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn symlinked_registry_is_rejected_fail_closed() {
    // The canonical helper opens the registry with O_NOFOLLOW and rejects
    // symlinks: a symlinked registry resolves to NO project (fail closed),
    // never follows the link, and never serves the linked content stale.
    #[cfg(unix)]
    {
        let base = tmpdir("reglink");
        let real = base.join("real.toml");
        let target = base.join("target-dir");
        std::fs::create_dir_all(&target).unwrap();
        write_registry(
            &real,
            &registry_doc(&[reg_table(A_ID, "A", &target.to_string_lossy(), "")]),
        );
        let link = base.join("projects.toml");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let mut r = resolver_for(&link);
        let f = target.join("x.md").to_string_lossy().to_string();
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&f), None, None), 16_000_000),
            None,
            "symlinked registry must be rejected, not followed"
        );
        // Still rejected on retry (no stale serve of the linked content).
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&f), None, None), 16_006_000),
            None
        );
        // Replacing the symlink with a real file recovers.
        std::fs::remove_file(&link).unwrap();
        write_registry(
            &link,
            &registry_doc(&[reg_table(A_ID, "A", &target.to_string_lossy(), "")]),
        );
        assert_eq!(
            r.resolve_resource(&res("neovim", Some(&f), None, None), 16_007_000)
                .map(|p| p.id),
            Some(A_ID.to_string()),
            "real registry at the same path must recover"
        );
        let _ = std::fs::remove_dir_all(&base);
    }
}
