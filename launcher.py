"""Desktop launcher for Cursor Chat Browser.

Opens the Flask app inside a native OS window via pywebview.
No HTTP server or port is used; pywebview calls the WSGI app
directly in-process.
"""

from app import create_app


def main():
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
    main()
