# CopySecureFast — Documento de Contexto General

> Este documento es la **fuente de verdad** viva del proyecto.
> Se actualiza a medida que se toman decisiones técnicas y de diseño.
> Cada vez que avancemos en una fase, agregamos/ajustamos secciones aquí.

---

## 1. Visión general del producto

**CopySecureFast** es un gestor de colas de copia/movimiento que se acopla
nativamente a **los principales gestores de archivos de Linux**: Nemo,
Nautilus, Dolphin, Thunar y Caja. Ofrece una alternativa moderna a
herramientas como **TeraCopy** o **SuperCopier** de Windows, pero pensada
desde cero para Linux.

El objetivo es mejorar drásticamente la experiencia de copiar/mover archivos
en el escritorio, integrándose de forma nativa con cada gestor de archivos y
aprovechando el stack gráfico GTK/libadwaita para una UI cuidada y coherente
con el sistema.

El sufijo "Secure" no es decorativo: la herramienta **verifica la integridad
de cada copia con hash SHA-256** y, opcionalmente, **cifra el destino con
AES-256** cuando el origen o el destino son remotos (red).

### Diferenciadores clave

- **Cobertura universal**: un único paquete funciona en Cinnamon (Nemo),
  GNOME (Nautilus), KDE (Dolphin), XFCE (Thunar) y MATE (Caja). Detección
  automática en `postinst`.
- **Cola persistente** de operaciones: sobrevive a cierres del gestor y
  reinicios.
- **Velocidad limitable** por tarea y global.
- **Verificación de integridad** SHA-256 pre y post-copia configurable.
- **Vista previa** al estilo Quick Look (pulsar espacio sobre un item de
  la cola muestra hash, tamaño, ruta y velocidad estimada).
- **Transferencias remotas** con cifrado opcional AES-256 cuando aplica
  (SMB/NFS/ssh).
- UI con **estética cuidada** y compartida entre todos los adaptadores:
 Adwaita, mini-gráficos de throughput, ETA en tiempo real, animaciones
  suaves.
- Una **única cola compartida** entre gestores: si copias desde Nemo y la
  cola la querés revisar desde Dolphin, ves lo mismo.

---

## 2. Funcionalidades objetivo (MVP y más allá)

### MVP (mínimo viable)

- [ ] Integración Nemo: entradas en el menú contextual de archivos y carpetas.
- [ ] Cola unificada (pendientes / en curso / completadas / fallidas).
- [ ] Operaciones: copiar aquí, mover aquí, pegar cola.
- [ ] Progreso por tarea + progreso global.
- [ ] Velocidad en tiempo real (bytes/s) y ETA por tarea.
- [ ] Pausar / reanudar / cancelar por tarea o global.
- [ ] Manejo de conflictos: sobrescribir / saltar / renombrar (regla configurable).
- [ ] Cola persistente (recuperable tras cerrar Nemo/reiniciar).
- [ ] Hash SHA-256 configurable (off por defecto, on con un toggle).
- [ ] Vista de cola con búsqueda y filtrado.

### Nice-to-have (post-MVP)

- [ ] Limitador de velocidad (bytes/s, ajustable en vivo).
- [ ] Mini-gráfico de throughput en vivo para la tarea activa.
- [ ] Vista previa tipo Quick Look con `Space` (hash parcial + metadata).
- [ ] Notificación nativa al completar cada tarea (libnotify).
- [ ] Manejo de archivos muy grandes con copia por bloques en hilo separado.
- [ ] Plantillas de reglas de conflicto (ej. "siempre renombrar si existe").
- [ ] Tema oscuro nativo (debería "salir gratis" con libadwaita).

### Stretch (cifrado y red)

- [ ] Detección automática de URIs remotas (smb://, nfs://, sftp://).
- [ ] Cifrado AES-256-GCM del archivo destino con passphrase derivable (Argon2).
- [ ] Verificación de integridad end-to-end con hash sobre el contenido cifrado.
- [ ] Empaquetado opcional del sidecar (`.csec`) con metadata (hash original,
  fecha, fuente).

---

## 3. Arquitectura y stack técnico

### Decisiones confirmadas (2026-07-03)

| Componente         | Decisión                                                            |
|--------------------|---------------------------------------------------------------------|
| Lenguaje frontend  | **Python 3** con **PyGObject** y **PyQt6 alternatives** (ver §3.1)  |
| Toolkit UI         | **GTK 4** + **libadwaita** (compartido entre todos los adaptadores) |
| Adaptadores        | **Nemo** (`libnemo-extension`), **Nautilus** (`libnautilus-extension`), **Thunar/Caja** (`thunarx`/libnautilus) y **Dolphin** (KDE ServiceMenus con `.desktop` + `Exec=`) |
| Backend I/O        | **Rust** (binario daemon, expuesto por socket Unix + protocolo JSON) |
| Comunicación       | **JSON-RPC** sobre socket Unix (rápido, sin red)                    |
| Persistencia cola  | **SQLite** (un archivo `~/.local/share/copysecurefast/queue.db`)    |
| Hashing            | librust `sha2` (Rust) + `hashlib.sha256` para verificación parcial  |
| Cifrado            | librust `aes-gcm` + `argon2` para derivación de clave               |
| Empaquetado        | `meson` + `cargo` (build unificado) + `.deb` único con detección de gestor en `postinst` |
| Notificaciones     | `libnotify` vía PyGObject                                           |
| Configuración      | GSettings (esquema propio) o TOML en `~/.config/copysecurefast/`   |

### 3.1 Estrategia multi-gestor

Los cinco gestores se dividen en **dos familias**:

**A. Familias con API de extensión nativa (Nemo / Nautilus / Thunar / Caja)**

- Cargan un módulo Python (`MenuProvider`) en su proceso.
- El módulo registra items en el menú contextual y encola jobs en el
  daemon vía socket Unix.
- Adaptadores comparten la mayor parte del código (la lógica de UI vive en
  un paquete `csf_ui/` y cada adaptador solo monta el `MenuProvider` con
  un nombre de clase distinto).

Detalle importante: **Nautilus y Caja comparten API** (Caja es fork de
Nautilus) → un solo adaptador sirve a ambos. **Thunar** usa `python-thunarx`
con API similar → otro adaptador.

**B. Dolphin (KDE) — ServiceMenus**

Dolphin no tiene API de extensión cargable; usa archivos `.desktop` con
acciones (`Exec=comando %F`) que aparecen en el menú contextual.

- Instalamos `copysecurefast-dolphin.desktop` con tres acciones:
  `CopyHere`, `MoveHere`, `PasteQueue`. Cada `Exec=` invoca el cliente
  Python (`csf-dolphin-helper`) que habla con el daemon.
- La UI de la cola se abre en una ventana GTK4 independiente, pero
  **funciona idéntica** a la usada por los otros adaptadores.
- En `postinst` detectamos Dolphin (`which dolphin`) y copiamos el
  `.desktop` a `/usr/share/kservices5/ServiceMenus/`.

### 3.2 Estructura de directorios (actualizada)

```
CopySecureFast/
├── docs/
│   ├── CONTEXTO.md            ← este archivo
│   └── plans/                 ← planes por fase
├── nemo-extension/            ← adaptador Nemo (libnemo-extension)
├── nautilus-extension/        ← adaptor Nautilus + Caja (libnautilus-extension)
├── thunar-extension/          ← adaptador Thunar (thunarx)
├── dolphin-helper/            ← ejecutable + .desktop que invoca Dolphin
├── csf_ui/                    ← **UI compartida** (ventana de cola, preview, dialogs)
├── csf_client/                ← cliente Python compartido (JSON-RPC) → daemon
├── daemon/                    ← binario Rust (todo el motor vive acá)
│   ├── src/
│   │   ├── main.rs
│   │   ├── rpc/
│   │   ├── queue/
│   │   ├── ops/               ← copy/move/hash/encrypt
│   │   └── throttle/
│   ├── tests/
│   └── Cargo.toml
├── packaging/                 ← .deb único
│   └── debian/postinst        ← detecta gestores y activa adaptadores
├── meson.build                ← build unificado
└── README.md
```

### 3.3 Diagrama lógico

```
  ┌──────────────────────────────────────────────────────────────────────┐
  │                       Gestores de archivos (5)                       │
  │  ┌─────┐  ┌──────────┐  ┌──────────┐  ┌─────┐  ┌───────────────┐   │
  │  │Nemo │  │ Nautilus │  │  Thunar  │  │ Caja│  │    Dolphin     │   │
  │  │ API │  │  Caja    │  │  thunarx │  │ API │  │  ServiceMenu   │   │
  │  │native│ │   API    │  │   API    │  │native│ │   (.desktop)   │   │
  │  └──┬──┘  └────┬─────┘  └────┬─────┘  └──┬──┘  └───────┬───────┘   │
  └─────┼──────────┼─────────────┼────────────┼─────────────┼───────────┘
        │          │             │            │             │
        ▼          ▼             ▼            ▼             ▼
   (adaptador)(adaptador)   (adaptador)  (adaptador)   (csf-dolphin-helper)
        │          │             │            │             │
        └──────────┴─────────────┴────────────┴─────────────┘
                                  │
                                  ▼
                  ┌──────────────────────────────┐
                  │        csf_ui + csf_client    │
                  │     (UI compartida, JSON-RPC) │
                  └──────────────┬───────────────┘
                                 │  unix socket
                                 ▼
                  ┌──────────────────────────────┐
                  │   Daemon Rust (csfd/csfd)     │
                  │  - cola persistente (SQLite)  │
                  │  - copy/sendfile              │
                  │  - hash SHA-256               │
                  │  - AES-256 (opcional)         │
                  │  - limitador velocidad        │
                  └──────────────────────────────┘
```

---

## 4. Estructura de directorios propuesta

```
CopySecureFast/
├── docs/
│   └── CONTEXTO.md            ← este archivo
├── nemo-extension/            ← paquete Python (entrada en menú)
│   ├── copysecurefast_nemo/
│   │   ├── __init__.py
│   │   ├── extension.py       ← NemoExtension, MenuProvider
│   │   ├── client.py          ← cliente JSON-RPC al daemon
│   │   └── ui/
│   │       ├── queue_window.py
│   │       ├── preview.py     ← vista Quick-Look
│   │       └── dialogs.py
│   ├── meson.build
│   └── pyproject.toml
├── daemon/                    ← binario Rust
│   ├── src/
│   │   ├── main.rs            ← bootstrap, CLI args, socket
│   │   ├── rpc/               ← JSON-RPC server
│   │   ├── queue/             ← cola persistente (SQLite)
│   │   ├── ops/               ← copy/move/hash/encrypt
│   │   └── throttle/          ← limitador de velocidad
│   ├── tests/
│   └── Cargo.toml
├── packaging/
│   ├── debian/
│   └── meson.build            ← un build raíz que orquesta Python + Rust
└── README.md
```

---

## 5. Fases de desarrollo

| Fase | Objetivo                                          | Estado        |
|------|---------------------------------------------------|---------------|
| 1    | Boceto de arquitectura + documento de contexto    | ✅ definido   |
| 2    | Spike del backend Rust: copia básica + cola SQLite + socket Unix | ✅ hecho |
| 3    | Spike csf_client + csf_ui (UI compartida, JSON-RPC)| ✅ hecho      |
| 4    | Adaptador Nemo (libnemo-extension) + cola + dialog| ⏳ pendiente  |
| 5    | Adaptador Nautilus/Caja + Adaptador Thunar        | 🟡 Thunar ✅, Nautilus pendiente |
| 6    | MVP integrado (progreso, pausa, conflictos, hash)  | 🟡 parcial: progreso+hash ✅, pausa/cancel/conflictos pendiente |
| 7    | Limitador de velocidad + vista Quick-Look + Dolphin (.desktop)    | 🟡 throttle ✅, Quick-Look + Dolphin pendiente |
| 8    | Cifrado AES-256 + soporte SMB/NFS/ssh             | ⏳ pendiente  |
| 9    | Pulido UI, notificaciones, empaquetado .deb       | ⏳ pendiente  |

> A medida que avancemos, iremos completando esta tabla y documentando
> particularidades que aparezcan en cada fase (lecciones aprendidas,
> decisiones tardías, regresiones, etc.).

---

## 6. Glosario y nombres internos

- **CSF** — CopySecureFast (forma corta en código y logs).
- **Job** — una unidad de trabajo en la cola (un archivo o una carpeta).
- **Task** — synonym informal de Job (evitar mezclar en código).
- **Sidecar `.csec`** — archivo metadata que acompaña un destino cifrado.
- **Daemon URI** — `unix:///run/user/<uid>/copysecurefast.sock` (a confirmar).

---

## 7. Preguntas abiertas / cosas por decidir

- ¿Nombre corto del binario daemon? `csfd` (CopySecureFast Daemon) es el candidato.
- ¿Qué esquema de GSettings usar para preferencias, o un TOML simple?
- ¿La cola persistente debe ser SQLite o un LMDB más liviano?
- ¿El cifrado AES debe ser por archivo o por bloque (streaming)?
- ¿Qué patrón usar para exponer la UI desde Nemo: ventana flotante, panel
  acoplable, o ambos?
- ¿Soporte de Wayland puro (sin XWayland) desde la primera versión? Hoy Nemo
  en Wayland todavía tiene zonas grises; lo dejaremos como requisito blando.

---

## 8. Changelog de decisiones

- **2026-07-03** — Stack híbrido Rust + Python/GTK4. UI estilo Adwaita con
  mini-gráficos de throughput. Verificación SHA-256 parte del MVP.
  Cobertura inicial: Nemo como único objetivo.
- **2026-07-03 (rev 2)** — Ampliación a **Nemo + Nautilus + Dolphin + Thunar +
  Caja**. UI única compartida entre todos. Empaquetado único con detección
  automática de gestores en `postinst`. Dolphin se integra vía KDE ServiceMenus
  (`.desktop` con `Exec=`), no API de extensión. Cartera de fases ampliada de 8
  a 9 para incorporar los adaptadores adicionales explícitamente.
- **2026-07-07** — Fases 2 y 3 + Thunar de Fase 5 implementadas en una sesión.
  Decisiones/pivotes que cambiaron el plan original:
  - **SQLite Send**: `rusqlite::Connection` no es `Send` → tuvimos que envolver
    en `Mutex<Connection>` para que el `tokio::spawn` del RPC server compile.
  - **Sin `libc`/`filetime`**: el plan original los incluía, pero `filetime 0.4`
    no existe en crates.io y `libc::EXDEV` se reemplaza con un fallback
    copy+unlink genérico.
  - **Tag-content serde**: el cliente Python aprendió que cuando un `Request`
    variant no tiene fields (como `Ping`), NO se debe enviar la clave `params`
    (sino `{}`), porque serde con `tag = "method"` rechaza unit variants con
    content. Documentado en `csf_client/daemon.py`.
  - **Adaptador Thunar vía .uca, no thunarx-python**: la extensión thunarx
    Python requeriría `thunarx-python` (AUR en Arch). El sistema nativo
    "Custom Actions" (`.uca.xml`) funciona sin instalar nada y es la vía que
    terminamos adoptando. La extensión thunarx Python igual está escrita en
    `thunar-extension/copysecurefast/extension.py` para quien quiera usarla.
  - **Wrapper global**: instalamos un wrapper en `~/.local/bin/csf-thunar-helper`
    que apunta al script del repo. Sin sudo, sin paquete Debian aún.
  - **Auto-discovery de socket**: el helper busca en
    `CSF_SOCKET > XDG_RUNTIME_DIR > /run/user/UID > /tmp` para que funcione
    sin tener que exportar variables.
  - **Self-copy + colisiones**: el helper detecta origen==destino y renombra
    automáticamente las colisiones (`foo (1).txt`) para no perder datos.
  - **GtkListView manual refresh**: PyGObject no permite GObject.Binding
    cuando un lado es una dataclass, así que el `JobRow` (GObject) envuelve
    al `JobItem` (dataclass) y los bindings van a través del wrapper.
