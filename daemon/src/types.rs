//! JSON-RPC protocol types between csfd (daemon) and Python adapters.
//!
//! Direction: both ways.
//! - Client -> Daemon: enqueue, pause, resume, cancel, get_queue, set_throttle, ping
//! - Daemon -> Client: queue_snapshot, job_started, job_progress, job_completed,
//!   job_failed, job_paused, job_resumed, job_cancelled, error, pong

use serde::{Deserialize, Serialize};
use std::path::PathBuf;

/// A single job in the queue. Can be a file or a folder (the latter
/// is expanded recursively into child jobs by the daemon).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct JobItem {
    /// Unique job ID (UUID).
    pub id: String,
    /// Source path.
    pub source: PathBuf,
    /// Destination path.
    pub dest: PathBuf,
    /// "copy" or "move".
    pub op: Operation,
    /// Current job state.
    pub state: JobState,
    /// Total bytes (0 if not yet known).
    pub total_bytes: u64,
    /// Bytes copied so far.
    pub copied_bytes: u64,
    /// SHA-256 hash (computed post-copy or pre-copy if requested).
    pub hash: Option<String>,
    /// Timestamp of when the job entered the queue (epoch seconds).
    pub enqueued_at: i64,
    /// Timestamp of when the job finished (0 if still running).
    pub finished_at: i64,
    /// Error message if state == Failed.
    pub error: Option<String>,
    /// If true, SHA-256 hash is computed post-copy to verify integrity.
    #[serde(default)]
    pub verify_hash: bool,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq)]
#[serde(rename_all = "lowercase")]
pub enum Operation {
    Copy,
    Move,
}

/// Current state of a job.
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

impl JobState {
    /// States in which a job can be cancelled.
    pub fn is_cancellable(&self) -> bool {
        matches!(self, JobState::Pending | JobState::Running | JobState::Paused)
    }

    /// States in which a job can be paused.
    pub fn is_pausable(&self) -> bool {
        matches!(self, JobState::Pending | JobState::Running)
    }

    /// States in which a job can be resumed.
    pub fn is_resumable(&self) -> bool {
        matches!(self, JobState::Paused)
    }
}

impl Default for JobState {
    fn default() -> Self {
        JobState::Pending
    }
}

/// Client -> Daemon request.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "method", content = "params")]
pub enum Request {
    /// Add files/folders to the queue.
    #[serde(rename = "enqueue")]
    Enqueue { items: Vec<EnqueueItem> },
    /// Pause a specific job (job_id) or all jobs (None).
    #[serde(rename = "pause")]
    Pause { job_id: Option<String> },
    /// Resume a paused job or all paused jobs.
    #[serde(rename = "resume")]
    Resume { job_id: Option<String> },
    /// Cancel a job or all jobs.
    #[serde(rename = "cancel")]
    Cancel { job_id: Option<String> },
    /// Get the current queue state.
    #[serde(rename = "get_queue")]
    GetQueue,
    /// Set the global speed limit (bytes/s, 0 = unlimited).
    #[serde(rename = "set_throttle")]
    SetThrottle { bytes_per_second: u64 },
    /// Ping to keep the connection alive / health check.
    #[serde(rename = "ping")]
    Ping,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnqueueItem {
    pub source: PathBuf,
    pub dest: PathBuf,
    pub op: Operation,
    /// If true, SHA-256 hash is computed post-copy to verify integrity.
    #[serde(default)]
    pub verify_hash: bool,
}

/// Daemon -> Client response.
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "event", content = "data")]
pub enum Response {
    /// Full queue state (reply to get_queue).
    #[serde(rename = "queue_snapshot")]
    QueueSnapshot {
        jobs: Vec<JobItem>,
        global_speed_bps: u64,
    },
    /// A job state changed.
    #[serde(rename = "job_update")]
    JobUpdate { job: JobItem },
    /// Acknowledgement of an enqueue request.
    #[serde(rename = "enqueued")]
    Enqueued { count: usize },
    /// Error in a request.
    #[serde(rename = "error")]
    Error { message: String },
    /// Reply to a ping.
    #[serde(rename = "pong")]
    Pong,
}
