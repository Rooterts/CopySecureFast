"""csfd-tray: app GTK4 con StatusIcon en el system tray.

Click en el tray icon: muestra/oculta la ventana flotante de cola.
Click derecho: menú con "Mostrar cola", "Pausar todo", "Reanudar todo",
"Cancelar todo", "Salir".

Proceso ligero: solo maneja el tray + un puntero a la ventana.
"""

from __future__ import annotations

import os
import sys

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, Gtk  # noqa: E402

from csf_client import DaemonClient, DaemonConnectionError
from csf_ui.queue_window import QueueWindow


APP_ID = "io.github.copysecurefast.tray"
SOCKET = os.environ.get("CSF_SOCKET")


class TrayApp(Adw.Application):
    def __init__(self, socket_path: str | None = None):
        super().__init__(
            application_id=APP_ID,
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )
        self._socket_path = socket_path
        self._win: QueueWindow | None = None
        self._tray: Gtk.StatusIcon | None = None

    def do_activate(self):
        # La ventana se crea lazily (al primer click del tray).
        pass

    def do_startup(self):
        Adw.Application.do_startup(self)
        self._build_tray()
        # Si el usuario ya tenía la ventana abierta (ej. csf-ui lanzado
        # por separado), la conectamos al singleton.
        for w in self.get_windows():
            if isinstance(w, QueueWindow):
                self._win = w
                break

    def _build_tray(self):
        """Crea el StatusIcon."""
        # GTK4 no tiene Gtk.StatusIcon estable; usamos un botón en un
        # popover-like via SystemTray. Como GTK4 todavía no expone
        # StatusNotifierItem nativamente en esta versión, recurrimos
        # al AppIndicator3 via GObject introspection si está disponible.
        # Si no, fallback: usamos una ventana invisible + un tray simulado.
        #
        # En la práctica, en CachyOS con un tray real (waybar, niri-dms,
        # etc.) queremos que aparezca un icono. GTK4 sólo lo expone a
        # través de libayatana-appindicator. Si no está, mostramos un
        # fallback: una mini-ventana con el icono + menú.
        try:
            gi.require_version("AyatanaAppIndicator3", "0.1")
            from gi.repository import AyatanaAppIndicator3 as AppIndicator
        except (ValueError, ImportError):
            self._tray = None
            self._build_fallback_window()
            return

        self._tray = AppIndicator.Indicator.new(
            "copysecurefast",
            "folder-copy-symbolic",
            AppIndicator.IndicatorCategory.APPLICATION_STATUS,
        )
        self._tray.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._tray.set_title("CopySecureFast")

        menu = Gtk.Menu()
        item_show = Gtk.MenuItem(label="Mostrar cola")
        item_show.connect("activate", lambda *_: self._toggle_window())
        item_show.show()
        menu.append(item_show)

        menu.append(Gtk.SeparatorMenuItem())

        item_pause = Gtk.MenuItem(label="Pausar todo")
        item_pause.connect("activate", self._menu_pause_all)
        item_pause.show()
        menu.append(item_pause)

        item_resume = Gtk.MenuItem(label="Reanudar todo")
        item_resume.connect("activate", self._menu_resume_all)
        item_resume.show()
        menu.append(item_resume)

        item_cancel = Gtk.MenuItem(label="Cancelar todo")
        item_cancel.connect("activate", self._menu_cancel_all)
        item_cancel.show()
        menu.append(item_cancel)

        menu.append(Gtk.SeparatorMenuItem())

        item_quit = Gtk.MenuItem(label="Salir")
        item_quit.connect("activate", lambda *_: self.quit())
        item_quit.show()
        menu.append(item_quit)

        self._tray.set_menu(menu)
        # Click izquierdo toggle ventana
        try:
            self._tray.connect("activate", self._on_tray_activated)
        except Exception:
            pass

    def _build_fallback_window(self):
        """Fallback: mini-ventana siempre visible con el menú.

        Si no hay appindicator, mostramos una ventanita chiquita arriba
        a la derecha que reemplaza al tray. Funcional pero menos elegante.
        """
        win = Gtk.ApplicationWindow(application=self)
        win.set_title("CSF Tray")
        win.set_default_size(0, 0)
        win.set_decorated(False)
        win.set_resizable(False)
        # Esquina superior derecha
        display = Gtk.Widget.get_default_direction()
        win.set_visible(True)
        self._tray = None  # signal que no hay tray real
        # Construimos la ventana flotante igual; el usuario la abre
        # desde un .desktop manual o la deja como ventana normal.
        self._show_queue_window()

    def _on_tray_activated(self, _tray):
        # Algunos appindicators no emiten activate. El menú ya
        # cubre el toggle.
        self._toggle_window()

    def _toggle_window(self):
        if self._win is None:
            self._show_queue_window()
        elif self._win.get_visible():
            self._win.set_visible(False)
        else:
            self._win.present()

    def _show_queue_window(self):
        if self._win is None:
            self._win = QueueWindow(self, socket_path=self._socket_path)
            self._win.connect("notify::visible", self._on_window_visible)
        self._win.present()

    def _on_window_visible(self, _win, _pspec):
        # Si el usuario cierra la ventana con la X, queda oculta pero
        # viva. Si quiere salir completamente, usa el menú "Salir".
        pass

    # ── Menu actions ────────────────────────────────────────────────
    def _menu_pause_all(self, *_):
        try:
            with DaemonClient(socket_path=self._socket_path) as c:
                c.pause()
        except DaemonConnectionError as e:
            self._notify(f"Pausa falló: {e}")

    def _menu_resume_all(self, *_):
        try:
            with DaemonClient(socket_path=self._socket_path) as c:
                c.resume()
        except DaemonConnectionError as e:
            self._notify(f"Resume falló: {e}")

    def _menu_cancel_all(self, *_):
        try:
            with DaemonClient(socket_path=self._socket_path) as c:
                c.cancel()
        except DaemonConnectionError as e:
            self._notify(f"Cancel falló: {e}")

    def _notify(self, msg: str):
        # Sin notify-send en deps; lo escribimos a stderr y el usuario lo ve.
        sys.stderr.write(f"[csfd-tray] {msg}\n")


def main() -> int:
    sock = os.environ.get("CSF_SOCKET")
    if not sock:
        for arg in sys.argv[1:]:
            if arg.startswith("--socket="):
                sock = arg.split("=", 1)[1]
    app = TrayApp(socket_path=sock)
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
