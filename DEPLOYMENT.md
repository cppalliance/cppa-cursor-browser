# Deployment and Threading

Cursor Chat Browser is a **local, single-user** tool for reading Cursor chat history. It binds to `127.0.0.1` by default and is not designed as a multi-tenant internet-facing service. This page documents supported WSGI configurations, threading guarantees, and path-configuration trust boundaries.

## Quick start (production WSGI)

Install runtime dependencies, then serve with gunicorn (Linux/macOS) or waitress (cross-platform):

```bash
pip install -r requirements-lock.txt   # or: pip install -e .
pip install gunicorn                     # Linux / macOS
# pip install waitress                   # Windows-friendly alternative (see below)

# Multi-process (recommended): one thread per worker avoids any per-worker state surprises.
# WEB_CONCURRENCY (or CURSOR_BROWSER_MULTI_WORKER=1) lets the app detect multi-worker mode so
# POST /api/set-workspace returns 409 instead of a misleading 200 on a single worker.
WEB_CONCURRENCY=2 gunicorn --factory --bind 127.0.0.1:3000 --workers 2 --threads 1 app:create_app

# Single-process, multi-threaded: safe after the #43 lock; useful for lighter deployments.
gunicorn --factory --bind 127.0.0.1:3000 --workers 1 --threads 4 app:create_app
```

**Waitress** (works well on Windows):

```bash
pip install waitress
python -c "from waitress import serve; from app import create_app; serve(create_app(), host='127.0.0.1', port=3000, threads=4)"
```

Set `WORKSPACE_PATH` (and optionally `CLI_CHATS_PATH`) in the environment **before** starting the server when the data directory is known at launch time — see [Path configuration](#path-configuration) below.

Do **not** enable Flask debug mode in production (`--debug` / `FLASK_DEBUG=1`). The Werkzeug debugger allows arbitrary code execution by anyone who can reach the port.

## Development server

```bash
python app.py
```

This uses Flask/Werkzeug's built-in server. It is **single-threaded by default** (`threaded=False`), which is appropriate for local development. Requests are handled one at a time; there is no concurrent access to in-process state.

Werkzeug's reloader is disabled on Windows to avoid socket conflicts; on other platforms it follows the debug flag.

## Threading guarantee

The web app keeps one piece of shared mutable state: the module-level workspace path override in `utils/workspace_path.py`, written by `POST /api/set-workspace` (or `--base-dir` at startup). Reads and writes are serialized with `threading.Lock` (issue #43), so **multi-threaded deployment within a single OS process is supported**. Regression tests in `tests/test_workspace_path_thread_safety.py` exercise concurrent set/resolve under thread pools.

| Concurrency model | Supported? | Notes |
|-------------------|------------|-------|
| Single-threaded (dev server, `--threads 1`) | Yes | Simplest; default for `python app.py`. |
| Multi-threaded, single process (`--workers 1 --threads N`, waitress) | Yes | Override protected by lock; safe for concurrent API requests. |
| Multi-process (`--workers N`, N > 1) | Yes, with caveats | Each worker is a separate process with **its own memory**. See [Multi-process deployments](#multi-process-deployments). |

SQLite reads are opened per request and closed via context managers; connections are not shared across threads.

## WSGI servers

| Server | Role | Threading notes |
|--------|------|-----------------|
| **Werkzeug** (via `python app.py`) | Local development | Single-threaded by default; safe without extra configuration. |
| **gunicorn** | Production (Linux/macOS) | Use `--factory app:create_app`. Prefer `--workers N --threads 1` for multi-process, or `--workers 1 --threads N` for threaded single-process. |
| **waitress** | Production (all platforms, especially Windows) | Multi-threaded by default; compatible with the workspace-path lock. |
| **pywebview** (desktop `.exe`) | Desktop GUI | No HTTP server or port; calls the WSGI app in-process. Request handling is serialized by the embedded server — equivalent to single-threaded from the app's perspective. |

gunicorn and waitress are **not** runtime dependencies; install them only when deploying behind a production WSGI server.

## Multi-process deployments

When gunicorn runs with `--workers 2` (or more), each worker is an independent Python process:

- **`POST /api/set-workspace`** cannot update every worker from a single request (each process has its own override). The API returns **HTTP 409** with code `set_workspace_multi_worker_unsupported` instead of success when multiple workers are detected (`WEB_CONCURRENCY`, `GUNICORN_WORKERS`, gunicorn `--workers` in `GUNICORN_CMD_ARGS`, or `CURSOR_BROWSER_MULTI_WORKER=1`). Set `WORKSPACE_PATH` or pass `--base-dir` at process start so every worker sees the same path.
- **Exclusion rules** (`EXCLUSION_RULES` in app config) are loaded once at worker startup from `--exclude-rules` or the default file. Changing the rules file requires restarting workers.

For a single-user localhost deployment with multiple workers, set the workspace path via environment variable or `--base-dir` at startup; do not rely on the Configuration page `set-workspace` call.

## Path configuration

Workspace path resolution order: **runtime override** (`POST /api/set-workspace` or `--base-dir`) → **`WORKSPACE_PATH` environment variable** → **OS auto-detection** (see README Configuration table).

### Trust boundaries

| Mechanism | Validation | Intended use |
|-----------|------------|--------------|
| `POST /api/set-workspace`, `POST /api/validate-path` | Canonical path (`realpath`), directory checks, Cursor workspace markers (`state.vscdb` in immediate subdirectories) | Web UI and API callers; safe for interactive use. |
| `WORKSPACE_PATH` env var | Tilde expansion only (`~` → home directory) | **Trusted-operator** escape hatch for automation, systemd units, and containers where the path is already known good. Not a substitute for API validation when input may be untrusted. |
| `--base-dir` CLI flag | None (passed through to the same override as set-workspace) | Startup override for operators who control the launch command. |
| OS auto-detection | N/A | Default when no override or env var is set. |

`CLI_CHATS_PATH` follows the same “tilde-expanded env var, trusted operator” model for Cursor CLI agent sessions under `~/.cursor/chats/`.

## Desktop mode (pywebview)

The Windows desktop build (`cursor-browser.spec` / `CursorChatBrowser.exe`) embeds the Flask app via [pywebview](https://pywebview.flowrl.com/). The UI is rendered in a native window (Edge WebView2 on Windows); no HTTP port is opened. Threading semantics match a single-threaded in-process WSGI server.

Install the desktop extra when building locally: `pip install -e ".[desktop]"`.

## CLI export (no web server)

`cursor-chat-export` / `python scripts/export.py` does not start Flask and has no threading or WSGI concerns. It reads `WORKSPACE_PATH` from the environment (or platform defaults) in a single process.

## Known limitations

- **Localhost-first**: Default bind address is `127.0.0.1`. Exposing the app on `0.0.0.0` without additional access controls is discouraged.
- **No authentication**: The API assumes a trusted local operator.
- **Exclusion rules**: Loaded at app startup; restart required after editing the rules file.
- **Read-only data access**: The app reads Cursor's SQLite databases; it does not write to Cursor storage.
