//! CopySecureFast daemon — entry point.
//!
//! Inicializa logging, abre la DB SQLite, levanta el servidor JSON-RPC
//! sobre socket Unix, mantiene un worker loop que procesa jobs pending
//! y opcionalmente auto-arranca `csfd-tray` para tener un tray icon.
//!
//! Rutas por defecto (resolubles vía env vars o paths estándar):
//! - Socket: `XDG_RUNTIME_DIR/copysecurefast.sock` (fallback `/tmp/copysecurefast.sock`)
//! - DB:     `XDG_DATA_HOME/copysecurefast/queue.db` (fallback `~/.local/share/...`)
//! - Logs:   `XDG_DATA_HOME/copysecurefast/logs/csfd.log` (rotación diaria)

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use tracing::{error, info, warn};
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
    /// Iniciar también el tray icon (default: sí)
    #[arg(long, default_value_t = true)]
    with_tray: bool,

    /// No iniciar el tray icon
    #[arg(long, conflicts_with = "with_tray")]
    no_tray: bool,

    /// Path del socket (override env / default)
    #[arg(long)]
    socket: Option<String>,

    /// Path de la DB (override env / default)
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

/// Busca el binario csfd-tray. Orden:
/// 1) `$HOME/.local/bin/csfd-tray` (wrapper que el repo provee)
/// 2) `$PATH` (cualquier csfd-tray ejecutable)
/// 3) Junto al binario actual de csfd (instalación via cargo)
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
            format!("csfd-tray no encontrado (buscado: ~/.local/bin/csfd-tray, $PATH, junto al binario)"),
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

    // Worker loop: procesa jobs pending
    {
        let db_w = db.clone();
        let server_w = rpc.clone();
        tokio::spawn(async move {
            rpc::server::worker_loop(db_w, server_w).await;
        });
    }

    // Tray icon (default: sí). Si no se puede iniciar, log warning y
    // continúa (modo headless; --no-tray para silenciar el warning).
    if want_tray {
        match spawn_tray(&sock) {
            Ok(pid) => info!(pid, "csfd-tray spawned (system tray activo)"),
            Err(e) => warn!(
                "no se pudo iniciar csfd-tray: {} (continuando sin tray; usá --no-tray para silenciar)",
                e
            ),
        }
    }

    rpc.clone().run(sock).await
}
