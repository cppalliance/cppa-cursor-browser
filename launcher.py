"""Desktop launcher for Cursor Chat Browser.

Opens the Flask app inside a native OS window via pywebview.
No HTTP server or port is used; pywebview calls the WSGI app
directly in-process.
"""

from __future__ import annotations

import argparse
import sys

from app import create_app


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="cursor-chat-browser",
        description=(
            "Cursor Chat Browser - opens the Flask app in a native OS window "
            "via pywebview (no HTTP server or port)."
        ),
    )
    parser.parse_args(argv)

    try:
        import webview
    except ImportError:
        raise SystemExit(
            "pywebview is not installed. Install the [desktop] extra, e.g.\n"
            '  pip install -e ".[desktop]"'
        ) from None

    app = create_app()
    webview.create_window(
        "Cursor Chat Browser",
        app,
        width=1280,
        height=860,
    )
    webview.start()


if __name__ == "__main__":
    main(sys.argv[1:])
