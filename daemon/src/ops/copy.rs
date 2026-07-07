//! Motor de copia/movimiento de archivos individuales.
//!
//! Características del spike actual:
//! - Streaming con buffer 128 KiB.
//! - Hash SHA-256 opcional en la misma pasada (no relee el archivo).
//! - Move: usa `fs::rename`; fallback a copy+unlink si rename falla
//!   (cross-device).
//! - **Pausa/cancelación cooperativa**: chequea un `Arc<AtomicU8>` cada
//!   bloque. 0 = continuar, 1 = pausar, 2 = cancelar.
//! - Preserva permisos del origen (best effort).
//! - Reporta bytes copiados en `job.copied_bytes` durante la operación.

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

/// Códigos de señal atómica para controlar un job en curso.
pub const SIGNAL_RUN: u8 = 0;
pub const SIGNAL_PAUSE: u8 = 1;
pub const SIGNAL_CANCEL: u8 = 2;

pub struct CopyOp;

impl CopyOp {
    /// Ejecuta la copia o movimiento del archivo del job.
    ///
    /// `signal` es compartido con el RPC server: si el usuario pausa
    /// o cancela, el server cambia el valor y el job reacciona.
    ///
    /// Devuelve el job actualizado con el estado final.
    pub fn run(job: &mut JobItem, signal: Arc<AtomicU8>) {
        let verify_hash = job.verify_hash;

        if job.total_bytes == 0 {
            job.total_bytes = Self::file_size(&job.source);
        }

        // Si ya estaba cancelado antes de empezar, no hacer nada.
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
                        // Borrar el archivo parcial para no dejar basura.
                        let _ = fs::remove_file(&dest);
                        job.state = JobState::Cancelled;
                        job.finished_at = now_secs();
                        info!(job_id = %job.id, "copy cancelled");
                    }
                    Err(Signal::Paused) => {
                        // No borramos el archivo parcial: al reanudar, el
                        // worker debería detectar el estado y seguir.
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
                            // Si fue cancelado durante la copia, no borrar
                            // el origen (puede haber fallado parcialmente).
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
        let mut src_f = File::open(src.as_ref()).map_err(Signal::Io)?;
        let mut dst_f = File::create(dst.as_ref()).map_err(Signal::Io)?;
        let mut buffer = vec![0u8; COPY_BUFFER_SIZE];
        let mut hasher = if verify_hash {
            Some(Sha256::new())
        } else {
            None
        };
        let mut last_check = std::time::Instant::now();

        loop {
            // Chequeo de señal cada ~80ms. Más rápido = más responsivo
            // a pausa/cancel, pero más overhead. 80ms es invisible al
            // usuario y permite reaccionar en archivos chicos.
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

        if let Ok(meta) = fs::metadata(src.as_ref()) {
            let _ = fs::set_permissions(dst.as_ref(), meta.permissions());
        }
        Ok(())
    }
}

/// Salida de una copia: éxito, error de I/O, pausa o cancelación.
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
    use std::sync::atomic::AtomicU8;
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
        let payload = b"hello world 12345 -- payload de prueba CopySecureFast";
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
        std::fs::write(&src, b"contenido a mover").unwrap();

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
        // Payload grande para que la copia tarde más de un bloque.
        let payload = vec![0xABu8; 5 * 1024 * 1024];
        std::fs::write(&src, &payload).unwrap();

        let s = sig();
        // Cancelar antes de empezar.
        s.store(SIGNAL_CANCEL, Ordering::Relaxed);

        let mut job = make_job(src, dst.clone(), Operation::Copy);
        CopyOp::run(&mut job, s);

        assert_eq!(job.state, JobState::Cancelled);
        assert!(!dst.exists(), "el archivo parcial debe haberse borrado");
    }
}
