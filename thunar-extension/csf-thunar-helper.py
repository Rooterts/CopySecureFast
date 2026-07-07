"""CopySecureFast — Helper CLI para integrar el daemon con Thunar y otros.

Usos típicos desde acciones .uca de Thunar:

    csf-thunar-helper copy <dest> <file1> [file2 ...]
    csf-thunar-helper move <dest> <file1> [file2 ...]
    csf-thunar-helper queue            # abre la ventana de cola
    csf-thunar-helper throttle <bps>
    csf-thunar-helper status
    csf-thunar-helper ping

Por cada llamada a copy/move, encola UN job por archivo origen. Si el
origen es un directorio, encola solo el directorio (el daemon lo maneja
como un solo job en este spike).

El script es bloqueante: termina después de encolar y (opcionalmente)
abrir la UI. Pensado para ser invocado por Thunar sin dejar procesos
colgados.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

# Asegurar que podemos importar csf_client cuando se ejecuta desde cualquier cwd.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

# GObject/GTK solo se importan si la acción lo requiere (queue, throttle)
# para no pagar el costo en acciones rápidas (copy/move).


def _find_daemon_socket() -> str | None:
    """Busca el socket del daemon en ubicaciones comunes.

    Orden de búsqueda:
    1. $CSF_SOCKET
    2. $XDG_RUNTIME_DIR/copysecurefast.sock
    3. /run/user/$UID/copysecurefast.sock
    4. /tmp/copysecurefast.sock
    """
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


def _op_from_name(name: str) -> "Operation":
    from csf_client import Operation
    return Operation.COPY if name == "copy" else Operation.MOVE


def _enqueue_paths(client, op, dest: str, sources: list[str]) -> int:
    """Encola cada source como un job separado apuntando a dest."""
    dest_path = Path(dest)
    items = []

    if dest_path.is_dir():
        for s in sources:
            src = Path(s)
            if src.resolve() == dest_path.resolve():
                print(
                    f"aviso: origen == destino para {src}; se omite",
                    file=sys.stderr,
                )
                continue
            target = dest_path / src.name
            if target.exists():
                stem, suffix = target.stem, target.suffix
                for i in range(1, 1000):
                    candidate = dest_path / f"{stem} ({i}){suffix}"
                    if not candidate.exists():
                        target = candidate
                        break
            items.append(_make_item(str(src), str(target), op))
    else:
        if len(sources) > 1:
            print(
                f"aviso: destino {dest} no es directorio; "
                f"solo se encolará el primero de {len(sources)} sources",
                file=sys.stderr,
            )
        if sources:
            src = Path(sources[0])
            if src.resolve() == dest_path.resolve():
                print(
                    f"aviso: origen == destino para {src}; se omite",
                    file=sys.stderr,
                )
                return 0
            items.append(_make_item(sources[0], dest, op))

    if not items:
        return 0
    n = client.enqueue(items)
    print(f"csfd: encolados {n} jobs ({op.value})")
    return n


def _make_item(source, dest, op):
    from csf_client import EnqueueItem
    return EnqueueItem(source, dest, op, verify_hash=False)


def cmd_copy_move(args: argparse.Namespace) -> int:
    from csf_client import DaemonClient, DaemonConnectionError
    op = _op_from_name(args.command)
    sock = _find_daemon_socket()
    if sock is None:
        print("csfd: daemon no disponible (¿corriendo?). Probá `csfd`.", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            _enqueue_paths(c, op, args.dest, args.sources)
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_queue(_args: argparse.Namespace) -> int:
    if os.environ.get("CSF_UI_LAUNCHED") == "1":
        return 0
    sock = _find_daemon_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["CSF_UI_LAUNCHED"] = "1"
    env["CSF_SOCKET"] = sock
    try:
        from csf_ui.main import main as csf_ui_main
        return csf_ui_main()
    except ImportError as e:
        print(f"csfd: no se pudo abrir la UI ({e})", file=sys.stderr)
        return 3


def cmd_throttle(args: argparse.Namespace) -> int:
    from csf_client import DaemonClient, DaemonConnectionError
    bps = args.bytes_per_second
    sock = _find_daemon_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            applied = c.set_throttle(bps)
            print(f"csfd: throttle aplicado = {applied} B/s")
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2
    return 0


def cmd_ping(_args: argparse.Namespace) -> int:
    from csf_client import DaemonClient, DaemonConnectionError
    sock = _find_daemon_socket()
    if sock is None:
        print("csfd: daemon no disponible", file=sys.stderr)
        return 2
    try:
        with DaemonClient(socket_path=sock) as c:
            if c.ping():
                print("csfd: pong (daemon vivo)")
                return 0
            print("csfd: respuesta inesperada", file=sys.stderr)
            return 1
    except DaemonConnectionError as e:
        print(f"csfd: {e}", file=sys.stderr)
        return 2


def cmd_status(_args: argparse.Namespace) -> int:
    from csf_client import DaemonClient, DaemonConnectionError
    sock = _find_daemon_socket()
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


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="csf-thunar-helper",
        description="Helper CLI de CopySecureFast (integración con Thunar)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    for name in ("copy", "move"):
        sp = sub.add_parser(name, help=f"{name.capitalize()} archivos vía daemon")
        sp.add_argument("dest", help="Ruta de destino (archivo o directorio)")
        sp.add_argument("sources", nargs="+", help="Uno o más paths origen")
        sp.set_defaults(func=cmd_copy_move)

    sp = sub.add_parser("queue", help="Abrir la ventana de cola (csf-ui)")
    sp.set_defaults(func=cmd_queue)

    sp = sub.add_parser("throttle", help="Ajustar el limitador de velocidad (B/s)")
    sp.add_argument("bytes_per_second", type=int, help="0 = sin límite")
    sp.set_defaults(func=cmd_throttle)

    sp = sub.add_parser("ping", help="Health check del daemon")
    sp.set_defaults(func=cmd_ping)

    sp = sub.add_parser("status", help="Mostrar la cola en formato texto")
    sp.set_defaults(func=cmd_status)

    return p


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
