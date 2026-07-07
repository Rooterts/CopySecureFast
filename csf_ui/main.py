"""Entry point CLI: `csf-ui` muestra la ventana de cola."""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

import sys  # noqa: E402

from gi.repository import Adw, Gio  # noqa: E402

from csf_ui.queue_window import QueueWindow  # noqa: E402


class CsfApp(Adw.Application):
    def __init__(self):
        super().__init__(
            application_id="io.github.copysecurefast",
            flags=Gio.ApplicationFlags.FLAGS_NONE,
        )

    def do_activate(self):
        win = self.props.active_window
        if win is None:
            win = QueueWindow(self)
        win.present()


def main() -> int:
    app = CsfApp()
    return app.run(sys.argv)


if __name__ == "__main__":
    sys.exit(main())
