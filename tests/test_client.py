"""Tests del cliente csf_client.

Dos niveles:
- Unit tests (rápidos, sin daemon): parsing de protocol, formato de EnqueueItem.
- Integration tests (marcados como `slow`): requieren un daemon csfd corriendo
  en $CSF_SOCKET. Se corren con `pytest -m slow` o `pytest --run-slow`.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from csf_client import (
    DaemonClient,
    DaemonConnectionError,
    DaemonResponseError,
    EnqueueItem,
    JobItem,
    JobState,
    Operation,
)
from csf_client.protocol import EnqueueItem as EI


class TestProtocol(unittest.TestCase):
    """Tests sin daemon: parsing y serialización de mensajes."""

    def test_enqueue_item_serialization(self):
        it = EnqueueItem("/src/a", "/dst/a", Operation.COPY, verify_hash=True)
        d = it.to_dict()
        self.assertEqual(d["source"], "/src/a")
        self.assertEqual(d["dest"], "/dst/a")
        self.assertEqual(d["op"], "copy")
        self.assertTrue(d["verify_hash"])

    def test_operation_serialization_is_lowercase(self):
        # El daemon usa serde rename_all = "lowercase" → tenemos que coincidir.
        self.assertEqual(Operation.COPY.value, "copy")
        self.assertEqual(Operation.MOVE.value, "move")

    def test_job_item_from_dict(self):
        d = {
            "id": "abc",
            "source": "/src",
            "dest": "/dst",
            "op": "copy",
            "state": "completed",
            "total_bytes": 1000,
            "copied_bytes": 1000,
            "hash": "deadbeef",
            "enqueued_at": 100,
            "finished_at": 200,
            "error": None,
            "verify_hash": True,
        }
        j = JobItem.from_dict(d)
        self.assertEqual(j.id, "abc")
        self.assertEqual(j.state, JobState.COMPLETED)
        self.assertEqual(j.op, Operation.COPY)
        self.assertTrue(j.verify_hash)
        self.assertEqual(j.progress, 1.0)
        self.assertTrue(j.is_terminal)

    def test_job_item_progress_unknown_total(self):
        j = JobItem(id="x", source="/a", dest="/b", op=Operation.COPY, state=JobState.PENDING)
        self.assertEqual(j.progress, 0.0)

    def test_job_state_terminal(self):
        for s in (JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED):
            self.assertTrue(s.is_terminal, f"{s} debería ser terminal")
        for s in (JobState.PENDING, JobState.RUNNING, JobState.PAUSED):
            self.assertFalse(s.is_terminal, f"{s} NO debería ser terminal")

    def test_basename(self):
        j = JobItem(
            id="x", source="/foo/bar/baz.txt", dest="/x", op=Operation.COPY, state=JobState.PENDING
        )
        self.assertEqual(j.basename, "baz.txt")


class TestClientConnection(unittest.TestCase):
    """Tests que NO requieren daemon corriendo."""

    def test_missing_socket_raises_connection_error(self):
        # Forzamos un path que no existe.
        c = DaemonClient(socket_path="/tmp/csfd-nonexistent-xyzzy.sock")
        with self.assertRaises(DaemonConnectionError):
            c.ping()

    def test_default_socket_path_resolves(self):
        # El default debe ser un string absoluto.
        c = DaemonClient()
        self.assertTrue(c.socket_path.startswith("/"))


class TestClientIntegration(unittest.TestCase):
    """Tests que requieren el daemon corriendo. Se saltean si no está."""

    @classmethod
    def setUpClass(cls):
        cls.client = DaemonClient()
        try:
            cls.client.ping()
        except DaemonConnectionError as e:
            raise unittest.SkipTest(f"daemon no disponible: {e}")

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    def test_ping(self):
        self.assertTrue(self.client.ping())

    def test_get_queue_returns_list(self):
        jobs = self.client.get_queue()
        self.assertIsInstance(jobs, list)
        for j in jobs:
            self.assertIsInstance(j, JobItem)

    def test_enqueue_and_get_back(self):
        # Encolar un archivo dummy (el daemon no verifica que exista en el spike,
        # pero igualmente va a fallar en el worker loop — eso está bien para el test).
        import tempfile

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"csf_client test payload\n")
            tmp = f.name
        try:
            n = self.client.enqueue(
                [EnqueueItem(tmp, tmp + ".bak", Operation.COPY, verify_hash=False)]
            )
            self.assertEqual(n, 1)

            # El job debería aparecer (estado pending o failed si el path es raro).
            jobs = self.client.get_queue()
            ids = [j.source for j in jobs]
            self.assertTrue(any(tmp in s for s in ids), f"job no encontrado en {ids}")
        finally:
            os.unlink(tmp)

    def test_set_throttle(self):
        # Devuelve la velocidad configurada.
        bps = self.client.set_throttle(12345)
        self.assertEqual(bps, 12345)
        # Volver a 0.
        self.client.set_throttle(0)


if __name__ == "__main__":
    unittest.main()
