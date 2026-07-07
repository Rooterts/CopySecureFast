"""Submenú de Configuración para el tray icon.

Lee/escribe settings en `~/.config/copysecurefast/settings.json` (via
`csf_config`). Los settings se aplican:

- `throttle_bps`: al iniciar el daemon y cuando se cambia desde acá
  (vía el daemon RPC `set_throttle`).
- `verify_hash`: se aplica en cada `enqueue` que se hace desde la UI.
- `show_notifications`: cuando los jobs terminan.
- `autostart_daemon`: crea/borra el .desktop en
  `~/.config/autostart/copysecurefast.desktop`.
- `default_dest_dir`: si está configurado, el "Pegar con" lo usa
  sin pedir destino. (Próxima mejora.)

El submenú se construye dinámicamente cuando el usuario hace click
derecho en el tray icon, leyendo los settings actuales del disco.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import Gtk  # noqa: E402

_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import csf_config  # noqa: E402


# Throttle presets en MB/s
THROTTLE_PRESETS_MB = [
    ("Sin límite", 0),
    ("1 MB/s", 1 * 1024 * 1024),
    ("10 MB/s", 10 * 1024 * 1024),
    ("50 MB/s", 50 * 1024 * 1024),
    ("100 MB/s", 100 * 1024 * 1024),
    ("200 MB/s", 200 * 1024 * 1024),
]

AUTOSTART_DIR = Path.home() / ".config" / "autostart"
AUTOSTART_FILE = AUTOSTART_DIR / "copysecurefast.desktop"


def _set_throttle(bps: int, parent_menu: Gtk.MenuItem | None = None) -> None:
    """Aplica el throttle via el daemon RPC."""
    sock = os.environ.get("CSF_SOCKET", "/tmp/copysecurefast.sock")
    try:
        from csf_client import DaemonClient
        with DaemonClient(socket_path=sock) as c:
            applied = c.set_throttle(bps)
            print(f"[csf-tray] throttle = {applied} B/s")
    except Exception as e:
        print(f"[csf-tray] no pude aplicar throttle: {e}", file=sys.stderr)


def _toggle_autostart(enable: bool) -> None:
    """Crea o borra el .desktop de autostart."""
    if enable:
        AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=CopySecureFast daemon\n"
            "Comment=Daemon de CopySecureFast con tray icon\n"
            "Exec=csfd\n"
            "Icon=folder-copy\n"
            "Terminal=false\n"
            "Categories=Utility;\n"
            "X-GNOME-Autostart-enabled=true\n"
        )
    else:
        if AUTOSTART_FILE.exists():
            AUTOSTART_FILE.unlink()


def _open_config_in_editor() -> None:
    """Abre el archivo de config con xdg-open o el editor del entorno."""
    path = csf_config._config_path()
    if not path.exists():
        # Crear con defaults primero
        csf_config.save(csf_config.Settings())
    # xdg-open (que en niri abre con el editor default o kitty+nvim según config)
    subprocess.Popen(
        ["xdg-open", str(path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _reload_settings() -> csf_config.Settings:
    return csf_config.load()


def _save_and_apply(settings: csf_config.Settings) -> None:
    """Persiste y aplica los settings que tengan side-effects."""
    csf_config.save(settings)
    _set_throttle(settings.throttle_bps)
    _toggle_autostart(settings.autostart_daemon)


def _add_check_item(
    menu: Gtk.Menu,
    label: str,
    active: bool,
    on_toggle,
) -> Gtk.CheckMenuItem:
    """Agrega un CheckMenuItem con callback."""
    item = Gtk.CheckMenuItem(label=label)
    item.set_active(active)
    item.connect("toggled", lambda w: on_toggle(w.get_active()))
    item.show()
    menu.append(item)
    return item


def _add_radio_items(
    menu: Gtk.Menu,
    group: Gtk.RadioMenuItem | None,
    options: list[tuple[str, object]],
    current_value,
    on_select,
) -> Gtk.RadioMenuItem | None:
    """Agrega un grupo de RadioMenuItems."""
    first = group
    for label, value in options:
        item = Gtk.RadioMenuItem.new_with_label_from_widget(first, label) if first else Gtk.RadioMenuItem(label=label)
        if first is None:
            first = item
        item.set_active(value == current_value)
        item.connect("toggled", lambda w, v=value: on_select(v) if w.get_active() else None)
        item.show()
        menu.append(item)
    return first


def build(parent_menu: Gtk.Menu) -> Gtk.MenuItem:
    """Construye el submenú 'Configuración' y lo agrega al menú del tray.

    Devuelve el Gtk.MenuItem agregado para que el caller lo ubique
    en la posición correcta.
    """
    settings = _reload_settings()

    config_item = Gtk.MenuItem(label="Configuración")
    config_item.show()
    config_submenu = Gtk.Menu()
    config_item.set_submenu(config_submenu)

    # ── Throttle (sub-submenu con presets)
    throttle_item = Gtk.MenuItem(label="Velocidad máxima")
    throttle_item.show()
    throttle_menu = Gtk.Menu()
    throttle_item.set_submenu(throttle_menu)
    _add_radio_items(
        throttle_menu, None,
        THROTTLE_PRESETS_MB,
        settings.throttle_bps,
        lambda bps: (
            _save_and_apply(csf_config.Settings(
                throttle_bps=bps,
                verify_hash=settings.verify_hash,
                show_notifications=settings.show_notifications,
                autostart_daemon=settings.autostart_daemon,
                default_dest_dir=settings.default_dest_dir,
                window_position=settings.window_position,
            ))
        ),
    )
    config_submenu.append(throttle_item)

    config_submenu.append(_separator())

    # ── Toggles booleanos
    def make_toggle(attr, on_apply):
        def cb(active):
            s = _reload_settings()
            setattr(s, attr, active)
            csf_config.save(s)
            on_apply(s)
        return cb

    _add_check_item(
        config_submenu,
        "Verificar hash SHA-256 al copiar",
        settings.verify_hash,
        make_toggle("verify_hash", lambda s: None),
    )
    _add_check_item(
        config_submenu,
        "Notificaciones al terminar",
        settings.show_notifications,
        make_toggle("show_notifications", lambda s: None),
    )
    _add_check_item(
        config_submenu,
        "Iniciar daemon al login (autostart)",
        settings.autostart_daemon,
        make_toggle("autostart_daemon", _toggle_autostart),
    )

    config_submenu.append(_separator())

    # ── Acciones
    open_item = Gtk.MenuItem(label="Editar configuración (JSON)…")
    open_item.connect("activate", lambda *_: _open_config_in_editor())
    open_item.show()
    config_submenu.append(open_item)

    reload_item = Gtk.MenuItem(label="Recargar configuración")
    reload_item.connect("activate", lambda *_: print("[csf-tray] settings recargados"))
    reload_item.show()
    config_submenu.append(reload_item)

    parent_menu.append(config_item)
    return config_item


def _separator() -> Gtk.SeparatorMenuItem:
    s = Gtk.SeparatorMenuItem()
    s.show()
    return s
