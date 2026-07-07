# Fase 5 — Adaptador Thunar (thunarx)
# CopySecureFast — Implementation Plan

> **Para Hermes:** ejecutar con subagent-driven-development, un subagente por tarea.

**Goal:** Una extensión para Thunar (thunarx-3) que agrega entradas en el menú
contextual: "Copiar con CopySecureFast", "Mover con CopySecureFast", "Ver cola
de CopySecureFast". Al invocarlas, encola jobs en el daemon vía csf_client.

**Architecture:** Extensión Python que Thunar carga como un módulo C embebido
vía `thunarx-python`. En Arch/CachyOS la instalación se hace mediante un
paquete Python (no oficial) llamado `thunarx-python` o, más comunmente,
compilando un wrapper con `python-gobject`. La forma más práctica en este
entorno es:

1. Crear un script Python que exporte los `MenuProvider` y `PropertyPageProvider`
2. Registrarlo en Thunar vía el sistema de extensiones C
3. En el `postinst` de un eventual paquete, instalarlo en
   `/usr/lib/thunarx-3/python/` o donde Thunar espere

Alternativa más simple y portable (la que implementamos en este spike):
- **Custom Actions (`.uca`)**: Thunar trae un sistema de acciones de usuario
  que se configuran en `~/.config/Thunar/uca.xml` o vía GUI en
  Editar → Configurar acciones personalizadas.
- Un script CLI (`csf-thunar-helper`) recibe paths por argv y habla al daemon
  vía csf_client. Thunar lo invoca con `%F` (lista de archivos).

**Decisión para el spike:** implementamos **AMBAS**:
- Una **extensión thunarx real** (preferida, ya tenemos PyGObject con Thunarx 3)
- Un **helper CLI + .uca** como fallback que funciona sin reinstalar Thunar

**Tech Stack:** Python 3.11+ · PyGObject · thunarx-3 (gi) · csf_client (ya hecho)

---

## Tareas

### Tarea 1: Helper CLI (`csf-thunar-helper`)

**Objective:** Script ejecutable que toma paths por argv y encola operaciones.

**API:**
```
csf-thunar-helper copy <dest> <file1> [file2 ...]
csf-thunar-helper move <dest> <file1> [file2 ...]
csf-thunar-helper queue       # abre csf-ui
csf-thunar-helper throttle <bps>
```

**Files:**
- Create: `thunar-extension/csf-thunar-helper.py`
- Create: `thunar-extension/copysecurefast-thunar.desktop` (entrada D-Bus opcional)

**Step 1:** Implementar el script con argparse, csf_client, csf_ui.

**Step 2:** Hacerlo ejecutable, validar que invoca csf_client correctamente.

**Step 3:** Tests con paths reales.

**Step 4:** Commit.

---

### Tarea 2: Extensión thunarx (MenuProvider)

**Objective:** Extensión Python que Thunar carga y muestra en el menú contextual.

**Files:**
- Create: `thunar-extension/copysecurefast/__init__.py`
- Create: `thunar-extension/copysecurefast/extension.py` (MenuProvider, PropertyPageProvider)
- Create: `thunar-extension/meson.build` (instalación)
- Create: `thunar-extension/README.md` (instrucciones de instalación)

**API thunarx:**
```python
class CopySecureFastExtension(GObject.Object, Thunarx.MenuProvider):
    def get_file_actions(self, window, files):
        # Devuelve una lista de Thunarx.MenuItem
        # Cada item es "Copiar con CopySecureFast", "Mover con CopySecureFast",
        # "Ver cola de CopySecureFast"
        ...
```

**Step 1:** Implementar extension.py con los 3 MenuItem.

**Step 2:** Handlers que llamen a csf_client.enqueue() o csf_ui.QueueWindow.

**Step 3:** meson.build que instale en `/usr/lib/thunarx-3/python/copysecurefast/`.

**Step 4:** Documentar en README cómo habilitar la extensión.

**Step 5:** Test manual: cargar la extensión en Thunar, verificar menú.

**Step 6:** Commit.

---

### Tarea 3: Custom Actions (.uca) — fallback

**Objective:** Archivo `uca.xml` que Thunar puede importar como acciones de
usuario, para que el helper CLI funcione sin la extensión Python.

**Files:**
- Create: `thunar-extension/copysecurefast-uca.xml`

**Step 1:** Generar XML con 3 acciones (Copy, Move, ViewQueue).

**Step 2:** Documentar instalación: copiar a `~/.config/Thunar/` y reiniciar Thunar.

**Step 3:** Commit.

---

## Notas

- En Arch/CachyOS Thunar carga extensiones thunarx nativas (C). Para Python,
  necesitamos `thunarx-python` (no oficial en Arch, requiere AUR o compilar).
- **Si no se puede cargar la extensión Python**, el .uca funciona siempre
  (es la vía oficial soportada por Thunar).
- El helper CLI es ejecutable directo, no necesita Thunar corriendo para
  probar.
