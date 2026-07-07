"""csfd-tray: app GTK3 con AyatanaAppIndicator3 (system tray).

Click en el tray icon: muestra/oculta la ventana flotante de cola.
Click derecho: menú con "Mostrar cola", "Pausar todo", "Reanudar todo",
"Cancelar todo", "Salir".

NOTA: AyatanaAppIndicator3 requiere Gtk 3.0 (no 4.0). Por eso este binario
usa Gtk 3 en lugar de Gtk 4. La ventana flotante también es Gtk 3
(la versión Gtk 4 de csf_ui/queue_window.py no se usa acá).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import gi

# Importante: AyatanaAppIndicator3 SÓLO funciona con Gtk 3.
# Si se carga Gtk 4 antes, falla el import del indicator.
gi.require_version("Gtk", "3.0")
gi.require_version("AyatanaAppIndicator3", "0.1")

from gi.repository import Gtk  # noqa: E402
from gi.repository import AyatanaAppIndicator3 as AppIndicator  # noqa: E402

# Asegurar que csf_client y csf_ui sean importables
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from csf_client import DaemonClient, DaemonConnectionError  # noqa: E402

APP_ID = "io.github.copysecurefast.tray"


def _format_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    for unit in ("KiB", "MiB", "GiB", "TiB"):
        n = n / 1024.0
        if n < 1024:
            return f"{n:.1f} {unit}"
    return f"{n:.1f} PiB"


def _format_speed(bps: float) -> str:
    if bps < 1:
        return "—"
    return f"{_format_bytes(int(bps))}/s"


class TrayApp:
    """Aplicación GTK3 con system tray icon."""

    POLL_INTERVAL_MS = 500

    def __init__(self, socket_path: str | None = None):
        self._socket_path = socket_path
        self._client: DaemonClient | None = None
        self._win: Gtk.Window | None = None
        self._tray: AppIndicator.Indicator | None = None
        self._rows: dict = {}
        self._last_status_text: str = ""
        self._last_total_bytes: int = 0
        self._last_copied: int = 0
        self._last_ts: float | None = None
        self._speed_bps: float = 0.0
        self._eta_s: float = 0.0

    def run(self):
        """Construye el tray y entra en el main loop."""
        self._build_tray()
        # Conectar al daemon (en background via GLib timeout)
        if self._socket_path is None:
            from csf_client.daemon import _default_socket_path
            self._socket_path = _default_socket_path()
        try:
            self._client = DaemonClient(socket_path=self._socket_path)
        except DaemonConnectionError as e:
            print(f"[csfd-tray] no se pudo conectar al daemon: {e}", file=sys.stderr)

        # Polling inicial: cada 500ms refresca el status y la ventana si está abierta
        from gi.repository import GLib
        GLib.timeout_add(self.POLL_INTERVAL_MS, self._on_tick)
        Gtk.main()

    def _build_tray(self):
        """Crea el status icon."""
        self._tray = AppIndicator.Indicator.new(
            "copysecurefast",
            "folder-copy",  # nombre de icono estándar de Gtk
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._tray.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._tray.set_title("CopySecureFast")

        menu = Gtk.Menu()

        def add_item(label, callback):
            item = Gtk.MenuItem(label=label)
            item.connect("activate", lambda *_: callback())
            item.show()
            menu.append(item)
            return item

        add_item("Mostrar cola", self._show_window)
        menu.append(Gtk.SeparatorMenuItem())
        add_item("Pausar todo", self._pause_all)
        add_item("Reanudar todo", self._resume_all)
        add_item("Cancelar todo", self._cancel_all)
        menu.append(Gtk.SeparatorMenuItem())
        add_item("Salir", Gtk.main_quit)

        self._tray.set_menu(menu)
        # El activate en AppIndicator no es estándar. El menú cubre el toggle.

    def _on_tick(self) -> bool:
        """Refresca el status y la ventana si está abierta."""
        if self._client is None:
            return True
        try:
            jobs = self._client.get_queue()
        except DaemonConnectionError:
            return True
        except Exception as e:
            print(f"[csfd-tray] tick error: {e}", file=sys.stderr)
            return True

        # Calcular velocidad global
        now = self._now()
        total_copied = sum(j.copied_bytes for j in jobs)
        total_bytes = sum(j.total_bytes for j in jobs)
        active = sum(1 for j in jobs if j.state.value == "running")
        pending = sum(1 for j in jobs if j.state.value == "pending")
        failed = sum(1 for j in jobs if j.state.value == "failed")

        if self._last_ts is not None:
            dt = now - self._last_ts
            if dt > 0.1:
                dbytes = total_copied - self._last_copied
                if dbytes >= 0:
                    self._speed_bps = dbytes / dt
                # ETA
                remaining = max(0, total_bytes - total_copied)
                if self._speed_bps > 1 and remaining > 0:
                    self._eta_s = remaining / self._speed_bps
                else:
                    self._eta_s = 0
        self._last_ts = now
        self._last_copied = total_copied

        # Texto del status (lo usa la ventana flotante)
        if not jobs:
            self._last_status_text = "Sin trabajos en cola"
        else:
            n = len(jobs)
            txt = f"{n} trabajo(s) — {active} activo(s), {pending} pendiente(s)"
            if failed:
                txt += f" — {failed} fallaron"
            self._last_status_text = txt

        # Actualizar tooltip del tray
        if self._tray is not None:
            tooltip = self._last_status_text
            if self._speed_bps > 1 and active:
                tooltip += f"  ·  {_format_speed(self._speed_bps)}"
            self._tray.set_title(f"CSF: {tooltip}")

        # Si la ventana flotante está abierta, refrescarla
        if self._win is not None and self._win.get_visible():
            self._refresh_window(jobs)

        return True  # continuar

    def _now(self) -> float:
        from gi.repository import GLib
        return GLib.get_monotonic_time() / 1_000_000.0

    def _show_window(self):
        if self._win is None:
            self._build_window()
        self._win.present()
        return None

    def _build_window(self):
        """Construye la ventana flotante (Gtk 3 simple)."""
        self._win = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        self._win.set_title("CopySecureFast — Cola")
        self._win.set_default_size(380, 320)
        self._win.set_resizable(True)
        self._win.set_position(Gtk.WindowPosition.MOUSE)

        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        vbox.set_border_width(10)
        self._win.add(vbox)

        # Status label
        self._status_lbl = Gtk.Label(label="Sin trabajos en cola")
        self._status_lbl.set_halign(Gtk.Align.START)
        vbox.pack_start(self._status_lbl, False, False, 0)

        # Global progress
        self._global_prog = Gtk.ProgressBar()
        self._global_prog.set_fraction(0.0)
        vbox.pack_start(self._global_prog, False, False, 0)

        # Meta
        self._meta_lbl = Gtk.Label(label="—")
        self._meta_lbl.set_halign(Gtk.Align.END)
        vbox.pack_start(self._meta_lbl, False, False, 0)

        # Separator
        vbox.pack_start(Gtk.Separator(), False, False, 4)

        # Scrolled list
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, 180)
        vbox.pack_start(scrolled, True, True, 0)

        self._list_store = Gtk.ListStore(str, str, float, str)  # name, state, progress, status
        treeview = Gtk.TreeView(model=self._list_store)
        treeview.set_headers_visible(False)

        for i, title in enumerate(["Archivo", "Estado", "Progreso", "Info"]):
            renderer = Gtk.CellRendererText()
            if i == 2:  # progress
                renderer = Gtk.CellRendererProgress()
            col = Gtk.TreeViewColumn(title, renderer)
            if i == 2:
                col.add_attribute(renderer, "value", 2)
            else:
                col.add_attribute(renderer, "text", i)
            treeview.append_column(col)

        scrolled.add(treeview)
        self._treeview = treeview
        self._win.show_all()

    def _refresh_window(self, jobs):
        if self._win is None:
            return
        self._status_lbl.set_text(self._last_status_text)
        # Meta
        meta = _format_speed(self._speed_bps)
        if self._eta_s > 0 and any(j.state.value == "running" for j in jobs):
            meta += f"  ·  ETA {int(self._eta_s // 60)}m {int(self._eta_s % 60)}s"
        self._meta_lbl.set_text(meta)
        # Global progress
        total_bytes = sum(j.total_bytes for j in jobs)
        total_copied = sum(j.copied_bytes for j in jobs)
        if total_bytes > 0:
            self._global_prog.set_fraction(min(1.0, total_copied / total_bytes))
        # List
        self._list_store.clear()
        for j in jobs:
            pct = (j.copied_bytes / j.total_bytes * 100) if j.total_bytes else 0
            state_lbl = {
                "pending": "Pendiente",
                "running": "Copiando",
                "paused": "Pausado",
                "completed": "Completado",
                "failed": "Falló",
                "cancelled": "Cancelado",
            }.get(j.state.value, j.state.value)
            self._list_store.append([j.basename, state_lbl, pct, f"{int(pct)}%"])

    def _pause_all(self):
        if self._client is None:
            return
        try:
            self._client.pause()
        except DaemonConnectionError as e:
            print(f"[csfd-tray] pause: {e}", file=sys.stderr)

    def _resume_all(self):
        if self._client is None:
            return
        try:
            self._client.resume()
        except DaemonConnectionError as e:
            print(f"[csfd-tray] resume: {e}", file=sys.stderr)

    def _cancel_all(self):
        if self._client is None:
            return
        try:
            self._client.cancel()
        except DaemonConnectionError as e:
            print(f"[csfd-tray] cancel: {e}", file=sys.stderr)


def main() -> int:
    sock = os.environ.get("CSF_SOCKET")
    for arg in sys.argv[1:]:
        if arg.startswith("--socket="):
            sock = arg.split("=", 1)[1]
    app = TrayApp(socket_path=sock)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
