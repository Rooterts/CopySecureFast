"""CopySecureFast CLI helper for integration with file managers.

Three invocation modes from Thunar (via .uca):

1. Right-click on files/folders:
   `csf-thunar-helper copy <sources>... <dest_dir>`
   -> enqueues copies. The helper infers the destination from Thunar.

2. Right-click on a folder with previously "copied" items:
   `csf-thunar-helper paste <dest_dir>`
   -> enqueues whatever is in `~/.cache/csf/clipboard.json` (managed
     by copy/cut).

3. Move (cut):
   `csf-thunar-helper cut <sources>... <dest_dir>`

Auxiliary commands:
- `csf-thunar-helper queue`    opens the floating window
- `csf-thunar-helper status`   text dump of the queue
- `csf-thunar-helper ping`     health check
- `csf-thunar-helper throttle <bps>`  speed limit

The helper is BLOCKING but fast: enqueue and exit. The UI/tray updates
in real time via daemon events.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from csf_client import DaemonClient, DaemonConnectionError, EnqueueItem, Operation  # noqa: E402


# File where the helper persists the "CSF clipboard queue"
# (which files were copied/cut for later paste).
CLIPBOARD_FILE = Path.home() / ".cache" / "csf" / "clipboard.json"


def _find_socket() -> str | None:
    env = os.environ.get("CSF_SOCKET")
    if env and os.path.exists(env):
        return env
    xdg = os.environ.get("XDG_RUNTIME_DIR")
    if xdg:
        p = os.path.join(xdg, "copysecurefast.sock")
        if os.path.exists(p):
            return p
    p = f"/run/user/{os.getuid()}/copysecurefast.sock"
    if os.path.exists(p):
        return p
    p = "/tmp/copysecurefast.sock"
    if os.path.exists(p):
        return p
    return None


def _load_clipboard() -> dict:
    """Reads the CSF clipboard. Format:
    {"op": "copy"|"cut", "items": ["/path/1", "/path/2", ...]}
    """
    if not CLIPBOARD_FILE.exists():
        return {"op": "copy", "items": []}
    try:
        return json.loads(CLIPBOARD_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return {"op": "copy", "items": []}


def _save_clipboard(op: str, items: list[str]) -> None:
    CLIPBOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
    CLIPBOARD_FILE.write_text(json.dumps({"op": op, "items": items}))


def _enqueue_paths(client: DaemonClient, op: Operation, dest: str, sources: list[str]) -> int:
    """Enqueues each source as a job pointing to dest.

    If the source is a directory, expands it recursively and creates
    one job per file (each one with destination =
    <dest>/<basename_source_dir>/<relative_path>). If the source is a
    file and dest is a directory, it goes to <dest>/<basename>.
    """
    dest_path = Path(dest)
    items: list[EnqueueItem] = []

    if dest_path.is_dir():
        for s in sources:
            src = Path(s)
            if not src.exists():
                print(f"warning: {src} does not exist; skipping", file=sys.stderr)
                continue
            if src.is_file():
                target = dest_path / src.name
                if target.exists() or target.resolve() == src.resolve():
                    stem, suffix = target.stem, target.suffix
                    for i in range(1, 1000):
                        candidate = dest_path / f"{stem} ({i}){suffix}"
                        if not candidate.exists():
                            target = candidate
                            break
                items.append(EnqueueItem(str(src), str(target), op))
            elif src.is_dir():
                # Expand recursively: each file goes to
                # <dest>/<basename_src>/<relative_path>
                subdir = dest_path / src.name
                for child in _walk_files(src):
                    rel = child.relative_to(src)
                    target = subdir / rel
                    items.append(EnqueueItem(str(child), str(target), op))
    else:
        if len(sources) > 1:
            print(
                f"warning: destination {dest} is not a directory; only the first of {len(sources)} sources will be enqueued",
                file=sys.stderr,
            )
        if sources:
            src = Path(sources[0])
            if not src.exists():
                print(f"warning: {src} does not exist", file=sys.stderr)
                return 0
            if src.is_dir():
                # Rename mode: copy the whole folder to the destination
                items.append(EnqueueItem(str(src), dest, op))
            else:
                items.append(EnqueueItem(str(src), dest, op))

    if not items:
        return 0
    n = client.enqueue(items)
    print(f"csfd: enqueued {n} jobs ({op.value})")
    return n


def _walk_files(root: Path):
    """Recursively iterates over all files under `root`."""
    for p in sorted(root.rglob("*")):
        if p.is_file():
            yield p


def _notify_tray_show() -> None:
    """Asks the tray to show its window. No-op if the tray isn't running."""
    # The tray listens on a FIFO at $XDG_RUNTIME_DIR/csf-tray.sock.
    fifo = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp")) / "csf-tray.sock"
    if not fifo.exists():
        return
    try:
        import socket as _s
        s = _s.socket(_s.AF_UNIX, _s.SOCK_STREAM)
        s.settimeout(0.5)
        s.connect(str(fifo))
        s.sendall(b"show\n")
        s.close()
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────
# Commands
# ─────────────────────────────────────────────────────────────────
def cmd_copy(args: argparse.Namespace) -> int:
    """`csf-thunar-helper copy <sources>...`: marks files in the CSF clipboard.

    Does NOT enqueue yet. The actual enqueue happens in `paste` when
    the user picks the destination folder. This is the TeraCopy flow.
    """
    sources = [s for s in args.sources if os.path.exists(s)]
    if not sources:
        print("csfd: none of the paths exist", file=sys.stderr)
        return 1
    _save_clipboard("copy", sources)
    print(f"csfd: {len(sources)} item(s) in CSF clipboard. Use Paste with CopySecureFast in the destination.")
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    """`csf-thunar-helper cut <sources>...`: marks files as move in the CSF clipboard."""
    sources = [s for s in args.sources if os.path.exists(s)]
    if not sources:
        print("csfd: none of the paths exist", file=sys.stderr)
        return 1
    _save_clipboard("cut", sources)
    print(f"csfd: {len(sources)} item(s) marked for move. Use Paste with CopySecureFast in the destination.")
    return 0


def cmd_paste(args: argparse.Namespace) -> int:
    """`csf-thunar-helper paste <dest_dir>`: enqueues whatever is in the CSF clipboard."""
    dest = args.dest
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon not available. Start it with `csfd`.", file=sys.stderr)
        return 2
    clip = _load_clipboard()
    if not clip["items"]:
        print("csfd: CSF clipboard empty. Use 'Copy with CSF' first.", file=sys.stderr)
        return 1
    op = Operation.MOVE if clip["op"] == "cut" else Operation.COPY
    try:
        with DaemonClient(socket_path=sock) as c:
            _enqueue_paths(c, op, dest, clip["items"])
            # Empty the clipboard after pasting.
            if op == Operation.MOVE:
                _save_clipboard("copy", [])
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    _notify_tray_show()
    return 0


def cmd_queue(_args) -> int:
    """Opens the floating queue window (csf-ui)."""
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon not available", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["CSF_SOCKET"] = sock
    env["CSF_UI_LAUNCHED"] = "1"
    try:
        from csf_ui.main import main as csf_ui_main
        return csf_ui_main()
    except ImportError as e:
        print(f"csfd: could not open UI ({e})", file=sys.stderr)
        return 3


def cmd_status(_args) -> int:
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon not available", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            jobs = c.get_queue()
            if not jobs:
                print("csfd: empty queue")
                return 0
            print(f"csfd: {len(jobs)} job(s) in queue:")
            for j in jobs:
                pct = f"{j.progress * 100:.0f}%" if j.total_bytes else "—"
                err = f"  ERR: {j.error}" if j.error else ""
                print(f"  [{j.state.value:10s}] {j.basename:30s} {pct:>5s}  {j.source} -> {j.dest}{err}")
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_ping(_args) -> int:
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon not available", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            if c.ping():
                print("csfd: pong (daemon alive)")
                return 0
            return 1
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2


def cmd_throttle(args) -> int:
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon not available", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            applied = c.set_throttle(args.bytes_per_second)
            print(f"csfd: throttle applied = {applied} B/s")
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    return 0


# ─────────────────────────────────────────────────────────────────
# Parser
# ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="csf-thunar-helper",
        description="CopySecureFast CLI helper (Thunar/file manager integration)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("copy", help="Copy with CopySecureFast (Thunar uses %%F and target folder)")
    sp.add_argument("sources", nargs="+", help="Sources and destination")
    sp.set_defaults(func=cmd_copy)

    sp = sub.add_parser("cut", help="Move with CopySecureFast (Thunar uses %%F and target folder)")
    sp.add_argument("sources", nargs="+", help="Sources and destination")
    sp.set_defaults(func=cmd_cut)

    sp = sub.add_parser("paste", help="Paste with CopySecureFast (CSF clipboard)")
    sp.add_argument("dest", help="Destination folder")
    sp.set_defaults(func=cmd_paste)

    sp = sub.add_parser("queue", help="Open the floating queue window")
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("status", help="Show the queue as text")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("ping", help="Daemon health check")
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("throttle", help="Set speed limit (B/s)")
    sp.add_argument("bytes_per_second", type=int, help="0 = unlimited")
    sp.set_defaults(func=cmd_throttle)

    return p

def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
