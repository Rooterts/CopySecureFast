//! Servidor JSON-RPC sobre socket Unix.
//!
//! Protocolo: una línea = un JSON. Cliente envía Request, servidor
//! responde Response. La conexión se mantiene abierta (long-lived).
//!
//! Métodos implementados en este spike:
//! - `ping`            → pong
//! - `get_queue`       → queue_snapshot
//! - `set_throttle`    → queue_snapshot
//! - `enqueue`         → enqueued
//! - `pause|resume|cancel` → error (pendiente fase 6)

use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::queue::QueueDb;
use crate::types::{EnqueueItem, JobItem, Operation, Request, Response};

pub struct RpcServer {
    db: Arc<QueueDb>,
    throttle_bps: AtomicU64,
}

impl RpcServer {
    pub fn new(db: Arc<QueueDb>) -> Self {
        Self {
            db,
            throttle_bps: AtomicU64::new(0),
        }
    }

    pub fn throttle_bps(&self) -> u64 {
        self.throttle_bps.load(Ordering::Relaxed)
    }

    pub fn set_throttle(&self, bps: u64) {
        self.throttle_bps.store(bps, Ordering::Relaxed);
    }

    /// Bucle principal: acepta conexiones y lanza una task por cliente.
    pub async fn run(
        self: Arc<Self>,
        socket_path: String,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        // Limpiar socket anterior si quedó colgado.
        let _ = std::fs::remove_file(&socket_path);

        let listener = UnixListener::bind(&socket_path)?;
        info!(path = %socket_path, "RPC server listening");

        loop {
            match listener.accept().await {
                Ok((stream, _addr)) => {
                    let db = self.db.clone();
                    let server = self.clone();
                    tokio::spawn(async move {
                        if let Err(e) = handle_connection(stream, db, server).await {
                            error!("connection error: {}", e);
                        }
                    });
                }
                Err(e) => {
                    error!("accept error: {}", e);
                }
            }
        }
    }
}

async fn handle_connection(
    stream: UnixStream,
    db: Arc<QueueDb>,
    server: Arc<RpcServer>,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let (rd, mut wr) = tokio::io::split(stream);
    let mut lines = BufReader::new(rd).lines();

    while let Ok(Some(line)) = lines.next_line().await {
        let line = line.trim();
        if line.is_empty() {
            continue;
        }

        let response = match serde_json::from_str::<Request>(line) {
            Ok(req) => dispatch(&req, &db, &server).await,
            Err(e) => {
                warn!(reason = %e, raw = %line, "invalid request");
                Response::Error {
                    message: format!("parse error: {}", e),
                }
            }
        };

        let serialized = match serde_json::to_string(&response) {
            Ok(s) => s,
            Err(e) => format!(
                r#"{{"event":"error","data":{{"message":"serialization failed: {}"}}}}"#,
                e
            ),
        };

        wr.write_all(serialized.as_bytes()).await?;
        wr.write_all(b"\n").await?;
        wr.flush().await?;
    }
    Ok(())
}

async fn dispatch(req: &Request, db: &Arc<QueueDb>, server: &Arc<RpcServer>) -> Response {
    match req {
        Request::Ping => Response::Pong,

        Request::GetQueue => match db.get_all_jobs() {
            Ok(jobs) => Response::QueueSnapshot {
                jobs,
                global_speed_bps: server.throttle_bps(),
            },
            Err(e) => Response::Error {
                message: format!("db error: {}", e),
            },
        },

        Request::SetThrottle { bytes_per_second } => {
            server.set_throttle(*bytes_per_second);
            let jobs = db.get_all_jobs().unwrap_or_default();
            Response::QueueSnapshot {
                jobs,
                global_speed_bps: *bytes_per_second,
            }
        }

        Request::Enqueue { items } => match enqueue_items(db, items) {
            Ok(count) => Response::Enqueued { count },
            Err(e) => Response::Error {
                message: format!("enqueue failed: {}", e),
            },
        },

        Request::Pause { .. } | Request::Resume { .. } | Request::Cancel { .. } => {
            Response::Error {
                message: format!(
                    "{:?} no implementado todavía (spike fase 2 — implementar en fase 6)",
                    req
                ),
            }
        }
    }
}

fn enqueue_items(db: &QueueDb, items: &[EnqueueItem]) -> Result<usize, String> {
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);
    let mut count = 0;
    for it in items {
        let job = JobItem {
            id: Uuid::new_v4().to_string(),
            source: it.source.clone(),
            dest: it.dest.clone(),
            op: it.op,
            state: Default::default(),
            total_bytes: 0,
            copied_bytes: 0,
            hash: None,
            enqueued_at: now,
            finished_at: 0,
            error: None,
            verify_hash: it.verify_hash,
        };
        db.insert_job(&job).map_err(|e| e.to_string())?;
        count += 1;
    }
    Ok(count)
}

// Silenciar warning de "Operation unused" en este spike: lo usamos en
// enqueue_items al construir el JobItem.
#[allow(dead_code)]
fn _force_op_use(o: Operation) -> &'static str {
    match o {
        Operation::Copy => "copy",
        Operation::Move => "move",
    }
}
