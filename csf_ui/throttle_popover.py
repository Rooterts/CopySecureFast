"""Popover para ajustar el limitador de velocidad (throttle).

- Slider de 0 a 200 MB/s (valores útiles para una copia).
- Presets: Sin límite, 10 MB/s, 50 MB/s, 100 MB/s.
- Label que muestra el valor actual en MB/s.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from csf_client import DaemonClient


class ThrottlePopover(Gtk.Popover):
    def __init__(self, client: DaemonClient):
        super().__init__()
        self._client = client

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        title = Gtk.Label(label="Limitador de velocidad")
        title.add_css_class("heading")
        box.append(title)

        # Slider: 0..200 MB/s, paso 1
        adj = Gtk.Adjustment.new(0, 0, 200, 1, 10, 0)
        self._slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adj)
        self._slider.set_size_request(220, -1)
        self._slider.set_hexpand(True)
        self._slider.set_draw_value(False)
        self._slider.connect("value-changed", self._on_value_changed)
        box.append(self._slider)

        # Label de valor
        self._value_label = Gtk.Label(label="Sin límite")
        self._value_label.add_css_class("dim-label")
        box.append(self._value_label)

        # Presets
        presets_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        for label, mb_s in [("Sin límite", 0), ("10 MB/s", 10), ("50 MB/s", 50), ("100 MB/s", 100)]:
            btn = Gtk.Button(label=label)
            btn.add_css_class("pill")
            btn.connect("clicked", lambda _b, v=mb_s: self._set_value(v))
            presets_box.append(btn)
        box.append(presets_box)

        self.set_child(box)

    def _set_value(self, mb_s: int) -> None:
        self._slider.set_value(mb_s)

    def _on_value_changed(self, scale: Gtk.Scale) -> None:
        mb_s = int(scale.get_value())
        if mb_s == 0:
            self._value_label.set_text("Sin límite")
            bps = 0
        else:
            self._value_label.set_text(f"{mb_s} MB/s ({mb_s * 1024 * 1024 / 1_000_000:.1f} MB/s)")
            bps = mb_s * 1024 * 1024
        try:
            self._client.set_throttle(bps)
        except Exception:
            # No hacer ruido en UI: el polling siguiente mostrará el estado real.
            pass
