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
    /// Timestamp de cuando entró en la cola (epoch seconds).
    pub enqueued_at: i64,
    /// Timestamp de cuando terminó (0 si no terminó).
    pub finished_at: i64,
    /// Mensaje de error si state == failed.
    pub error: Option<String>,
    /// Si true, calcula hash SHA-256 post-copia para verificar integridad.
    #[serde(default)]
    pub verify_hash: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Operation {
    Copy,
    Move,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum JobState {
    Pending,
    Running,
    Paused,
    Completed,
    Failed,
    Cancelled,
}

impl Default for JobState {
    fn default() -> Self {
        JobState::Pending
    }
}

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
    #[serde(default)]
    pub verify_hash: bool,
}

/// Response del daemon → cliente.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event", content = "data")]
pub enum Response {
    /// La cola actual completa (respuesta a get_queue).
    #[serde(rename = "queue_snapshot")]
    QueueSnapshot {
        jobs: Vec<JobItem>,
        global_speed_bps: u64,
    },
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