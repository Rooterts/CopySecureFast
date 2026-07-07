# CopySecureFast

> A TeraCopy/SuperCopier-style copy/move queue for Linux, integrated
> with Thunar (and other file managers planned). Rust daemon + Python
> GTK4 UI + system tray.

![Status: prototype - works on Thunar](https://img.shields.io/badge/status-prototype-orange)
![License: GPL--3.0](https://img.shields.io/badge/license-GPL--3.0-blue)
![Rust + Python](https://img.shields.io/badge/stack-Rust%20%2B%20Python-orange)

CopySecureFast adds a **copy/move queue with pause/resume, speed
limit, SHA-256 verification and live progress** to your file manager,
without replacing it. The TeraCopy/SuperCopier flow, on Linux.

## Features

- **TeraCopy-style flow** in Thunar: right-click `Copy with CSF` on
  files, then right-click `Paste with CSF` on the destination folder.
- **Floating queue window** with live progress, per-job speed, ETA,
  and pause/resume/cancel buttons.
- **System tray icon** (auto-spawned by the daemon) with quick actions
  and a Settings submenu.
- **Persistent queue** survives daemon restarts (SQLite at
  `~/.local/share/copysecurefast/queue.db`).
- **SHA-256 verification** optional per job.
- **Speed limit** (B/s) configurable at runtime from the tray.
- **No external dependencies** beyond what Arch/CachyOS already has
  (`libayatana-appindicator3`, `python-gobject`, GTK3/GTK4).

## Architecture

```
┌──────────┐   ┌───────────────┐   ┌─────────────────────────────┐
│  Thunar  │──>│ csfd-tray     │──>│  csfd (Rust daemon)          │
│  .uca    │   │ (Python GTK3) │   │  - JSON-RPC over Unix socket│
└──────────┘   └───────────────┘   │  - SQLite queue             │
                                  │  - copy/move/hash engine    │
                                  └─────────────────────────────┘
                                            ▲
                                  ┌─────────┴─────────┐
                                  │ csf-ui (Python GTK4)│
                                  │  floating window   │
                                  └────────────────────┘
```

- `csfd` is the Rust daemon: handles the queue, copy/move/hash
  operations, and the JSON-RPC over Unix socket.
- `csfd-tray` is a separate Python process with the system tray icon
  (uses `AyatanaAppIndicator3`, which only works on Gtk 3).
- `csf-ui` is a Python GTK4/libadwaita app: the floating queue window
  (uses Gtk 4 because of the modern Adwaita look).
- `thunar-extension/csf-thunar-helper.py` is the CLI the `.uca` actions
  invoke; it talks to `csfd` over the socket.

## Quick start

### Requirements

Arch / CachyOS:

```bash
sudo pacman -S rust python python-gobject gtk4 libadwaita \
                libayatana-appindicator zenity
```

For tests: `cargo test` (bundled), `python -m unittest` (stdlib).

### Build

```bash
git clone https://github.com/your-user/CopySecureFast.git
cd CopySecureFast

# Build the Rust daemon (release)
cd daemon
cargo build --release
cd ..

# Install Python packages (editable / dev mode)
pip install --user -e .
# or just make sure the repo is on PYTHONPATH
```

### Run

```bash
# 1. Start the daemon (auto-spawns the tray)
./daemon/target/release/csfd

# 2. Install Thunar custom actions
cp thunar-extension/copysecurefast-uca.xml ~/.config/Thunar/uca.xml
# (or merge the <action> elements into your existing uca.xml)

# 3. Restart Thunar (close all windows, reopen)
pkill thunar && thunar &

# 4. Install the wrapper scripts in your PATH
cp csfd-tray ~/.local/bin/csfd-tray
cp thunar-extension/csf-thunar-helper.py ~/.local/bin/csf-thunar-helper
chmod +x ~/.local/bin/csfd-tray ~/.local/bin/csf-thunar-helper
```

That's it. In Thunar:

- Right-click files/folders → **Copiar con CopySecureFast**
- Right-click destination folder → **Pegar con CopySecureFast**

A tray icon (folder) appears in your system tray. Click the icon to
show/hide the floating queue window, right-click for the menu
(including Settings).

### Manual test (without Thunar)

```bash
# 1. Start the daemon in a terminal
csfd

# 2. In another terminal
csf-thunar-helper copy /etc/hosts /tmp/csf-test
csf-thunar-helper paste /tmp/csf-test
csf-thunar-helper status
# Watch the queue in the tray window
csf-thunar-helper queue
```

## Configuration

Settings live in `~/.config/copysecurefast/settings.json` (overridable
via `CSF_CONFIG_FILE` env var). Default values:

```json
{
  "throttle_bps": 0,
  "verify_hash": false,
  "show_notifications": true,
  "autostart_daemon": false,
  "default_dest_dir": "",
  "window_position": "mouse"
}
```

Edit them via the tray menu (`Settings` submenu) or directly. The
config file is written atomically (`.tmp` + rename).

## Tests

```bash
# Rust (8 tests, ~1s)
cd daemon && cargo test

# Python (8 tests, skip the integration one if no daemon)
cd .. && python -m unittest tests.test_client
```

## Project structure

```
CopySecureFast/
├── README.md              <- this file
├── pyproject.toml         <- Python package config
├── docs/
│   ├── OVERVIEW.md        <- architecture, decisions, phases
│   └── plans/             <- per-phase plans (historical, .implemented)
├── daemon/                <- Rust binary `csfd`
│   ├── Cargo.toml
│   ├── src/
│   │   ├── main.rs
│   │   ├── types.rs
│   │   ├── queue/db.rs
│   │   ├── ops/copy.rs
│   │   └── rpc/server.rs
│   └── tests/             <- cargo test target
├── csf_client/            <- Python client (JSON-RPC)
├── csf_ui/                <- Python GTK4 queue window + GTK3 tray
├── csf_config.py          <- settings persistence
├── thunar-extension/       <- .uca + helper CLI for Thunar
├── tests/                  <- Python unit tests
└── csfd-tray              <- shell wrapper for the tray process
```

## Roadmap

Implemented:
- Rust daemon (copy/move/hash, SQLite queue, JSON-RPC)
- Python client with event subscription
- Floating queue window (GTK4)
- System tray icon (GTK3 + AyatanaAppIndicator3)
- Thunar integration (Custom Actions)
- Settings persistence + tray menu
- Cooperative pause/cancel
- Recursive folder copy/move

Planned:
- Nemo adapter (libnemo-extension)
- Nautilus + Caja adapter (shared via libnautilus-extension)
- Dolphin adapter via KDE ServiceMenus (.desktop)
- AES-256 encryption for remote destinations (SMB/NFS/ssh)
- Native notifications on completion
- Configurable conflict policies (overwrite/skip/rename)
- .deb packaging

## License

GPL-3.0-or-later.

## Credits

Inspired by **TeraCopy** (Windows) and **Ultracopier** (cross-platform).
Neither of them works well on Thunar/XfCE, so this project fills the gap.
