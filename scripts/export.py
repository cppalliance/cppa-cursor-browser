#!/usr/bin/env python3
"""
CLI: Export Cursor chats to Markdown (zip archive by default).
Usage: python scripts/export.py [--since all|last] [--out DIR] [--no-zip] [--no-composer]
Run with --help for full usage information.
Env: WORKSPACE_PATH for Cursor workspaceStorage path.

When the package is installed via ``pip install -e .`` (or ``pip install .``),
this module is importable as ``scripts.export`` without any sys.path hacks.
The guard below is only necessary for direct invocation (``python scripts/export.py``).
"""

from __future__ import annotations

import json
import logging
import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict

# sys.path guard: only needed when the script is invoked directly
# (``python scripts/export.py``). When installed via the pyproject.toml
# entry point (``cursor-chat-export``) or imported as a module, the
# project root is already on sys.path.
if __name__ == "__main__":
    _project_root = Path(__file__).resolve().parent.parent
    if str(_project_root) not in sys.path:
        sys.path.insert(0, str(_project_root))

from models import ExportEntry, SchemaError  # noqa: E402
from services.export_engine import collect_export_entries  # noqa: E402
from utils.exclusion_rules import (  # noqa: E402
    load_rules,
    resolve_exclusion_rules_path,
)
from utils.path_helpers import to_epoch_ms  # noqa: E402
from utils.workspace_path import resolve_workspace_path  # noqa: E402

_logger = logging.getLogger(__name__)


class ExportCliOptions(TypedDict):
    since: Literal["all", "last"]
    out_dir: str
    include_composer: bool
    zip: bool
    exclusion_rules_path: str | None
    base_dir: str | None


def configure_cli_logging() -> None:
    """Route log records to stderr so stdout stays for export progress lines."""
    root = logging.getLogger()
    if root.handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )


def load_manifest_entries(manifest_path: str) -> dict[str, dict[str, object]]:
    """Load manifest entries keyed by log_id from a JSONL file."""
    existing: dict[str, dict[str, object]] = {}
    if not os.path.isfile(manifest_path):
        return existing
    try:
        with open(manifest_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = ExportEntry.from_dict(json.loads(line))
                    existing[entry.log_id] = entry.raw
                except (SchemaError, json.JSONDecodeError, ValueError) as e:
                    # Pre-PR-30 manifests lack title/workspace — skip them so the
                    # next export rebuilds the entry under the new schema.
                    _logger.debug("Skipping manifest line in %s: %s", manifest_path, e)
    except OSError as e:
        _logger.debug("Failed to read manifest %s: %s", manifest_path, e)
    return existing


def write_manifest_entries(
    manifest_path: str,
    entries_by_id: dict[str, dict[str, object]],
) -> None:
    """Write manifest entries to JSONL."""
    os.makedirs(os.path.dirname(manifest_path), exist_ok=True)
    with open(manifest_path, "w", encoding="utf-8") as f:
        for entry in entries_by_id.values():
            f.write(json.dumps(entry) + "\n")


def get_global_state_dir() -> str:
    # Honor XDG_STATE_HOME when set so the export state file (and manifest)
    # can be redirected — required for hermetic test runs and useful for
    # users following the XDG Base Directory spec. Falls back to the
    # historical ~/.cursor-chat-browser location when the env var is unset.
    xdg = os.environ.get("XDG_STATE_HOME")
    if xdg:
        return os.path.join(xdg, "cursor-chat-browser")
    return os.path.join(str(Path.home()), ".cursor-chat-browser")


def parse_args() -> ExportCliOptions:
    import argparse

    parser = argparse.ArgumentParser(
        description="Export Cursor chat history to Markdown files.",
        epilog=(
            "By default exports ALL chats (including composer logs) as a zip archive\n"
            "into the current directory. Use the flags below to narrow the export.\n\n"
            "Env: WORKSPACE_PATH overrides the Cursor workspaceStorage path."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--since",
        choices=["all", "last"],
        default="all",
        help="Export all chats or only those updated since last export. Default: all",
    )
    parser.add_argument(
        "--out",
        default=".",
        help="Output directory. Default: current working directory (.)",
    )
    parser.add_argument(
        "--no-zip",
        action="store_true",
        default=False,
        help="Write individual Markdown files instead of a zip archive.",
    )
    parser.add_argument(
        "--no-composer",
        action="store_true",
        default=False,
        help="Exclude composer logs (export only chat logs).",
    )
    parser.add_argument(
        "--base-dir",
        default=None,
        help="Override Cursor workspaceStorage path (also settable via WORKSPACE_PATH env var).",
    )
    parser.add_argument(
        "--exclude-rules",
        "-e",
        default=None,
        metavar="PATH",
        dest="exclude_rules",
        help="Path to exclusion rules file (sensitive projects/chats are omitted). "
        "If omitted, uses ~/.cursor-chat-browser/exclusion-rules.txt if present.",
    )
    args = parser.parse_args()
    return {
        "since": args.since,
        "out_dir": args.out,
        "include_composer": not args.no_composer,
        "zip": not args.no_zip,
        "exclusion_rules_path": args.exclude_rules,
        "base_dir": args.base_dir,
    }


def _read_last_export_ms(state_path: str, since: Literal["all", "last"]) -> int:
    if since != "last" or not os.path.isfile(state_path):
        return 0
    try:
        with open(state_path, "r", encoding="utf-8") as f:
            st = json.load(f)
        ts = st.get("lastExportTime")
        if ts:
            return to_epoch_ms(ts)
    except (json.JSONDecodeError, ValueError, OSError) as e:
        _logger.warning(
            "Could not read last export timestamp; defaulting to full export: %s",
            e,
        )
    return 0


def main() -> None:
    configure_cli_logging()
    opts = parse_args()
    since = opts["since"]
    out_dir = os.path.abspath(opts["out_dir"])
    use_zip = opts["zip"]
    exclusion_rules = load_rules(
        resolve_exclusion_rules_path(opts.get("exclusion_rules_path")),
    )
    workspace_path = resolve_workspace_path(override=opts.get("base_dir"))

    state_dir = get_global_state_dir()
    state_path = os.path.join(state_dir, "export_state.json")
    last_export = _read_last_export_ms(state_path, since)

    exported = collect_export_entries(
        workspace_path=workspace_path,
        exclusion_rules=exclusion_rules,
        since=since,
        last_export_ms=last_export,
        out_dir=out_dir,
        include_composer=opts.get("include_composer", True),
    )
    count = len(exported)

    if count == 0:
        label = " since last export" if since == "last" else ""
        print(f"No conversations found{label}.")
        sys.exit(0)

    os.makedirs(out_dir, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")

    if use_zip:
        zip_name = f"cursor-export-{today}.zip"
        zip_path = os.path.join(out_dir, zip_name)
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in exported:
                zf.writestr(entry["rel_path"], entry["content"])
        print(f"Exported {count} chat(s) to {zip_path}")
    else:
        for entry in exported:
            os.makedirs(os.path.dirname(entry["out_path"]), exist_ok=True)
            with open(entry["out_path"], "w", encoding="utf-8") as f:
                f.write(entry["content"])

        manifest_path = os.path.join(out_dir, "manifest.jsonl")
        existing = load_manifest_entries(manifest_path)
        for entry in exported:
            existing[entry["id"]] = {
                "log_id": entry["id"],
                "title": entry["title"],
                "workspace": entry["workspace"],
                "path": os.path.relpath(entry["out_path"], out_dir),
                "updated_at": (
                    datetime.fromtimestamp(entry["updatedAt"] / 1000).isoformat()
                    if entry["updatedAt"]
                    else datetime.now().isoformat()
                ),
            }
        if existing:
            write_manifest_entries(manifest_path, existing)

        global_manifest_path = os.path.join(state_dir, "manifest.jsonl")
        global_existing = load_manifest_entries(global_manifest_path)
        for entry in exported:
            global_existing[entry["id"]] = {
                "log_id": entry["id"],
                "title": entry["title"],
                "workspace": entry["workspace"],
                "path": entry["out_path"],
                "updated_at": (
                    datetime.fromtimestamp(entry["updatedAt"] / 1000).isoformat()
                    if entry["updatedAt"]
                    else datetime.now().isoformat()
                ),
            }
        if global_existing:
            write_manifest_entries(global_manifest_path, global_existing)
        print(f"Exported {count} chat(s) to {out_dir}")

    state = {
        "lastExportTime": datetime.now().isoformat(),
        "exportedCount": count,
        "exportDir": out_dir,
    }
    os.makedirs(state_dir, exist_ok=True)
    with open(os.path.join(state_dir, "export_state.json"), "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)

    print(f"State saved to {os.path.join(state_dir, 'export_state.json')}")


if __name__ == "__main__":
    main()
