//! CopySecureFast daemon - entry point.
//!
//! Initializes logging, opens the SQLite database, starts the JSON-RPC
//! server over a Unix socket, runs a worker loop that processes pending
//! jobs, and optionally auto-spawns `csfd-tray` for a system tray icon.
//!
//! Default paths (overridable via env vars or CLI flags):
//! - Socket: `XDG_RUNTIME_DIR/copysecurefast.sock` (fallback `/tmp/copysecurefast.sock`)
//! - DB:     `XDG_DATA_HOME/copysecurefast/queue.db` (fallback `~/.local/share/...`)
//! - Logs:   `XDG_DATA_HOME/copysecurefast/logs/csfd.log` (daily rotation)

use std::path::PathBuf;
use std::sync::Arc;

use clap::Parser;
use tracing::{info, warn};
use tracing_appender::rolling::Rotation;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

mod types;
mod queue;
mod ops;
mod rpc;

use queue::QueueDb;
use rpc::server::RpcServer;

const DEFAULT_SOCKET_NAME: &str = "copysecurefast.sock";
const DEFAULT_DIR_NAME: &str = "copysecurefast";
const DEFAULT_DB_FILE: &str = "queue.db";
const DEFAULT_LOG_FILE: &str = "csfd.log";

#[derive(Parser, Debug)]
#[command(name = "csfd", version, about = "CopySecureFast daemon")]
struct Args {
    /// Spawn the tray icon as well (default: yes)
    #[arg(long, default_value_t = true)]
    with_tray: bool,

    /// Do not spawn the tray icon
    #[arg(long, conflicts_with = "with_tray")]
    no_tray: bool,

    /// Socket path (overrides env / default)
    #[arg(long)]
    socket: Option<String>,

    /// Database path (overrides env / default)
    #[arg(long)]
    db: Option<String>,
}

fn setup_logging() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let log_dir = data_dir().join("logs");
    std::fs::create_dir_all(&log_dir)?;

    let file_appender = tracing_appender::rolling::RollingFileAppender::new(
        Rotation::DAILY,
        &log_dir,
        DEFAULT_LOG_FILE,
    );

    tracing_subscriber::registry()
        .with(
            EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| EnvFilter::new("info,csf_client=warn")),
        )
        .with(fmt::layer().with_writer(file_appender).with_ansi(false))
        .with(fmt::layer().with_writer(std::io::stdout))
        .try_init()?;

    Ok(())
}

fn data_dir() -> PathBuf {
    if let Ok(p) = std::env::var("CSF_DATA_DIR") {
        return PathBuf::from(p);
    }
    dirs::data_local_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join(DEFAULT_DIR_NAME)
}

fn runtime_dir() -> PathBuf {
    if let Ok(p) = std::env::var("CSF_RUNTIME_DIR") {
        return PathBuf::from(p);
    }
    dirs::runtime_dir().unwrap_or_else(|| PathBuf::from("/tmp"))
}

fn db_path() -> PathBuf {
    data_dir().join(DEFAULT_DB_FILE)
}

fn socket_path() -> PathBuf {
    runtime_dir().join(DEFAULT_SOCKET_NAME)
}

/// Locates the `csfd-tray` binary. Search order:
/// 1) `$HOME/.local/bin/csfd-tray` (wrapper shipped by this repo)
/// 2) `$PATH` (any `csfd-tray` executable)
/// 3) Next to the current csfd binary (cargo install location)
fn find_tray_binary() -> PathBuf {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Some(home) = dirs::home_dir() {
        candidates.push(home.join(".local/bin/csfd-tray"));
    }
    candidates.push(PathBuf::from("csfd-tray"));
    if let Ok(exe) = std::env::current_exe() {
        if let Some(dir) = exe.parent() {
            candidates.push(dir.join("csfd-tray"));
        }
    }
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .unwrap_or_else(|| PathBuf::from("csfd-tray"))
}

fn spawn_tray(socket: &str) -> std::io::Result<u32> {
    use std::process::{Command, Stdio};
    let tray_bin = find_tray_binary();
    if !tray_bin.is_file() {
        return Err(std::io::Error::new(
            std::io::ErrorKind::NotFound,
            format!(
                "csfd-tray not found (looked in: ~/.local/bin/csfd-tray, $PATH, next to csfd)"
            ),
        ));
    }
    Command::new(&tray_bin)
        .arg("--socket")
        .arg(socket)
        .env("CSF_SOCKET", socket)
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .map(|c| c.id())
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let args = Args::parse();
    setup_logging()?;

    let sock = args
        .socket
        .clone()
        .unwrap_or_else(|| socket_path().to_string_lossy().to_string());
    let db_p = match args.db.clone() {
        Some(p) => PathBuf::from(p),
        None => db_path(),
    };

    let want_tray = args.with_tray && !args.no_tray;
    info!(
        version = env!("CARGO_PKG_VERSION"),
        socket = %sock,
        db = %db_p.display(),
        with_tray = want_tray,
        "csfd starting"
    );

    let db = Arc::new(QueueDb::new(&db_p)?);
    let rpc = Arc::new(RpcServer::new(db.clone()));

    // Worker loop: processes pending jobs.
    {
        let db_w = db.clone();
        let server_w = rpc.clone();
        tokio::spawn(async move {
            rpc::server::worker_loop(db_w, server_w).await;
        });
    }

    // Tray icon (default: yes). If it cannot be started, log a warning
    // and continue in headless mode (use --no-tray to silence the warning).
    if want_tray {
        match spawn_tray(&sock) {
            Ok(pid) => info!(pid, "csfd-tray spawned (system tray active)"),
            Err(e) => warn!(
                "could not start csfd-tray: {} (continuing without tray; use --no-tray to silence this warning)",
                e
            ),
        }
    }

    rpc.clone().run(sock).await
}
