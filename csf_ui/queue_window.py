"""Ventana flotante de cola — estilo TeraCopy/SuperCopier.

Características:
- Tamaño compacto, NO se maximiza, queda arriba a la derecha.
- Header con título + botones minimizar/cerrar + throttle.
- Barra global de progreso + velocidad agregada + ETA.
- Lista scrolleable de jobs en curso con barra individual.
- Botones de pausar/reanudar/cancelar por fila.
- Suscripción a eventos del daemon (no polling): actualización en vivo.

Modos:
- Modo normal: lista vertical con todas las filas.
- Modo minimal: solo la barra global, para cuando no hay nada interesante.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gio, GLib, GObject, Gtk  # noqa: E402

from csf_client import DaemonClient, DaemonConnectionError, JobItem, JobState
from csf_client.protocol import (
    JobCancelled,
    JobCompleted,
    JobFailed,
    JobPaused,
    JobProgress,
    JobResumed,
    JobStarted,
)
from csf_ui.throttle_popover import ThrottlePopover

# Tamaño de la ventana flotante (compacto).
WIN_W = 380
WIN_H = 420


def _format_bytes(n: int) -> str:
    """1024-based: 1.5 KiB, 2.3 MiB, etc. Para ETA/velocidad."""
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


def _format_eta(seconds: float) -> str:
    if seconds < 0 or seconds > 24 * 3600:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m}m {s}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


# ─────────────────────────────────────────────────────────────────
# Modelo de fila (GObject) que envuelve un JobItem
# ─────────────────────────────────────────────────────────────────
class JobRow(GObject.Object):
    __gtype_name__ = "CSFJobRow"

    def __init__(self, job: JobItem):
        super().__init__()
        self._job = job

    def update(self, job: JobItem) -> None:
        self._job = job
        for prop in (
            "basename", "state", "state_label", "progress", "progress_text",
            "speed_text", "eta_text", "source", "dest", "error",
            "can_pause", "can_resume", "can_cancel",
        ):
            try:
                self.notify(prop)
            except Exception:
                pass

    @property
    def id(self) -> str:
        return self._job.id

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
            JobState.RUNNING: "Copiando",
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
    def speed_text(self) -> str:
        # Se actualiza por evento job_progress.
        return getattr(self, "_speed", "—")

    @GObject.Property(type=str)
    def eta_text(self) -> str:
        return getattr(self, "_eta", "—")

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
        return self._job.state.is_pausable

    @GObject.Property(type=bool, default=False)
    def can_resume(self) -> bool:
        return self._job.state.is_resumable

    @GObject.Property(type=bool, default=False)
    def can_cancel(self) -> bool:
        return self._job.state.is_cancellable


# ─────────────────────────────────────────────────────────────────
# Ventana principal
# ─────────────────────────────────────────────────────────────────
class QueueWindow(Adw.ApplicationWindow):
    """Ventana flotante de la cola. Compacta, queda sobre otras ventanas."""

    def __init__(self, app: Adw.Application, socket_path: str | None = None):
        super().__init__(application=app)
        self.set_title("CopySecureFast")
        self.set_default_size(WIN_W, WIN_H)
        # No maximizar nunca, comportamiento de herramienta.
        self.set_resizable(True)

        self._client = DaemonClient(socket_path=socket_path)
        self._rows_by_id: dict[str, JobRow] = {}
        self._list_store = Gio.ListStore.new(JobRow)
        self._bindings: list = []
        # Métricas globales: bytes copiados desde la última muestra
        self._global_copied_prev = 0
        self._global_prev_ts: float | None = None
        self._global_speed_bps: float = 0.0
        self._global_eta_s: float = 0.0

        self._build_ui()
        self._load_initial_queue()
        # Suscripción a eventos (no polling). El reader thread del
        # cliente corre en background y empuja eventos a una queue.Queue.
        self._start_event_loop()

    # ── UI ─────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        # Header bar compacto
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(False)  # usamos botones custom

        # Botón throttle en el header
        throttle_btn = Gtk.MenuButton()
        throttle_btn.set_icon_name("speedometer-symbolic")
        throttle_btn.set_tooltip_text("Limitador de velocidad")
        throttle_btn.set_popover(ThrottlePopover(self._client))
        header.pack_start(throttle_btn)

        # Botón cerrar (X) — minimiza al tray en vez de cerrar
        close_btn = Gtk.Button()
        close_btn.set_icon_name("window-close-symbolic")
        close_btn.set_tooltip_text("Cerrar (minimiza al tray)")
        close_btn.connect("clicked", self._on_close_clicked)
        header.pack_end(close_btn)

        # Botón minimizar
        min_btn = Gtk.Button()
        min_btn.set_icon_name("window-minimize-symbolic")
        min_btn.set_tooltip_text("Minimizar")
        min_btn.connect("clicked", lambda _: self.set_visible(False))
        header.pack_end(min_btn)

        title = Adw.WindowTitle.new("CopySecureFast", "")
        header.set_title_widget(title)

        # ── Barra global de estado
        global_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        global_box.set_margin_start(12)
        global_box.set_margin_end(12)
        global_box.set_margin_top(8)
        global_box.set_margin_bottom(4)

        self._global_status = Gtk.Label(label="Sin trabajos en cola")
        self._global_status.set_halign(Gtk.Align.START)
        self._global_status.add_css_class("caption")
        self._global_status.add_css_class("dim-label")
        global_box.append(self._global_status)

        self._global_progress = Gtk.ProgressBar()
        self._global_progress.set_show_text(False)
        self._global_progress.set_hexpand(True)
        self._global_progress.set_fraction(0.0)
        global_box.append(self._global_progress)

        self._global_meta = Gtk.Label(label="—")
        self._global_meta.set_halign(Gtk.Align.END)
        self._global_meta.set_xalign(1.0)
        self._global_meta.add_css_class("caption")
        self._global_meta.add_css_class("dim-label")
        global_box.append(self._global_meta)

        # ── Lista de jobs
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_vexpand(True)
        scrolled.set_min_content_height(200)

        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_row_setup)
        factory.connect("bind", self._on_row_bind)
        factory.connect("unbind", self._on_row_unbind)

        selection = Gtk.NoSelection.new(self._list_store)
        listview = Gtk.ListView.new(selection, factory)
        listview.set_show_separators(True)
        scrolled.set_child(listview)

        # ── Empty state
        empty = Adw.StatusPage()
        empty.set_title("Sin trabajos")
        empty.set_description("Iniciá una copia desde tu file manager")
        empty.set_icon_name("folder-copy-symbolic")
        empty.set_vexpand(True)

        # Stack que alterna
        self._stack = Gtk.Stack()
        self._stack.add_titled(scrolled, "list", "Lista")
        self._stack.add_titled(empty, "empty", "Vacío")
        self._stack.set_vexpand(True)

        # ── Botones globales: pausar todos / reanudar todos / cancelar todos
        actions_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        actions_box.set_margin_start(12)
        actions_box.set_margin_end(12)
        actions_box.set_margin_top(4)
        actions_box.set_margin_bottom(8)
        actions_box.set_homogeneous(True)

        pause_all = Gtk.Button()
        pause_all.set_label("Pausar todo")
        pause_all.add_css_class("pill")
        pause_all.connect("clicked", lambda _: self._client.pause())

        resume_all = Gtk.Button()
        resume_all.set_label("Reanudar todo")
        resume_all.add_css_class("pill")
        resume_all.connect("clicked", lambda _: self._client.resume())

        cancel_all = Gtk.Button(label="Cancelar todo")
        cancel_all.add_css_class("pill")
        cancel_all.add_css_class("destructive-action")
        cancel_all.connect("clicked", lambda _: self._client.cancel())

        actions_box.append(pause_all)
        actions_box.append(resume_all)
        actions_box.append(cancel_all)

        # ── Ensamblar
        vbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        vbox.append(global_box)
        vbox.append(self._stack)
        vbox.append(actions_box)

        toolbar = Adw.ToolbarView()
        toolbar.add_top_bar(header)
        toolbar.set_content(vbox)
        self.set_content(toolbar)

    # ── listview factory ────────────────────────────────────────────
    def _on_row_setup(self, _factory, list_item: Gtk.ListItem) -> None:
        """Crea los widgets una sola vez por slot."""
        # Layout por fila: nombre + estado; path origen→destino; progress + speed + eta; botones
        grid = Gtk.Grid()
        grid.set_column_spacing(8)
        grid.set_row_spacing(2)
        grid.set_margin_top(6)
        grid.set_margin_bottom(6)
        grid.set_margin_start(10)
        grid.set_margin_end(10)

        name_lbl = Gtk.Label()
        name_lbl.set_halign(Gtk.Align.START)
        name_lbl.set_ellipsize(3)
        name_lbl.add_css_class("heading")
        grid.attach(name_lbl, 0, 0, 1, 1)

        state_lbl = Gtk.Label()
        state_lbl.set_halign(Gtk.Align.END)
        state_lbl.add_css_class("caption")
        state_lbl.add_css_class("dim-label")
        grid.attach(state_lbl, 1, 0, 1, 1)

        path_lbl = Gtk.Label()
        path_lbl.set_halign(Gtk.Align.START)
        path_lbl.set_ellipsize(3)
        path_lbl.add_css_class("caption")
        path_lbl.add_css_class("dim-label")
        grid.attach(path_lbl, 0, 1, 2, 1)

        progress = Gtk.ProgressBar()
        progress.set_show_text(False)
        progress.set_hexpand(True)
        grid.attach(progress, 0, 2, 1, 1)

        pct_lbl = Gtk.Label()
        pct_lbl.set_size_request(48, -1)
        pct_lbl.set_xalign(1.0)
        pct_lbl.add_css_class("caption")
        grid.attach(pct_lbl, 1, 2, 1, 1)

        speed_lbl = Gtk.Label()
        speed_lbl.set_halign(Gtk.Align.START)
        speed_lbl.add_css_class("caption")
        grid.attach(speed_lbl, 0, 3, 1, 1)

        eta_lbl = Gtk.Label()
        eta_lbl.set_halign(Gtk.Align.END)
        eta_lbl.set_xalign(1.0)
        eta_lbl.add_css_class("caption")
        eta_lbl.add_css_class("dim-label")
        grid.attach(eta_lbl, 1, 3, 1, 1)

        # Botones de acción
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        action_box.set_halign(Gtk.Align.END)
        pause_btn = Gtk.Button()
        pause_btn.set_icon_name("media-playback-pause-symbolic")
        pause_btn.set_tooltip_text("Pausar")
        pause_btn.add_css_class("flat")
        resume_btn = Gtk.Button()
        resume_btn.set_icon_name("media-playback-start-symbolic")
        resume_btn.set_tooltip_text("Reanudar")
        resume_btn.add_css_class("flat")
        cancel_btn = Gtk.Button()
        cancel_btn.set_icon_name("process-stop-symbolic")
        cancel_btn.set_tooltip_text("Cancelar")
        cancel_btn.add_css_class("flat")
        action_box.append(pause_btn)
        action_box.append(resume_btn)
        action_box.append(cancel_btn)
        grid.attach(action_box, 0, 4, 2, 1)

        # Guardamos refs como atributos del grid
        grid._csf_widgets = {
            "name": name_lbl, "state": state_lbl, "path": path_lbl,
            "progress": progress, "pct": pct_lbl,
            "speed": speed_lbl, "eta": eta_lbl,
            "pause": pause_btn, "resume": resume_btn, "cancel": cancel_btn,
            "action_box": action_box,
        }
        list_item.set_child(grid)

    def _on_row_bind(self, _factory, list_item: Gtk.ListItem) -> None:
        row: JobRow = list_item.get_item()
        grid: Gtk.Grid = list_item.get_child()
        w = grid._csf_widgets

        # Bindings GObject → GObject
        self._bindings.extend([
            row.bind_property("basename", w["name"], "label", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("state_label", w["state"], "label", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("source", w["path"], "label", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("progress", w["progress"], "fraction", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("progress_text", w["pct"], "label", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("speed_text", w["speed"], "label", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("eta_text", w["eta"], "label", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("can_pause", w["pause"], "visible", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("can_resume", w["resume"], "visible", GObject.BindingFlags.SYNC_CREATE),
            row.bind_property("can_cancel", w["cancel"], "visible", GObject.BindingFlags.SYNC_CREATE),
        ])

        # El path_label necesita formato especial "src → dst"
        def update_path(r, _pspec=None):
            try:
                w["path"].set_text(f"{r.source}  →  {r.dest}")
            except Exception:
                pass
        row.connect("notify::source", update_path)
        row.connect("notify::dest", update_path)
        update_path(row)

        # Click handlers de los botones
        w["pause"].connect("clicked", self._on_pause_clicked, row)
        w["resume"].connect("clicked", self._on_resume_clicked, row)
        w["cancel"].connect("clicked", self._on_cancel_clicked, row)

    def _on_row_unbind(self, _factory, list_item: Gtk.ListItem) -> None:
        self._bindings.clear()

    # ── load + events ─────────────────────────────────────────────
    def _load_initial_queue(self) -> None:
        """Snapshot inicial desde el daemon."""
        try:
            jobs = self._client.get_queue()
        except DaemonConnectionError as e:
            self._global_status.set_text(f"Daemon no disponible: {e.message}")
            return
        self._reconcile(jobs)

    def _start_event_loop(self) -> None:
        """Usa GLib.timeout_add para drenar la cola de eventos cada 100ms."""
        # El reader thread del DaemonClient empuja eventos a queue.Queue.
        # GLib.timeout_add nos da un lugar seguro para actualizar widgets
        # desde el main loop de GTK.
        GLib.timeout_add(100, self._drain_events)

    def _drain_events(self) -> bool:
        try:
            evs = self._client.drain_events()
        except Exception as e:
            self._global_status.set_text(f"event loop: {e}")
            return True
        for ev in evs:
            self._apply_event(ev)
        self._update_global_metrics()
        return True

    def _apply_event(self, ev) -> None:
        if isinstance(ev, JobStarted):
            # Insertar o actualizar
            if ev.job.id in self._rows_by_id:
                self._rows_by_id[ev.job.id].update(ev.job)
            else:
                r = JobRow(ev.job)
                self._rows_by_id[ev.job.id] = r
                self._list_store.append(r)
        elif isinstance(ev, JobProgress):
            r = self._rows_by_id.get(ev.id)
            if r is not None:
                # Actualizar speed y ETA
                speed = (ev.copied_bytes - r._job.copied_bytes) / 0.2  # 200ms entre eventos
                if speed < 0:
                    speed = 0
                r._speed = _format_speed(speed)
                remaining = max(0, ev.total_bytes - ev.copied_bytes)
                r._eta = _format_eta(remaining / speed) if speed > 1 else "—"
                r._job.copied_bytes = ev.copied_bytes
                r._job.total_bytes = ev.total_bytes
                r.update(r._job)
        elif isinstance(ev, JobCompleted):
            if ev.job.id in self._rows_by_id:
                self._rows_by_id[ev.job.id].update(ev.job)
        elif isinstance(ev, JobFailed):
            if ev.job.id in self._rows_by_id:
                self._rows_by_id[ev.job.id]._job.error = ev.error
                self._rows_by_id[ev.job.id]._job.state = JobState.FAILED
                self._rows_by_id[ev.job.id]._job.finished_at = 0
                self._rows_by_id[ev.job.id].update(self._rows_by_id[ev.job.id]._job)
        elif isinstance(ev, JobPaused):
            if ev.id in self._rows_by_id:
                self._rows_by_id[ev.id]._job.state = JobState.PAUSED
                self._rows_by_id[ev.id].update(self._rows_by_id[ev.id]._job)
        elif isinstance(ev, JobResumed):
            if ev.id in self._rows_by_id:
                self._rows_by_id[ev.id]._job.state = JobState.RUNNING
                self._rows_by_id[ev.id].update(self._rows_by_id[ev.id]._job)
        elif isinstance(ev, JobCancelled):
            if ev.id in self._rows_by_id:
                self._rows_by_id[ev.id]._job.state = JobState.CANCELLED
                self._rows_by_id[ev.id].update(self._rows_by_id[ev.id]._job)

        # Empty/list switch
        has_any = len(self._rows_by_id) > 0
        self._stack.set_visible_child_name("list" if has_any else "empty")

    def _reconcile(self, jobs: list[JobItem]) -> None:
        """Snapshot inicial: reescribe el list store."""
        self._list_store.remove_all()
        self._rows_by_id.clear()
        for j in jobs:
            r = JobRow(j)
            self._rows_by_id[j.id] = r
            self._list_store.append(r)
        self._stack.set_visible_child_name("list" if jobs else "empty")
        self._update_global_metrics()

    def _update_global_metrics(self) -> None:
        """Calcula velocidad global y ETA usando los eventos recientes."""
        now = GLib.get_monotonic_time() / 1_000_000.0
        total_copied = sum(r._job.copied_bytes for r in self._rows_by_id.values())
        total_bytes = sum(r._job.total_bytes for r in self._rows_by_id.values())
        active = [r for r in self._rows_by_id.values() if r._job.state == JobState.RUNNING]
        pending = [r for r in self._rows_by_id.values() if r._job.state == JobState.PENDING]
        done = [r for r in self._rows_by_id.values() if r._job.state.is_terminal]
        failed = [r for r in self._rows_by_id.values() if r._job.state == JobState.FAILED]

        # Velocidad global basada en delta de bytes
        if self._global_prev_ts is not None:
            dt = now - self._global_prev_ts
            if dt > 0.1:
                dbytes = total_copied - self._global_copied_prev
                if dbytes >= 0:
                    self._global_speed_bps = dbytes / dt
                self._global_prev_ts = now
                self._global_copied_prev = total_copied
        else:
            self._global_prev_ts = now
            self._global_copied_prev = total_copied

        # ETA: bytes restantes / velocidad
        remaining = max(0, total_bytes - total_copied)
        if self._global_speed_bps > 1:
            self._global_eta_s = remaining / self._global_speed_bps
        else:
            self._global_eta_s = 0

        # Texto del status global
        if not self._rows_by_id:
            self._global_status.set_text("Sin trabajos en cola")
            self._global_meta.set_text("—")
            self._global_progress.set_fraction(0.0)
        else:
            n = len(self._rows_by_id)
            status = f"{n} trabajo(s) — {len(active)} activo(s), {len(pending)} pendiente(s)"
            if failed:
                status += f" — {len(failed)} fallaron"
            self._global_status.set_text(status)

            meta = f"{_format_speed(self._global_speed_bps)}"
            if self._global_eta_s > 0 and active:
                meta += f"  ·  ETA {_format_eta(self._global_eta_s)}"
            self._global_meta.set_text(meta)

            frac = (total_copied / total_bytes) if total_bytes > 0 else 0
            self._global_progress.set_fraction(min(1.0, frac))

    # ── button handlers ────────────────────────────────────────────
    def _on_pause_clicked(self, _btn, row: JobRow) -> None:
        try:
            self._client.pause(row.id)
        except Exception as e:
            self._global_status.set_text(f"pause: {e}")

    def _on_resume_clicked(self, _btn, row: JobRow) -> None:
        try:
            self._client.resume(row.id)
        except Exception as e:
            self._global_status.set_text(f"resume: {e}")

    def _on_cancel_clicked(self, _btn, row: JobRow) -> None:
        try:
            self._client.cancel(row.id)
        except Exception as e:
            self._global_status.set_text(f"cancel: {e}")

    def _on_close_clicked(self, _btn) -> None:
        """Cerrar: ocultamos la ventana (sigue viva en el tray)."""
        self.set_visible(False)
