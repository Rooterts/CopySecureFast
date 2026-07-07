"""Cliente JSON-RPC del daemon csfd.

API principal:
    >>> from csf_client import DaemonClient, EnqueueItem, Operation
    >>> with DaemonClient() as c:
    ...     c.ping()
    ...     c.enqueue([EnqueueItem("/a", "/b", Operation.COPY)])
    ...     c.pause()           # pausa todos
    ...     c.pause(job_id="x") # pausa uno
    ...     for ev in c.subscribe():
    ...         print(ev)

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
import queue
import socket
import threading
import time
from contextlib import AbstractContextManager
from typing import Iterable, Iterator, List, Optional

from csf_client.exceptions import (
    DaemonConnectionError,
    DaemonProtocolError,
    DaemonResponseError,
)
from csf_client.protocol import (
    EnqueueItem,
    JobItem,
    ServerEvent,
    parse_event,
)

log = logging.getLogger("csf_client")

DEFAULT_SOCKET_NAME = "copysecurefast.sock"
DEFAULT_RECV_TIMEOUT_S = 5.0


def _default_socket_path() -> str:
    """Resuelve el path del socket.

    Prioridad:
    1. Env `CSF_SOCKET`
    2. `$XDG_RUNTIME_DIR/copysecurefast.sock`
    3. `/run/user/<uid>/copysecurefast.sock`
    4. `/tmp/copysecurefast.sock` (fallback)
    """
    env = os.environ.get("CSF_SOCKET")
    if env:
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        return os.path.join(xdg, DEFAULT_SOCKET_NAME)
    return os.path.join(f"/run/user/{os.getuid()}", DEFAULT_SOCKET_NAME)


class DaemonClient(AbstractContextManager):
    """Cliente del daemon csfd. Usar como context manager o directamente.

    El cliente mantiene UNA conexión long-lived con el daemon. Cuando
    llamás a métodos como `ping` o `enqueue`, envía un request y lee
    la respuesta de la línea siguiente. Los eventos espontáneos del
    daemon (job_started, job_progress, etc.) se leen en background
    y se encolan en una cola interna accesible vía `subscribe()`.
    """

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
        # Cola thread-safe para eventos espontáneos del server.
        self._events: "queue.Queue[ServerEvent]" = queue.Queue()
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # Para correlacionar responses con requests, el server responde
        # en orden FIFO por conexión. No necesitamos IDs en este spike.

    # ── context manager ──────────────────────────────────────────────
    def __enter__(self) -> "DaemonClient":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def __del__(self):
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
            self._start_reader()
        except OSError as e:
            raise DaemonConnectionError(
                f"no se pudo conectar a {self.socket_path}: {e}"
            ) from e

    def close(self) -> None:
        self._stop.set()
        if self._reader_thread is not None:
            # El reader termina cuando el socket se cierre
            self._reader_thread.join(timeout=1.0)
            self._reader_thread = None
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    @property
    def connected(self) -> bool:
        return self._sock is not None

    # ── reader thread ────────────────────────────────────────────────
    def _start_reader(self) -> None:
        """Inicia un thread que lee líneas del socket y las dispatcha.

        La primera línea después de un request es su respuesta. Las
        líneas siguientes, si llegaron espontáneamente, son eventos.
        Para simplificar, el reader pone TODAS las líneas en la cola
        de eventos, y los métodos request/response sacan la primera
        línea como respuesta y devuelven el resto a la cola.
        """
        self._stop.clear()

        def reader():
            assert self._sock is not None
            f = self._sock.makefile("r", encoding="utf-8", newline="\n")
            try:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        log.warning("línea no-JSON del daemon: %r", line)
                        continue
                    # Si tiene "method" o "event" con un id de response,
                    # se procesa como respuesta. Si tiene "event" puro
                    # (job_started, etc), es un evento.
                    if "event" in data and data["event"] not in (
                        "queue_snapshot", "pong", "enqueued", "error",
                    ):
                        ev = parse_event(data)
                        if ev is not None:
                            self._events.put(ev)
                    else:
                        # Es un response a un request previo. Encolamos
                        # en una cola aparte y el método que espera
                        # respuesta lo lee de ahí.
                        self._events.put(("response", data))
            except (OSError, ValueError) as e:
                if not self._stop.is_set():
                    log.warning("reader terminado: %s", e)
            finally:
                try:
                    f.close()
                except Exception:
                    pass

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()

    def _send_and_await(self, method: str, params: Optional[dict] = None, timeout: float = 5.0) -> dict:
        """Envía un request y espera la respuesta."""
        if params:
            msg = {"method": method, "params": params}
        else:
            msg = {"method": method}
        payload = (json.dumps(msg) + "\n").encode("utf-8")

        for attempt in range(2):
            try:
                if self._sock is None:
                    self.connect()
                assert self._sock is not None
                self._sock.sendall(payload)
                # Esperar la siguiente respuesta (no evento)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        item = self._events.get(timeout=max(0.1, deadline - time.time()))
                    except queue.Empty:
                        continue
                    if isinstance(item, tuple) and item[0] == "response":
                        return item[1]
                    # Si era un evento, reencolamos y seguimos esperando
                    self._events.put(item)
                raise DaemonConnectionError("timeout esperando respuesta")
            except (OSError, DaemonConnectionError) as e:
                self.close()
                if attempt == 0 and self.auto_reconnect:
                    log.warning("reconectando al daemon tras error: %s", e)
                    time.sleep(0.1)
                    continue
                raise DaemonConnectionError(f"comunicación con daemon falló: {e}") from e

    # ── API pública ──────────────────────────────────────────────────
    def ping(self) -> bool:
        resp = self._send_and_await("ping")
        return resp.get("event") == "pong"

    def get_queue(self) -> List[JobItem]:
        resp = self._send_and_await("get_queue")
        data = resp.get("data", {})
        return [JobItem.from_dict(j) for j in data.get("jobs", [])]

    def enqueue(self, items: Iterable[EnqueueItem]) -> int:
        items_list = [it.to_dict() for it in items]
        resp = self._send_and_await("enqueue", {"items": items_list})
        data = resp.get("data", {})
        return int(data.get("count", 0))

    def set_throttle(self, bytes_per_second: int) -> int:
        resp = self._send_and_await("set_throttle", {"bytes_per_second": int(bytes_per_second)})
        data = resp.get("data", {})
        return int(data.get("global_speed_bps", 0))

    def pause(self, job_id: Optional[str] = None) -> int:
        """Pausa un job (o todos). Devuelve cantidad afectada."""
        resp = self._send_and_await("pause", {"job_id": job_id})
        # El server devuelve un response "error" con el count en el message.
        # En un futuro, agregaremos un Response::Paused dedicado.
        return self._extract_count(resp)

    def resume(self, job_id: Optional[str] = None) -> int:
        resp = self._send_and_await("resume", {"job_id": job_id})
        return self._extract_count(resp)

    def cancel(self, job_id: Optional[str] = None) -> int:
        resp = self._send_and_await("cancel", {"job_id": job_id})
        return self._extract_count(resp)

    def _extract_count(self, resp: dict) -> int:
        # El server devuelve {"event": "error", "data": {"message": "..."}}
        # con el count en el mensaje. Hacemos un parseo best-effort.
        msg = resp.get("data", {}).get("message", "")
        # Buscar "N job(s)" en el mensaje
        import re
        m = re.search(r"(\d+)\s+job", msg)
        if m:
            return int(m.group(1))
        return 0

    # ── Suscripción a eventos ────────────────────────────────────────
    def subscribe(self, block: bool = True, timeout: float = 1.0) -> Iterator[ServerEvent]:
        """Itera sobre eventos del server (job_started, job_progress, etc).

        Útil para que la UI se actualice en tiempo real sin polling.
        """
        while True:
            try:
                item = self._events.get(timeout=timeout, block=block)
            except queue.Empty:
                if not block:
                    return
                continue
            if isinstance(item, tuple):
                # Es una respuesta a un request, la devolvemos a la cola
                # para que el método que espera la pueda leer.
                self._events.put(item)
                if not block:
                    return
                continue
            yield item

    def drain_events(self) -> list[ServerEvent]:
        """Devuelve todos los eventos pendientes sin bloquear."""
        events = []
        while True:
            try:
                item = self._events.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                # Es respuesta a un request: reencolar
                self._events.put(item)
                break
            events.append(item)
        return events
