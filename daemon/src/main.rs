//! CopySecureFast daemon — entry point.
//!
//! Inicializa logging, abre la DB SQLite, levanta el servidor JSON-RPC
//! sobre socket Unix y mantiene un worker loop que procesa jobs pending.
//!
//! Rutas por defecto (resolubles vía env vars o paths estándar):
//! - Socket: `XDG_RUNTIME_DIR/copysecurefast.sock` (fallback `/tmp/copysecurefast.sock`)
//! - DB:     `XDG_DATA_HOME/copysecurefast/queue.db` (fallback `~/.local/share/...`)
//! - Logs:   `XDG_DATA_HOME/copysecurefast/logs/csfd.log` (rotación diaria)

use std::path::PathBuf;
use std::sync::Arc;
use std::time::Duration;

use clap::Parser;
use tracing::{error, info, warn, Level};
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

    info!(
        version = env!("CARGO_PKG_VERSION"),
        socket = %sock,
        db = %db_p.display(),
        with_tray = args.with_tray && !args.no_tray,
        "csfd starting"
    );

    let db = Arc::new(QueueDb::new(&db_p)?);
    let rpc = Arc::new(RpcServer::new(db.clone()));

    // Worker loop
    {
        let db_w = db.clone();
        let server_w = rpc.clone();
        tokio::spawn(async move {
            rpc::server::worker_loop(db_w, server_w).await;
        });
    }

    // Tray icon (default: sí). Lanza el binario `csfd-tray` que es la
    // app GTK4 con el status icon. Si el binario no existe, log warning
    // y continúa (modo headless).
    let want_tray = args.with_tray && !args.no_tray;
    if want_tray {
        match spawn_tray(&sock) {
            Ok(pid) => info!(pid, "csfd-tray spawned"),
            Err(e) => warn!("no se pudo iniciar csfd-tray: {} (continuando sin tray)", e),
        }
    }

    rpc.clone().run(sock).await
}

fn spawn_tray(socket: &str) -> std::io::Result<u32> {
    use std::process::Command;
    // Buscar csfd-tray: 1) junto al binario actual, 2) en $PATH
    let exe = std::env::current_exe()?;
    let dir = exe.parent().unwrap_or(std::path::Path::new("."));
    let tray_path = dir.join("csfd-tray");
    let candidate = if tray_path.exists() {
        tray_path
    } else {
        PathBuf::from("csfd-tray")
    };

    Command::new(&candidate)
        .arg("--socket")
        .arg(socket)
        .env("CSF_SOCKET", socket)
        .stdin(std::process::Stdio::null())
        .stdout(std::process::Stdio::null())
        .stderr(std::process::Stdio::null())
        .spawn()
        .map(|c| c.id())
}
