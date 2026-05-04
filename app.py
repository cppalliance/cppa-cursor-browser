"""
Cursor Chat Browser — Python Edition
A Flask web application for browsing and managing chat histories
from the Cursor editor's AI chat feature.
"""

import os
import sys
from datetime import datetime
from pathlib import Path

from flask import Flask, render_template, send_from_directory

from utils.debug_flag import resolve_debug_flag

from api.workspaces import bp as workspaces_bp
from api.composers import bp as composers_bp
from api.logs import bp as logs_bp
from api.search import bp as search_bp
from api.export_api import bp as export_bp
from api.pdf import bp as pdf_bp
from api.config_api import bp as config_bp
from utils.exclusion_rules import resolve_exclusion_rules_path, load_rules


def _get_base_path():
    """Return the directory that contains templates/ and static/.

    In a PyInstaller bundle the files live under sys._MEIPASS;
    otherwise they sit next to this source file.
    """
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def create_app(exclusion_rules_path=None):
    base = _get_base_path()
    app = Flask(
        __name__,
        static_folder=str(base / "static"),
        template_folder=str(base / "templates"),
    )
    app.config["JSON_SORT_KEYS"] = False

    # Exclusion rules: optional path (CLI or default ~/.cursor-chat-browser/exclusion-rules.txt).
    # Rules are loaded once at startup; an app restart is required to pick up changes to the file.
    resolved = resolve_exclusion_rules_path(exclusion_rules_path)
    app.config["EXCLUSION_RULES_PATH"] = resolved
    app.config["EXCLUSION_RULES"] = load_rules(resolved)

    @app.context_processor
    def inject_year():
        return {"current_year": datetime.now().year}

    # Register API blueprints
    app.register_blueprint(workspaces_bp)
    app.register_blueprint(composers_bp)
    app.register_blueprint(logs_bp)
    app.register_blueprint(search_bp)
    app.register_blueprint(export_bp)
    app.register_blueprint(pdf_bp)
    app.register_blueprint(config_bp)

    # ---------- Page routes ----------

    @app.route("/")
    def home():
        return render_template("index.html")

    @app.route("/config")
    def config_page():
        return render_template("config.html")

    @app.route("/search")
    def search_page():
        return render_template("search.html")

    @app.route("/workspace/<workspace_id>")
    def workspace_page(workspace_id):
        return render_template("workspace.html", workspace_id=workspace_id)

    # Serve favicon
    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(app.static_folder, "favicon.ico", mimetype="image/x-icon")

    return app


if __name__ == "__main__":
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Cursor Chat Browser (Python)")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--base-dir", default=None,
                        help="Override Cursor workspaceStorage path")
    parser.add_argument(
        "--exclude-rules", "-e",
        default=None,
        metavar="PATH",
        help="Path to exclusion rules file (sensitive projects/chats are omitted). "
             "If omitted, uses ~/.cursor-chat-browser/exclusion-rules.txt if present.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable Flask debug mode and the Werkzeug debugger. "
             "DANGEROUS: allows remote code execution if the port is exposed. "
             "Off by default; can also be enabled via FLASK_DEBUG=1.",
    )
    args = parser.parse_args()

    if args.base_dir:
        from utils.workspace_path import set_workspace_path_override
        set_workspace_path_override(args.base_dir)

    app = create_app(exclusion_rules_path=args.exclude_rules)
    print(f"Cursor Chat Browser (Python) running at http://{args.host}:{args.port}")

    debug_enabled = resolve_debug_flag(os.environ.get("FLASK_DEBUG"), args.debug)
    if debug_enabled:
        # Print the warning to stderr so it's visible even when stdout is
        # piped/redirected. The Werkzeug debugger is a remote-code-execution
        # primitive — anyone reaching the host:port can hijack the process.
        print(
            "WARNING: Flask debug mode ENABLED. The Werkzeug debugger allows "
            "arbitrary code execution by anyone who can reach this server. "
            "Bind only to 127.0.0.1 and never expose to untrusted networks.",
            file=sys.stderr,
        )

    # Disable reloader on Windows to avoid a socket conflict with Flask's stat reloader.
    app.run(
        host=args.host,
        port=args.port,
        debug=debug_enabled,
        use_reloader=debug_enabled and (sys.platform != "win32"),
    )
