//! Single-instance OS advisory lock, one per database file.
//!
//! The collector holds this lock for its whole lifetime. A second collector
//! for the same database exits with a clear error instead of interleaving
//! writes. Implemented with `flock(LOCK_EX|LOCK_NB)` on a per-database
//! sibling lock file (`<dbname>.lock`, see [`lock_path_for`]) inside the
//! private database directory.
//!
//! Security contract (local, reasonably scoped):
//! - Parent directories needed for the lock are created `0700` from the
//!   outset; existing parents are never `chmod`ed.
//! - The lock file itself is created `0600` from the outset. An existing
//!   lock file must be a regular file owned by the current user with private
//!   permissions and must not be a symlink; violations are errors.
//! - Symlinked parents/components are rejected to avoid alias escapes.

use crate::desktop_paths::{ensure_parent_for_file, validate_file_private};
use std::fs::{File, OpenOptions};
use std::os::unix::io::AsRawFd;
use std::path::{Path, PathBuf};

#[derive(Debug)]
pub enum LockError {
    Held(String),
    Io(String),
}

impl std::fmt::Display for LockError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LockError::Held(m) => write!(f, "already locked: {m}"),
            LockError::Io(m) => write!(f, "lock I/O error: {m}"),
        }
    }
}

impl std::error::Error for LockError {}

/// Held lock guard: dropping releases. Keep alive for the process lifetime.
pub struct DesktopLock {
    _file: File,
    path: PathBuf,
}

impl std::fmt::Debug for DesktopLock {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("DesktopLock")
            .field("path", &self.path)
            .finish()
    }
}

impl DesktopLock {
    pub fn path(&self) -> &Path {
        &self.path
    }
}

/// Acquire the lock, creating parent dirs and the lock file as needed.
/// Non-blocking: returns [`LockError::Held`] when another collector owns it.
///
/// Fails closed on unsafe parents/files (symlink, non-regular, foreign
/// owner, group/other permissions) without modifying them.
pub fn acquire_lock(lock_path: &Path) -> Result<DesktopLock, LockError> {
    if lock_path.as_os_str().is_empty() {
        return Err(LockError::Io("lock path is empty".to_string()));
    }
    if lock_path.file_name().is_none() {
        return Err(LockError::Io(format!(
            "lock path has no filename: {}",
            lock_path.display()
        )));
    }
    // Secure parent (create missing 0700, validate existing owner/dir, never
    // chmod). Keeps integration-tmpdir (0755, owned) working while refusing
    // foreign (/tmp owned by root) or symlinked parents.
    ensure_parent_for_file(lock_path)
        .map_err(|e| LockError::Io(format!("unsafe lock parent: {e}")))?;

    // Pre-check an existing lock file before opening (no chmod, no follow).
    match std::fs::symlink_metadata(lock_path) {
        Ok(md) => {
            if md.file_type().is_symlink() {
                return Err(LockError::Io(format!(
                    "unsafe lock file (symlink): {}",
                    lock_path.display()
                )));
            }
            if !md.is_file() {
                return Err(LockError::Io(format!(
                    "lock path is not a regular file: {}",
                    lock_path.display()
                )));
            }
            validate_file_private(lock_path)
                .map_err(|e| LockError::Io(e))?;
        }
        Err(e) if e.kind() == std::io::ErrorKind::NotFound => {
            // Will be created 0600 below.
        }
        Err(e) if e.kind() == std::io::ErrorKind::PermissionDenied => {
            return Err(LockError::Io(format!(
                "permission denied inspecting {}: {e}",
                lock_path.display()
            )));
        }
        Err(e) => {
            return Err(LockError::Io(format!(
                "cannot inspect {}: {e}",
                lock_path.display()
            )));
        }
    }

    // Open (creating) with 0600 from the outset.
    let mut opts = OpenOptions::new();
    opts.create(true).read(true).write(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        opts.mode(0o600);
    }
    let file = opts
        .open(lock_path)
        .map_err(|e| LockError::Io(format!("cannot open {}: {e}", lock_path.display())))?;

    // Post-open (fstat) race check: the fd must be a regular private file we
    // own. Covers symlink-swap between the pre-check and open.
    {
        let md = file
            .metadata()
            .map_err(|e| LockError::Io(format!("cannot stat {}: {e}", lock_path.display())))?;
        if !md.is_file() {
            return Err(LockError::Io(format!(
                "lock path is not a regular file: {}",
                lock_path.display()
            )));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            use std::os::unix::fs::PermissionsExt;
            let euid = unsafe { libc::geteuid() };
            if md.uid() != euid {
                return Err(LockError::Io(format!(
                    "lock file {} not owned by current user",
                    lock_path.display()
                )));
            }
            let mode = md.permissions().mode() & 0o777;
            if mode & 0o077 != 0 {
                return Err(LockError::Io(format!(
                    "lock file {} has group/other permissions ({:o}); expected private (0600)",
                    lock_path.display(),
                    mode
                )));
            }
        }
        // Re-confirm the path itself did not become a symlink.
        if let Ok(lmd) = std::fs::symlink_metadata(lock_path) {
            if lmd.file_type().is_symlink() {
                return Err(LockError::Io(format!(
                    "unsafe lock file (symlink): {}",
                    lock_path.display()
                )));
            }
        }
    }

    let rc = unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    if rc != 0 {
        let errno = unsafe { *libc::__errno_location() };
        if errno == libc::EWOULDBLOCK {
            return Err(LockError::Held(format!(
                "another qs-desktop-context collector owns {}",
                lock_path.display()
            )));
        }
        return Err(LockError::Io(format!(
            "flock {} failed: errno {errno}",
            lock_path.display()
        )));
    }
    Ok(DesktopLock {
        _file: file,
        path: lock_path.to_path_buf(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_dir(tag: &str) -> PathBuf {
        let p = std::env::temp_dir().join(format!(
            "qs-desktop-locktest-{}-{}-{tag}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .map(|d| d.as_nanos())
                .unwrap_or(0)
        ));
        std::fs::create_dir_all(&p).unwrap();
        // Private leaf owned by us so `ensure_parent_for_file` accepts it.
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(&p, std::fs::Permissions::from_mode(0o700));
        }
        p
    }

    #[test]
    fn second_acquire_is_held() {
        let dir = tmp_dir("held");
        let path = dir.join("test-held.db.lock");
        let _first = acquire_lock(&path).expect("first acquires");
        match acquire_lock(&path) {
            Err(LockError::Held(_)) => {}
            other => panic!("expected Held, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn lock_released_on_drop() {
        let dir = tmp_dir("drop");
        let path = dir.join("test-drop.db.lock");
        {
            let _guard = acquire_lock(&path).expect("acquire");
        }
        // After drop a new acquire succeeds.
        let _again = acquire_lock(&path).expect("re-acquire after drop");
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn lock_file_created_0600() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tmp_dir("mode");
        let path = dir.join("fresh.db.lock");
        let _guard = acquire_lock(&path).expect("acquire");
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600, "lock file must be 0600 from creation");
        drop(_guard);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn symlink_lock_rejected() {
        let dir = tmp_dir("symlink");
        let real = dir.join("real.db.lock");
        std::fs::write(&real, b"x").unwrap();
        // Make the real file private so only the symlink itself is at fault.
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&real, std::fs::Permissions::from_mode(0o600));
        let link = dir.join("link.db.lock");
        std::os::unix::fs::symlink(&real, &link).unwrap();
        match acquire_lock(&link) {
            Err(LockError::Io(_)) => {}
            other => panic!("expected Io for symlink, got {other:?}"),
        }
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[cfg(unix)]
    #[test]
    fn world_readable_lock_rejected_unchanged() {
        use std::os::unix::fs::PermissionsExt;
        let dir = tmp_dir("open");
        let path = dir.join("open.db.lock");
        std::fs::write(&path, b"x").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        match acquire_lock(&path) {
            Err(LockError::Io(_)) => {}
            other => panic!("expected Io for 0644 lock, got {other:?}"),
        }
        // Nothing chmodded.
        let mode = std::fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o644);
        let _ = std::fs::remove_dir_all(&dir);
    }
}
