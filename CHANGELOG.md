# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Web UI** — browse and search all Cursor AI workspaces; conversation view with syntax-highlighted code blocks, dark/light mode, and bookmarkable chat URLs (#63)
- **Export formats** — one-click export of chats as Markdown, HTML, PDF, JSON, and CSV from the web UI (#63)
- **CLI export** (`cursor-chat-export` / `scripts/export.py`) — zip archive or individual Markdown files with YAML frontmatter; incremental mode (`--since last`) preserves state across runs (#63, #42, #61)
- **Cursor CLI agent session support** — browse and export sessions stored in `~/.cursor/chats/` by the `cursor agent` CLI; gracefully degrades when the IDE database is absent (#7, #8, #63)
- **Desktop app packaging** — Windows `.exe` via PyInstaller + pywebview; no Python installation required on the target machine (#63)
- **Type-safe models** with schema validation at SQLite read boundaries (#24, #30)
- **CI matrix** (Linux / macOS / Windows) running pytest, mypy, and gitleaks (#13, #19, #44, #62)
- **Python packaging infrastructure** (`pyproject.toml` with hatchling, bounded dependency pins, `requirements-lock.txt`, Dependabot) (#45, #47, #49, #53)
- Optional exclusion rules for sensitive projects and chats (#1, #2)
- Full-text search with workspace and log-type filters (#63)
- Hypothesis property-based tests for blob and bubble parsing (#71, #81)
- PDF export endpoint coverage in CI (#72)

### Changed
- Extract shared `from_dict` validation helpers for model classes, reducing duplication (#70, #80)
- Enable mypy `strict-optional` and fix nullability gaps across the codebase (#69, #79)

### Fixed
- Path traversal and symlink-escape protection on `/api/set-workspace` (#15, #22)
- Disabled Werkzeug debug mode by default; opt-in via `--debug` / `FLASK_DEBUG=1` (#9, #20)
- Sanitise Marked.js HTML output with DOMPurify (#11, #21)
- Wrapped all production `sqlite3.connect()` calls in context managers (#17, #23)
- Skip NULL bubble rows in workspace tabs loader (#50, #52)
- Thread-unsafe `_workspace_path_override` race condition (#43, #54)
- Normalise Windows-style paths on non-Windows hosts (#8)
- Add incomplete-result signaling on parse failure so callers can distinguish partial vs. complete data (#67, #78)
- Replace `print()` error output with structured logging throughout (#68, #77)
- Replace silent `except Exception: pass` with structured logging in workspace and bubble load paths (#66, #76)
- Decouple API handlers from private `_`-prefixed service internals (#73)

[Unreleased]: https://github.com/cppalliance/cppa-cursor-browser/commits/HEAD
