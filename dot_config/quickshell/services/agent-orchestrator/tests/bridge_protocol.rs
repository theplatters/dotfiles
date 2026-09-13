use std::io::{BufRead, BufReader, Write};
use std::path::PathBuf;
use std::process::{Command, Stdio};
use std::time::Duration;

fn binary() -> PathBuf {
    let mut p = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    p.push("target");
    p.push("debug");
    p.push("qs-agent-orchestrator");
    p
}

fn ensure_built() {
    let status = Command::new("cargo")
        .args(["build"])
        .current_dir(env!("CARGO_MANIFEST_DIR"))
        .status()
        .expect("cargo build");
    assert!(status.success());
}

fn spawn_bridge(extra: &[&str]) -> std::process::Child {
    ensure_built();
    let mut args = vec![
        "--root".to_string(),
        env!("CARGO_MANIFEST_DIR").to_string(),
        "--mode".to_string(),
        "journal".to_string(),
    ];
    for e in extra {
        args.push(e.to_string());
    }
    Command::new(binary())
        .args(args)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("spawn bridge")
}

fn send(
    child: &mut std::process::Child,
    id: &str,
    op: &str,
    args: serde_json::Value,
) -> serde_json::Value {
    let stdin = child.stdin.as_mut().expect("stdin");
    let line =
        serde_json::json!({"version": 1, "id": id, "op": op, "args": args}).to_string() + "\n";
    stdin.write_all(line.as_bytes()).expect("write");
    stdin.flush().expect("flush");
    let stdout = child.stdout.as_mut().expect("stdout");
    let mut reader = BufReader::new(stdout);
    let mut out = String::new();
    // Read until the matching ack arrives.
    loop {
        out.clear();
        reader.read_line(&mut out).expect("read update");
        assert!(!out.is_empty(), "bridge closed stdout");
        let v: serde_json::Value = serde_json::from_str(out.trim()).expect("valid update json");
        assert_eq!(v["version"], 1);
        assert_eq!(v["type"], "update");
        assert!(v["state"].is_object(), "state object present");
        if v.get("ack").and_then(|a| a.as_str()) == Some(id) {
            return v;
        }
        // Asynchronous update without ack: keep waiting (bounded).
    }
}

#[test]
fn startup_does_not_autostart_and_reports_ready_fields() {
    let mut child = spawn_bridge(&[]);
    // No Pi child yet: processStarted false, ready false.
    let ack = send(&mut child, "ui-1", "requestMessages", serde_json::json!({}));
    assert_eq!(ack["ack"], "ui-1");
    // requestMessages without a live Pi child is rejected, never launches one.
    assert_eq!(ack["accepted"], false);
    assert_eq!(ack["state"]["processStarted"], false);
    assert_eq!(ack["state"]["ready"], false);
    assert!(ack["state"]["retryable"].is_boolean());
    // Unknown op is rejected without side effects.
    let ack2 = send(&mut child, "ui-2", "bogusOp", serde_json::json!({}));
    assert_eq!(ack2["accepted"], false);
    // shutdown cleans up.
    let _ = send(&mut child, "ui-3", "shutdown", serde_json::json!({}));
    let _ = child.wait_timeout(Duration::from_secs(5));
    let _ = child.kill();
}

#[test]
fn cli_rejects_bad_mode() {
    ensure_built();
    let out = Command::new(binary())
        .args(["--root", "/tmp", "--mode", "bogus"])
        .output()
        .expect("run");
    assert!(!out.status.success());
}

// wait_timeout is unstable; implement small helper via try_wait loop.
trait WaitTimeout {
    fn wait_timeout(&mut self, dur: Duration) -> std::io::Result<Option<std::process::ExitStatus>>;
}
impl WaitTimeout for std::process::Child {
    fn wait_timeout(&mut self, dur: Duration) -> std::io::Result<Option<std::process::ExitStatus>> {
        let start = std::time::Instant::now();
        loop {
            match self.try_wait()? {
                Some(s) => return Ok(Some(s)),
                None => {
                    if start.elapsed() >= dur {
                        return Ok(None);
                    }
                    std::thread::sleep(Duration::from_millis(50));
                }
            }
        }
    }
}
