"""Exception hierarchy for the csf_client package.

Hierarchy:
    DaemonError (base)
    ├── DaemonConnectionError   - could not open the socket or it dropped
    ├── DaemonProtocolError     - JSON could not be parsed as JSON-RPC
    └── DaemonResponseError     - daemon returned {"event": "error", ...}
"""


class DaemonError(Exception):
    """Base error for the CopySecureFast client."""


class DaemonConnectionError(DaemonError):
    """Could not connect to the daemon (socket missing, permission denied, dropped)."""


class DaemonProtocolError(DaemonError):
    """A message from the daemon could not be parsed as valid JSON-RPC."""


class DaemonResponseError(DaemonError):
    """The daemon responded with an explicit error event."""
