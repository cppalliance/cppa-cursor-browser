"""Resolution of the Flask debug / Werkzeug debugger flag.

Lives in `utils/` so it can be unit-tested without importing Flask
(which the test suite intentionally avoids — see tests/test_cli_args.py).
"""

from __future__ import annotations


def resolve_debug_flag(env_value: str | None, cli_flag: bool) -> bool:
    """Return True iff Flask debug / Werkzeug debugger should be enabled.

    Off by default. The Werkzeug debugger lets a remote attacker execute
    arbitrary Python in the server process, so debug mode must be opt-in
    and never the default. Enabled only when:
      - the operator explicitly passes --debug on the command line, or
      - FLASK_DEBUG is set to a truthy value ("1", "true", "yes").
    """
    if cli_flag:
        return True
    if env_value is None:
        return False
    return env_value.strip().lower() in ("1", "true", "yes")
