"""Tipos de mensajes del protocolo csfd.

Espejo de `daemon/src/types.rs`. Mantener ambos sincronizados: cualquier
cambio en el daemon requiere cambio acá.

Convenciones de serde aplicadas:
- `Operation` y `JobState` son enums con `#[serde(rename_all = "lowercase")]`
  → en JSON van en minúsculas: "copy", "pending", etc.
- `Request` y `Response` usan tag interno (`method` / `event`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class Operation(str, Enum):
    """Tipo de operación de un job."""

    COPY = "copy"
    MOVE = "move"


class JobState(str, Enum):
    """Estado de un job en la cola."""

    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class EnqueueItem:
    """Petición de encolar una operación de copia/move."""

    source: str
    dest: str
    op: Operation
    verify_hash: bool = False

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "dest": self.dest,
            "op": self.op.value,
            "verify_hash": self.verify_hash,
        }


@dataclass
class JobItem:
    """Un job individual en la cola. Refleja `daemon::types::JobItem`."""

    id: str
    source: str
    dest: str
    op: Operation
    state: JobState
    total_bytes: int = 0
    copied_bytes: int = 0
    hash: Optional[str] = None
    enqueued_at: int = 0
    finished_at: int = 0
    error: Optional[str] = None
    verify_hash: bool = False

    @property
    def progress(self) -> float:
        """Progreso de 0.0 a 1.0. Si no hay total_bytes conocido, devuelve 0."""
        if self.total_bytes == 0:
            return 0.0
        return min(1.0, self.copied_bytes / self.total_bytes)

    @property
    def is_terminal(self) -> bool:
        """True si el job ya terminó (no necesita más updates)."""
        return self.state in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)

    @property
    def basename(self) -> str:
        """Nombre corto del archivo origen, útil para mostrar en la UI."""
        return self.source.rsplit("/", 1)[-1] if "/" in self.source else self.source

    @classmethod
    def from_dict(cls, d: dict) -> "JobItem":
        """Construye un JobItem desde el dict que devuelve el daemon.

        Acepta tanto la forma con la key `data` (cuando viene en un
        Response::QueueSnapshot) como el dict crudo del job.
        """
        return cls(
            id=d["id"],
            source=d["source"],
            dest=d["dest"],
            op=Operation(d["op"]),
            state=JobState(d["state"]),
            total_bytes=int(d.get("total_bytes", 0)),
            copied_bytes=int(d.get("copied_bytes", 0)),
            hash=d.get("hash"),
            enqueued_at=int(d.get("enqueued_at", 0)),
            finished_at=int(d.get("finished_at", 0)),
            error=d.get("error"),
            verify_hash=bool(d.get("verify_hash", False)),
        )
