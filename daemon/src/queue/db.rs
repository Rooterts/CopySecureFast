//! Capa de persistencia SQLite de la cola de jobs.
//!
//! Maneja el schema y CRUD básico de jobs. Usa rusqlite con SQLite bundled
//! (no requiere libsqlite3-sys del sistema). La conexión NO es thread-safe
//! por sí sola, por eso en main.rs se accede detrás de un Mutex si se
//! necesita concurrencia. Para el spike usamos operaciones sincrónicas
//! protegidas por el Arc<Mutex<>> que se puede agregar más adelante.

use rusqlite::{params, Connection, OptionalExtension};
use std::path::Path;
use std::sync::Mutex;

use crate::types::{JobItem, JobState, Operation};

/// Wrapper sobre la conexión SQLite de la cola.
///
/// Usa `Mutex<Connection>` porque rusqlite::Connection no es `Send`
/// (tiene RefCell internamente). En el spike esto es suficiente; en
/// producción se migraría a un connection pool (`r2d2_sqlite` o similar).
pub struct QueueDb {
    conn: Mutex<Connection>,
}

impl QueueDb {
    /// Abre (o crea) la DB en `path` e inicializa el schema.
    pub fn new(path: &Path) -> Result<Self, rusqlite::Error> {
        if let Some(parent) = path.parent() {
            let _ = std::fs::create_dir_all(parent);
        }
        let conn = Connection::open(path)?;
        let db = Self {
            conn: Mutex::new(conn),
        };
        db.init_schema()?;
        Ok(db)
    }

    fn lock(&self) -> std::sync::MutexGuard<'_, Connection> {
        self.conn.lock().expect("QueueDb mutex poisoned")
    }

    fn init_schema(&self) -> Result<(), rusqlite::Error> {
        self.lock().execute(
            "CREATE TABLE IF NOT EXISTS jobs (
                id           TEXT PRIMARY KEY,
                source       TEXT NOT NULL,
                dest         TEXT NOT NULL,
                op           TEXT NOT NULL,
                state        TEXT NOT NULL DEFAULT 'pending',
                total_bytes  INTEGER NOT NULL DEFAULT 0,
                copied_bytes INTEGER NOT NULL DEFAULT 0,
                hash         TEXT,
                enqueued_at  INTEGER NOT NULL,
                finished_at  INTEGER NOT NULL DEFAULT 0,
                error        TEXT,
                verify_hash  INTEGER NOT NULL DEFAULT 0
            )",
            [],
        )?;
        // Índice por estado para consultas rápidas del worker loop.
        self.lock().execute(
            "CREATE INDEX IF NOT EXISTS idx_jobs_state ON jobs(state)",
            [],
        )?;
        Ok(())
    }

    /// Inserta un job nuevo. `verify_hash` se persiste aparte porque en
    /// `EnqueueItem` puede cambiar respecto al `JobItem` encolado.
    pub fn insert_job(&self, job: &JobItem) -> Result<(), rusqlite::Error> {
        self.lock().execute(
            "INSERT INTO jobs (id, source, dest, op, state, total_bytes,
             copied_bytes, hash, enqueued_at, finished_at, error, verify_hash)
             VALUES (?1,?2,?3,?4,?5,?6,?7,?8,?9,?10,?11,?12)",
            params![
                job.id,
                job.source.to_string_lossy().into_owned(),
                job.dest.to_string_lossy().into_owned(),
                op_to_str(job.op),
                state_to_str(job.state),
                job.total_bytes as i64,
                job.copied_bytes as i64,
                job.hash,
                job.enqueued_at,
                job.finished_at,
                job.error,
                job.verify_hash as i32,
            ],
        )?;
        Ok(())
    }

    /// Actualiza el estado y métricas de un job existente.
    pub fn update_state(&self, id: &str, new_state: JobState) -> Result<bool, rusqlite::Error> {
        // Devuelve true si actualizó una fila (es decir, el job existía y
        // el cambio de estado estaba permitido).
        let rows = self.lock().execute(
            "UPDATE jobs SET state = ?1 WHERE id = ?2",
            params![state_to_str(new_state), id],
        )?;
        Ok(rows > 0)
    }

    pub fn update_job(&self, job: &JobItem) -> Result<(), rusqlite::Error> {
        self.lock().execute(
            "UPDATE jobs SET state=?1, total_bytes=?2, copied_bytes=?3,
             hash=?4, finished_at=?5, error=?6 WHERE id=?7",
            params![
                state_to_str(job.state),
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

    /// Devuelve todos los jobs (sin orden particular).
    pub fn get_all_jobs(&self) -> Result<Vec<JobItem>, rusqlite::Error> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, source, dest, op, state, total_bytes, copied_bytes,
             hash, enqueued_at, finished_at, error, verify_hash
             FROM jobs",
        )?;
        let rows = stmt.query_map([], row_to_job)?;
        let mut out = Vec::new();
        for r in rows {
            out.push(r?);
        }
        Ok(out)
    }

    /// Devuelve un job por id.
    pub fn get_job(&self, id: &str) -> Result<Option<JobItem>, rusqlite::Error> {
        let conn = self.lock();
        let mut stmt = conn.prepare(
            "SELECT id, source, dest, op, state, total_bytes, copied_bytes,
             hash, enqueued_at, finished_at, error, verify_hash
             FROM jobs WHERE id = ?1",
        )?;
        stmt.query_row(params![id], row_to_job).optional()
    }

    /// Borra jobs en estado Completed/Failed/Cancelled (limpieza).
    pub fn purge_finished(&self) -> Result<usize, rusqlite::Error> {
        let n = self.lock().execute(
            "DELETE FROM jobs WHERE state IN ('completed','failed','cancelled')",
            [],
        )?;
        Ok(n)
    }
}

fn row_to_job(row: &rusqlite::Row<'_>) -> rusqlite::Result<JobItem> {
    let op_str: String = row.get(3)?;
    let state_str: String = row.get(4)?;
    Ok(JobItem {
        id: row.get(0)?,
        source: std::path::PathBuf::from(row.get::<_, String>(1)?),
        dest: std::path::PathBuf::from(row.get::<_, String>(2)?),
        op: str_to_op(&op_str),
        state: str_to_state(&state_str),
        total_bytes: row.get::<_, i64>(5)? as u64,
        copied_bytes: row.get::<_, i64>(6)? as u64,
        hash: row.get(7)?,
        enqueued_at: row.get(8)?,
        finished_at: row.get(9)?,
        error: row.get(10)?,
        verify_hash: row.get::<_, i32>(11)? != 0,
    })
}

fn op_to_str(op: Operation) -> &'static str {
    match op {
        Operation::Copy => "copy",
        Operation::Move => "move",
    }
}

fn str_to_op(s: &str) -> Operation {
    match s {
        "move" => Operation::Move,
        _ => Operation::Copy,
    }
}

fn state_to_str(s: JobState) -> &'static str {
    match s {
        JobState::Pending => "pending",
        JobState::Running => "running",
        JobState::Paused => "paused",
        JobState::Completed => "completed",
        JobState::Failed => "failed",
        JobState::Cancelled => "cancelled",
    }
}

fn str_to_state(s: &str) -> JobState {
    match s {
        "running" => JobState::Running,
        "paused" => JobState::Paused,
        "completed" => JobState::Completed,
        "failed" => JobState::Failed,
        "cancelled" => JobState::Cancelled,
        _ => JobState::Pending,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn now() -> i64 {
        SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs() as i64
    }

    #[test]
    fn schema_init_creates_table() {
        let dir = std::env::temp_dir().join(format!("csfd-test-{}", uuid::Uuid::new_v4()));
        let db = QueueDb::new(&dir.join("q.db")).unwrap();
        let jobs = db.get_all_jobs().unwrap();
        assert!(jobs.is_empty());
    }

    #[test]
    fn insert_and_retrieve_roundtrip() {
        let dir = std::env::temp_dir().join(format!("csfd-test-{}", uuid::Uuid::new_v4()));
        let db = QueueDb::new(&dir.join("q.db")).unwrap();

        let job = JobItem {
            id: "abc-123".into(),
            source: "/tmp/a".into(),
            dest: "/tmp/b".into(),
            op: Operation::Copy,
            state: JobState::Pending,
            total_bytes: 1024,
            copied_bytes: 0,
            hash: None,
            enqueued_at: now(),
            finished_at: 0,
            error: None,
            verify_hash: true,
        };
        db.insert_job(&job).unwrap();

        let got = db.get_job("abc-123").unwrap().unwrap();
        assert_eq!(got.id, "abc-123");
        assert_eq!(got.op, Operation::Copy);
        assert_eq!(got.state, JobState::Pending);
        assert_eq!(got.total_bytes, 1024);
        assert!(got.verify_hash);
    }

    #[test]
    fn update_state_persists() {
        let dir = std::env::temp_dir().join(format!("csfd-test-{}", uuid::Uuid::new_v4()));
        let db = QueueDb::new(&dir.join("q.db")).unwrap();

        let mut job = JobItem {
            id: "x".into(),
            source: "/tmp/a".into(),
            dest: "/tmp/b".into(),
            op: Operation::Move,
            state: JobState::Pending,
            total_bytes: 0,
            copied_bytes: 0,
            hash: None,
            enqueued_at: now(),
            finished_at: 0,
            error: None,
            verify_hash: false,
        };
        db.insert_job(&job).unwrap();

        job.state = JobState::Completed;
        job.finished_at = now();
        db.update_job(&job).unwrap();

        let got = db.get_job("x").unwrap().unwrap();
        assert_eq!(got.state, JobState::Completed);
        assert_eq!(got.op, Operation::Move);
    }
}
