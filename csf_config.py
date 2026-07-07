"""Configuración persistente de CopySecureFast.

Se guarda en `$XDG_CONFIG_HOME/copysecurefast/settings.json` (default
`~/.config/copysecurefast/settings.json`).

Formato JSON simple. Schema:

    {
        "throttle_bps": 0,            # 0 = sin límite
        "verify_hash": false,         # calcular SHA-256 al copiar
        "show_notifications": true,   # notify-send al terminar jobs
        "autostart_daemon": false,    # arrancar csfd al login
        "default_dest_dir": "",       # ruta por defecto (vacío = preguntar)
        "window_position": "mouse"    # mouse | top-right | remember
    }
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path


DEFAULT_SETTINGS = {
    "throttle_bps": 0,
    "verify_hash": False,
    "show_notifications": True,
    "autostart_daemon": False,
    "default_dest_dir": "",
    "window_position": "mouse",
}

CONFIG_DIR = Path(
    os.environ.get("CSF_CONFIG_DIR", Path.home() / ".config" / "copysecurefast")
)
CONFIG_FILE = CONFIG_DIR / "settings.json"


@dataclass
class Settings:
    throttle_bps: int = 0
    verify_hash: bool = False
    show_notifications: bool = True
    autostart_daemon: bool = False
    default_dest_dir: str = ""
    window_position: str = "mouse"

    def to_dict(self) -> dict:
        return asdict(self)


def _config_path() -> Path:
    """Path del archivo de config, permite override por env var."""
    override = os.environ.get("CSF_CONFIG_FILE")
    if override:
        return Path(override)
    return CONFIG_FILE


def load() -> Settings:
    """Lee settings del disco. Si no existe, devuelve defaults."""
    path = _config_path()
    if not path.exists():
        return Settings()
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return Settings()
    # Solo tomar keys conocidas
    known = {f.name for f in fields(Settings)}
    filtered = {k: v for k, v in data.items() if k in known}
    try:
        return Settings(**filtered)
    except (TypeError, ValueError):
        return Settings()


def save(settings: Settings) -> None:
    """Persiste settings. Crea el directorio si no existe."""
    path = _config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(settings.to_dict(), indent=2, sort_keys=True))
    # Atomic rename
    tmp.replace(path)
