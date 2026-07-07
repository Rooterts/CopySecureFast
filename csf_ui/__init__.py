"""CopySecureFast — UI compartida con GTK4 + libadwaita.

Dos modos de uso:
- `csf-ui`: ventana flotante de cola (la app que muestra el progreso).
- `csfd-tray`: binario separado que solo vive en la system tray y
  abre/cierra la ventana flotante al hacer click.
"""

from csf_ui.queue_window import QueueWindow

__all__ = ["QueueWindow"]
