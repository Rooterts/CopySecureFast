//! File copy/move engine for individual files.
//!
//! Features of the current spike:
//! - Streaming with 128 KiB buffer.
//! - Optional SHA-256 hash in the same pass (no second read of the file).
//! - Move: uses `fs::rename`; falls back to copy+unlink on cross-device.
//! - **Cooperative pause/cancel**: checks an `Arc<AtomicU8>` every
//!   block. 0 = run, 1 = pause, 2 = cancel.
//! - Preserves source permissions (best effort).
//! - Reports copied bytes in `job.copied_bytes` during the operation.

use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::Path;
use std::sync::atomic::{AtomicU8, Ordering};
use std::sync::Arc;
use std::time::{SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use tracing::{error, info, warn};

use crate::types::{JobItem, JobState, Operation};

const COPY_BUFFER_SIZE: usize = 128 * 1024;
const CHECK_SIGNAL_EVERY_MS: u128 = 80;

/// Atomic signal codes for controlling an in-flight job.
pub const SIGNAL_RUN: u8 = 0;
pub const SIGNAL_PAUSE: u8 = 1;
pub const SIGNAL_CANCEL: u8 = 2;

pub struct CopyOp;

impl CopyOp {
    /// Runs the copy or move of the file in `job`.
    ///
    /// `signal` is shared with the RPC server: if the user pauses or
    /// cancels, the server changes the value and the job reacts.
    ///
    /// Returns the job updated with its final state.
    pub fn run(job: &mut JobItem, signal: Arc<AtomicU8>) {
        let verify_hash = job.verify_hash;

        if job.total_bytes == 0 {
            job.total_bytes = Self::file_size(&job.source);
        }

        // If already cancelled before we start, exit early.
        if signal.load(Ordering::Relaxed) == SIGNAL_CANCEL {
            job.state = JobState::Cancelled;
            job.finished_at = now_secs();
            return;
        }

        let source = job.source.clone();
        let dest = job.dest.clone();

        match job.op {
            Operation::Copy => {
                match Self::copy_file(&source, &dest, job, verify_hash, &signal) {
                    Ok(()) => {
                        job.state = JobState::Completed;
                        job.finished_at = now_secs();
                        info!(job_id = %job.id, "copy completed");
                    }
                    Err(Signal::Cancelled) => {
                        // Clean up the partial file so we don't leave garbage.
                        let _ = fs::remove_file(&dest);
                        job.state = JobState::Cancelled;
                        job.finished_at = now_secs();
                        info!(job_id = %job.id, "copy cancelled");
                    }
                    Err(Signal::Paused) => {
                        // Don't delete the partial: on resume the worker
                        // should pick it up from where it left off.
                        job.state = JobState::Paused;
                        info!(job_id = %job.id, "copy paused at {} bytes", job.copied_bytes);
                    }
                    Err(Signal::Io(e)) => {
                        job.state = JobState::Failed;
                        job.error = Some(e.to_string());
                        error!(job_id = %job.id, "copy failed: {}", e);
                    }
                }
            }
            Operation::Move => match fs::rename(&source, &dest) {
                Ok(()) => {
                    job.state = JobState::Completed;
                    job.finished_at = now_secs();
                    info!(job_id = %job.id, "move completed (rename)");
                }
                Err(e) => {
                    warn!(
                        job_id = %job.id,
                        "rename failed ({}), trying copy+unlink", e
                    );
                    match Self::copy_file(&source, &dest, job, verify_hash, &signal) {
                        Ok(()) => {
                            // If cancelled during the copy, don't delete
                            // the source (may have failed partially).
                            if job.state == JobState::Cancelled {
                                job.finished_at = now_secs();
                                return;
                            }
                            if let Err(rm_err) = fs::remove_file(&source) {
                                job.state = JobState::Failed;
                                job.error = Some(format!(
                                    "copy OK but source remove failed: {}",
                                    rm_err
                                ));
                                error!(job_id = %job.id, "{}", job.error.as_ref().unwrap());
                                return;
                            }
                            job.state = JobState::Completed;
                            job.finished_at = now_secs();
                            info!(job_id = %job.id, "move completed (copy+unlink)");
                        }
                        Err(Signal::Cancelled) => {
                            let _ = fs::remove_file(&dest);
                            job.state = JobState::Cancelled;
                            job.finished_at = now_secs();
                            info!(job_id = %job.id, "move cancelled");
                        }
                        Err(Signal::Paused) => {
                            job.state = JobState::Paused;
                            info!(job_id = %job.id, "move paused at {} bytes", job.copied_bytes);
                        }
                        Err(Signal::Io(copy_err)) => {
                            job.state = JobState::Failed;
                            job.error = Some(copy_err.to_string());
                            error!(job_id = %job.id, "move failed: {}", copy_err);
                        }
                    }
                }
            },
        }
    }

    /// File size in bytes, 0 if it cannot be read.
    pub fn file_size(path: &Path) -> u64 {
        fs::metadata(path).map(|m| m.len()).unwrap_or(0)
    }

    fn copy_file<P: AsRef<Path>, Q: AsRef<Path>>(
        src: P,
        dst: Q,
        job: &mut JobItem,
        verify_hash: bool,
        signal: &Arc<AtomicU8>,
    ) -> Result<(), Signal> {
        let dst_ref = dst.as_ref();
        // Create parent directory if it doesn't exist (case "paste folder").
        if let Some(parent) = dst_ref.parent() {
            if !parent.as_os_str().is_empty() {
                if let Err(e) = std::fs::create_dir_all(parent) {
                    return Err(Signal::Io(e));
                }
            }
        }
        let mut src_f = File::open(src.as_ref()).map_err(Signal::Io)?;
        let mut dst_f = File::create(dst_ref).map_err(Signal::Io)?;
        let mut buffer = vec![0u8; COPY_BUFFER_SIZE];
        let mut hasher = if verify_hash {
            Some(Sha256::new())
        } else {
            None
        };
        let mut last_check = std::time::Instant::now();

        loop {
            // Check the signal every ~80ms. Faster = more responsive to
            // pause/cancel but more overhead. 80ms is invisible to the
            // user and reacts in time even for small files.
            if last_check.elapsed().as_millis() >= CHECK_SIGNAL_EVERY_MS {
                match signal.load(Ordering::Relaxed) {
                    SIGNAL_CANCEL => return Err(Signal::Cancelled),
                    SIGNAL_PAUSE => return Err(Signal::Paused),
                    _ => {}
                }
                last_check = std::time::Instant::now();
            }

            let n = src_f.read(&mut buffer).map_err(Signal::Io)?;
            if n == 0 {
                break;
            }
            if let Some(ref mut h) = hasher {
                h.update(&buffer[..n]);
            }
            dst_f.write_all(&buffer[..n]).map_err(Signal::Io)?;
            job.copied_bytes = job.copied_bytes.saturating_add(n as u64);

            // Mark as running as soon as we start copying.
            if job.state == JobState::Pending {
                job.state = JobState::Running;
            }
        }
        dst_f.flush().map_err(Signal::Io)?;

        if let Some(h) = hasher {
            let digest = h.finalize();
            job.hash = Some(format!("{:x}", digest));
            info!(
                job_id = %job.id,
                hash = %job.hash.as_ref().unwrap(),
                "hash computed"
            );
        }

        // Preserve source permissions (best effort).
        if let Ok(meta) = fs::metadata(src.as_ref()) {
            let _ = fs::set_permissions(dst_ref, meta.permissions());
        }
        Ok(())
    }
}

/// Outcome of a copy: success, I/O error, paused or cancelled.
#[derive(Debug)]
pub enum Signal {
    Io(std::io::Error),
    Paused,
    Cancelled,
}

impl std::fmt::Display for Signal {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Signal::Io(e) => write!(f, "io: {}", e),
            Signal::Paused => write!(f, "paused"),
            Signal::Cancelled => write!(f, "cancelled"),
        }
    }
}

fn now_secs() -> i64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
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

    fn make_job(src: std::path::PathBuf, dst: std::path::PathBuf, op: Operation) -> JobItem {
        JobItem {
            id: uuid::Uuid::new_v4().to_string(),
            source: src,
            dest: dst,
            op,
            state: JobState::Pending,
            total_bytes: 0,
            copied_bytes: 0,
            hash: None,
            enqueued_at: now(),
            finished_at: 0,
            error: None,
            verify_hash: false,
        }
    }

    fn sig() -> Arc<AtomicU8> {
        Arc::new(AtomicU8::new(SIGNAL_RUN))
    }

    #[test]
    fn copy_preserves_content() {
        let dir = std::env::temp_dir().join(format!("csfd-cp-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("src.txt");
        let dst = dir.join("dst.txt");
        let payload = b"hello world 12345 -- CopySecureFast test payload";
        std::fs::write(&src, payload).unwrap();

        let mut job = make_job(src.clone(), dst.clone(), Operation::Copy);
        job.total_bytes = payload.len() as u64;
        CopyOp::run(&mut job, sig());

        assert_eq!(job.state, JobState::Completed, "err: {:?}", job.error);
        assert_eq!(std::fs::read(&dst).unwrap(), payload);
        assert!(job.error.is_none());
        assert!(job.finished_at > 0);
    }

    #[test]
    fn copy_with_hash_computes_sha256() {
        let dir = std::env::temp_dir().join(format!("csfd-h-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("src.txt");
        let dst = dir.join("dst.txt");
        std::fs::write(&src, b"abc").unwrap();

        // SHA-256("abc") = ba7816bf...
        let expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";
        let mut job = make_job(src, dst, Operation::Copy);
        job.verify_hash = true;
        CopyOp::run(&mut job, sig());

        assert_eq!(job.state, JobState::Completed);
        assert_eq!(job.hash.as_deref(), Some(expected));
    }

    #[test]
    fn move_within_same_dir_uses_rename() {
        let dir = std::env::temp_dir().join(format!("csfd-mv-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("src.txt");
        let dst = dir.join("dst.txt");
        std::fs::write(&src, b"content to move").unwrap();

        let mut job = make_job(src.clone(), dst.clone(), Operation::Move);
        CopyOp::run(&mut job, sig());

        assert_eq!(job.state, JobState::Completed, "err: {:?}", job.error);
        assert!(!src.exists(), "source should be removed after move");
        assert!(dst.exists());
    }

    #[test]
    fn copy_fails_gracefully_on_missing_source() {
        let dir = std::env::temp_dir().join(format!("csfd-fail-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("does-not-exist.txt");
        let dst = dir.join("dst.txt");

        let mut job = make_job(src, dst, Operation::Copy);
        CopyOp::run(&mut job, sig());

        assert_eq!(job.state, JobState::Failed);
        assert!(job.error.is_some());
    }

    #[test]
    fn cancel_signal_aborts_copy() {
        let dir = std::env::temp_dir().join(format!("csfd-cancel-{}", uuid::Uuid::new_v4()));
        std::fs::create_dir_all(&dir).unwrap();
        let src = dir.join("src.txt");
        let dst = dir.join("dst.txt");
        // Large enough payload so the copy takes more than one block.
        let payload = vec![0xABu8; 5 * 1024 * 1024];
        std::fs::write(&src, &payload).unwrap();

        let s = sig();
        // Cancel before starting.
        s.store(SIGNAL_CANCEL, Ordering::Relaxed);

        let mut job = make_job(src, dst.clone(), Operation::Copy);
        CopyOp::run(&mut job, s);

        assert_eq!(job.state, JobState::Cancelled);
        assert!(!dst.exists(), "the partial file must be removed");
    }
}
