"""CopySecureFast — cliente Python para el daemon csfd.

Importable desde adaptadores (Nemo, Nautilus, Thunar, Caja) y desde la UI.
Sin dependencias externas: solo stdlib + json + socket.
"""

from csf_client.daemon import DaemonClient
from csf_client.protocol import (
    EnqueueItem,
    JobItem,
    JobState,
    Operation,
)
from csf_client.exceptions import (
    DaemonError,
    DaemonConnectionError,
    DaemonProtocolError,
    DaemonResponseError,
)

__all__ = [
    "DaemonClient",
    "EnqueueItem",
    "JobItem",
    "JobState",
    "Operation",
    "DaemonError",
    "DaemonConnectionError",
    "DaemonProtocolError",
    "DaemonResponseError",
]

__version__ = "0.1.0"
