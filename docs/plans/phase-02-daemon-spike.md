# Fase 2 — Spike: Daemon Rust (csfd)
# CopySecureFast — Implementation Plan

> **Para Hermes:** ejecutar con subagent-driven-development, un subagente por tarea
> con revisión de dos etapas (spec + calidad).

**Goal:** Un daemon Rust funcional que acepta trabajos de copia/mover por JSON-RPC
sobre socket Unix, los persiste en SQLite, los ejecuta con progreso medible y los
reporta de vuelta al cliente.

**Architecture:**Daemon singleton que corre como proceso separado (no thread-per-job;
un event loop asincrónico con Tokio). Un job = un archivo. Las carpetas se
desglosan recursivamente en una lista de jobs hijos.

**Tech Stack:** Rust 1.90 + tokio (async I/O) + serde_json + rusqlite +
sha2 (hash) + uuid + tracing (logs).

---

## Tareas

### Tarea 1: Inicializar crate Rust (`daemon/`) con dependencias

**Objective:** Crear el esqueleto Cargo del daemon con las dependencias necesarias.

**Files:**
- Create: `daemon/Cargo.toml`
- Create: `daemon/src/main.rs` (stub mínimo que compila y sale 0)

**Step 1: Escribir Cargo.toml**

```toml
[package]
name = "csfd"
version = "0.1.0"
edition = "2021"

[dependencies]
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
rusqlite = { version = "0.32", features = ["bundled"] }
sha2 = "0.10"
uuid = { version = "1", features = ["v4"] }
tracing = "0.1"
tracing-subscriber = { version = "0.3", features = ["env-filter"] }
tracing-appender = "0.2"

[lib]
path = "src/lib.rs"

[[bin]]
name = "csfd"
path = "src/main.rs"
```

**Step 2: Escribir stub main.rs**

```rust
fn main() {
    println!("csfd v0.1.0 — CopySecureFast daemon");
}
```

**Step 3: Compilar para verificar que compila**

Run: `cd /home/rooterts/Projects/CopySecureFast/daemon && cargo build --release 2>&1`
Expected: compilación exitosa (puede tardar en la primera vez descargando crates)

**Step 4: Commit**

```bash
cd /home/rooterts/Projects/CopySecureFast
git add -A && git commit -m "feat(daemon): inicializar crate Rust con dependencias"
```

---

### Tarea 2: Definir tipos del protocolo JSON-RPC

**Objective:** Definir las structs de serde que representan los mensajes del
protocolo daemon ↔ cliente.

**Files:**
- Create: `daemon/src/types.rs`

**Step 1: Escribir types.rs**

```rust
//! Tipos del protocolo JSON-RPC entre csfd y los adaptadores Python.
//!
//! Dirección: ambos sentidos.
//! - Cliente → Daemon: enqueue, pause, resume, cancel, get_status, set_throttle
//! - Daemon → Cliente: job_started, job_progress, job_done, job_failed, job_paused, queue_snapshot

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// Un ítem individual en la cola. Puede ser un archivo o una carpeta
/// (desglosada recursivamente en jobs hijos por el daemon).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobItem {
    /// ID único del job (UUID).
    pub id: String,
    /// Ruta de origen.
    pub source: PathBuf,
    /// Ruta de destino.
    pub dest: PathBuf,
    /// "copy" o "move".
    pub op: Operation,
    /// Estado actual del job.
    pub state: JobState,
    /// Bytes totales (0 si se calcula después).
    pub total_bytes: u64,
    /// Bytes copiados hasta ahora.
    pub copied_bytes: u64,
    /// Hash SHA-256 del archivo (calculado post-copia o pre-copia si se requiere).
    pub hash: Option<String>,
    /// Timestamp de cuando entró en la cola.
    pub enqueued_at: i64,
    /// Timestamp de cuando terminó (0 si no terminó).
    pub finished_at: i64,
    /// Mensaje de error si state == failed.
    pub error: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Operation { Copy, Move }

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum JobState { Pending, Running, Paused, Completed, Failed, Cancelled }

/// Request del cliente → daemon.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "method", content = "params")]
pub enum Request {
    /// Agregar archivos/carpetas a la cola.
    #[serde(rename = "enqueue")]
    Enqueue { items: Vec<EnqueueItem> },
    /// Pausar un job específico o "global" para pausar todos.
    #[serde(rename = "pause")]
    Pause { job_id: Option<String> },
    /// Reanudar un job pausado o todos.
    #[serde(rename = "resume")]
    Resume { job_id: Option<String> },
    /// Cancelar un job o todos.
    #[serde(rename = "cancel")]
    Cancel { job_id: Option<String> },
    /// Obtener estado actual de la cola.
    #[serde(rename = "get_queue")]
    GetQueue,
    /// Limitar velocidad global (bytes/s, 0 = sin límite).
    #[serde(rename = "set_throttle")]
    SetThrottle { bytes_per_second: u64 },
    /// Ping para mantener conexión viva.
    #[serde(rename = "ping")]
    Ping,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnqueueItem {
    pub source: PathBuf,
    pub dest: PathBuf,
    pub op: Operation,
    /// Si true, calcula hash SHA-256 post-copia para verificar integridad.
    pub verify_hash: bool,
}

/// Response del daemon → cliente.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event", content = "data")]
pub enum Response {
    /// La cola actual completa (respuesta a get_queue).
    #[serde(rename = "queue_snapshot")]
    QueueSnapshot { jobs: Vec<JobItem>, global_speed_bps: u64 },
    /// Un job cambió de estado.
    #[serde(rename = "job_update")]
    JobUpdate { job: JobItem },
    /// Confirmación de acción (ack del enqueue).
    #[serde(rename = "enqueued")]
    Enqueued { count: usize },
    /// Error en el request.
    #[serde(rename = "error")]
    Error { message: String },
    /// Respuesta a ping.
    #[serde(rename = "pong")]
    Pong,
}
```

**Step 2: Commit**

```bash
cd /home/rooterts/Projects/CopySecureFast
git add -A && git commit -m "feat(daemon): definir tipos del protocolo JSON-RPC"
```

---

### Tarea 3: Capa de persistencia SQLite (cola + jobs)

**Objective:** Module `queue/db.rs` que maneja el schema SQLite y CRUD
básico de jobs.

**Files:**
- Create: `daemon/src/queue/mod.rs`
- Create: `daemon/src/queue/db.rs`
- Modify: `daemon/src/lib.rs` (agregar mod queue)

**Step 1: Crear queue/mod.rs**

```rust
pub mod db;
pub use db::QueueDb;
```

**Step 2: Escribir queue/db.rs**

```rust
use rusqlite::{Connection, params};
use std::path::Path;
use crate::types::{JobItem, JobState, Operation};

pub struct QueueDb {
    conn: Connection,
}

impl QueueDb {
    pub fn new(path: &Path) -> Result<Self, rusqlite::Error> {
        let conn = Connection::open(path)?;
        let db = Self { conn };
        db.init_schema()?;
        Ok(db)
    }

    fn init_schema(&self) -> Result<(), rusqlite::Error> {
        self.conn.execute(
            "CREATE TABLE IF NOT EXISTS jobs (
                id          TEXT PRIMARY KEY,
                source      TEXT NOT NULL,
                dest        TEXT NOT NULL,
                op          TEXT NOT NULL,
                state       TEXT NOT NULL DEFAULT 'pending',
                total_bytes INTEGER NOT NULL DEFAULT 0,
                copied_bytes INTEGER NOT NULL DEFAULT 0,
                hash        TEXT,
                enqueued_at INTEGER NOT NULL,
                finished_at INTEGER NOT NULL DEFAULT 0,
                error       TEXT,
                verify_hash INTEGER NOT NULL DEFAULT 0
            )",
            [],
        )?;
        Ok(())
    }

    pub fn insert_job(&self, job: &JobItem, verify_hash: bool) -> Result<(), rusqlite::Error> {
        self.conn.execute(
            "INSERT INTO jobs (id, source, dest, op, state, total_bytes,
             copied_bytes, hash, enqueued_at, finished_at, error, verify_hash)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
            params![
                job.id,
                job.source.to_string_lossy(),
                job.dest.to_string_lossy(),
                serde_json::to_string(&job.op).unwrap_or_default().trim_matches('"'),
                serde_json::to_string(&job.state).unwrap_or_default().trim_matches('"'),
                job.total_bytes as i64,
                job.copied_bytes as i64,
                job.hash,
                job.enqueued_at,
                job.finished_at,
                job.error,
                verify_hash as i32,
            ],
        )?;
        Ok(())
    }

    pub fn update_job(&self, job: &JobItem) -> Result<(), rusqlite::Error> {
        self.conn.execute(
            "UPDATE jobs SET state=?1, total_bytes=?2, copied_bytes=?3,
             hash=?4, finished_at=?5, error=?6 WHERE id=?7",
            params![
                serde_json::to_string(&job.state).unwrap_or_default().trim_matches('"'),
                job.total_bytes as i64,
                job.copied_bytes as i64,
                job.hash,
                job.finished_at,
                job.error,
                job.id,
            ],
        )?;
        Ok(())
    }

    pub fn get_all_jobs(&self) -> Result<Vec<JobItem>, rusqlite::Error> {
        let mut stmt = self.conn.prepare(
            "SELECT id,source,dest,op,state,total_bytes,copied_bytes,
             hash,enqueued_at,finished_at,error FROM jobs"
        )?;
        let rows = stmt.query_map([], |row| {
            let state_str: String = row.get(4)?;
            let op_str: String = row.get(3)?;
            Ok(JobItem {
                id: row.get(0)?,
                source: std::path::PathBuf::from(row.get::<_, String>(1)?),
                dest: std::path::PathBuf::from(row.get::<_, String>(2)?),
                op: serde_json::from_str(&format!("\"{}\"", op_str)).unwrap_or(Operation::Copy),
                state: serde_json::from_str(&format!("\"{}\"", state_str)).unwrap_or(JobState::Pending),
                total_bytes: row.get::<_, i64>(5)? as u64,
                copied_bytes: row.get::<_, i64>(6)? as u64,
                hash: row.get(7)?,
                enqueued_at: row.get(8)?,
                finished_at: row.get::<_, i64>(9)? as i64,
                error: row.get(10)?,
            })
        })?;
        rows.collect::<Result<Vec<_>, _>>()
    }

    pub fn get_job(&self, id: &str) -> Result<Option<JobItem>, rusqlite::Error> {
        let mut stmt = self.conn.prepare(
            "SELECT id,source,dest,op,state,total_bytes,copied_bytes,
             hash,enqueued_at,finished_at,error FROM jobs WHERE id=?1"
        )?;
        let mut rows = stmt.query(params![id])?;
        if let Some(row) = rows.next()? {
            let state_str: String = row.get(4)?;
            let op_str: String = row.get(3)?;
            Ok(Some(JobItem {
                id: row.get(0)?,
                source: std::path::PathBuf::from(row.get::<_, String>(1)?),
                dest: std::path::PathBuf::from(row.get::<_, String>(2)?),
                op: serde_json::from_str(&format!("\"{}\"", op_str)).unwrap_or(Operation::Copy),
                state: serde_json::from_str(&format!("\"{}\"", state_str)).unwrap_or(JobState::Pending),
                total_bytes: row.get::<_, i64>(5)? as u64,
                copied_bytes: row.get::<_, i64>(6)? as u64,
                hash: row.get(7)?,
                enqueued_at: row.get(8)?,
                finished_at: row.get::<_, i64>(9)? as i64,
                error: row.get(10)?,
            }))
        } else {
            Ok(None)
        }
    }
}
```

**Step 3: Agregar mod queue a lib.rs**

```rust
pub mod types;
pub mod queue;
```

**Step 4: Verificar compilación**

Run: `cd /home/rooterts/Projects/CopySecureFast/daemon && cargo build 2>&1`
Expected: compilación exitosa (advertencia sobre código no usado es OK por ahora)

**Step 5: Commit**

```bash
cd /home/rooterts/Projects/CopySecureFast
git add -A && git commit -m "feat(daemon): persistencia SQLite (cola y jobs)"
```

---

### Tarea 4: Motor de operaciones I/O (copiar, mover, hash)

**Objective:** Module `ops/copy.rs` que realiza la copia/mover con reportes de
progreso. Debe usar `std::fs::copy` con un wrapper que emita progreso, y
calcular SHA-256 si se requiere.

**Files:**
- Create: `daemon/src/ops/mod.rs`
- Create: `daemon/src/ops/copy.rs`
- Modify: `daemon/src/lib.rs` (agregar mod ops)
- Create: `daemon/src/ops/tests.rs` (tests de integración)

**Step 1: Escribir ops/mod.rs**

```rust
pub mod copy;
pub use copy::CopyOp;
```

**Step 2: Escribir ops/copy.rs**

```rust
use crate::types::{JobItem, JobState};
use sha2::{Sha256, Digest};
use std::fs::{self, File};
use std::io::{Read, Write};
use std::time::{SystemTime, UNIX_EPOCH};
use tracing::{info, warn, error};

pub struct CopyOp;

impl CopyOp {
    /// Ejecuta la copia o movimiento de UN archivo individual.
    /// Devuelve el JobItem actualizado con el resultado.
    pub fn run(job: &mut JobItem, verify_hash: bool) {
        let total = job.total_bytes;
        let mut copied: u64 = 0;

        match job.op {
            crate::types::Operation::Copy => {
                if let Err(e) = Self::copy_file(job, &mut copied, total, verify_hash) {
                    job.state = JobState::Failed;
                    job.error = Some(e.to_string());
                    error!(job_id = %job.id, "copy failed: {}", e);
                } else {
                    job.state = JobState::Completed;
                    job.finished_at = SystemTime::now()
                        .duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
                    info!(job_id = %job.id, "copy completed");
                }
            }
            crate::types::Operation::Move => {
                match fs::rename(&job.source, &job.dest) {
                    Ok(()) => {
                        job.state = JobState::Completed;
                        job.finished_at = SystemTime::now()
                            .duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
                        info!(job_id = %job.id, "move completed");
                    }
                    Err(e) => {
                        // Si renamecross-filesystem, fallback a copy+unlink
                        if e.kind() == std::io::ErrorKind::CrossesLinks ||
                           e.raw_os_error() == Some(libc::EXDEV) {
                            if let Ok(()) = Self::copy_file(job, &mut copied, total, verify_hash) {
                                let _ = fs::remove_file(&job.source);
                                job.state = JobState::Completed;
                                job.finished_at = SystemTime::now()
                                    .duration_since(UNIX_EPOCH).unwrap().as_secs() as i64;
                                info!(job_id = %job.id, "move (cross-device) completed");
                            } else {
                                job.state = JobState::Failed;
                                job.error = Some(e.to_string());
                            }
                        } else {
                            job.state = JobState::Failed;
                            job.error = Some(e.to_string());
                            error!(job_id = %job.id, "move failed: {}", e);
                        }
                    }
                }
            }
        }
    }

    fn copy_file(job: &mut JobItem, copied: &mut u64, total: u64,
                 verify_hash: bool) -> std::io::Result<()> {
        let mut src = File::open(&job.source)?;
        let mut dest = File::create(&job.dest)?;
        let mut buffer = vec![0u8; 128 * 1024]; // 128 KiB buffer
        let mut hasher = Sha256::new();
        let mut last_reported = std::time::Instant::now();

        loop {
            let n = src.read(&mut buffer)?;
            if n == 0 { break; }
            hasher.update(&buffer[..n]);
            dest.write_all(&buffer[..n])?;
            *copied += n as u64;
            job.copied_bytes = *copied;

            // Reportar progreso cada ~100ms (evitar flooding del socket)
            if last_reported.elapsed().as_millis() >= 100 {
                last_reported = std::time::Instant::now();
                // El caller es responsable de enviar el update al cliente
                // Aquí solo actualizamos el job state a Running si estaba Pending
                if job.state == JobState::Pending {
                    job.state = JobState::Running;
                }
            }
        }
        dest.flush()?;

        if verify_hash {
            let hash = format!("{:x}", hasher.finalize());
            job.hash = Some(hash);
            info!(job_id = %job.id, hash = %job.hash.as_ref().unwrap());
        }

        // Preservar timestamps
        if let Ok(meta) = fs::metadata(&job.source) {
            let _ = filetime::set_file_mtime(&job.dest, filetime::FileTime::from_last_modification_time(&meta));
        }

        Ok(())
    }

    /// Obtiene el tamaño en bytes de un archivo.
    pub fn file_size(path: &std::path::Path) -> u64 {
        fs::metadata(path).map(|m| m.len()).unwrap_or(0)
    }
}
```

**Nota:** Agregar `filetime = "0.4"` a Cargo.toml si se usa set_file_mtime.

**Step 3: Agregar deps a Cargo.toml** (actualizar Cargo.toml existente):

```toml
filetime = "0.4"
```

**Step 4: Modificar lib.rs** para agregar `mod ops;`

**Step 5: Verificar compilación**

Run: `cargo build 2>&1` (desde daemon/)

**Step 6: Tests en ops/tests.rs**

```rust
#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use tempfile::tempdir;

    #[test]
    fn test_copy_file_preserves_content() {
        let dir = tempdir().unwrap();
        let src = dir.path().join("src.txt");
        let dest = dir.path().join("dst.txt");

        fs::write(&src, b"hello world 12345").unwrap();

        let mut job = JobItem {
            id: uuid::Uuid::new_v4().to_string(),
            source: src.clone(),
            dest: dest.clone(),
            op: crate::types::Operation::Copy,
            state: crate::types::JobState::Pending,
            total_bytes: 0,
            copied_bytes: 0,
            hash: None,
            enqueued_at: 0,
            finished_at: 0,
            error: None,
        };
        job.total_bytes = CopyOp::file_size(&src);

        CopyOp::run(&mut job, false);

        assert_eq!(job.state, crate::types::JobState::Completed);
        assert_eq!(fs::read(&dest).unwrap(), b"hello world 12345");
    }
}
```

**Step 7: Compilar + tests**

Run: `cargo build && cargo test 2>&1`
Expected: build OK, test PASS

**Step 8: Commit**

```bash
git add -A && git commit -m "feat(daemon): motor de operaciones I/O (copy/move/hash)"
```

---

### Tarea 5: Servidor JSON-RPC sobre socket Unix

**Objective:** Module `rpc/server.rs` que escucha en un socket Unix,
procesa requests JSON-RPC y despacha al QueueManager.

**Files:**
- Create: `daemon/src/rpc/mod.rs`
- Create: `daemon/src/rpc/server.rs`
- Modify: `daemon/src/lib.rs` (agregar mod rpc)

**Step 1: Escribir rpc/mod.rs**

```rust
pub mod server;
pub use server::RpcServer;
```

**Step 2: Escribir rpc/server.rs**

```rust
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::net::{UnixListener, UnixStream};
use tracing::{info, error, warn};
use std::sync::Arc;
use crate::types::{Request, Response};
use crate::queue::QueueDb;

pub struct RpcServer {
    db: Arc<QueueDb>,
    throttle: std::sync::atomic::AtomicU64, // bytes/s, 0 = sin límite
}

impl RpcServer {
    pub fn new(db: Arc<QueueDb>) -> Self {
        Self { db, throttle: std::sync::atomic::AtomicU64::new(0) }
    }

    pub fn get_throttle(&self) -> u64 {
        self.throttle.load(std::sync::atomic::Ordering::Relaxed)
    }

    pub fn set_throttle(&self, bps: u64) {
        self.throttle.store(bps, std::sync::atomic::Ordering::Relaxed);
    }

    pub async fn run(self: Arc<Self>, socket_path: String) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        // Limpiar socket anterior si existe
        let _ = std::fs::remove_file(&socket_path);

        let listener = UnixListener::bind(&socket_path)?;
        info!(path = %socket_path, "RPC server listening");
        let db = self.db.clone();

        loop {
            match listener.accept().await {
                Ok((stream, _)) => {
                    let db = db.clone();
                    let server = self.clone();
                    tokio::spawn(async move {
                        if let Err(e) = Self::handle_connection(stream, db, server).await {
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

    async fn handle_connection(
        stream: UnixStream,
        db: Arc<QueueDb>,
        server: Arc<Self>,
    ) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
        let (rd, mut wr) = tokio::io::split(stream);
        let mut lines = BufReader::new(rd).lines();

        while let Ok(Some(line)) = lines.next_line().await {
            let line = line.trim();
            if line.is_empty() { continue; }

            let response = match serde_json::from_str::<Request>(&line) {
                Ok(req) => {
                    let resp = Self::handle_request(&req, &db, &server).await;
                    serde_json::to_string(&resp).unwrap_or_else(|_| r#"{"event":"error","data":{"message":"serialization failed"}}"#.to_string())
                }
                Err(e) => {
                    warn!(reason = %e, "invalid request");
                    serde_json::to_string(&Response::Error { message: format!("parse error: {}", e) }).unwrap()
                }
            };

            writeln!(wr, "{}", response).await?;
        }
        Ok(())
    }

    async fn handle_request(req: &Request, db: &Arc<QueueDb>, server: &Arc<Self>) -> Response {
        match req {
            Request::GetQueue => {
                let jobs = db.get_all_jobs().unwrap_or_default();
                Response::QueueSnapshot { jobs, global_speed_bps: server.get_throttle() }
            }
            Request::SetThrottle { bytes_per_second } => {
                server.set_throttle(*bytes_per_second);
                let jobs = db.get_all_jobs().unwrap_or_default();
                Response::QueueSnapshot { jobs, global_speed_bps: *bytes_per_second }
            }
            Request::Ping => Response::Pong,
            _ => Response::Error { message: format!("method not implemented: {:?}", req) },
        }
    }
}
```

**Step 3: Actualizar lib.rs** con `mod rpc;`

**Step 4: Compilar**

Run: `cargo build 2>&1`

**Step 5: Commit**

```bash
git add -A && git commit -m "feat(daemon): servidor JSON-RPC sobre socket Unix"
```

---

### Tarea 6: Main que integra todo y corre el event loop

**Objective:** Reescribir `main.rs` para que inicialice logging, abra la DB,
levante el servidor RPC y mantenga el proceso vivo. También agregar el loop de
procesamiento de jobs en background.

**Files:**
- Modify: `daemon/src/main.rs`

**Step 1: Reescribir main.rs**

```rust
use std::sync::Arc;
use tracing::{info, error, Level};
use tracing_subscriber::{fmt, prelude::*, EnvFilter};
use tracing_appender::rolling::{RollingFileAppender, Rotation};
use std::path::PathBuf;

mod types;
mod queue;
mod ops;
mod rpc;

use queue::QueueDb;
use rpc::RpcServer;

fn setup_logging() -> Result<(), Box<dyn std::error::Error>> {
    let log_dir = dirs::data_local_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("copysecurefast")
        .join("logs");
    std::fs::create_dir_all(&log_dir)?;

    let file_appender = RollingFileAppender::new(Rotation::DAILY, &log_dir, "csfd.log");
    let (non_blocking, _guard) = tracing_appender::non_blocking(file_appender);

    tracing_subscriber::registry()
        .with(EnvFilter::from_default_env().add_directive(Level::INFO.into()))
        .with(fmt::layer().with_writer(non_blocking).with_ansi(false))
        .with(fmt::layer().with_writer(std::io::stdout))
        .try_init()?;

    // Mantener el guard vivo (leak intencional — el proceso vive y muere con él)
    std::mem::forget(_guard);
    Ok(())
}

fn get_socket_path() -> PathBuf {
    dirs::runtime_dir()
        .unwrap_or_else(|| PathBuf::from("/run/user/1000"))
        .join("copysecurefast.sock")
}

fn get_db_path() -> PathBuf {
    dirs::data_local_dir()
        .unwrap_or_else(|| PathBuf::from("."))
        .join("copysecurefast")
        .join("queue.db")
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    setup_logging()?;

    let socket_path = get_socket_path().to_string_lossy().to_string();
    let db_path = get_db_path();

    // Asegurar que el directorio de la DB exista
    if let Some(parent) = db_path.parent() {
        std::fs::create_dir_all(parent)?;
    }

    info!(version = env!("CARGO_PKG_VERSION"), socket = %socket_path, db = %db_path,
          "csfd starting");

    let db = Arc::new(QueueDb::new(&db_path)?);
    let rpc = Arc::new(RpcServer::new(db.clone()));

    // Background: procesar cola
    let db_bg = db.clone();
    tokio::spawn(async move {
        loop {
            let jobs = db_bg.get_all_jobs().unwrap_or_default();
            for mut job in jobs.into_iter() {
                if job.state == crate::types::JobState::Pending {
                    // Process job
                    ops::CopyOp::run(&mut job, false);
                    let _ = db_bg.update_job(&job);
                }
            }
            tokio::time::sleep(tokio::time::Duration::from_millis(500)).await;
        }
    });

    // Levantar RPC server
    rpc.clone().run(socket_path).await
}
```

**Dependencias extra** (agregar a Cargo.toml):
```toml
dirs = "6"
tokio = { version = "1", features = ["full"] }
```

**Step 2: Compilar**

Run: `cargo build --release 2>&1` desde daemon/
Expected: compilación exitosa

**Step 3: Commit**

```bash
git add -A && git commit -m "feat(daemon): main con logging, DB, RPC server y loop de jobs"
```

---

## Verificación final

Después de completar todas las tareas:

1. `cd /home/rooterts/Projects/CopySecureFast/daemon && cargo build --release`
   → 0 errores de compilación.

2. `find . -name '*.rs' | head -20` → confirmar que todos los archivos existen.

3. `git log --oneline` → 6 commits, uno por tarea.

## Notas de implementación

- El throttle (limitador de velocidad) está definido en el server pero **no se
  aplica todavía** en CopyOp — se implementará en la Fase 6.
- La expansión recursiva de carpetas en jobs hijos se maneja simplificado: por
  ahora solo archivos individuales; carpetas se manejan como un solo job que
  delega a la capa de I/O (sin desglose fino de progreso por archivo dentro de
  carpeta).
- El scheduler en main.rs es naïf (polls cada 500ms); en versiones futuras se
  reemplazará por un canal de notificación interno para evitar polling.
- El protocolo es unidireccional sobre sockets Unix stream: cliente envía un
  JSON por línea, servidor responde un JSON por línea. Mantener la conexión
  abierta es soportado (el bucle while lines).

---

## Changelog de esta fase

- **Tarea 1**: Cargo.toml + stub main
- **Tarea 2**: types.rs (protocolo JSON-RPC)
- **Tarea 3**: queue/db.rs (SQLite)
- **Tarea 4**: ops/copy.rs (I/O engine)
- **Tarea 5**: rpc/server.rs (Unix socket JSON-RPC)
- **Tarea 6**: main.rs integrado