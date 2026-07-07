"""JSON-RPC client for the csfd daemon.

Main API:
    >>> from csf_client import DaemonClient, EnqueueItem, Operation
    >>> with DaemonClient() as c:
    ...     c.ping()
    ...     c.enqueue([EnqueueItem("/a", "/b", Operation.COPY)])
    ...     c.pause()           # pause all
    ...     c.pause(job_id="x") # pause one
    ...     for ev in c.subscribe():
    ...         print(ev)

Connection:
- Unix socket at `$XDG_RUNTIME_DIR/copysecurefast.sock` by default.
- Override with env `CSF_SOCKET` or the `socket_path` argument.
- Raises `DaemonConnectionError` if the daemon is not running.
- Lazy reconnect: if the connection drops, the next call reopens it.

Thread safety: not thread-safe. Use one client per thread, or add an
external lock.
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
    """Resolves the socket path.

    Priority:
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
    """Client for the csfd daemon. Use as a context manager or directly.

    The client keeps a single long-lived connection to the daemon.
    Methods like `ping` or `enqueue` send a request and read the next
    line as the response. Spontaneous events (job_started, job_progress,
    etc.) are read in the background and queued in an internal `queue.Queue`
    accessible via `subscribe()`.
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
        # Thread-safe queue for events read off the socket.
        self._events: "queue.Queue[ServerEvent]" = queue.Queue()
        # The reader thread also pushes raw response dicts so request/response
        # correlation works over the same connection.
        self._reader_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

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
        """Opens the Unix socket. Raises DaemonConnectionError on failure."""
        if self._sock is not None:
            return
        if not os.path.exists(self.socket_path):
            raise DaemonConnectionError(
                f"socket not found: {self.socket_path} "
                f"(is the csfd daemon running? Try `csfd` in another terminal)"
            )
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(self.recv_timeout)
            s.connect(self.socket_path)
            self._sock = s
            self._start_reader()
        except OSError as e:
            raise DaemonConnectionError(
                f"could not connect to {self.socket_path}: {e}"
            ) from e

    def close(self) -> None:
        self._stop.set()
        if self._reader_thread is not None:
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
        """Starts a background thread that reads lines from the socket.

        Lines that look like responses (have "event" with one of the
        response names) are routed to the request/response correlator
        (via a separate tuple queue). All other event lines are converted
        to `ServerEvent` instances and pushed onto the events queue.
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
                        log.warning("non-JSON line from daemon: %r", line)
                        continue
                    if "event" in data and data["event"] not in (
                        "queue_snapshot", "pong", "enqueued", "error",
                    ):
                        ev = parse_event(data)
                        if ev is not None:
                            self._events.put(ev)
                    else:
                        # Response to a previous request: route via
                        # a tagged tuple so the awaiting call picks it up.
                        self._events.put(("response", data))
            except (OSError, ValueError) as e:
                if not self._stop.is_set():
                    log.warning("reader terminated: %s", e)
            finally:
                try:
                    f.close()
                except Exception:
                    pass

        self._reader_thread = threading.Thread(target=reader, daemon=True)
        self._reader_thread.start()

    def _send_and_await(self, method: str, params: Optional[dict] = None, timeout: float = 5.0) -> dict:
        """Sends a request and waits for the response."""
        # Important: with serde's `#[serde(tag = "method", content = "params")]`,
        # unit variants (like Ping) MUST NOT send a "params" key. Sending {}
        # makes the daemon try to deserialize Ping { params: {} } which fails
        # with "expected unit variant". Only add params if it has data.
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
                # Wait for the next response (not event)
                deadline = time.time() + timeout
                while time.time() < deadline:
                    try:
                        item = self._events.get(timeout=max(0.1, deadline - time.time()))
                    except queue.Empty:
                        continue
                    if isinstance(item, tuple) and item[0] == "response":
                        return item[1]
                    # It was an event, re-enqueue and keep waiting
                    self._events.put(item)
                raise DaemonConnectionError("timeout waiting for response")
            except (OSError, DaemonConnectionError) as e:
                self.close()
                if attempt == 0 and self.auto_reconnect:
                    log.warning("reconnecting to daemon after error: %s", e)
                    time.sleep(0.1)
                    continue
                raise DaemonConnectionError(f"communication with daemon failed: {e}") from e

    # ── public API ──────────────────────────────────────────────────
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
        """Pauses one job (or all if job_id is None). Returns the count affected."""
        resp = self._send_and_await("pause", {"job_id": job_id})
        return self._extract_count(resp)

    def resume(self, job_id: Optional[str] = None) -> int:
        resp = self._send_and_await("resume", {"job_id": job_id})
        return self._extract_count(resp)

    def cancel(self, job_id: Optional[str] = None) -> int:
        resp = self._send_and_await("cancel", {"job_id": job_id})
        return self._extract_count(resp)

    def _extract_count(self, resp: dict) -> int:
        # The server currently returns a {"event": "error", "data": {"message": "..."}}
        # reply for control actions, with the count embedded in the message.
        # Best-effort parse until we add a dedicated response variant.
        msg = resp.get("data", {}).get("message", "")
        import re
        m = re.search(r"(\d+)\s+job", msg)
        if m:
            return int(m.group(1))
        return 0

    # ── event subscription ───────────────────────────────────────────
    def subscribe(self, block: bool = True, timeout: float = 1.0) -> Iterator[ServerEvent]:
        """Iterates over server events (job_started, job_progress, etc).

        Useful for the UI to update in real time without polling.
        """
        while True:
            try:
                item = self._events.get(timeout=timeout, block=block)
            except queue.Empty:
                if not block:
                    return
                continue
            if isinstance(item, tuple):
                # Response to a previous request: re-enqueue for the waiter
                self._events.put(item)
                if not block:
                    return
                continue
            yield item

    def drain_events(self) -> list[ServerEvent]:
        """Returns all pending events without blocking."""
        events = []
        while True:
            try:
                item = self._events.get_nowait()
            except queue.Empty:
                break
            if isinstance(item, tuple):
                # Response to a request: re-enqueue
                self._events.put(item)
                break
            events.append(item)
        return events
