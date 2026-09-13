use std::path::PathBuf;

#[derive(Debug, Clone)]
pub struct Config {
    pub root: PathBuf,
    pub mode: String,
    pub project: Option<String>,
    pub session: Option<String>,
    pub pending_name: Option<String>,
    pub new_session: bool,
}

pub fn parse_args(argv: &[String]) -> Result<Config, String> {
    let mut root: Option<String> = None;
    let mut mode: Option<String> = None;
    let mut project: Option<String> = None;
    let mut session: Option<String> = None;
    let mut pending_name: Option<String> = None;
    let mut new_session = false;
    let mut i = 1;
    while i < argv.len() {
        match argv[i].as_str() {
            "--root" => {
                i += 1;
                if i >= argv.len() {
                    return Err("--root requires a value".to_string());
                }
                root = Some(argv[i].clone());
            }
            "--mode" => {
                i += 1;
                if i >= argv.len() {
                    return Err("--mode requires a value".to_string());
                }
                mode = Some(argv[i].clone());
            }
            "--project" => {
                i += 1;
                if i >= argv.len() {
                    return Err("--project requires a value".to_string());
                }
                project = Some(argv[i].clone());
            }
            "--session" => {
                i += 1;
                if i >= argv.len() {
                    return Err("--session requires a value".to_string());
                }
                session = Some(argv[i].clone());
            }
            "--pending-name" => {
                i += 1;
                if i >= argv.len() {
                    return Err("--pending-name requires a value".to_string());
                }
                pending_name = Some(argv[i].clone());
            }
            "--new-session" => {
                new_session = true;
            }
            "--help" | "-h" => {
                return Err(usage());
            }
            other => {
                return Err(format!("unknown argument: {other}\n{0}", usage()));
            }
        }
        i += 1;
    }
    let root = root.ok_or_else(|| format!("missing --root\n{}", usage()))?;
    let mode = mode.ok_or_else(|| format!("missing --mode\n{}", usage()))?;
    if mode != "project" && mode != "journal" && mode != "palette" {
        return Err(format!(
            "--mode must be project|journal|palette\n{}",
            usage()
        ));
    }
    if mode == "project" && project.as_deref().unwrap_or("").is_empty() {
        return Err(format!(
            "--project is required in project mode\n{}",
            usage()
        ));
    }
    if mode != "project" && project.is_some() {
        return Err(format!(
            "--project is only permitted in project mode\n{}",
            usage()
        ));
    }
    Ok(Config {
        root: PathBuf::from(root),
        mode,
        project,
        session,
        pending_name,
        new_session,
    })
}

fn usage() -> String {
    "usage: qs-agent-orchestrator --root PATH --mode project|journal|palette [--project PAGE] [--session FILE] [--pending-name NAME] [--new-session]".to_string()
}

#[cfg(test)]
mod tests {
    use super::*;

    fn argv(parts: &[&str]) -> Vec<String> {
        parts.iter().map(|s| s.to_string()).collect()
    }

    #[test]
    fn palette_mode_accepts_session_identity_without_project() {
        let cfg = parse_args(&argv(&[
            "qs-agent-orchestrator",
            "--root",
            "/tmp",
            "--mode",
            "palette",
            "--session",
            "/tmp/s.jsonl",
            "--pending-name",
            "draft",
        ]))
        .expect("palette parses");
        assert_eq!(cfg.mode, "palette");
        assert_eq!(cfg.session.as_deref(), Some("/tmp/s.jsonl"));
        assert_eq!(cfg.pending_name.as_deref(), Some("draft"));
        assert!(cfg.project.is_none());
    }

    #[test]
    fn palette_rejects_project_flag() {
        let err = parse_args(&argv(&[
            "qs-agent-orchestrator",
            "--root",
            "/tmp",
            "--mode",
            "palette",
            "--project",
            "pages/P.md",
        ]))
        .expect_err("palette + --project must fail");
        assert!(err.contains("--project"), "unexpected: {err}");
    }

    #[test]
    fn journal_rejects_project_flag() {
        let err = parse_args(&argv(&[
            "qs-agent-orchestrator",
            "--root",
            "/tmp",
            "--mode",
            "journal",
            "--project",
            "pages/P.md",
        ]))
        .expect_err("journal + --project must fail");
        assert!(err.contains("--project"), "unexpected: {err}");
    }

    #[test]
    fn project_requires_project_flag() {
        let err = parse_args(&argv(&[
            "qs-agent-orchestrator",
            "--root",
            "/tmp",
            "--mode",
            "project",
        ]))
        .expect_err("project without --project must fail");
        assert!(err.contains("--project"), "unexpected: {err}");
    }

    #[test]
    fn bad_mode_still_rejected() {
        let err = parse_args(&argv(&[
            "qs-agent-orchestrator",
            "--root",
            "/tmp",
            "--mode",
            "bogus",
        ]))
        .expect_err("bogus mode must fail");
        assert!(err.contains("project|journal|palette"), "unexpected: {err}");
    }
}
