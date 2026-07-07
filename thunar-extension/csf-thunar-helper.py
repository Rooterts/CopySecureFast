"""CopySecureFast — Helper CLI para integración con file managers.

Tres modos de invocación desde Thunar (vía .uca):

1. Click derecho sobre archivos/carpetas seleccionados:
   `csf-thunar-helper copy <sources>... <dest_dir>`
   → encola copias. El helper infiere el destino de Thunar.

2. Click derecho sobre una carpeta con archivos "copiados previamente":
   `csf-thunar-helper paste <dest_dir>`
   → encola lo que esté en `~/.cache/csf/clipboard.json` (gestionado
     por copy/cut).

3. Mover (cortar):
   `csf-thunar-helper cut <sources>... <dest_dir>`

Comandos auxiliares:
- `csf-thunar-helper queue`    abre la ventana flotante
- `csf-thunar-helper status`   texto de la cola
- `csf-thunar-helper ping`     health check
- `csf-thunar-helper throttle <bps>`  limitador

El helper es BLOQUEANTE pero rápido: encola y termina. La UI/tray
se actualiza en vivo vía eventos.
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


# Archivo donde el helper persiste la "cola del portapapeles CSF"
# (qué archivos fueron copiados/cortados para pegar después).
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
    """Lee el clipboard CSF. Formato:
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
    """Encola cada source como un job apuntando a dest.

    - Si `dest` es un directorio existente (o no existe pero su parent sí),
      cada source va a `dest/<basename>`. Si hay colisión, se renombra.
    - Si `dest` no existe, el primer source va a dest y los demás se
      descartan con warning (modo "rename").
    """
    dest_path = Path(dest)
    items: list[EnqueueItem] = []

    if dest_path.is_dir():
        for s in sources:
            src = Path(s)
            if not src.exists():
                print(f"aviso: {src} no existe; se omite", file=sys.stderr)
                continue
            target = dest_path / src.name
            if target.exists() or target.resolve() == src.resolve():
                # Colisión: renombrar a "name (1).ext", "name (2).ext"...
                stem, suffix = target.stem, target.suffix
                for i in range(1, 1000):
                    candidate = dest_path / f"{stem} ({i}){suffix}"
                    if not candidate.exists():
                        target = candidate
                        break
            items.append(EnqueueItem(str(src), str(target), op))
    else:
        # Modo rename: solo el primer source
        if len(sources) > 1:
            print(
                f"aviso: destino {dest} no es directorio; solo el primero de {len(sources)}",
                file=sys.stderr,
            )
        if sources:
            src = Path(sources[0])
            if not src.exists():
                print(f"aviso: {src} no existe", file=sys.stderr)
                return 0
            items.append(EnqueueItem(str(src), dest, op))

    if not items:
        return 0
    n = client.enqueue(items)
    print(f"csfd: encolados {n} jobs ({op.value})")
    return n


def _notify_tray_show() -> None:
    """Pide al tray que muestre la ventana. Si el tray no está corriendo,
    no hace nada (la UI puede abrirse manualmente con csf-ui).
    """
    # Protocolo simple: el tray escucha un FIFO en $XDG_RUNTIME_DIR.
    # Si no existe, lo creamos.
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
# Comandos
# ─────────────────────────────────────────────────────────────────
def cmd_copy(args: argparse.Namespace) -> int:
    """`csf-thunar-helper copy <sources>...`: marca en el clipboard CSF.

    NO encola todavía. El encolado real ocurre en `paste` cuando el
    usuario elige la carpeta destino. Esto es el flujo TeraCopy.
    """
    sources = [s for s in args.sources if os.path.exists(s)]
    if not sources:
        print("csfd: ninguno de los paths existe", file=sys.stderr)
        return 1
    _save_clipboard("copy", sources)
    print(f"csfd: {len(sources)} item(s) en clipboard CSF. Pegar con CopySecureFast en destino.")
    return 0


def cmd_cut(args: argparse.Namespace) -> int:
    """`csf-thunar-helper cut <sources>...`: marca en el clipboard CSF como move."""
    sources = [s for s in args.sources if os.path.exists(s)]
    if not sources:
        print("csfd: ninguno de los paths existe", file=sys.stderr)
        return 1
    _save_clipboard("cut", sources)
    print(f"csfd: {len(sources)} item(s) marcados para mover. Pegar con CopySecureFast en destino.")
    return 0


def cmd_paste(args: argparse.Namespace) -> int:
    """`csf-thunar-helper paste <dest_dir>`: encola lo que esté en el clipboard CSF."""
    dest = args.dest
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    clip = _load_clipboard()
    if not clip["items"]:
        print("csfd: clipboard CSF vacío. Hacé 'Copiar con CSF' primero.", file=sys.stderr)
        return 1
    op = Operation.MOVE if clip["op"] == "cut" else Operation.COPY
    try:
        with DaemonClient(socket_path=sock) as c:
            _enqueue_paths(c, op, dest, clip["items"])
            # Vaciar el clipboard después de pegar
            if op == Operation.MOVE:
                _save_clipboard("copy", [])
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    _notify_tray_show()
    return 0


def cmd_queue(_args) -> int:
    """Abre la ventana flotante (csf-ui)."""
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["CSF_SOCKET"] = sock
    env["CSF_UI_LAUNCHED"] = "1"
    try:
        from csf_ui.main import main as csf_ui_main
        return csf_ui_main()
    except ImportError as e:
        print(f"csfd: no se pudo abrir la UI ({e})", file=sys.stderr)
        return 3


def cmd_status(_args) -> int:
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            jobs = c.get_queue()
            if not jobs:
                print("csfd: cola vacía")
                return 0
            print(f"csfd: {len(jobs)} jobs en cola:")
            for j in jobs:
                pct = f"{j.progress * 100:.0f}%" if j.total_bytes else "—"
                err = f"  ERR: {j.error}" if j.error else ""
                print(f"  [{j.state.value:10s}] {j.basename:30s} {pct:>5s}  {j.source} → {j.dest}{err}")
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_ping(_args) -> int:
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            if c.ping():
                print("csfd: pong (daemon vivo)")
                return 0
            return 1
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2


def cmd_throttle(args) -> int:
    sock = _find_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            applied = c.set_throttle(args.bytes_per_second)
            print(f"csfd: throttle aplicado = {applied} B/s")
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
        description="Helper CLI de CopySecureFast (integración con Thunar y otros)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("copy", help="Copiar con CopySecureFast (Thunar usa %%F y carpeta destino)")
    sp.add_argument("sources", nargs="+", help="Sources y destino")
    sp.set_defaults(func=cmd_copy)

    sp = sub.add_parser("cut", help="Mover con CopySecureFast (Thunar usa %%F y carpeta destino)")
    sp.add_argument("sources", nargs="+", help="Sources y destino")
    sp.set_defaults(func=cmd_cut)

    sp = sub.add_parser("paste", help="Pegar con CopySecureFast (clipboard CSF)")
    sp.add_argument("dest", help="Carpeta destino")
    sp.set_defaults(func=cmd_paste)

    sp = sub.add_parser("queue", help="Abrir la ventana flotante de cola")
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("status", help="Mostrar la cola en formato texto")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("ping", help="Health check del daemon")
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("throttle", help="Ajustar limitador de velocidad (B/s)")
    sp.add_argument("bytes_per_second", type=int, help="0 = sin límite")
    sp.set_defaults(func=cmd_throttle)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
