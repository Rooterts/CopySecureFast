# Fase 3 — Spike: Cliente Python + UI GTK4
# CopySecureFast — Implementation Plan

> **Para Hermes:** ejecutar con subagent-driven-development, un subagente por tarea.

**Goal:** Un cliente Python (`csf_client`) que se conecta al daemon por socket
Unix y una UI GTK4 (`csf_ui`) que muestra la cola de jobs en vivo. El cliente
debe ser importable por todos los adaptadores (Nemo, Nautilus, Thunar, Caja,
Dolphin) y la UI debe ser la misma para todos.

**Architecture:**
- `csf_client/` — paquete Python puro sin GTK. Habla JSON-RPC con el daemon.
- `csf_ui/` — paquete Python con GTK4/libadwaita. Consume csf_client.
- Comunicación cliente↔daemon: una línea = un JSON. Conexión long-lived
  opcional para escuchar eventos `job_update`.

**Tech Stack:** Python 3.11+ · PyGObject (GTK4) · libadwaita (opcional,
adapta a GTK4 puro si falta) · Zero deps externas (sin requests, etc.).

---

## Tareas

### Tarea 1: csf_client — Protocolo + transporte JSON-RPC

**Objective:** Módulo Python que envía/recibe mensajes JSON-RPC al daemon por
socket Unix. API simple: `client.enqueue(items)`, `client.get_queue()`,
`client.set_throttle(bps)`, `client.ping()`.

**Files:**
- Create: `csf_client/__init__.py`
- Create: `csf_client/protocol.py` (dataclasses que espejan `daemon/src/types.rs`)
- Create: `csf_client/daemon.py` (clase `DaemonClient` con connect/send)
- Create: `csf_client/exceptions.py`
- Create: `pyproject.toml` (paquete instalable)
- Create: `tests/test_client.py` (smoke tests contra el daemon real)

**API mínima:**

```python
from csf_client import DaemonClient, EnqueueItem, Operation

with DaemonClient() as c:
    c.ping()                                  # bool
    jobs = c.get_queue()                      # list[JobItem]
    c.enqueue([EnqueueItem("/src", "/dst", Operation.COPY, verify_hash=True)])
    c.set_throttle(1_048_576)                 # 1 MB/s
```

**Step 1: pyproject.toml** con setuptools, sin deps externas, Python 3.11+.

**Step 2: exceptions.py** — `DaemonError(Exception)`, `ConnectionError`,
`ProtocolError`. Jerarquía clara.

**Step 3: protocol.py** — dataclasses:
- `Operation(str, Enum)`: COPY, MOVE
- `JobState(str, Enum)`: PENDING, RUNNING, PAUSED, COMPLETED, FAILED, CANCELLED
- `JobItem`: con `from_dict()` y `to_dict()` (compatibles con serde)
- `EnqueueItem`: source, dest, op, verify_hash

**Step 4: daemon.py** — clase `DaemonClient`:
- Context manager (`__enter__`/`__exit__`)
- `connect()` lazy en primera llamada, o explícito
- `send(method, **params) -> dict` — bajo nivel
- Wrappers tipados: `ping()`, `get_queue()`, `enqueue(items)`,
  `set_throttle(bps)`, `cancel(job_id=None)`, `pause(job_id=None)`,
  `resume(job_id=None)`
- Reconexión automática si el socket se cae (best effort, log warning)
- Path del socket desde env `CSF_SOCKET` o default
  `$XDG_RUNTIME_DIR/copysecurefast.sock`

**Step 5: tests** — smoke tests que arranquen el daemon, hagan ping/enqueue/
get_queue y validen. Marcados como `slow` para correr bajo demanda.

**Step 6: Commit.**

---

### Tarea 2: csf_ui — Ventana de cola GTK4

**Objective:** Ventana GTK4 con libadwaita (si está) o GTK4 puro (fallback)
que muestra la cola en una lista (GtkListView/GtkColumnView) con:
- Nombre del archivo (source basename → dest basename)
- Estado (icono + texto)
- Barra de progreso (GtkProgressBar)
- Velocidad y ETA
- Botones: pausar, reanudar, cancelar, abrir carpeta destino
- Botón flotante para ajustar throttle

**Files:**
- Create: `csf_ui/__init__.py`
- Create: `csf_ui/queue_window.py` (ventana principal con header bar)
- Create: `csf_ui/job_row.py` (factory para filas de la lista)
- Create: `csf_ui/throttle_popover.py` (popover para limitar velocidad)
- Create: `csf_ui/main.py` (CLI: `csf-ui` muestra la ventana)
- Create: `pyproject.toml` entry point `csf-ui = csf_ui.main:main`

**Step 1:** Estructura de paquetes.
**Step 2:** `queue_window.py` con Adw.ApplicationWindow.
**Step 3:** `job_row.py` con Gtk.ColumnViewColumn para cada campo.
**Step 4:** Polling cada 500ms que llame a `daemon.get_queue()`.
**Step 5:** Botones con handlers que llamen al daemon.
**Step 6:** `main.py` con `Adw.Application.run()`.
**Step 7:** Commit.

---

### Tarea 3: Verificación end-to-end

**Objective:** Probar csf_client + csf_ui contra el daemon real.

**Steps:**
1. Arrancar daemon en background.
2. Encolar 3 archivos (al menos uno grande, ~50MB).
3. Abrir `csf-ui` y verificar que la cola aparece y progresa.
4. Probar pause/resume/cancel desde la UI.
5. Tests automatizados: `pytest tests/`.
6. Capturar screenshot de la UI para confirmar look & feel.
7. Commit final de la fase.

---

## Notas

- libadwaita no es estrictamente necesario para el spike. Si está
  disponible se usa (`gi.require_version('Adw', '1')`); si no,
  fallback a Gtk.ApplicationWindow con header bar manual.
- Para Wayland puro hay zonas grises con Gdk.Display; por ahora
  aceptamos XWayland.
- El polling cada 500ms es suficiente para el spike. En la fase 6 se
  puede migrar a eventos push (cambiar el protocolo a subscriptions
  bidireccionales).
