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

use tracing::{error, info, warn, Level};
use tracing_appender::rolling::Rotation;
use tracing_subscriber::{fmt, prelude::*, EnvFilter};

mod types;
mod queue;
mod ops;
mod rpc;

use queue::QueueDb;
use rpc::RpcServer;

const DEFAULT_SOCKET_NAME: &str = "copysecurefast.sock";
const DEFAULT_DIR_NAME: &str = "copysecurefast";
const DEFAULT_DB_FILE: &str = "queue.db";
const DEFAULT_LOG_FILE: &str = "csfd.log";

fn setup_logging() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let log_dir = data_dir().join("logs");
    std::fs::create_dir_all(&log_dir)?;

    // RollingFileAppender sin guard non-blocking: el writer va directo
    // al archivo (más simple, sin overhead de channel).
    let file_appender =
        tracing_appender::rolling::RollingFileAppender::new(Rotation::DAILY, &log_dir, DEFAULT_LOG_FILE);

    tracing_subscriber::registry()
        .with(EnvFilter::try_from_default_env().unwrap_or_else(|_| EnvFilter::new("info")))
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
    setup_logging()?;

    let sock = socket_path();
    let db_p = db_path();
    info!(
        version = env!("CARGO_PKG_VERSION"),
        socket = %sock.display(),
        db = %db_p.display(),
        "csfd starting"
    );

    let db = Arc::new(QueueDb::new(&db_p)?);
    let rpc = Arc::new(RpcServer::new(db.clone()));

    // Background: worker loop que toma jobs pending y los procesa.
    // Por simplicidad del spike, es un único worker. En fase 6 se
    // convierte en un pool con N workers y un canal interno.
    let db_w = db.clone();
    tokio::spawn(async move {
        loop {
            if let Ok(jobs) = db_w.get_all_jobs() {
                for job in jobs {
                    if job.state == types::JobState::Pending {
                        let mut job = job;
                        ops::CopyOp::run(&mut job);
                        if let Err(e) = db_w.update_job(&job) {
                            error!(job_id = %job.id, "db update failed: {}", e);
                        } else {
                            info!(
                                job_id = %job.id,
                                state = ?job.state,
                                "job finalized"
                            );
                        }
                    }
                }
            } else {
                warn!("worker: failed to load jobs from db");
            }
            tokio::time::sleep(Duration::from_millis(500)).await;
        }
    });

    rpc.clone().run(sock.to_string_lossy().to_string()).await
}
