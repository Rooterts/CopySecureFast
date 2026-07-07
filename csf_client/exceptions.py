"""Excepciones del cliente csf_client.

Jerarquía:
    DaemonError (base)
    ├── DaemonConnectionError   — no se pudo abrir el socket o se cayó
    ├── DaemonProtocolError     — JSON inválido o no se pudo parsear
    └── DaemonResponseError     — el daemon devolvió {"event": "error", ...}
"""


class DaemonError(Exception):
    """Error base del cliente CopySecureFast."""


class DaemonConnectionError(DaemonError):
    """No se pudo conectar al daemon (socket no existe, permiso denegado, caído)."""


class DaemonProtocolError(DaemonError):
    """El mensaje recibido del daemon no se pudo parsear como JSON-RPC válido."""


class DaemonResponseError(DaemonError):
    """El daemon respondió con un evento de error explícito."""
