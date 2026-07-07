# CopySecureFast - Project Overview

> Living document. Single source of truth for project goals, architecture
> and decisions. Update when phases land or when a meaningful pivot
> happens.

---

## 1. Product vision

**CopySecureFast** is a copy/move queue manager that integrates natively
with **the major Linux file managers**: Nemo, Nautilus, Dolphin, Thunar
and Caja. It offers a modern alternative to Windows-only tools like
**TeraCopy** or **SuperCopier**, designed from scratch for Linux.

The goal is to drastically improve the copy/move experience on the
Linux desktop, integrating with each file manager through its native
extension API and using the GTK/libadwaita stack for a cohesive UI.

The "Secure" in the name is not decorative: the tool **verifies the
integrity of every copy with a SHA-256 hash** and optionally **encrypts
the destination with AES-256** when the source or destination is remote.

### Key differentiators

- **Universal coverage**: a single package works in Cinnamon (Nemo),
  GNOME (Nautilus), KDE (Dolphin), XfCE (Thunar) and MATE (Caja). The
  appropriate adapter is enabled automatically per environment.
- **Persistent queue**: the queue survives file-manager restarts and
  reboots (via SQLite at `~/.local/share/copysecurefast/queue.db`).
- **Per-job and global speed limit**, configurable in real time.
- **Integrity verification** (SHA-256 pre and post copy, optional).
- **TeraCopy-style floating window** with live progress, ETA and per-job
  pause/resume/cancel.
- **Single shared queue**: if you copy from Nemo and want to review
  the queue from Dolphin, you see the same jobs.

---

## 2. Goals (MVP and beyond)

### MVP (Minimum Viable)

- [x] Thunar integration via .uca (Custom Actions).
- [x] Floating queue window with per-job progress.
- [x] Operations: copy, move, paste.
- [x] Per-task and global progress.
- [x] Real-time speed (bytes/s) and ETA per task.
- [x] Pause / resume / cancel per task or globally.
- [x] Hash SHA-256 optional.
- [x] System tray icon with quick actions.
- [x] Speed limit (bytes/s, adjustable on the fly).

### Nice-to-have (post-MVP)

- [ ] Drag and drop visual feedback.
- [ ] Native notifications on completion.
- [ ] Very large files via spawn_blocking.
- [ ] Conflict policies (overwrite / skip / rename, configurable).
- [ ] Dark theme (free with libadwaita).

### Stretch (encryption and network)

- [ ] Auto-detect remote URIs (smb://, nfs://, sftp://).
- [ ] AES-256-GCM encryption of the destination file with a
  passphrase-derivable key (Argon2).
- [ ] End-to-end integrity verification on encrypted content.
- [ ] Optional `.csec` sidecar with metadata (original hash, date,
  source).

---

## 3. Architecture and tech stack

### Confirmed decisions

| Component         | Decision                                                            |
|-------------------|---------------------------------------------------------------------|
| Daemon language   | **Rust** (single binary, async I/O via tokio)                       |
| Daemon IPC        | **JSON-RPC** over Unix socket (no network overhead)                 |
| Daemon storage    | **SQLite** (single file at `~/.local/share/copysecurefast/queue.db`)|
| Client language   | **Python 3.10+** with PyGObject                                     |
| UI toolkit        | **GTK 4** + **libadwaita** for the queue window                    |
| Tray icon         | **GTK 3** + **AyatanaAppIndicator3** (mandatory, SNI protocol)     |
| Hashing           | `sha2` (Rust) — streaming, computed during the copy pass           |
| File manager      | **Thunar** (primary, via .uca), **Nemo** (planned, via NemoExtension)|
| Configuration     | JSON at `~/.config/copysecurefast/settings.json`                    |
| Packaging         | Single Cargo project + shell wrappers, no .deb yet                 |
| Logging           | `tracing` (file + stdout, daily rotation)                           |

### Multi-file-manager strategy

The five file managers split into two families:

**A. Native extension API (Nemo / Nautilus / Thunar / Caja)**
- Load a Python module (`MenuProvider`) in their process.
- The module registers context-menu items and enqueues jobs in the
  daemon via the Unix socket.
- Adapters share most of the code (UI lives in `csf_ui/` and each
  adapter just mounts the `MenuProvider` with a different class name).

**B. Dolphin (KDE) - ServiceMenus**
- Dolphin has no loadable extension API; it uses `.desktop` files
  with `Exec=` actions.
- We'd install `copysecurefast-dolphin.desktop` with three actions.
- The Dolphin queue UI is a standalone GTK4 window (same one used
  by the other adapters).

### Code structure

```
CopySecureFast/
├── README.md              <- quick start
├── docs/
│   ├── OVERVIEW.md         <- this file (single source of truth)
│   ├── plans/              <- per-phase implementation plans
│   └── CHANGELOG.md        <- per-commit decisions log
├── daemon/                 <- Rust binary `csfd`
│   ├── Cargo.toml
│   ├── src/
│   │   ├── main.rs         <- bootstrap, CLI args, tray spawn
│   │   ├── types.rs        <- JSON-RPC types (mirror in csf_client)
│   │   ├── queue/db.rs     <- SQLite persistence
│   │   ├── ops/copy.rs     <- copy/move/hash engine
│   │   └── rpc/server.rs   <- Unix socket + worker loop
│   └── tests/
├── csf_client/             <- Python client library (used by all UI)
├── csf_ui/                 <- GTK4 queue window (shared across adapters)
│   ├── queue_window.py
│   ├── tray.py             <- GTK3 system tray (separate process)
│   └── settings_menu.py    <- settings submenu
├── csf_config.py           <- persistent settings (JSON, atomic write)
├── thunar-extension/        <- .uca + CLI helper for Thunar
├── tests/                   <- Python unit tests
└── pyproject.toml
```

### Data flow

```
┌──────────────────────────────────────────────────────────────────────┐
│                     File managers (5)                               │
│  ┌─────┐  ┌──────────┐  ┌──────────┐  ┌─────┐  ┌───────────────┐   │
│  │Nemo │  │ Nautilus │  │  Thunar  │  │ Caja│  │    Dolphin     │   │
│  │ API │  │   API    │  │  .uca    │  │ API │  │  ServiceMenu   │   │
│  │native│ │  native  │  │  action  │  │native│ │  (.desktop)   │   │
│  └──┬──┘  └────┬─────┘  └────┬─────┘  └──┬──┘  └───────┬───────┘   │
└─────┼──────────┼─────────────┼────────────┼─────────────┼───────────┘
      │          │             │            │             │
      ▼          ▼             ▼            ▼             ▼
   (adapter)  (adapter)    (helper)    (adapter)    (csf-dolphin-helper)
      │          │             │            │             │
      └──────────┴─────────────┴────────────┴─────────────┘
                                │
                                ▼
                  ┌──────────────────────────────┐
                  │        csf_ui + csf_client    │
                  │     (shared UI, JSON-RPC)     │
                  └──────────────┬───────────────┘
                                 │  Unix socket
                                 ▼
                  ┌──────────────────────────────┐
                  │   Daemon Rust (csfd)         │
                  │  - persistent queue (SQLite) │
                  │  - copy/sendfile + hash      │
                  │  - speed limit (token bucket)│
                  │  - cooperative pause/cancel   │
                  └──────────────────────────────┘
```

---

## 4. Phases

| Phase | Goal                                            | Status      |
|-------|-------------------------------------------------|-------------|
| 1     | Architecture sketch + context document          | done        |
| 2     | Rust daemon spike: copy + SQLite + Unix socket   | done        |
| 3     | csf_client + csf_ui (shared UI, JSON-RPC)        | done        |
| 4     | Nemo adapter (libnemo-extension)                 | pending     |
| 5     | Nautilus/Caja + Thunar adapters                   | Thunar done |
| 6     | MVP integration (progress, pause, conflict, hash) | partial     |
| 7     | Speed limit + Quick-Look + Dolphin (.desktop)    | partial     |
| 8     | AES-256 encryption + SMB/NFS/ssh support         | pending     |
| 9     | UI polish, notifications, .deb packaging         | pending     |

---

## 5. Glossary and internal names

- **CSF** - CopySecureFast (shorthand in code and logs).
- **Job** - a unit of work in the queue (one file or one folder).
- **Task** - informal synonym of Job (avoid mixing in code).
- **`.csec` sidecar** - metadata file that accompanies an encrypted destination.
- **Daemon URI** - `unix:///run/user/<uid>/copysecurefast.sock` (or `/tmp/copysecurefast.sock` fallback).

---

## 6. Open questions

- Schema for GSettings vs plain TOML config?
- Should the queue be SQLite or a lighter LMDB?
- AES encryption: per-file or per-block (streaming)?
- Wayland pure support (no XWayland)? Thunar on Wayland still has
  rough edges; we'll treat it as a soft requirement.

---

## 7. Changelog

### 2026-07-03
Stack: hybrid Rust + Python/GTK4. UI Adwaita style with throughput
mini-charts. SHA-256 verification part of MVP. Initial coverage: Nemo
as the only target.

### 2026-07-03 (rev 2)
Expanded to **Nemo + Nautilus + Dolphin + Thunar + Caja**. Single shared
UI across all. Single package with auto-detection of file managers in
`postinst`. Dolphin via KDE ServiceMenus (`.desktop` with `Exec=`).

### 2026-07-07
Phases 2, 3 and Thunar (phase 5) implemented. Notable pivots:
- `Mutex<Connection>` wrapping the rusqlite connection (it's not `Send`).
- Skipped `libc`/`filetime` deps (4.x not available, EXDEV handled by
  copy+unlink fallback).
- Thunar integration via `.uca` (Custom Actions) instead of thunarx-python
  (AUR-only). The thunarx Python module is still shipped for completeness.
- Wrapper script `csfd-tray` that lives at `~/.local/bin/csfd-tray` and
  delegates to the repo's `csf_ui/tray.py`. Handles the case where
  the binary doesn't exist in `$PATH`.
- Auto-discovery of the socket in the helper
  (`CSF_SOCKET > XDG_RUNTIME_DIR > /run/user/UID > /tmp`).
- `csfd-tray` requires **Gtk 3** (AyatanaAppIndicator3 doesn't work with
  Gtk 4), so the tray and the queue window are separate processes.
- Recursive directory expansion in the helper (`_walk_files`).
- Floating window stays compact (380x420) and not maximised; closed
  with the X = hidden, not destroyed (continues running in the tray).
- Settings persisted to `~/.config/copysecurefast/settings.json` with
  atomic write (`.tmp` + rename).
- Tray icon has a Settings submenu with throttle presets, hash toggle,
  notifications, autostart, "open config" and "reload".
