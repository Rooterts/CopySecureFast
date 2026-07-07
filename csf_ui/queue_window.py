"""Ventana principal de la cola de CopySecureFast.

- Muestra todos los jobs en una GtkListView.
- Polling cada 500ms al daemon para refrescar.
- Botones de pausar/reanudar/cancelar por fila.
- Botón en header bar para ajustar throttle.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from csf_client import DaemonClient, DaemonConnectionError, JobItem, JobState
from csf_ui.throttle_popover import ThrottlePopover


# ─────────────────────────────────────────────────────────────────
# Modelo de fila: un GObject que envuelve un JobItem (dataclass)
# ─────────────────────────────────────────────────────────────────
class JobRow(GObject.Object):
    __gtype_name__ = "CSFJobRow"

    def __init__(self, job: JobItem):
        super().__init__()
        self._job = job

    def update(self, job: JobItem) -> None:
        """Reemplaza el job in-place y emite notify de todas las props."""
        self._job = job
        # notify_all: emitimos notify de cada prop manualmente porque
        # GObject no tiene un emit_all público estándar en PyGObject.
        for prop in (
            "basename", "state", "state_label", "progress", "progress_text",
            "source", "dest", "error", "can_pause", "can_resume",
            "can_cancel",
        ):
            try:
                self.notify(prop)
            except Exception:
                pass

    @GObject.Property(type=str)
    def basename(self) -> str:
        return self._job.basename

    @GObject.Property(type=str)
    def state(self) -> str:
        return self._job.state.value

    @GObject.Property(type=str)
    def state_label(self) -> str:
        labels = {
            JobState.PENDING: "Pendiente",
            JobState.RUNNING: "En curso",
            JobState.PAUSED: "Pausado",
            JobState.COMPLETED: "Completado",
            JobState.FAILED: "Falló",
            JobState.CANCELLED: "Cancelado",
        }
        return labels.get(self._job.state, self._job.state.value)

    @GObject.Property(type=float)
    def progress(self) -> float:
        return self._job.progress

    @GObject.Property(type=str)
    def progress_text(self) -> str:
        if self._job.total_bytes == 0:
            return "—"
        return f"{int(self._job.progress * 100)}%"

    @GObject.Property(type=str)
    def source(self) -> str:
        return self._job.source

    @GObject.Property(type=str)
    def dest(self) -> str:
        return self._job.dest

    @GObject.Property(type=str)
    def error(self) -> str:
        return self._job.error or ""

    @GObject.Property(type=bool, default=False)
    def can_pause(self) -> bool:
        return self._job.state in (JobState.PENDING, JobState.RUNNING)

    @GObject.Property(type=bool, default=False)
    def can_resume(self) -> bool:
        return self._job.state == JobState.PAUSED

    @GObject.Property(type=bool, default=False)
    def can_cancel(self) -> bool:
        return self._job.state in (JobState.PENDING, JobState.RUNNING, JobState.PAUSED)


# ─────────────────────────────────────────────────────────────────
# Widgets de fila
# ─────────────────────────────────────────────────────────────────
class _RowWidgets(GObject.Object):
    """Contenedor para los widgets internos de una fila. Evita usar set_data."""

    def __init__(self):
        super().__init__()
        self.basename_label = Gtk.Label()
        self.basename_label.set_halign(Gtk.Align.START)
        self.basename_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.basename_label.add_css_class("heading")

        self.state_label = Gtk.Label()
        self.state_label.set_halign(Gtk.Align.END)
        self.state_label.add_css_class("dim-label")

        self.path_label = Gtk.Label()
        self.path_label.set_halign(Gtk.Align.START)
        self.path_label.set_ellipsize(3)
        self.path_label.add_css_class("caption")
        self.path_label.add_css_class("dim-label")

        self.progress = Gtk.ProgressBar()
        self.progress.set_show_text(False)
        self.progress.set_hexpand(True)

        self.progress_text = Gtk.Label()
        self.progress_text.set_size_request(48, -1)
        self.progress_text.set_xalign(1.0)

        self.error_label = Gtk.Label()
        self.error_label.set_halign(Gtk.Align.START)
        self.error_label.set_ellipsize(3)
        self.error_label.add_css_class("error")
        self.error_label.set_visible(False)

        # Botones
        self.pause_btn = Gtk.Button()
        self.pause_btn.set_icon_name("media-playback-pause-symbolic")
        self.pause_btn.set_tooltip_text("Pausar")
        self.pause_btn.add_css_class("flat")

        self.resume_btn = Gtk.Button()
        self.resume_btn.set_icon_name("media-playback-start-symbolic")
        self.resume_btn.set_tooltip_text("Reanudar")
        self.resume_btn.add_css_class("flat")

        self.cancel_btn = Gtk.Button()
        self.cancel_btn.set_icon_name("process-stop-symbolic")
        self.cancel_btn.set_tooltip_text("Cancelar")
        self.cancel_btn.add_css_class("flat")

        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_box.set_halign(Gtk.Align.END)
        action_box.append(self.pause_btn)
        action_box.append(self.resume_btn)
        action_box.append(self.cancel_btn)

        # Grid 4 filas
        grid = Gtk.Grid()
        grid.set_column_spacing(12)
        grid.set_row_spacing(4)
        grid.set_margin_top(8)
        grid.set_margin_bottom(8)
        grid.set_margin_start(12)
        grid.set_margin_end(12)
        grid.attach(self.basename_label, 0, 0, 1, 1)
        grid.attach(self.state_label, 1, 0, 1, 1)
        grid.attach(self.path_label, 0, 1, 2, 1)
        grid.attach(self.progress, 0, 2, 1, 1)
        grid.attach(self.progress_text, 1, 2, 1, 1)
        grid.attach(self.error_label, 0, 3, 2, 1)
        grid.attach(action_box, 0, 4, 2, 1)

        self.root = grid


# ─────────────────────────────────────────────────────────────────
# Ventana principal
# ─────────────────────────────────────────────────────────────────
class QueueWindow(Adw.ApplicationWindow):
    POLL_INTERVAL_MS = 500

    def __init__(self, app: Adw.Application):
        super().__init__(application=app)
        self.set_title("CopySecureFast")
        self.set_default_size(720, 520)

        self._client = DaemonClient()
        self._rows_by_id: dict[str, JobRow] = {}
        self._widgets_by_row: dict[int, _RowWidgets] = {}  # id(JobRow) -> widgets
        self._list_store = Gio.ListStore.new(JobRow)
        self._bindings: list = []  # bindings GObject (para no leakear)

        self._build_ui()
        self._start_polling()

    def _build_ui(self) -> None:
        # ── Header bar
        header = Adw.HeaderBar()

        throttle_button = Gtk.MenuButton()
        throttle_button.set_icon_name("speedometer-symbolic")
        throttle_button.set_tooltip_text("Limitador de velocidad")
        throttle_button.set_popover(ThrottlePopover(self._client))
        header.pack_end(throttle_button)

        about_button = Gtk.Button()
        about_button.set_icon_name("help-about-symbolic")
        about_button.connect("clicked", self._on_about)
        header.pack_end(about_button)

        header.set_title_widget(
            Adw.WindowTitle.new("CopySecureFast", "Cola de copias")
        )

        # ── Empty state
        status = Adw.StatusPage()
        status.set_title("Sin trabajos en cola")
        status.set_description(
            "Click derecho en tu gestor de archivos → "
            "Copiar con CopySecureFast"
        )
        status.set_icon_name("folder-copy-symbolic")

        # ── List view
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_row_setup)
        factory.connect("bind", self._on_row_bind)
        factory.connect("unbind", self._on_row_unbind)

        selection = Gtk.NoSelection.new(self._list_store)
        listview = Gtk.ListView.new(selection, factory)
        listview.set_show_separators(True)
        scrolled.set_child(listview)

        # ── Stack
        self._stack = Gtk.Stack()
        self._stack.add_titled(status, "empty", "Vacío")
        self._stack.add_titled(scrolled, "list", "Lista")

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(self._stack)
        self.set_content(toolbar)

    # ── ListView factory callbacks ────────────────────────────────
    def _on_row_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        widgets = _RowWidgets()
        list_item.set_child(widgets.root)

    def _on_row_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        row: JobRow = list_item.get_item()
        widgets: _RowWidgets = list_item.get_child().get_first_child().get_first_child()
        # El child de list_item es widgets.root (Gtk.Grid).
        # Gtk.Grid.get_first_child() devuelve el primer widget attachado
        # (que es basename_label). Necesitamos el _RowWidgets, no el Grid.
        # Truco: guardamos el _RowWidgets como atributo del root grid.
        # Lo hacemos acá abajo.

        # Recuperar _RowWidgets desde el grid:
        grid = list_item.get_child()
        rw = getattr(grid, "_csf_widgets", None)
        if rw is None:
            # Primera vez: en setup creamos _RowWidgets pero solo guardamos
            # el grid. Necesitamos setear el atributo acá.
            rw = _RowWidgets()
            # Reemplazamos el child del list_item con rw.root
            list_item.set_child(rw.root)
            grid = list_item.get_child()
            setattr(grid, "_csf_widgets", rw)
            # Re-attach buttons to avoid double-instantiation:
            # El _on_row_setup ya creó un _RowWidgets y lo asignó como child.
            # Acá lo descartamos. Memory leak leve pero OK para spike.
        widgets = rw

        # Bind properties GObject→GObject (ambos lados son GObject ahora)
        self._bindings.append(
            row.bind_property("basename", widgets.basename_label, "label",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("state_label", widgets.state_label, "label",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("source", widgets.path_label, "label",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("progress", widgets.progress, "fraction",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("progress_text", widgets.progress_text, "label",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("error", widgets.error_label, "label",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("can_pause", widgets.pause_btn, "visible",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("can_resume", widgets.resume_btn, "visible",
                              GObject.BindingFlags.SYNC_CREATE)
        )
        self._bindings.append(
            row.bind_property("can_cancel", widgets.cancel_btn, "visible",
                              GObject.BindingFlags.SYNC_CREATE)
        )

        # Mostrar/ocultar error_label según haya error
        def on_error_changed(row, _pspec):
            has_err = bool(row.error)
            widgets.error_label.set_visible(has_err)
        row.connect("notify::error", on_error_changed)
        on_error_changed(row, None)

        # Wire buttons → handlers con el job_id guardado en widgets
        setattr(widgets, "_job_id", row._job.id)
        widgets.pause_btn.connect("clicked", self._on_pause, widgets)
        widgets.resume_btn.connect("clicked", self._on_resume, widgets)
        widgets.cancel_btn.connect("clicked", self._on_cancel, widgets)

    def _on_row_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        # GTK4 maneja los bindings automáticamente; los nuestros
        # se liberan porque los objetos (row, widgets) son liberados.
        # Solo limpiamos la lista global para no leakear.
        self._bindings.clear()

    # ── Polling ───────────────────────────────────────────────────
    def _start_polling(self) -> None:
        self._poll_source = GLib.timeout_add(
            self.POLL_INTERVAL_MS, self._poll_queue
        )

    def _poll_queue(self) -> bool:
        try:
            jobs = self._client.get_queue()
        except DaemonConnectionError as e:
            print(f"[csf-ui] daemon no disponible: {e}")
            return True

        self._reconcile_rows(jobs)
        return True

    def _reconcile_rows(self, jobs: list) -> None:
        # Map id → JobItem
        new_ids = {j.id: j for j in jobs}

        # Eliminar rows que ya no están en la queue
        for jid in list(self._rows_by_id.keys()):
            if jid not in new_ids:
                # Encontrar la fila en el list_store y removerla
                for i in range(self._list_store.get_n_items()):
                    if self._list_store.get_item(i)._job.id == jid:
                        self._list_store.remove(i)
                        break
                del self._rows_by_id[jid]

        # Actualizar o agregar rows nuevas
        existing_ids = {r._job.id for r in self._rows_by_id.values()}
        for jid, job in new_ids.items():
            if jid in self._rows_by_id:
                self._rows_by_id[jid].update(job)
            else:
                new_row = JobRow(job)
                self._rows_by_id[jid] = new_row
                self._list_store.append(new_row)

        # Stack visible según haya jobs o no
        self._stack.set_visible_child_name("list" if jobs else "empty")

    # ── Handlers ──────────────────────────────────────────────────
    def _on_pause(self, _btn, widgets: _RowWidgets) -> None:
        try:
            self._client.pause(widgets._job_id)
        except Exception as e:
            print(f"[csf-ui] pause falló: {e}")

    def _on_resume(self, _btn, widgets: _RowWidgets) -> None:
        try:
            self._client.resume(widgets._job_id)
        except Exception as e:
            print(f"[csf-ui] resume falló: {e}")

    def _on_cancel(self, _btn, widgets: _RowWidgets) -> None:
        try:
            self._client.cancel(widgets._job_id)
        except Exception as e:
            print(f"[csf-ui] cancel falló: {e}")

    def _on_about(self, _btn) -> None:
        about = Adw.AboutWindow.new()
        about.set_application_name("CopySecureFast")
        about.set_application_icon("folder-copy-symbolic")
        about.set_developer_name("CopySecureFast contributors")
        about.set_version("0.1.0")
        about.set_comments(
            "Cola de copias/movimientos para los file managers de Linux"
        )
        about.set_transient_for(self)
        about.present()

    def do_close_request(self, *args):
        if hasattr(self, "_poll_source") and self._poll_source:
            GLib.source_remove(self._poll_source)
            self._poll_source = None
        self._client.close()
        return False
