"""Cliente JSON-RPC del daemon csfd.

API principal:
    >>> from csf_client import DaemonClient, EnqueueItem, Operation
    >>> with DaemonClient() as c:
    ...     c.ping()
    ...     c.enqueue([EnqueueItem("/a", "/b", Operation.COPY)])
    ...     queue = c.get_queue()

Conexión:
- Socket Unix en `$XDG_RUNTIME_DIR/copysecurefast.sock` por defecto.
- Override con env `CSF_SOCKET` o argumento `socket_path`.
- Si el daemon no está corriendo, lanza `DaemonConnectionError`.
- Reconexión lazy: si la conexión se cae, la siguiente llamada reabre.

Thread-safety: el cliente no es thread-safe. Para uso desde múltiples
hilos usar un cliente por hilo, o agregar un lock externo.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
from contextlib import AbstractContextManager
from typing import Iterable, List, Optional, Union

from csf_client.exceptions import (
    DaemonConnectionError,
    DaemonProtocolError,
    DaemonResponseError,
)
from csf_client.protocol import EnqueueItem, JobItem

log = logging.getLogger("csf_client")

DEFAULT_SOCKET_NAME = "copysecurefast.sock"
DEFAULT_RECV_TIMEOUT_S = 5.0


def _default_socket_path() -> str:
    """Resuelve el path del socket.

    Prioridad:
    1. Env `CSF_SOCKET`
    2. `$XDG_RUNTIME_DIR/copysecurefast.sock`
    3. `/tmp/copysecurefast.sock` (fallback para sistemas sin XDG)
    """
    env = os.environ.get("CSF_SOCKET")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, DEFAULT_SOCKET_NAME)
    return os.path.join("/tmp", DEFAULT_SOCKET_NAME)


class DaemonClient(AbstractContextManager):
    """Cliente del daemon csfd. Usar como context manager o directamente."""

    def __init__(
        self,
        socket_path: Optional[str] = None,
        recv_timeout: float = DEFAULT_RECV_TIMEOUT_S,
        auto_reconnect: bool = True,
    ):
        self.socket_path = socket_path or _default_socket_path()
        self.recv_timeout = recv_timeout
        self.auto_reconnect = auto_reconnect
        self._sock: Optional[socket.socket] = None

    # ── context manager ──────────────────────────────────────────────
    def __enter__(self) -> "DaemonClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
        # Best-effort cleanup si el usuario olvidó `with`.
        try:
            self.close()
        except Exception:
            pass

    # ── lifecycle ────────────────────────────────────────────────────
    def connect(self) -> None:
        """Abre el socket Unix. Lanza DaemonConnectionError si falla."""
        if self._sock is not None:
            return
        if not os.path.exists(self.socket_path):
            raise DaemonConnectionError(
                f"socket no encontrado: {self.socket_path} "
                f"(¿daemon csfd corriendo? Probá `csfd` en otra terminal)"
            )
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.recv_timeout)
            s.connect(self.socket_path)
            self._sock = s
        except OSError as e:
            raise DaemonConnectionError(
                f"no se pudo conectar a {self.socket_path}: {e}"
            ) from e

    def close(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ── I/O de bajo nivel ───────────────────────────────────────────
    def _send_request(self, method: str, params: Optional[dict] = None) -> dict:
        """Envía un request y devuelve el dict de la respuesta.

        Reabre el socket si se cayó (auto_reconnect=True).

        Importante: serde con `#[serde(tag = "method", content = "params")]`
        exige que cuando el variant no tiene fields (como Ping), NO se
        envíe la clave `params`. Si la mandamos como `{}`, el daemon
        intenta deserializar `Request::Ping { params: {} }` y falla con
        "expected unit variant". Solo agregamos `params` si hay datos.
        """
        if params:
            msg = {"method": method, "params": params}
        else:
            msg = {"method": method}
        payload = (json.dumps(msg) + "\n").encode("utf-8")

        for attempt in range(2):
            try:
                if self._sock is None:
                    self.connect()
                assert self._sock is not None  # para el type checker
                self._sock.sendall(payload)
                return self._read_response()
            except (OSError, DaemonConnectionError) as e:
                self.close()
                if attempt == 0 and self.auto_reconnect:
                    log.warning("reconectando al daemon tras error: %s", e)
                    time.sleep(0.1)
                    continue
                raise DaemonConnectionError(f"comunicación con daemon falló: {e}") from e
        # Inalcanzable: el bucle siempre retorna o raise.
        raise DaemonConnectionError("loop de envío terminó sin resultado")

    def _read_response(self) -> dict:
        """Lee una línea JSON del socket."""
        if self._sock is None:
            raise DaemonConnectionError("socket cerrado")
        buf = bytearray()
        try:
            while True:
                chunk = self._sock.recv(4096)
                if not chunk:
                    raise DaemonConnectionError(
                        "daemon cerró la conexión sin responder"
                    )
                buf.extend(chunk)
                if b"\n" in buf:
                    line, _, _ = buf.partition(b"\n")
                    text = line.decode("utf-8").strip()
                    if not text:
                        raise DaemonProtocolError("respuesta vacía del daemon")
                    try:
                        return json.loads(text)
                    except json.JSONDecodeError as e:
                        raise DaemonProtocolError(
                            f"JSON inválido del daemon: {e!r} en {text!r}"
                        ) from e
        except socket.timeout as e:
            raise DaemonConnectionError(
                f"timeout ({self.recv_timeout}s) leyendo respuesta del daemon"
            ) from e

    # ── dispatch tipado ─────────────────────────────────────────────
    def _dispatch(self, event: str, data: dict) -> dict:
        """Valida que la respuesta no sea un error, devuelve `data`."""
        if event == "error":
            raise DaemonResponseError(data.get("message", "error desconocido"))
        return data

    # ── API pública ──────────────────────────────────────────────────
    def ping(self) -> bool:
        """Ping. Devuelve True si el daemon responde."""
        resp = self._send_request("ping")
        self._dispatch(resp.get("event", ""), resp.get("data", {}))
        return resp.get("event") == "pong"

    def get_queue(self) -> List[JobItem]:
        """Snapshot de la cola actual."""
        resp = self._send_request("get_queue")
        data = self._dispatch(resp.get("event", ""), resp.get("data", {}))
        return [JobItem.from_dict(j) for j in data.get("jobs", [])]

    def enqueue(self, items: Iterable[EnqueueItem]) -> int:
        """Encola uno o más items. Devuelve la cantidad aceptada."""
        items_list = [it.to_dict() for it in items]
        resp = self._send_request("enqueue", {"items": items_list})
        data = self._dispatch(resp.get("event", ""), resp.get("data", {}))
        return int(data.get("count", 0))

    def set_throttle(self, bytes_per_second: int) -> int:
        """Ajusta el limitador de velocidad. 0 = sin límite."""
        resp = self._send_request("set_throttle", {"bytes_per_second": int(bytes_per_second)})
        data = self._dispatch(resp.get("event", ""), resp.get("data", {}))
        return int(data.get("global_speed_bps", 0))

    def cancel(self, job_id: Optional[str] = None) -> None:
        """Cancela un job (o todos si job_id=None). Stub en el spike actual."""
        resp = self._send_request("cancel", {"job_id": job_id})
        self._dispatch(resp.get("event", ""), resp.get("data", {}))

    def pause(self, job_id: Optional[str] = None) -> None:
        """Pausa un job (o todos). Stub en el spike actual."""
        resp = self._send_request("pause", {"job_id": job_id})
        self._dispatch(resp.get("event", ""), resp.get("data", {}))

    def resume(self, job_id: Optional[str] = None) -> None:
        """Reanuda un job (o todos). Stub en el spike actual."""
        resp = self._send_request("resume", {"job_id": job_id})
        self._dispatch(resp.get("event", ""), resp.get("data", {}))
