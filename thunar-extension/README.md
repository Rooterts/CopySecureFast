# CopySecureFast — Adaptador Thunar

Adaptador de CopySecureFast para el gestor de archivos **Thunar** (Xfce).

## Modos de integración

Este paquete ofrece **dos formas** de integrar CSF con Thunar. Podés usar
una o ambas según tu setup:

### A) Custom Actions (`.uca`) — recomendado, sin instalación

Thunar trae un sistema nativo de "acciones de usuario" que se configuran
sin instalar nada: el archivo `copysecurefast-uca.xml` define las
acciones y Thunar las muestra en el menú contextual al iniciar.

Instalación:

1. Compilá el binario daemon y dejá `csf-thunar-helper.py` accesible:
   ```bash
   cd /path/to/CopySecureFast
   pip install --user .  # o editable: pip install --user -e .
   csf-thunar-helper ping   # smoke test
   ```

2. Copiá el archivo de acciones a tu config de Thunar:
   ```bash
   cp copysecurefast-uca.xml ~/.config/Thunar/uca.xml
   ```
   (Si ya tenés un `uca.xml`, agregá las acciones dentro de `<actions>`
   con IDs únicos.)

3. Reiniciá Thunar (cerrá todas las ventanas) y volvé a abrirlo.

Aparecen tres entradas en el menú contextual sobre archivos/carpetas:
- "Copiar con CopySecureFast" → `csf-thunar-helper copy %F <dest>`
- "Mover con CopySecureFast" → `csf-thunar-helper move %F <dest>`
- "Ver cola de CopySecureFast" → `csf-thunar-helper queue`

### B) Extensión thunarx nativa — más integración

Si querés que las entradas se inyecten directamente en el menú contextual
sin configurar nada, instalá la extensión Python `copysecurefast/`.

**Dependencia extra:** `thunarx-python` (en Arch requiere AUR). Como
Thunar carga extensiones Python solo si `thunarx-python` está instalado,
este modo es opcional.

Instalación (si tenés `thunarx-python`):

```bash
# Copia el módulo a donde Thunar espera las extensiones Python.
# El path varía; en Arch suele ser /usr/lib/python3.X/site-packages/
# o /usr/lib/thunarx-3/python/
sudo cp -r copysecurefast /usr/lib/thunarx-3/python/
```

Reiniciá Thunar.

## Requisitos

- Daemon `csfd` corriendo (arrancá con `csfd` en una terminal, o agregalo
  a tu sesión de autostart).
- `csf_client` Python importable (`pip install --user .` desde la raíz
  del repo).
- Para abrir la UI: GTK4 + libadwaita + `csf_ui` instalado.

## Desarrollo

```bash
# Probar el helper sin Thunar
CSF_DATA_DIR=/tmp/csfd CSF_RUNTIME_DIR=/tmp csfd &
CSF_SOCKET=/tmp/copysecurefast.sock csf-thunar-helper status

# Encolar manualmente
CSF_SOCKET=/tmp/copysecurefast.sock csf-thunar-helper copy /tmp/destino /tmp/origen

# Abrir la UI
CSF_SOCKET=/tmp/copysecurefast.sock csf-thunar-helper queue
```

## Limitaciones del spike

- El helper encola UN job por archivo; los directorios no se desglosan
  recursivamente todavía.
- `pause`, `resume` y `cancel` están como stubs en el daemon; en el
  helper se exponen pero devuelven error.
- La UI es la misma para todos los adaptadores; no hay UI específica
  de Thunar.
