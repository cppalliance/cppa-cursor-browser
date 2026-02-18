"""
Cursor Chat Browser — Python Edition
A Flask web application for browsing and managing chat histories
from the Cursor editor's AI chat feature.
"""

from flask import Flask, render_template, send_from_directory

from api.workspaces import bp as workspaces_bp
from api.composers import bp as composers_bp
from api.logs import bp as logs_bp
from api.search import bp as search_bp
from api.export_api import bp as export_bp
from api.pdf import bp as pdf_bp
from api.config_api import bp as config_bp
from utils.exclusion_rules import resolve_exclusion_rules_path, load_rules


def create_app(exclusion_rules_path=None):
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.config["JSON_SORT_KEYS"] = False

    # Exclusion rules: optional path (CLI or default ~/.cursor-chat-browser/exclusion-rules.txt).
    # Rules are loaded once at startup; an app restart is required to pick up changes to the file.
    resolved = resolve_exclusion_rules_path(exclusion_rules_path)
    app.config["EXCLUSION_RULES_PATH"] = resolved
    app.config["EXCLUSION_RULES"] = load_rules(resolved)

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
    import sys

    exclusion_path = None
    argv = sys.argv[1:]
    i = 0
    while i < len(argv):
        if argv[i] in ("--exclude-rules", "-e") and i + 1 < len(argv):
            exclusion_path = argv[i + 1]
            i += 2
            continue
        i += 1

    app = create_app(exclusion_rules_path=exclusion_path)
    print("Cursor Chat Browser (Python) running at http://localhost:3000")
    # use_reloader=False avoids a Windows socket issue with Flask's stat reloader
    app.run(
        host="0.0.0.0",
        port=3000,
        debug=True,
        use_reloader=(sys.platform != "win32"),
    )
