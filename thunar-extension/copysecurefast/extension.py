"""Extensión thunarx para CopySecureFast.

Esta extensión agrega entradas al menú contextual de Thunar:
- "Copiar con CopySecureFast"
- "Mover con CopySecureFast"
- "Ver cola de CopySecureFast"

REQUISITOS:
- thunarx-python (no oficial en Arch; AUR o compilación manual)
- csf_client y csf_ui instalables como paquetes Python
- Daemon csfd corriendo

INSTALACIÓN:
- En sistemas con thunarx-python disponible, copiar este paquete a la
  ruta de extensiones Python que Thunar espera. Lo más común es:
    /usr/lib/python3.X/site-packages/copysecurefast/
  o agregar la raíz del repo a PYTHONPATH y dejar que thunarx-python
  lo descubra.

REFERENCIAS:
- https://docs.xfce.org/xfce/thunar/extend
- https://github.com/linuxmint/nemo-extensions/tree/master/nemo-python
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

# Hacemos que csf_client sea importable desde esta ubicación del repo.
_HERE = Path(__file__).resolve().parent
_REPO = _HERE.parent.parent
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Thunarx", "3.0")

from gi.repository import GObject, Gtk, Thunarx  # noqa: E402

# Importación lazy: solo cuando se invoca la acción.
# (En el momento de cargar la extensión GTK4 ya está inicializado).


def _file_path_from_thunar(file_info) -> str | None:
    """Extrae el path local de un Thunarx.FileInfo.

    Thunar entrega URIs (file://...) o nombres ya procesados según el
    backend. Esta helper los normaliza a un path absoluto local.
    """
    try:
        uri = file_info.get_uri()
    except Exception:
        uri = None
    if not uri:
        try:
            return file_info.get_location().get_path()
        except Exception:
            return None
    parsed = urlparse(uri)
    if parsed.scheme in ("file", ""):
        return unquote(parsed.path) if parsed.scheme == "file" else uri
    # URIs remotas: no soportadas en este spike.
    return None


def _selected_paths(menupath_item) -> list[str]:
    """Devuelve la lista de paths de un Thunarx.MenuPath."""
    paths = []
    # En thunarx-3 Python, get_file_list() devuelve [Thunarx.FileInfo, ...]
    for f in menupath_item.get_file_list():
        p = _file_path_from_thunar(f)
        if p:
            paths.append(p)
    return paths


class CopySecureFastMenuProvider(GObject.Object, Thunarx.MenuProvider):
    """Inyecta las acciones de CSF en el menú contextual de Thunar."""

    def __init__(self):
        super().__init__()

    # thunarx-3 pide estos tres métodos
    def get_file_menu_items(self, window, files):
        items = []
        paths = [_file_path_from_thunar(f) for f in files]
        paths = [p for p in paths if p]
        if not paths:
            return items

        for op_name, op_label, icon in [
            ("copy", "Copiar con CopySecureFast", "edit-copy"),
            ("move", "Mover con CopySecureFast", "edit-cut"),
        ]:
            item = Thunarx.MenuItem.new(
                f"csf::{op_name}::{','.join(paths)}",
                op_label,
                f"Encolar {'copia' if op_name == 'copy' else 'movimiento'} seguro en CSF",
            )
            item.set_icon(Gtk.Image.new_from_icon_name(icon))
            item.connect("activate", self._on_op_activate, op_name, paths, window)
            items.append(item)

        return items

    def get_folder_menu_items(self, window, folder):
        # También disparamos en carpetas (puede ser el destino).
        return self.get_file_menu_items(window, [folder])

    def get_drag_menu_items(self, window, folder, files):
        # Drag & drop: al soltar archivos sobre una carpeta, ofrecemos
        # copiar o mover.
        paths = [_file_path_from_thunar(f) for f in files]
        paths = [p for p in paths if p]
        items = []
        for op_name, op_label in [("copy", "Copiar aquí con CopySecureFast"),
                                  ("move", "Mover aquí con CopySecureFast")]:
            item = Thunarx.MenuItem.new(
                f"csf::drop::{op_name}",
                op_label,
                f"Encolar {'copia' if op_name == 'copy' else 'movimiento'} seguro en CSF",
            )
            item.connect("activate", self._on_drop_activate, op_name, paths, folder, window)
            items.append(item)
        return items

    # ── Handlers ──────────────────────────────────────────────────
    def _on_op_activate(self, _item, op_name: str, paths: list[str], window) -> None:
        # Cuando se activa "Copiar con CSF" sobre archivos seleccionados,
        # necesitamos que el usuario elija el destino. Lo más simple:
        # usar Gtk.FileDialog nativo.
        self._ask_dest_and_enqueue(op_name, paths, window)

    def _on_drop_activate(self, _item, op_name: str, paths: list[str], folder, window) -> None:
        # Drag & drop: el destino ya está definido (la carpeta sobre la que se soltó).
        dest = _file_path_from_thunar(folder)
        if not dest:
            return
        self._enqueue(op_name, dest, paths, window)

    def _ask_dest_and_enqueue(self, op_name: str, paths: list[str], window) -> None:
        """Pide destino al usuario con Gtk.FileDialog."""
        dialog = Gtk.FileDialog()
        dialog.set_title(f"CSF: destino de {'copia' if op_name == 'copy' else 'movimiento'}")

        # Si hay varios paths, pedimos directorio; si hay uno, podemos
        # pedir tanto archivo como directorio.
        if len(paths) == 1:
            dialog.set_accept_label("Elegir")
        else:
            dialog.set_accept_label("Copiar/mover acá")

        # FileDialog solo maneja archivos. Para elegir directorio usamos
        # la opción de seleccionar carpeta.
        from gi.repository import Gio

        def on_folder_selected(dialog, result):
            try:
                folder = dialog.select_folder_finish(result)
                if folder is None:
                    return
                dest = folder.get_path()
                self._enqueue(op_name, dest, paths, window)
            except Exception as e:
                self._notify_error(window, f"CSF: error eligiendo destino: {e}")

        def on_file_selected(dialog, result):
            try:
                f = dialog.open_finish(result)
                if f is None:
                    return
                dest = f.get_path()
                self._enqueue(op_name, dest, paths, window)
            except Exception as e:
                self._notify_error(window, f"CSF: error eligiendo destino: {e}")

        if len(paths) > 1:
            dialog.select_folder(window, None, on_folder_selected)
        else:
            # Dejamos que el usuario elija carpeta o archivo individual.
            # Para simplificar, forzamos carpeta con un FileDialog custom.
            dialog.select_folder(window, None, on_folder_selected)

    def _enqueue(self, op_name: str, dest: str, sources: list[str], window) -> None:
        from csf_client import DaemonClient, DaemonConnectionError, EnqueueItem, Operation
        op = Operation.COPY if op_name == "copy" else Operation.MOVE
        try:
            with DaemonClient() as c:
                from pathlib import Path
                dest_path = Path(dest)
                items = []
                if dest_path.is_dir():
                    for s in sources:
                        src = Path(s)
                        items.append(EnqueueItem(str(src), str(dest_path / src.name), op))
                else:
                    items.append(EnqueueItem(sources[0], dest, op))
                n = c.enqueue(items)
                self._notify(window, f"CSF: {n} trabajo(s) encolado(s)")
        except DaemonConnectionError as e:
            self._notify_error(window, f"CSF: daemon no disponible ({e})")
        except Exception as e:
            self._notify_error(window, f"CSF: error encolando: {e}")

    def _notify(self, window, msg: str) -> None:
        # GTK4: usamos Gtk.Window.invalidate() no funciona para notifs;
        # lo más portable es un toast via Adw si el window es Adw.
        # En el spike: print al log de thunar.
        print(f"[csf-thunar] {msg}")

    def _notify_error(self, window, msg: str) -> None:
        print(f"[csf-thunar] {msg}", file=sys.stderr)
