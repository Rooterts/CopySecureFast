"""Tipos de mensajes del protocolo csfd.

Espejo de `daemon/src/types.rs`. Mantener ambos sincronizados.

Convenciones de serde aplicadas:
- `Operation` y `JobState` son enums con `#[serde(rename_all = "lowercase")]`
  → en JSON van en minúsculas: "copy", "pending", etc.
- `Request` y `Response` usan tag interno (`method` / `event`).
- Eventos espontáneos del server tienen tag `event` (job_started,
  job_progress, job_completed, etc).
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

    @property
    def is_terminal(self) -> bool:
        return self in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED)

    @property
    def is_cancellable(self) -> bool:
        return self in (JobState.PENDING, JobState.RUNNING, JobState.PAUSED)

    @property
    def is_pausable(self) -> bool:
        return self in (JobState.PENDING, JobState.RUNNING)

    @property
    def is_resumable(self) -> bool:
        return self == JobState.PAUSED


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
        if self.total_bytes == 0:
            return 0.0
        return min(1.0, self.copied_bytes / self.total_bytes)

    @property
    def basename(self) -> str:
        return self.source.rsplit("/", 1)[-1] if "/" in self.source else self.source

    @property
    def is_terminal(self) -> bool:
        return self.state.is_terminal

    @classmethod
    def from_dict(cls, d: dict) -> "JobItem":
        return cls(
            id=d["id"],
            source=str(d["source"]),
            dest=str(d["dest"]),
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


# ─────────────────────────────────────────────────────────────────
# Eventos espontáneos del server (streaming)
# ─────────────────────────────────────────────────────────────────
@dataclass
class JobStarted:
    job: JobItem


@dataclass
class JobProgress:
    id: str
    copied_bytes: int
    total_bytes: int

    @property
    def progress(self) -> float:
        if self.total_bytes == 0:
            return 0.0
        return min(1.0, self.copied_bytes / self.total_bytes)


@dataclass
class JobCompleted:
    job: JobItem


@dataclass
class JobFailed:
    id: str
    error: str


@dataclass
class JobPaused:
    id: str


@dataclass
class JobResumed:
    id: str


@dataclass
class JobCancelled:
    id: str


ServerEvent = (
    JobStarted | JobProgress | JobCompleted | JobFailed
    | JobPaused | JobResumed | JobCancelled
)


def parse_event(data: dict) -> Optional[ServerEvent]:
    """Parsea un mensaje del server. Devuelve None si no es un evento válido."""
    event = data.get("event")
    payload = data.get("data", {})
    if event == "job_started":
        return JobStarted(JobItem.from_dict(payload))
    if event == "job_progress":
        return JobProgress(
            id=payload["id"],
            copied_bytes=int(payload.get("copied_bytes", 0)),
            total_bytes=int(payload.get("total_bytes", 0)),
        )
    if event == "job_completed":
        return JobCompleted(JobItem.from_dict(payload))
    if event == "job_failed":
        return JobFailed(id=payload["id"], error=payload.get("error", ""))
    if event == "job_paused":
        return JobPaused(id=payload)
    if event == "job_resumed":
        return JobResumed(id=payload)
    if event == "job_cancelled":
        return JobCancelled(id=payload)
    return None
