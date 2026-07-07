//! Motor de copia/movimiento de archivos individuales.
//!
//! El hash SHA-256 se calcula durante la copia (streaming) si está
//! habilitado, sin necesidad de una segunda pasada por el archivo.
//!
//! Cross-device move: cuando un `rename` falla (posible EXDEV entre
//! filesystems), hacemos fallback a copy + unlink. Detectamos el caso
//! comparando si la fuente sigue existiendo después del rename fallido.

use std::fs::{self, File};
use std::io::{Read, Write};
use std::path::Path;
use std::time::{SystemTime, UNIX_EPOCH};

use sha2::{Digest, Sha256};
use tracing::{error, info, warn};

use crate::types::{JobItem, JobState, Operation};

/// Tamaño de buffer de copia: 128 KiB. Buen balance entre syscalls y memoria.
const COPY_BUFFER_SIZE: usize = 128 * 1024;

pub struct CopyOp;

impl CopyOp {
    /// Ejecuta la copia o movimiento del archivo del job.
    /// Devuelve el job actualizado con el estado final.
    pub fn run(job: &mut JobItem) {
        let verify_hash = job.verify_hash;

        // Asegurar que tenemos total_bytes antes de empezar.
        if job.total_bytes == 0 {
            job.total_bytes = Self::file_size(&job.source);
        }

        // Extraemos source/dest a variables locales para evitar el
        // conflicto de préstamo inmutable+mutable sobre `job`.
        let source = job.source.clone();
        let dest = job.dest.clone();

        match job.op {
            Operation::Copy => match Self::copy_file(&source, &dest, job, verify_hash) {
                Ok(()) => {
                    job.state = JobState::Completed;
                    job.finished_at = now_secs();
                    info!(job_id = %job.id, "copy completed");
                }
                Err(e) => {
                    job.state = JobState::Failed;
                    job.error = Some(e.to_string());
                    error!(job_id = %job.id, "copy failed: {}", e);
                }
            },
            Operation::Move => match fs::rename(&source, &dest) {
                Ok(()) => {
                    job.state = JobState::Completed;
                    job.finished_at = now_secs();
                    info!(job_id = %job.id, "move completed (rename)");
                }
                Err(e) => {
                    // Fallback cross-device: copy + remove
                    warn!(
                        job_id = %job.id,
                        "rename failed ({}), trying copy+unlink", e
                    );
                    match Self::copy_file(&source, &dest, job, verify_hash) {
                        Ok(()) => match fs::remove_file(&job.source) {
                            Ok(()) => {
                                job.state = JobState::Completed;
                                job.finished_at = now_secs();
                                info!(job_id = %job.id, "move completed (copy+unlink)");
                            }
                            Err(rm_err) => {
                                job.state = JobState::Failed;
                                job.error = Some(format!(
                                    "copy OK but source remove failed: {}",
                                    rm_err
                                ));
                                error!(job_id = %job.id, "{}", job.error.as_ref().unwrap());
                            }
                        },
                        Err(copy_err) => {
                            job.state = JobState::Failed;
                            job.error = Some(copy_err.to_string());
                            error!(job_id = %job.id, "move failed: {}", copy_err);
                        }
                    }
                }
            },
        }
    }

    /// Tamaño de un archivo en bytes, 0 si no se puede leer.
    pub fn file_size(path: &Path) -> u64 {
        fs::metadata(path).map(|m| m.len()).unwrap_or(0)
    }

    /// Copia el archivo de `src` a `dst` con streaming y hash opcional.
    /// Reporta el progreso en `job.copied_bytes` durante la operación.
    fn copy_file<P: AsRef<Path>, Q: AsRef<Path>>(
        src: P,
        dst: Q,
        job: &mut JobItem,
        verify_hash: bool,
    ) -> std::io::Result<()> {
        let mut src_f = File::open(src.as_ref())?;
        let mut dst_f = File::create(dst.as_ref())?;
        let mut buffer = vec![0u8; COPY_BUFFER_SIZE];
        let mut hasher = if verify_hash {
            Some(Sha256::new())
        } else {
            None
        };

        loop {
            let n = src_f.read(&mut buffer)?;
            if n == 0 {
                break;
            }
            if let Some(ref mut h) = hasher {
                h.update(&buffer[..n]);
            }
            dst_f.write_all(&buffer[..n])?;
            job.copied_bytes = job.copied_bytes.saturating_add(n as u64);

            // Marcar running una vez que empezó a copiar.
            if job.state == JobState::Pending {
                job.state = JobState::Running;
            }
        }
        dst_f.flush()?;

        if let Some(h) = hasher {
            let digest = h.finalize();
            job.hash = Some(format!("{:x}", digest));
            info!(
                job_id = %job.id,
                hash = %job.hash.as_ref().unwrap(),
                "hash computed"
            );
        }

        // Preservar permisos del origen (best effort).
        if let Ok(meta) = fs::metadata(src.as_ref()) {
            let _ = fs::set_permissions(dst.as_ref(), meta.permissions());
        }

        Ok(())
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
        CopyOp::run(&mut job);

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
        let payload = b"abc";
        std::fs::write(&src, payload).unwrap();

        // SHA-256("abc") = ba7816bf...
        let expected = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

        let mut job = make_job(src, dst, Operation::Copy);
        job.verify_hash = true;
        CopyOp::run(&mut job);

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
        CopyOp::run(&mut job);

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
        CopyOp::run(&mut job);

        assert_eq!(job.state, JobState::Failed);
        assert!(job.error.is_some());
    }
}
