"""CopySecureFast — UI compartida con GTK4 + libadwaita.

Usada por todos los adaptadores (Nemo, Nautilus, Thunar, Caja) y por
el ejecutable `csf-ui` para abrir la ventana de cola manualmente.
"""

from csf_ui.queue_window import QueueWindow

__all__ = ["QueueWindow"]
