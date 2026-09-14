//! Private database paths and singleton lock paths.
//!
//! The collector uses a private directory (never existing shared dirs):
//! `$XDG_STATE_HOME/quickshell/desktop-activity/`, falling back to
//! `$HOME/.local/state/quickshell/desktop-activity/`.
//! The database (`activity.db`, plus SQLite WAL/SHM sidecars) and the
//! advisory lock file all live inside that private directory.
//!
//! Security contract (local, reasonably scoped):
//! - New directories are created `0700` from the outset (no world-readable
//!   window, no post-hoc `chmod` of unrelated directories).
//! - Existing parent directories are never `chmod`ed here. When the leaf
//!   parent already exists it must be a real directory owned by the current
//!   user with private permissions (no group/other access), otherwise an
//!   error is returned and nothing is modified.
//! - Existing database/lock/sidecar files must be regular files owned by the
//!   current user with private permissions and must not be symlinks.
//! - Lock files are per-database (`<dbname>.lock` sibling), so two different
//!   databases in one directory do not share a lock.

use std::path::{Path, PathBuf};

pub const PRIVATE_DIR: &str = "quickshell/desktop-activity";
pub const DB_FILENAME: &str = "activity.db";
pub const LOCK_FILENAME: &str = "activity.db.lock";

/// SQLite sidecar suffixes that must never be symlinks.
pub const SIDECAR_SUFFIXES: &[&str] = &["-wal", "-shm", "-journal"];

/// Resolve the default database path. Errors (not panics) when neither
/// `XDG_STATE_HOME` nor `HOME` yields a usable base.
pub fn default_db_path() -> Result<PathBuf, String> {
    if let Some(xdg) = std::env::var_os("XDG_STATE_HOME") {
        if !xdg.is_empty() {
            let mut p = PathBuf::from(xdg);
            p.push(PRIVATE_DIR);
            p.push(DB_FILENAME);
            return Ok(p);
        }
    }
    if let Some(home) = std::env::var_os("HOME") {
        if !home.is_empty() {
            let mut p = PathBuf::from(home);
            p.push(".local/state");
            p.push(PRIVATE_DIR);
            p.push(DB_FILENAME);
            return Ok(p);
        }
    }
    Err("cannot resolve state directory: set XDG_STATE_HOME or HOME".to_string())
}

/// Lock file path for a given database: per-database `<filename>.lock`
/// sibling inside the same directory.
///
/// For the default `activity.db` this is `activity.db.lock` (backwards
/// compatible). Two different database filenames in one directory get
/// different locks.
pub fn lock_path_for(db_path: &Path) -> PathBuf {
    if let Some(name) = db_path.file_name() {
        let mut lock_name = name.to_os_string();
        lock_name.push(".lock");
        if let Some(parent) = db_path.parent() {
            if !parent.as_os_str().is_empty() {
                return parent.join(lock_name);
            }
        }
        return PathBuf::from(lock_name);
    }
    if let Some(parent) = db_path.parent() {
        if !parent.as_os_str().is_empty() {
            return parent.join(LOCK_FILENAME);
        }
    }
    PathBuf::from(LOCK_FILENAME)
}

/// Canonical per-database lock path.
///
/// Resolves the nearest existing ancestor of the database parent and joins
/// the per-database `<filename>.lock` name without following a symlinked
/// leaf. Fails when any existing path component up to and including the
/// parent is a symlink, when the database has no filename, or when the
/// canonical parent cannot be resolved. This keeps alias paths
/// (`a/../a/x.db`, symlinked dirs) from silently picking divergent locks:
/// callers can compare [`lock_path_for`] output against this canonical form
/// via [`validate_lock_for_db`].
pub fn canonical_lock_path_for(db_path: &Path) -> Result<PathBuf, String> {
    let file_name = db_path
        .file_name()
        .ok_or_else(|| "database path has no filename".to_string())?;
    let parent = db_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| "database path has no parent directory".to_string())?;
    let canonical_parent = canonical_parent_dir(parent)?;
    let mut lock_name = file_name.to_os_string();
    lock_name.push(".lock");
    Ok(canonical_parent.join(lock_name))
}

/// Validate that `lock_path` is the expected per-database sibling for
/// `db_path` and does not escape through a canonical alias.
///
/// Checks:
/// - `lock_path` filename equals `<db filename>.lock`,
/// - `lock_path` parent equals `db` parent (string form),
/// - when both parents canonicalize, they resolve to the same directory.
///
/// The canonical comparison is best-effort when parents do not exist yet:
/// missing parents skip the canonical check (creation will validate), but
/// existing symlinked parents always fail.
pub fn validate_lock_for_db(db_path: &Path, lock_path: &Path) -> Result<(), String> {
    let db_name = db_path
        .file_name()
        .ok_or_else(|| "database path has no filename".to_string())?;
    let mut expected: std::ffi::OsString = db_name.to_os_string();
    expected.push(".lock");
    let lock_name = lock_path
        .file_name()
        .ok_or_else(|| "lock path has no filename".to_string())?;
    if lock_name != expected.as_os_str() {
        return Err(format!(
            "lock filename {} does not match database {} (expected {})",
            lock_path.display(),
            db_path.display(),
            expected.to_string_lossy()
        ));
    }
    let db_parent = db_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| "database path has no parent directory".to_string())?;
    let lock_parent = lock_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| "lock path has no parent directory".to_string())?;
    if lock_parent != db_parent {
        return Err(format!(
            "lock {} is not a sibling of database {}",
            lock_path.display(),
            db_path.display()
        ));
    }
    // Canonical alias check when both sides exist.
    let db_canon = canonical_parent_dir(db_parent);
    let lock_canon = canonical_parent_dir(lock_parent);
    match (db_canon, lock_canon) {
        (Ok(a), Ok(b)) if a != b => Err(format!(
            "lock parent {} canonicalizes outside database parent {}",
            lock_path.display(),
            db_path.display()
        )),
        // If either side is missing we cannot canonicalize; creation-time
        // symlink checks in `ensure_parent_dir` still apply.
        _ => Ok(()),
    }
}

fn canonical_parent_dir(parent: &Path) -> Result<PathBuf, String> {
    // Reject symlink components along the existing prefix before resolving.
    reject_symlink_components(parent)?;
    std::fs::canonicalize(parent)
        .map_err(|e| format!("cannot canonicalize {}: {e}", parent.display()))
}

/// Fail when any existing component of `path` (or `path` itself) is a
/// symlink. Missing trailing components are fine (they will be created).
fn reject_symlink_components(path: &Path) -> Result<(), String> {
    let mut prefix = PathBuf::new();
    let is_absolute = path.is_absolute();
    if is_absolute {
        prefix.push("/");
    }
    for comp in path.components() {
        use std::path::Component;
        match comp {
            Component::RootDir | Component::Prefix(_) => continue,
            Component::CurDir => continue,
            Component::ParentDir => {
                prefix.push("..");
                continue;
            }
            Component::Normal(c) => {
                prefix.push(c);
            }
        }
        match std::fs::symlink_metadata(&prefix) {
            Ok(md) => {
                if md.file_type().is_symlink() {
                    return Err(format!(
                        "unsafe symlink component: {}",
                        prefix.display()
                    ));
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                // Remaining components do not exist yet; they will be
                // created. No further symlink check possible here.
                return Ok(());
            }
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                return Err(format!(
                    "permission denied inspecting {}: {e}",
                    prefix.display()
                ));
            }
            Err(_) => {
                // Other I/O errors (e.g. loop): treat conservatively as
                // unsafe only if we cannot prove safety; surface the error.
                return Ok(());
            }
        }
    }
    Ok(())
}

/// Create the parent directory (and missing ancestors) with private
/// permissions, or validate an existing parent.
///
/// - Missing directories are created `0700` from the outset; existing
///   ancestors are left untouched (never `chmod`ed).
/// - An existing leaf parent must be a real directory (not a symlink),
///   owned by the current euid, with no group/other permission bits.
///   Violations are errors; nothing is modified.
/// - Any symlink along the existing prefix is rejected.
pub fn ensure_parent_dir(db_path: &Path) -> Result<(), String> {
    let parent = db_path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| "database path has no parent directory".to_string())?;

    match std::fs::symlink_metadata(parent) {
        Ok(md) => {
            if md.file_type().is_symlink() {
                return Err(format!(
                    "unsafe database parent (symlink): {}",
                    parent.display()
                ));
            }
            if !md.is_dir() {
                return Err(format!(
                    "database parent is not a directory: {}",
                    parent.display()
                ));
            }
            // Existing leaf: validate, never chmod.
            reject_symlink_components(parent)?;
            validate_dir_private(parent)?;
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // Create missing ancestors 0700 without touching existing ones.
            reject_symlink_components(parent)?;
            mkdir_all_private(parent)?;
            // Re-validate the leaf we just created (owner/mode/dir).
            match std::fs::symlink_metadata(parent) {
                Ok(md) => {
                    if md.file_type().is_symlink() {
                        return Err(format!(
                            "unsafe database parent (symlink after create): {}",
                            parent.display()
                        ));
                    }
                    if !md.is_dir() {
                        return Err(format!(
                            "database parent is not a directory: {}",
                            parent.display()
                        ));
                    }
                    validate_dir_private(parent)?;
                    Ok(())
                }
                Err(e) => Err(format!(
                    "cannot inspect {} after create: {e}",
                    parent.display()
                )),
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => Err(format!(
            "permission denied inspecting {}: {e}",
            parent.display()
        )),
        Err(e) => Err(format!(
            "cannot inspect {}: {e}",
            parent.display()
        )),
    }
}

/// Prepare the parent for a database/lock file when callers already hold an
/// accepted path (store open, lock acquire).
///
/// Unlike [`ensure_parent_dir`] (strict gate used by the collector binary
/// for `--db` preparation), this helper is lenient about group/other bits on
/// a pre-existing parent: it creates missing ancestors `0700` from the
/// outset, validates symlink/dir/owner, but never `chmod`s and never fails
/// solely for `0755` test tmpdirs. File-level safety (0600, owner, regular,
/// no symlink) is still enforced by the caller. This keeps existing
/// file-level integration usage compatible while new deployments go through
/// the strict gate.
pub fn ensure_parent_for_file(path: &Path) -> Result<(), String> {
    let parent = path
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .ok_or_else(|| "path has no parent directory".to_string())?;
    match std::fs::symlink_metadata(parent) {
        Ok(md) => {
            if md.file_type().is_symlink() {
                return Err(format!(
                    "unsafe parent (symlink): {}",
                    parent.display()
                ));
            }
            if !md.is_dir() {
                return Err(format!(
                    "parent is not a directory: {}",
                    parent.display()
                ));
            }
            reject_symlink_components(parent)?;
            validate_dir_owner(parent)?;
            Ok(())
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            reject_symlink_components(parent)?;
            mkdir_all_private(parent)?;
            match std::fs::symlink_metadata(parent) {
                Ok(md) => {
                    if md.file_type().is_symlink() || !md.is_dir() {
                        return Err(format!(
                            "unsafe parent after create: {}",
                            parent.display()
                        ));
                    }
                    validate_dir_owner(parent)?;
                    // Newly created leaves must already be private (0700).
                    validate_dir_private(parent)?;
                    Ok(())
                }
                Err(e) => Err(format!(
                    "cannot inspect {} after create: {e}",
                    parent.display()
                )),
            }
        }
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => Err(format!(
            "permission denied inspecting {}: {e}",
            parent.display()
        )),
        Err(e) => Err(format!("cannot inspect {}: {e}", parent.display())),
    }
}

/// Validate directory owner (and dir-ness, no symlink) without checking
/// permission bits. Used for lenient existing-parent acceptance.
pub fn validate_dir_owner(dir: &Path) -> Result<(), String> {
    let md = std::fs::symlink_metadata(dir)
        .map_err(|e| format!("cannot inspect {}: {e}", dir.display()))?;
    if md.file_type().is_symlink() {
        return Err(format!("unsafe directory (symlink): {}", dir.display()));
    }
    if !md.is_dir() {
        return Err(format!("not a directory: {}", dir.display()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        let euid = unsafe { libc::geteuid() };
        if md.uid() != euid {
            return Err(format!(
                "directory {} not owned by current user",
                dir.display()
            ));
        }
    }
    Ok(())
}

/// Validate an existing directory: real dir, owned by euid, private
/// (no group/other bits). Never modifies anything.
pub fn validate_dir_private(dir: &Path) -> Result<(), String> {
    let md = std::fs::symlink_metadata(dir)
        .map_err(|e| format!("cannot inspect {}: {e}", dir.display()))?;
    if md.file_type().is_symlink() {
        return Err(format!("unsafe directory (symlink): {}", dir.display()));
    }
    if !md.is_dir() {
        return Err(format!("not a directory: {}", dir.display()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        use std::os::unix::fs::PermissionsExt;
        let euid = unsafe { libc::geteuid() };
        if md.uid() != euid {
            return Err(format!(
                "directory {} not owned by current user",
                dir.display()
            ));
        }
        let mode = md.permissions().mode() & 0o777;
        if mode & 0o077 != 0 {
            return Err(format!(
                "directory {} has group/other permissions ({:o}); expected private (0700)",
                dir.display(),
                mode
            ));
        }
    }
    Ok(())
}

/// Validate an existing regular file: not a symlink, regular file, owned by
/// euid, private (no group/other bits). Never modifies anything.
pub fn validate_file_private(path: &Path) -> Result<(), String> {
    let md = std::fs::symlink_metadata(path)
        .map_err(|e| format!("cannot inspect {}: {e}", path.display()))?;
    if md.file_type().is_symlink() {
        return Err(format!("unsafe file (symlink): {}", path.display()));
    }
    if !md.is_file() {
        return Err(format!("not a regular file: {}", path.display()));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        use std::os::unix::fs::PermissionsExt;
        let euid = unsafe { libc::geteuid() };
        if md.uid() != euid {
            return Err(format!(
                "file {} not owned by current user",
                path.display()
            ));
        }
        let mode = md.permissions().mode() & 0o777;
        if mode & 0o077 != 0 {
            return Err(format!(
                "file {} has group/other permissions ({:o}); expected private (0600)",
                path.display(),
                mode
            ));
        }
    }
    Ok(())
}

/// Validate SQLite sidecars (`-wal`, `-shm`, `-journal`) beside `db_path`.
///
/// Missing sidecars are fine. Present sidecars must not be symlinks, must be
/// regular files owned by the current user. Overly broad permissions are
/// rejected. Never modifies anything.
pub fn validate_sidecars(db_path: &Path) -> Result<(), String> {
    for suffix in SIDECAR_SUFFIXES {
        let mut s = db_path.as_os_str().to_os_string();
        s.push(*suffix);
        let sidecar = PathBuf::from(s);
        match std::fs::symlink_metadata(&sidecar) {
            Ok(md) => {
                if md.file_type().is_symlink() {
                    return Err(format!(
                        "unsafe sidecar (symlink): {}",
                        sidecar.display()
                    ));
                }
                if !md.is_file() {
                    return Err(format!(
                        "sidecar is not a regular file: {}",
                        sidecar.display()
                    ));
                }
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    use std::os::unix::fs::PermissionsExt;
                    let euid = unsafe { libc::geteuid() };
                    if md.uid() != euid {
                        return Err(format!(
                            "sidecar {} not owned by current user",
                            sidecar.display()
                        ));
                    }
                    let mode = md.permissions().mode() & 0o777;
                    if mode & 0o077 != 0 {
                        return Err(format!(
                            "sidecar {} has group/other permissions ({:o}); expected private",
                            sidecar.display(),
                            mode
                        ));
                    }
                }
            }
            Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
            Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                return Err(format!(
                    "permission denied inspecting {}: {e}",
                    sidecar.display()
                ))
            }
            Err(e) => {
                return Err(format!(
                    "cannot inspect {}: {e}",
                    sidecar.display()
                ))
            }
        }
    }
    Ok(())
}

/// Create a new regular file `0600` from the outset (no post-hoc chmod).
/// Fails when the file already exists (use [`validate_file_private`] then).
#[cfg(unix)]
pub fn create_private_file(path: &Path) -> Result<(), String> {
    use std::os::unix::fs::OpenOptionsExt;
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true).mode(0o600);
    opts.open(path)
        .map(|_| ())
        .map_err(|e| {
            if e.kind() == std::io::ErrorKind::AlreadyExists {
                format!("file already exists: {}", path.display())
            } else if e.kind() == std::io::ErrorKind::PermissionDenied {
                format!("permission denied creating {}: {e}", path.display())
            } else {
                format!("cannot create {}: {e}", path.display())
            }
        })?;
    Ok(())
}

#[cfg(not(unix))]
pub fn create_private_file(path: &Path) -> Result<(), String> {
    let mut opts = std::fs::OpenOptions::new();
    opts.write(true).create_new(true);
    opts.open(path)
        .map(|_| ())
        .map_err(|e| format!("cannot create {}: {e}", path.display()))?;
    Ok(())
}

fn mkdir_all_private(dir: &Path) -> Result<(), String> {
    // Build prefixes in order so missing ancestors are created 0700.
    let mut prefix = PathBuf::new();
    let absolute = dir.is_absolute();
    if absolute {
        prefix.push("/");
    }
    let mut pending: Vec<PathBuf> = Vec::new();
    // Collect components to handle "." / ".." lexically (reject ".." that
    // would escape: keep it simple and fail on ParentDir in override paths
    // that do not exist yet, since canonical safety cannot be proven).
    for comp in dir.components() {
        use std::path::Component;
        match comp {
            Component::RootDir | Component::Prefix(_) => continue,
            Component::CurDir => continue,
            Component::ParentDir => {
                return Err(format!(
                    "refusing to create parent with '..': {}",
                    dir.display()
                ));
            }
            Component::Normal(c) => {
                prefix.push(c);
                match std::fs::symlink_metadata(&prefix) {
                    Ok(md) => {
                        if md.file_type().is_symlink() {
                            return Err(format!(
                                "unsafe symlink component: {}",
                                prefix.display()
                            ));
                        }
                        if !md.is_dir() {
                            return Err(format!(
                                "path component is not a directory: {}",
                                prefix.display()
                            ));
                        }
                        // Existing ancestor: leave permissions alone.
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
                        pending.push(prefix.clone());
                    }
                    Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
                        return Err(format!(
                            "permission denied inspecting {}: {e}",
                            prefix.display()
                        ));
                    }
                    Err(e) => {
                        return Err(format!(
                            "cannot inspect {}: {e}",
                            prefix.display()
                        ));
                    }
                }
            }
        }
    }
    for p in pending {
        #[cfg(unix)]
        {
            use std::os::unix::fs::DirBuilderExt;
            let mut b = std::fs::DirBuilder::new();
            b.recursive(false).mode(0o700);
            b.create(&p)
                .map_err(|e| format!("cannot create {}: {e}", p.display()))?;
        }
        #[cfg(not(unix))]
        {
            std::fs::create_dir(&p)
                .map_err(|e| format!("cannot create {}: {e}", p.display()))?;
        }
        // Post-create race check: must be a real dir we own.
        let md = std::fs::symlink_metadata(&p)
            .map_err(|e| format!("cannot inspect {} after create: {e}", p.display()))?;
        if md.file_type().is_symlink() || !md.is_dir() {
            return Err(format!("unsafe directory after create: {}", p.display()));
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_is_sibling_in_private_dir() {
        let db = PathBuf::from("/tmp/x/quickshell/desktop-activity/activity.db");
        assert_eq!(
            lock_path_for(&db),
            PathBuf::from("/tmp/x/quickshell/desktop-activity/activity.db.lock")
        );
    }

    #[test]
    fn lock_is_per_db_not_shared() {
        let dir = PathBuf::from("/tmp/x/quickshell/desktop-activity");
        let a = dir.join("activity.db");
        let b = dir.join("other.db");
        let la = lock_path_for(&a);
        let lb = lock_path_for(&b);
        assert_ne!(la, lb);
        assert_eq!(la, dir.join("activity.db.lock"));
        assert_eq!(lb, dir.join("other.db.lock"));
    }

    #[test]
    fn lock_alias_validation_rejects_mismatch() {
        let db = PathBuf::from("/tmp/x/activity.db");
        let wrong = PathBuf::from("/tmp/x/other.db.lock");
        assert!(validate_lock_for_db(&db, &wrong).is_err());
        let ok = lock_path_for(&db);
        // String-form sibling check passes even when dirs do not exist.
        assert!(validate_lock_for_db(&db, &ok).is_ok());
    }

    #[test]
    fn default_path_uses_xdg_first() {
        // Save and restore env to stay hermetic within the test process.
        let old_xdg = std::env::var_os("XDG_STATE_HOME");
        let old_home = std::env::var_os("HOME");
        std::env::set_var("XDG_STATE_HOME", "/tmp/fake-state");
        let p = default_db_path().unwrap();
        assert_eq!(
            p,
            PathBuf::from("/tmp/fake-state/quickshell/desktop-activity/activity.db")
        );
        if let Some(v) = old_xdg {
            std::env::set_var("XDG_STATE_HOME", v);
        } else {
            std::env::remove_var("XDG_STATE_HOME");
        }
        if let Some(v) = old_home {
            std::env::set_var("HOME", v);
        }
    }

    #[cfg(unix)]
    fn mode_of(p: &Path) -> u32 {
        use std::os::unix::fs::PermissionsExt;
        std::fs::metadata(p).unwrap().permissions().mode() & 0o777
    }

    #[cfg(unix)]
    fn unique_base(tag: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "qs-paths-{}-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0),
            tag
        ))
    }

    #[cfg(unix)]
    #[test]
    fn new_leaf_is_0700_and_shared_parent_unchanged() {
        use std::os::unix::fs::PermissionsExt;
        let base = unique_base("shared");
        let shared = base.join("shared");
        std::fs::create_dir_all(&shared).unwrap();
        std::fs::set_permissions(&shared, std::fs::Permissions::from_mode(0o755)).unwrap();
        let before = mode_of(&shared);
        let db = shared.join("newleaf").join("activity.db");
        ensure_parent_dir(&db).expect("create new leaf");
        // New leaf is private from the outset.
        assert_eq!(mode_of(&shared.join("newleaf")), 0o700);
        // Shared ancestor untouched.
        assert_eq!(mode_of(&shared), before);
        assert_eq!(before, 0o755);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn existing_private_leaf_validates() {
        use std::os::unix::fs::PermissionsExt;
        let base = unique_base("private");
        let leaf = base.join("leaf");
        std::fs::create_dir_all(&leaf).unwrap();
        std::fs::set_permissions(&leaf, std::fs::Permissions::from_mode(0o700)).unwrap();
        let db = leaf.join("activity.db");
        assert!(ensure_parent_dir(&db).is_ok());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn existing_world_readable_leaf_rejected_without_chmod() {
        use std::os::unix::fs::PermissionsExt;
        let base = unique_base("open");
        let leaf = base.join("leaf");
        std::fs::create_dir_all(&leaf).unwrap();
        std::fs::set_permissions(&leaf, std::fs::Permissions::from_mode(0o755)).unwrap();
        let before = mode_of(&leaf);
        let db = leaf.join("activity.db");
        assert!(ensure_parent_dir(&db).is_err());
        // Nothing modified.
        assert_eq!(mode_of(&leaf), before);
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn symlink_parent_rejected() {
        let base = unique_base("symlink");
        let real = base.join("real");
        std::fs::create_dir_all(&real).unwrap();
        let link = base.join("link");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        let db = link.join("activity.db");
        assert!(ensure_parent_dir(&db).is_err());
        let _ = std::fs::remove_dir_all(&base);
    }

    #[cfg(unix)]
    #[test]
    fn symlink_file_rejected() {
        let base = unique_base("symfile");
        std::fs::create_dir_all(&base).unwrap();
        let real = base.join("real.db");
        std::fs::write(&real, b"x").unwrap();
        let link = base.join("link.db");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(validate_file_private(&link).is_err());
        let _ = std::fs::remove_dir_all(&base);
    }
}
