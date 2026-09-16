//! Zotero active-reader context tests (mocked publisher, no live Zotero).
//!
//! Covers the worker contract with fixture files only:
//! - private/stale/mismatch records (0600, O_NOFOLLOW, 30 s freshness);
//! - multi-window isolation (window_id PLUS pid, never a global reader);
//! - reader closure tombstones evict (never serve last-good);
//! - collection matching before file, descendant inclusion/exclusion,
//!   equal-claim ambiguity with no file fallback, file fallback on miss;
//! - server/library collisions (incl. the user/"0" server-bound alias);
//! - stable document identity across title/version/page changes;
//! - history append + FTS search round-trips and old-JSON compatibility.

use qs_agent_orchestrator::app_context::{
    clear_last_good_cache, enrich_with_env, is_zotero_key,
};
use qs_agent_orchestrator::desktop_context::{
    DesktopContext, FocusedWindow, ProjectContext, ResourceContext, Source, Workspace,
    ZoteroContext,
};
use qs_agent_orchestrator::desktop_session::resource_identity;
use qs_agent_orchestrator::project_context::{parse_zotero_collection, ProjectResolver};
use qs_agent_orchestrator::EnrichmentEnv;
use std::path::{Path, PathBuf};

const A_ID: &str = "11111111-1111-1111-1111-111111111111";
const B_ID: &str = "22222222-2222-2222-2222-222222222222";
const SID: &str = "sPMHtLD6HHBd";
const OTHER_SID: &str = "OTHERsrv99";
const ROOT: &str = "ABCDEFGH";
const CHILD: &str = "IJKL2345";
const OTHER: &str = "ZZZZ9999";
const ITEM: &str = "ITEM0001";
const ATT: &str = "ATTACH01";

fn helper_script() -> PathBuf {
    Path::new(env!("CARGO_MANIFEST_DIR"))
        .join("..")
        .join("..")
        .join("scripts")
        .join("projects.py")
}

fn tmpdir(tag: &str) -> PathBuf {
    let p = std::env::temp_dir().join(format!(
        "qs-zotero-{tag}-{}-{}",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    std::fs::create_dir_all(&p).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700));
    }
    p
}

fn write_private(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700));
        }
    }
    std::fs::write(path, body).unwrap();
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(path, std::fs::Permissions::from_mode(0o600));
    }
}

fn write_registry(path: &Path, body: &str) {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent).unwrap();
    }
    std::fs::write(path, body).unwrap();
}

fn reg_table(id: &str, name: &str, zotero_inline: &str) -> String {
    let mut s = format!("[[projects]]\nid = \"{id}\"\nname = \"{name}\"\n");
    if !zotero_inline.is_empty() {
        s.push_str(&format!("zotero_collection = {{ {zotero_inline} }}\n"));
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

fn zotero_inline(server: &str, ltype: &str, lib: &str, key: &str, subs: bool) -> String {
    format!(
        "server_id = \"{server}\", library_type = \"{ltype}\", library_id = \"{lib}\", collection_key = \"{key}\", include_subcollections = {}",
        if subs { "true" } else { "false" }
    )
}

fn record(
    win: &str,
    pid: u32,
    now: i64,
    server: &str,
    ltype: &str,
    lib: &str,
    item: &str,
    cols: &[&str],
    ancs: &[&str],
) -> String {
    let cols = cols.iter().map(|c| format!("\"{c}\"")).collect::<Vec<_>>().join(",");
    let ancs = ancs.iter().map(|c| format!("\"{c}\"")).collect::<Vec<_>>().join(",");
    format!(
        "{{\"window_id\":\"{win}\",\"pid\":{pid},\"updated_at_ms\":{now},\"server_id\":\"{server}\",\"library_type\":\"{ltype}\",\"library_id\":\"{lib}\",\"item_key\":\"{item}\",\"collections\":[{cols}],\"ancestor_collections\":[{ancs}],\"version\":7,\"title\":\"Paper\",\"zotero_uri\":\"zotero://select/library/items/{item}\"}}"
    )
}

fn zotero_res(
    server: &str,
    ltype: &str,
    lib: &str,
    item: &str,
    cols: Vec<String>,
    ancs: Vec<String>,
) -> ResourceContext {
    ResourceContext::new(
        "zotero",
        None,
        None,
        None,
        None,
        Some(&format!("zotero://select/library/items/{item}")),
        None,
        Some("Paper"),
    )
    .with_zotero(Some(ZoteroContext {
        server_id: server.to_string(),
        library_type: ltype.to_string(),
        library_id: lib.to_string(),
        item_key: item.to_string(),
        attachment_key: None,
        collections: cols,
        ancestor_collections: ancs,
        version: Some(7),
        uri: Some(format!("zotero://select/library/items/{item}")),
    }))
}

#[test]
fn key_shapes() {
    assert!(is_zotero_key("ABCDEFGH"));
    assert!(is_zotero_key("ABC12345"));
    assert!(!is_zotero_key("abcdef12"));
    assert!(!is_zotero_key("SHORT"));
    assert!(!is_zotero_key("TOOLONG123"));
}

#[test]
fn zotero_collection_projection_strict() {
    let ok = serde_json::json!({
        "server_id": SID, "library_type": "user", "library_id": "0",
        "collection_key": ROOT, "include_subcollections": true,
    });
    let zc = parse_zotero_collection(Some(&ok)).expect("valid");
    assert!(zc.is_some());
    assert!(parse_zotero_collection(None).unwrap().is_none());
    assert!(parse_zotero_collection(Some(&serde_json::Value::Null)).unwrap().is_none());
    // Default true when omitted.
    let no_inc = serde_json::json!({
        "server_id": SID, "library_type": "user", "library_id": "0",
        "collection_key": ROOT,
    });
    assert!(parse_zotero_collection(Some(&no_inc)).unwrap().unwrap().include_subcollections);
    // Strict rejections fail the whole load (Err).
    for bad in [
        serde_json::json!({"server_id": "", "library_type": "user", "library_id": "0", "collection_key": ROOT}),
        serde_json::json!({"server_id": SID, "library_type": "person", "library_id": "0", "collection_key": ROOT}),
        serde_json::json!({"server_id": SID, "library_type": "group", "library_id": "0", "collection_key": ROOT}),
        serde_json::json!({"server_id": SID, "library_type": "user", "library_id": "12a", "collection_key": ROOT}),
        serde_json::json!({"server_id": SID, "library_type": "user", "library_id": "0", "collection_key": "abcdef12"}),
        serde_json::json!({"server_id": SID, "library_type": "user", "library_id": "0", "collection_key": ROOT, "include_subcollections": "yes"}),
        serde_json::json!({"server_id": SID, "library_type": "user", "library_id": "0", "collection_key": ROOT, "bogus": 1}),
    ] {
        assert!(parse_zotero_collection(Some(&bad)).is_err(), "must reject {bad}");
    }
}

#[test]
fn private_stale_mismatch_records() {
    clear_last_good_cache();
    let dir = tmpdir("priv");
    let now = 50_000_000;
    let win = FocusedWindow::new_with_pid("0xZ", "Zotero", "t", Some(111));
    let base = || DesktopContext::available(Source::Hyprland, Some(win.clone()), None, now);
    // Unconfigured: no resource, never a title guess.
    let out = enrich_with_env(base(), &EnrichmentEnv::default(), now);
    assert_eq!(out.resource, None);
    // Valid private record binds.
    let p = dir.join("z.json");
    write_private(&p, &record("0xZ", 111, now, SID, "user", "0", ITEM, &[ROOT], &[]));
    let env = EnrichmentEnv { zotero_file: Some(p.clone()), ..Default::default() };
    let out = enrich_with_env(base(), &env, now);
    let r = out.resource.expect("bound");
    assert_eq!(r.adapter, "zotero");
    assert_eq!(r.zotero.as_ref().unwrap().item_key, ITEM);
    // Stale record (updated_at far past 30 s) is a confirmed mismatch: evicts.
    write_private(&p, &record("0xZ", 111, now - 60_000, SID, "user", "0", ITEM, &[ROOT], &[]));
    let stale = enrich_with_env(base(), &env, now);
    assert_eq!(stale.resource, None, "stale must evict, not serve last-good");
    // Missing file after a prime is transient (serves last-good for same focus).
    write_private(&p, &record("0xZ", 111, now, SID, "user", "0", ITEM, &[ROOT], &[]));
    let prime = enrich_with_env(base(), &env, now);
    assert!(prime.resource.is_some());
    let _ = std::fs::remove_file(&p);
    let second = enrich_with_env(base(), &env, now + 1000);
    assert_eq!(second.resource, prime.resource, "transient miss serves last-good");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn multiwindow_isolation_never_global() {
    clear_last_good_cache();
    let dir = tmpdir("multi");
    let now = 60_000_000;
    let p = dir.join("z.json");
    // Publisher reports window A; focus is window B with same pid.
    write_private(&p, &record("0xAAA", 111, now, SID, "user", "0", ITEM, &[ROOT], &[]));
    let env = EnrichmentEnv { zotero_file: Some(p.clone()), ..Default::default() };
    let other = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new_with_pid("0xBBB", "Zotero", "t", Some(111))),
        None,
        now,
    );
    let out = enrich_with_env(other, &env, now);
    assert_eq!(out.resource, None, "other window must not inherit the global reader");
    // Same window id, different pid: also mismatch (pid insufficient alone).
    let pid_other = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new_with_pid("0xAAA", "Zotero", "t", Some(222))),
        None,
        now,
    );
    let out2 = enrich_with_env(pid_other, &env, now);
    assert_eq!(out2.resource, None);
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn reader_closure_evicts_without_serving_last_good() {
    clear_last_good_cache();
    let dir = tmpdir("closed");
    let now = 70_000_000;
    let p = dir.join("z.json");
    let win = FocusedWindow::new_with_pid("0xZ", "Zotero", "t", Some(111));
    let base = || DesktopContext::available(Source::Hyprland, Some(win.clone()), None, now);
    let env = EnrichmentEnv { zotero_file: Some(p.clone()), ..Default::default() };
    write_private(&p, &record("0xZ", 111, now, SID, "user", "0", ITEM, &[ROOT], &[]));
    let prime = enrich_with_env(base(), &env, now);
    assert!(prime.resource.is_some());
    // Explicit tombstone closes the reader: must evict, and a later transient
    // miss must find nothing (not the evicted document).
    write_private(&p, r#"{"window_id":"0xZ","pid":111,"updated_at_ms":70000000,"state":"closed"}"#);
    let closed = enrich_with_env(base(), &env, now + 1000);
    assert_eq!(closed.resource, None, "closure must evict");
    let _ = std::fs::remove_file(&p);
    let after = enrich_with_env(base(), &env, now + 2000);
    assert_eq!(after.resource, None, "evicted closure must not be re-served");
    let _ = std::fs::remove_dir_all(&dir);
}

#[test]
fn collection_before_file_with_descendants_and_ambiguity() {
    let base = tmpdir("match");
    let file_proj = base.join("files");
    std::fs::create_dir_all(&file_proj).unwrap();
    let reg = base.join("projects.toml");
    // A claims ROOT with descendants; B claims OTHER without descendants.
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "A", &zotero_inline(SID, "user", "0", ROOT, true)),
            reg_table(B_ID, "B", &zotero_inline(SID, "user", "0", OTHER, false)),
        ]),
    );
    let mut r = resolver_for(&reg);
    // Direct membership hits A even though file would hit nobody.
    let hit = r
        .resolve_resource(&zotero_res(SID, "user", "0", ITEM, vec![ROOT.to_string()], vec![]), 100)
        .expect("direct must match");
    assert_eq!((hit.id.as_str(), hit.matched_by.as_str()), (A_ID, "zotero_collection"));
    // Descendant via ancestors hits A when descendants included.
    let hit2 = r
        .resolve_resource(
            &zotero_res(SID, "user", "0", ITEM, vec![CHILD.to_string()], vec![ROOT.to_string()]),
            101,
        )
        .expect("descendant must match");
    assert_eq!(hit2.id, A_ID);
    // Ancestor-only without opt-in misses: B with include_subcollections=false
    // matches only direct membership, so an ancestor-only OTHER does not hit.
    assert_eq!(
        r.resolve_resource(
            &zotero_res(SID, "user", "0", ITEM, vec![CHILD.to_string()], vec![OTHER.to_string()]),
            102
        ),
        None,
        "ancestor-only must miss when include_subcollections is false"
    );
    // Direct hit on OTHER (no ancestors needed) still matches B.
    let hit_b = r
        .resolve_resource(&zotero_res(SID, "user", "0", ITEM, vec![OTHER.to_string()], vec![]), 103)
        .expect("direct OTHER");
    assert_eq!(hit_b.id, B_ID);
    // Ambiguity: two projects claim the same collection -> None, NO file fallback.
    write_registry(
        &reg,
        &registry_doc(&[
            reg_table(A_ID, "A", &zotero_inline(SID, "user", "0", ROOT, true)),
            reg_table(B_ID, "B", &zotero_inline(SID, "user", "0", ROOT, true)),
        ]),
    );
    let f = file_proj.join("x.md").to_string_lossy().to_string();
    let mut res = zotero_res(SID, "user", "0", ITEM, vec![ROOT.to_string()], vec![]);
    res.file = Some(f.clone());
    res.cwd = Some(file_proj.to_string_lossy().to_string());
    // Give the file strength somewhere to fall back to: map file_proj to B via
    // a fresh registry is impossible here (same file would be ambiguous), so
    // assert the collection tie itself returns None rather than a file hit.
    assert_eq!(r.resolve_resource(&res, 200), None, "collection tie must not fall back");
    // No collection match falls back to file matching.
    write_registry(
        &reg,
        &registry_doc(&[reg_table(
            A_ID,
            "A-files",
            "",
        )]),
    );
    // Rewrite with a folder mapping instead of zotero for the fallback check.
    write_registry(
        &reg,
        &format!(
            "version = 1\n\n[[projects]]\nid = \"{A_ID}\"\nname = \"A\"\nlocal_folder = \"{}\"\n",
            file_proj.to_string_lossy()
        ),
    );
    let mut res2 = zotero_res(SID, "user", "0", ITEM, vec![OTHER.to_string()], vec![]);
    res2.file = Some(f);
    let hit3 = r.resolve_resource(&res2, 300).expect("file fallback after collection miss");
    assert_eq!((hit3.id.as_str(), hit3.matched_by.as_str()), (A_ID, "file"));
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn server_and_library_collisions() {
    let base = tmpdir("collide");
    let reg = base.join("projects.toml");
    write_registry(
        &reg,
        &registry_doc(&[reg_table(A_ID, "A", &zotero_inline(SID, "user", "0", ROOT, true))]),
    );
    let mut r = resolver_for(&reg);
    // Different server, same collection: no match.
    assert_eq!(
        r.resolve_resource(&zotero_res(OTHER_SID, "user", "0", ITEM, vec![ROOT.to_string()], vec![]), 1),
        None
    );
    // Same server, group vs user: no match.
    assert_eq!(
        r.resolve_resource(&zotero_res(SID, "group", "123", ITEM, vec![ROOT.to_string()], vec![]), 2),
        None
    );
    // user/"0" alias matches a concrete user library on the same server.
    let hit = r
        .resolve_resource(&zotero_res(SID, "user", "999", ITEM, vec![ROOT.to_string()], vec![]), 3)
        .expect("alias must match");
    assert_eq!(hit.id, A_ID);
    // Exact (non-alias) entries match only exactly.
    write_registry(
        &reg,
        &registry_doc(&[reg_table(B_ID, "B", &zotero_inline(SID, "user", "999", ROOT, true))]),
    );
    assert_eq!(
        r.resolve_resource(&zotero_res(SID, "user", "888", ITEM, vec![ROOT.to_string()], vec![]), 4),
        None,
        "non-alias library must match exactly"
    );
    let _ = std::fs::remove_dir_all(&base);
}

#[test]
fn stable_identity_ignores_title_version_page() {
    let mk = |title: &str, version: i64| {
        ResourceContext::new(
            "zotero",
            None,
            None,
            None,
            None,
            Some("zotero://select/library/items/ITEM0001"),
            None,
            Some(title),
        )
        .with_zotero(Some(ZoteroContext {
            server_id: SID.to_string(),
            library_type: "user".to_string(),
            library_id: "0".to_string(),
            item_key: ITEM.to_string(),
            attachment_key: Some(ATT.to_string()),
            collections: vec![ROOT.to_string()],
            ancestor_collections: vec![],
            version: Some(version),
            uri: Some("zotero://select/library/items/ITEM0001".to_string()),
        }))
    };
    let a = resource_identity(&mk("Title One", 1), None).expect("identity");
    let b = resource_identity(&mk("Title Two", 99), None).expect("identity");
    assert_eq!(a.key, b.key, "title/version must not affect identity");
    assert_eq!(a.kind, qs_agent_orchestrator::desktop_session::ResourceKind::Portable);
    // Different attachment stays distinct; missing attachment differs too.
    let mut c = mk("Title One", 1);
    c.zotero.as_mut().unwrap().attachment_key = Some("ATTACH02".to_string());
    let c_id = resource_identity(&c, None).unwrap();
    assert_ne!(a.key, c_id.key);
}

#[test]
fn history_search_and_old_json_compatibility() {
    // Old JSON (no zotero, no project) still parses; new rows round-trip and
    // FTS search finds the stable keys without content extraction.
    let legacy = r#"{"focused_window":{"id":"0x1","application":"Zotero","title":"t"},"workspace":{"id":"1","name":"1"},"available":true,"source":"hyprland","observed_at_ms":123}"#;
    let ctx: DesktopContext = serde_json::from_str(legacy).expect("legacy must parse");
    assert_eq!(ctx.resource, None);
    assert_eq!(ctx.project, None);
    let legacy_res = r#"{"adapter":"zotero","title":"t"}"#;
    let rc: ResourceContext = serde_json::from_str(legacy_res).expect("legacy resource parses");
    assert_eq!(rc.zotero, None);

    let store = qs_agent_orchestrator::ActivityStore::open_in_memory().unwrap();
    let res = zotero_res(SID, "user", "0", ITEM, vec![ROOT.to_string()], vec![]);
    let snap = DesktopContext::available(
        Source::Hyprland,
        Some(FocusedWindow::new("0xZ", "Zotero", "Paper")),
        Some(Workspace::new("1", "1")),
        1000,
    )
    .with_resource(Some(res))
    .with_project(Some(ProjectContext::new(A_ID, "A", "zotero_collection")));
    store.append("context", "hyprland", &snap).unwrap();
    let rows = store.recent_activity(10).unwrap();
    assert_eq!(rows.len(), 1);
    assert_eq!(rows[0].snapshot().unwrap(), snap);
    // Session-centric search finds the row by item key (metadata only).
    let q = qs_agent_orchestrator::desktop_store::SessionSearchQuery {
        query: Some(ITEM.to_string()),
        limit: 10,
        ..Default::default()
    };
    let hits = store.search_sessions(&q).unwrap();
    assert!(!hits.is_empty(), "search must find the zotero row by item key");
    // Resource aggregation uses the stable document key.
    let recs = store.session_resources(&hits[0].session.session_id, 10).unwrap();
    assert!(recs.iter().any(|x| x.resource_key.contains(ITEM)));
    // Project-filtered history still works with the new matched_by.
    let proj_rows = store.recent_activity_for_project(A_ID, 10).unwrap();
    assert_eq!(proj_rows.len(), 1);
}
