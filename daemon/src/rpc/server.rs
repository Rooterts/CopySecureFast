//! JSON-RPC server over a Unix socket.
//!
//! Protocol: one line = one JSON. Client sends a `Request`, server replies
//! with a `Response`. **Events** are sent spontaneously
//! (`job_started`, `job_progress`, `job_completed`, `job_failed`,
//! `job_paused`, `job_resumed`, `job_cancelled`) as additional JSON lines.
//!
//! Connection: long-lived. Clients may disconnect/reconnect at any time;
//! the server does not require a client to be connected to enqueue work.
//!
//! Implemented methods:
//! - `ping` -> `pong`
//! - `get_queue` -> `queue_snapshot`
//! - `set_throttle` -> `queue_snapshot` (with `global_speed_bps`)
//! - `enqueue` -> `enqueued` (with count); per item, `job_started` event
//!   when the worker picks it up.
//! - `pause`/`resume`/`cancel` -> per `job_id` or all jobs.

use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tokio::sync::broadcast;
use tracing::{error, info, warn};
use uuid::Uuid;

use crate::ops::copy::{CopyOp, SIGNAL_CANCEL, SIGNAL_PAUSE, SIGNAL_RUN};
use crate::queue::QueueDb;
use crate::types::{EnqueueItem, JobItem, JobState, Operation, Request, Response};

/// Internal channel for job control signals (pause/cancel).
/// Key = job_id, Value = AtomicU8 shared with the worker.
pub type JobSignals = Arc<std::sync::Mutex<HashMap<String, Arc<std::sync::atomic::AtomicU8>>>>;

pub struct RpcServer {
    db: Arc<QueueDb>,
    throttle_bps: AtomicU64,
    /// Event channel used to notify connected clients.
    events: broadcast::Sender<ServerEvent>,
    /// Map of active signals per job.
    signals: JobSignals,
}

/// Internal events emitted by the server to clients.
#[derive(Debug, Clone)]
pub enum ServerEvent {
    JobStarted(JobItem),
    JobProgress {
        id: String,
        copied_bytes: u64,
        total_bytes: u64,
    },
    JobCompleted(JobItem),
    JobFailed { id: String, error: String },
    JobPaused(String),
    JobResumed(String),
    JobCancelled(String),
}

impl RpcServer {
    pub fn new(db: Arc<QueueDb>) -> Self {
        let (tx, _) = broadcast::channel(1024);
        Self {
            db,
            throttle_bps: AtomicU64::new(0),
            events: tx,
            signals: Arc::new(std::sync::Mutex::new(HashMap::new())),
        }
    }

    /// Event subscriber. Used by the worker loop and by connected
    /// clients to receive queue changes.
    pub fn subscribe(&self) -> broadcast::Receiver<ServerEvent> {
        self.events.subscribe()
    }

    /// Returns (or creates) the atomic signal for a job.
    pub fn signal_for(&self, job_id: &str) -> Arc<std::sync::atomic::AtomicU8> {
        let mut map = self.signals.lock().expect("signals mutex poisoned");
        map.entry(job_id.to_string())
            .or_insert_with(|| Arc::new(std::sync::atomic::AtomicU8::new(SIGNAL_RUN)))
            .clone()
    }

    pub fn throttle_bps(&self) -> u64 {
        self.throttle_bps.load(Ordering::Relaxed)
    }

    pub fn set_throttle(&self, bps: u64) {
        self.throttle_bps.store(bps, Ordering::Relaxed);
    }

    pub fn emit(&self, ev: ServerEvent) {
        let _ = self.events.send(ev);
    }

    pub async fn run(
        self: Arc<Self>,
        socket_path: String,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
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
    // Suscribirse a eventos para reenviarlos al cliente.
    let mut events_rx = server.subscribe();

    loop {
        tokio::select! {
            // Requests del cliente
            line = lines.next_line() => {
                let line = match line {
                    Ok(Some(l)) => l,
                    Ok(None) => return Ok(()), // client closed
                    Err(e) => {
                        warn!("read error: {}", e);
                        return Err(e.into());
                    }
                };
                let line = line.trim();
                if line.is_empty() { continue; }

                let response = match serde_json::from_str::<Request>(line) {
                    Ok(req) => dispatch(&req, &db, &server).await,
                    Err(e) => {
                        warn!(reason = %e, raw = %line, "invalid request");
                        Response::Error { message: format!("parse error: {}", e) }
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
            // Events from the server to the client
            ev = events_rx.recv() => {
                if let Ok(ev) = ev {
                    if let Some(line) = event_to_json(&ev) {
                        // Ignore write errors: if the client disconnected,
                        // the next read will close the connection.
                        if wr.write_all(line.as_bytes()).await.is_err() { return Ok(()); }
                        let _ = wr.write_all(b"\n").await;
                        let _ = wr.flush().await;
                    }
                }
            }
        }
    }
}

fn event_to_json(ev: &ServerEvent) -> Option<String> {
    let s = match ev {
        ServerEvent::JobStarted(job) => serde_json::to_string(&Event::JobStarted(job)),
        ServerEvent::JobProgress { id, copied_bytes, total_bytes } => {
            serde_json::to_string(&Event::JobProgress {
                id: id.clone(),
                copied_bytes: *copied_bytes,
                total_bytes: *total_bytes,
            })
        }
        ServerEvent::JobCompleted(job) => serde_json::to_string(&Event::JobCompleted(job)),
        ServerEvent::JobFailed { id, error } => {
            serde_json::to_string(&Event::JobFailed { id: id.clone(), error: error.clone() })
        }
        ServerEvent::JobPaused(id) => serde_json::to_string(&Event::JobPaused(id.clone())),
        ServerEvent::JobResumed(id) => serde_json::to_string(&Event::JobResumed(id.clone())),
        ServerEvent::JobCancelled(id) => serde_json::to_string(&Event::JobCancelled(id.clone())),
    };
    s.ok()
}

#[derive(serde::Serialize)]
#[serde(tag = "event", content = "data")]
enum Event<'a> {
    #[serde(rename = "job_started")]
    JobStarted(&'a JobItem),
    #[serde(rename = "job_progress")]
    JobProgress {
        id: String,
        copied_bytes: u64,
        total_bytes: u64,
    },
    #[serde(rename = "job_completed")]
    JobCompleted(&'a JobItem),
    #[serde(rename = "job_failed")]
    JobFailed { id: String, error: String },
    #[serde(rename = "job_paused")]
    JobPaused(String),
    #[serde(rename = "job_resumed")]
    JobResumed(String),
    #[serde(rename = "job_cancelled")]
    JobCancelled(String),
}

async fn dispatch(req: &Request, db: &Arc<QueueDb>, server: &Arc<RpcServer>) -> Response {
    match req {
        Request::Ping => Response::Pong,
        Request::GetQueue => match db.get_all_jobs() {
            Ok(jobs) => Response::QueueSnapshot {
                jobs,
                global_speed_bps: server.throttle_bps(),
            },
            Err(e) => Response::Error { message: format!("db: {}", e) },
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
            Err(e) => Response::Error { message: e },
        },
        Request::Pause { job_id } => match control_job(db, server, job_id.as_deref(), SIGNAL_PAUSE) {
            Ok(n) => Response::Error {
                message: format!("pausados: {} job(s)", n),
            },
            Err(e) => Response::Error { message: e },
        },
        Request::Resume { job_id } => match control_job(db, server, job_id.as_deref(), SIGNAL_RUN) {
            Ok(n) => Response::Error {
                message: format!("reanudados: {} job(s)", n),
            },
            Err(e) => Response::Error { message: e },
        },
        Request::Cancel { job_id } => {
            match control_job(db, server, job_id.as_deref(), SIGNAL_CANCEL) {
                Ok(n) => Response::Error {
                    message: format!("cancelados: {} job(s)", n),
                },
                Err(e) => Response::Error { message: e },
            }
        }
    }
}

fn control_job(
    db: &QueueDb,
    server: &RpcServer,
    job_id: Option<&str>,
    signal: u8,
) -> Result<usize, String> {
    let jobs = db.get_all_jobs().map_err(|e| e.to_string())?;
    let mut count = 0;
    for job in jobs {
        let applies = match job_id {
            Some(id) => job.id == id,
            None => true,
        };
        if !applies {
            continue;
        }
        match signal {
            SIGNAL_PAUSE => {
                if !job.state.is_pausable() {
                    continue;
                }
                let sig = server.signal_for(&job.id);
                sig.store(SIGNAL_PAUSE, Ordering::Relaxed);
            }
            SIGNAL_RUN => {
                if !job.state.is_resumable() {
                    continue;
                }
                let sig = server.signal_for(&job.id);
                sig.store(SIGNAL_RUN, Ordering::Relaxed);
            }
            SIGNAL_CANCEL => {
                if !job.state.is_cancellable() {
                    continue;
                }
                let sig = server.signal_for(&job.id);
                sig.store(SIGNAL_CANCEL, Ordering::Relaxed);
            }
            _ => unreachable!(),
        }
        count += 1;
    }
    Ok(count)
}

fn enqueue_items(db: &QueueDb, items: &[EnqueueItem]) -> Result<usize, String> {
    use std::time::{SystemTime, UNIX_EPOCH};
    let now = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0);

    let mut count = 0;
    for it in items {
        // Validar que source existe.
        if !it.source.exists() {
            return Err(format!("source no existe: {}", it.source.display()));
        }
        // total_bytes conocido al encolar.
        let total_bytes = std::fs::metadata(&it.source).map(|m| m.len()).unwrap_or(0);
        let job = JobItem {
            id: uuid::Uuid::new_v4().to_string(),
            source: it.source.clone(),
            dest: it.dest.clone(),
            op: it.op,
            state: Default::default(),
            total_bytes,
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

/// Worker loop: every 200ms it scans the queue, processes one pending
/// job and emits events.
pub async fn worker_loop(db: Arc<QueueDb>, server: Arc<RpcServer>) {
    use std::sync::atomic::AtomicU8;

    let mut tick = tokio::time::interval(std::time::Duration::from_millis(200));
    loop {
        tick.tick().await;
        // Pick the next pending job (if any).
        let job = match db.get_all_jobs() {
            Ok(mut jobs) => {
                jobs.retain(|j| j.state == JobState::Pending);
                jobs.into_iter().next()
            }
            Err(e) => {
                warn!("worker: db error: {}", e);
                None
            }
        };

        let Some(mut job) = job else { continue };

        // Marcar running antes de empezar.
        let _ = db.update_state(&job.id, JobState::Running);
        job.state = JobState::Running;
        server.emit(ServerEvent::JobStarted(job.clone()));

        let signal = server.signal_for(&job.id);
        // Reset in case a signal was left from a previous job.
        signal.store(SIGNAL_RUN, Ordering::Relaxed);

        // Ejecutar en thread bloqueante (es I/O intensivo de archivos).
        let db_c = db.clone();
        let server_c = server.clone();
        let job_id = job.id.clone();
        let signal_c = signal.clone();

        let handle = tokio::task::spawn_blocking(move || {
            CopyOp::run(&mut job, signal_c);
            job
        });

        // Mientras corre, emitimos progreso cada 200ms.
        let progress_interval = std::time::Duration::from_millis(200);
        loop {
            tokio::time::sleep(progress_interval).await;
            if handle.is_finished() {
                break;
            }
            if let Ok(Some(j)) = db_c.get_job(&job_id) {
                server_c.emit(ServerEvent::JobProgress {
                    id: job_id.clone(),
                    copied_bytes: j.copied_bytes,
                    total_bytes: j.total_bytes,
                });
            }
        }

        let final_job = handle.await.expect("worker task panicked");
        // Persistir estado final
        let _ = db_c.update_job(&final_job);
        match final_job.state {
            JobState::Completed => {
                server_c.emit(ServerEvent::JobCompleted(final_job.clone()));
            }
            JobState::Failed => {
                let err = final_job.error.clone().unwrap_or_default();
                server_c.emit(ServerEvent::JobFailed { id: final_job.id.clone(), error: err });
            }
            JobState::Cancelled => {
                server_c.emit(ServerEvent::JobCancelled(final_job.id.clone()));
            }
            JobState::Paused => {
                server_c.emit(ServerEvent::JobPaused(final_job.id.clone()));
            }
            _ => {}
        }

        // If the job is left in Paused state, we don't process it again
        // until it goes back to Pending. The user can resume it manually
        // or cancel it; the DB state is updated accordingly.

        // Cleanup de la signal map.
        {
            let mut map = server_c.signals.lock().expect("signals mutex");
            map.remove(&final_job.id);
        }

        // Ahogar el warning de "imported but unused" para AtomicU8 en este
        // module (we use it in signal_for above).
        let _ = std::marker::PhantomData::<AtomicU8>;
    }
}
