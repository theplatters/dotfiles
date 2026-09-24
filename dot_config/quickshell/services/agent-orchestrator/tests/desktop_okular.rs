//! Okular project-association tests (no live Okular, no `busctl`).
//!
//! Covers the worker contract with injected D-Bus documents only:
//! - a focused Okular window showing a PDF inside a registered project
//!   folder resolves with `matched_by == "file"`;
//! - a document in an unregistered folder matches no project;
//! - the welcome screen (no document) carries no resource and no project.

use qs_agent_orchestrator::app_context::{
    clear_last_good_cache, clear_okular_test_docs, enrich_with_env, set_okular_test_docs,
};
use qs_agent_orchestrator::desktop_context::{DesktopContext, FocusedWindow, Source};
use qs_agent_orchestrator::project_context::ProjectResolver;
use qs_agent_orchestrator::EnrichmentEnv;
use std::path::{Path, PathBuf};

const A_ID: &str = "11111111-1111-1111-1111-111111111111";

fn helper_script() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("scripts")
        .join("projects.py")
}

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-okular-{tag}-{}-{}",
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

fn registry_doc(folder: &str) -> String {
    format!("version = 1\n\n[[projects]]\nid = \"{A_ID}\"\nname = \"Alpha\"\nlocal_folder = \"{folder}\"\n")
}

fn resolver_for(registry: &Path) -> ProjectResolver {
    ProjectResolver::with_paths(registry.to_path_buf(), helper_script())
}

fn okular_ctx(doc_title: &str, pid: u32) -> DesktopContext {
    DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new_with_pid(
            "0xO",
            "org.kde.okular",
            doc_title,
            Some(pid),
        )),
        None,
        1_000_000,
    )
}

#[test]
fn okular_document_inside_project_matches_by_file() {
    clear_last_good_cache();
    let base = tmpdir("match");
    let proj = base.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    let doc = proj.join("paper.pdf").to_string_lossy().to_string();
    let reg = base.join("projects.toml");
    write_registry(&reg, &registry_doc(&proj.to_string_lossy()));
    let mut r = resolver_for(&reg);

    set_okular_test_docs(Some(vec![doc.clone()]));
    let apped = enrich_with_env(
        okular_ctx("paper — Okular", 4242),
        &EnrichmentEnv::default(),
        1_000_000,
    );
    let res = apped.resource.clone().expect("okular file resource");
    assert_eq!(res.adapter, "okular");
    assert_eq!(res.file.as_deref(), Some(doc.as_str()));
    let enriched = r.enrich(apped, 1_000_000);
    let hit = enriched.project.expect("project must match");
    assert_eq!(hit.id, A_ID);
    assert_eq!(hit.matched_by, "file");

    clear_okular_test_docs();
    clear_last_good_cache();
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn okular_document_outside_projects_matches_nothing() {
    clear_last_good_cache();
    let base = tmpdir("nomatch");
    let proj = base.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    let elsewhere = base.join("elsewhere");
    std::fs::create_dir_all(&elsewhere).unwrap();
    let doc = elsewhere.join("paper.pdf").to_string_lossy().to_string();
    let reg = base.join("projects.toml");
    write_registry(&reg, &registry_doc(&proj.to_string_lossy()));
    let mut r = resolver_for(&reg);

    set_okular_test_docs(Some(vec![doc.clone()]));
    let apped = enrich_with_env(
        okular_ctx("paper — Okular", 4242),
        &EnrichmentEnv::default(),
        2_000_000,
    );
    // The file resource is still honest — it just matches no project.
    assert_eq!(
        apped.resource.as_ref().and_then(|x| x.file.as_deref()),
        Some(doc.as_str())
    );
    let enriched = r.enrich(apped, 2_000_000);
    assert_eq!(enriched.project, None);

    clear_okular_test_docs();
    clear_last_good_cache();
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn okular_welcome_screen_has_no_resource_or_project() {
    clear_last_good_cache();
    let base = tmpdir("welcome");
    let proj = base.join("proj");
    std::fs::create_dir_all(&proj).unwrap();
    let reg = base.join("projects.toml");
    write_registry(&reg, &registry_doc(&proj.to_string_lossy()));
    let mut r = resolver_for(&reg);

    // Empty discovery = welcome screen (`s ""`): confirmed no document, so
    // no fabricated resource and no project.
    set_okular_test_docs(Some(Vec::new()));
    let apped = enrich_with_env(
        okular_ctx("Okular", 4242),
        &EnrichmentEnv::default(),
        3_000_000,
    );
    assert_eq!(apped.resource, None);
    let enriched = r.enrich(apped, 3_000_000);
    assert_eq!(enriched.project, None);

    clear_okular_test_docs();
    clear_last_good_cache();
    let _ = std::fs::remove_dir_all(&base);
}
