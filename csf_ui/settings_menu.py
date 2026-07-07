"""Settings submenu for the tray icon.

Reads/writes settings in `~/.config/copysecurefast/settings.json` (via
`csf_config`). Settings are applied as follows:

- `throttle_bps`: applied immediately via the daemon RPC `set_throttle`.
- `verify_hash`: applied on each enqueue made from the UI.
- `show_notifications`: shown when jobs finish.
- `autostart_daemon`: creates/removes the .desktop in
  `~/.config/autostart/copysecurefast.desktop`.
- `default_dest_dir`: if set, "Paste with" uses it without asking.
  (Future improvement.)

The submenu is built dynamically every time the user right-clicks the
tray icon, reading the current settings from disk.
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
    """Applies the throttle via the daemon RPC."""
    sock = os.environ.get("CSF_SOCKET", "/tmp/copysecurefast.sock")
    try:
        from csf_client import DaemonClient
        with DaemonClient(socket_path=sock) as c:
            applied = c.set_throttle(bps)
            print(f"[csfd-tray] throttle = {applied} B/s")
    except Exception as e:
        print(f"[csfd-tray] could not apply throttle: {e}", file=sys.stderr)


def _toggle_autostart(enable: bool) -> None:
    """Creates or removes the autostart .desktop file."""
    if enable:
        AUTOSTART_DIR.mkdir(parents=True, exist_ok=True)
        AUTOSTART_FILE.write_text(
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=CopySecureFast daemon\n"
            "Comment=CopySecureFast daemon with tray icon\n"
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
    """Opens the config file with xdg-open (uses the default editor)."""
    path = csf_config._config_path()
    if not path.exists():
        # Create with defaults first
        csf_config.save(csf_config.Settings())
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
    """Builds the 'Settings' submenu and attaches it to the tray menu.

    Returns the Gtk.MenuItem added so the caller can position it.
    """
    settings = _reload_settings()

    config_item = Gtk.MenuItem(label="Configuración")
    config_item.show()
    config_submenu = Gtk.Menu()
    config_item.set_submenu(config_submenu)

    # ── Throttle (sub-submenu with presets)
    throttle_item = Gtk.MenuItem(label="Max speed")
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

    # ── Boolean toggles
    def make_toggle(attr, on_apply):
        def cb(active):
            s = _reload_settings()
            setattr(s, attr, active)
            csf_config.save(s)
            on_apply(s)
        return cb

    _add_check_item(
        config_submenu,
        "Verify SHA-256 hash on copy",
        settings.verify_hash,
        make_toggle("verify_hash", lambda s: None),
    )
    _add_check_item(
        config_submenu,
        "Show notifications on finish",
        settings.show_notifications,
        make_toggle("show_notifications", lambda s: None),
    )
    _add_check_item(
        config_submenu,
        "Start daemon at login (autostart)",
        settings.autostart_daemon,
        make_toggle("autostart_daemon", _toggle_autostart),
    )

    config_submenu.append(_separator())

    # ── Actions
    open_item = Gtk.MenuItem(label="Edit configuration (JSON)...")
    open_item.connect("activate", lambda *_: _open_config_in_editor())
    open_item.show()
    config_submenu.append(open_item)

    reload_item = Gtk.MenuItem(label="Reload configuration")
    reload_item.connect("activate", lambda *_: print("[csfd-tray] settings reloaded"))
    reload_item.show()
    config_submenu.append(reload_item)

    parent_menu.append(config_item)
    return config_item


def _separator() -> Gtk.SeparatorMenuItem:
    s = Gtk.SeparatorMenuItem()
    s.show()
    return s
