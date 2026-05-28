"""
Exclusion rules for filtering sensitive projects/chats.

Rule file: UTF-8 text. Lines starting with # or empty are ignored.
Each other line is one rule. If ANY rule matches the combined searchable text
(project title, chat title, model names, content), the item is excluded.

Rule syntax:
  - Terms separated by AND or OR (case-insensitive).
  - AND has higher precedence: "a OR b AND c" means (a) OR (b AND c).
  - Term = single word (substring match, case-insensitive) or "exact phrase" (exact phrase match).
  - One rule per line.

Example exclusion-rules.txt:
  # Exclude anything mentioning secret or internal
  secret OR internal
  "project alpha" AND confidential
  password

Note: Rules are loaded once at startup (or at the start of a CLI export run).
Changes to the exclusion rules file require an application restart (or re-running
the CLI export) to take effect.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

_logger = logging.getLogger(__name__)

# Default path when no --exclude-rules is given: ~/.cursor-chat-browser/exclusion-rules.txt
DEFAULT_EXCLUSION_RULES_FILENAME = "exclusion-rules.txt"


def get_default_exclusion_rules_path() -> str:
    """Return the path to the default exclusion rules file in the user config directory."""
    return os.path.join(str(Path.home()), ".cursor-chat-browser", DEFAULT_EXCLUSION_RULES_FILENAME)


def resolve_exclusion_rules_path(cli_path: str | None) -> str | None:
    """
    Resolve the exclusion rules file path.

    - If *cli_path* is given: expand and return its absolute path.  If the
      file doesn't exist a warning is emitted so the user knows their rules
      aren't being applied (the path is still returned so load_rules can
      explain the absence).
    - If *cli_path* is None and the default file
      (``~/.cursor-chat-browser/exclusion-rules.txt``) exists, return that.
    - Otherwise return None (no filtering).
    """
    if cli_path:
        p = os.path.abspath(os.path.expanduser(cli_path))
        if not os.path.isfile(p):
            _logger.warning(
                "Exclusion rules file not found: %s — no filtering will be applied.", p
            )
        return p
    default = get_default_exclusion_rules_path()
    if os.path.isfile(default):
        return default
    return None


def tokenize_rule(line: str) -> list:
    """
    Tokenize a rule line into terms and operators.

    Returns a list of tokens where each token is either the string ``"AND"``,
    the string ``"OR"``, or a ``(kind, value)`` tuple where *kind* is
    ``"word"`` or ``"phrase"``.
    """
    tokens: list[str | tuple[str, str]] = []
    rest = line.strip()
    while rest:
        # Skip whitespace
        m = re.match(r"\s+", rest)
        if m:
            rest = rest[m.end():]
            continue
        # AND keyword (word boundary, case-insensitive)
        if re.match(r"\bAND\b", rest, re.IGNORECASE):
            tokens.append("AND")
            rest = rest[3:].lstrip()
            continue
        # OR keyword (word boundary, case-insensitive)
        if re.match(r"\bOR\b", rest, re.IGNORECASE):
            tokens.append("OR")
            rest = rest[2:].lstrip()
            continue
        # Double-quoted phrase
        if rest.startswith('"'):
            end = rest.find('"', 1)
            if end == -1:
                # Unclosed quote: treat remainder as a word term
                tokens.append(("word", rest[1:].strip()))
                break
            tokens.append(("phrase", rest[1:end]))
            rest = rest[end + 1:].lstrip()
            continue
        # Unquoted word (until next whitespace)
        m = re.match(r"\S+", rest)
        if m:
            tokens.append(("word", m.group(0)))
            rest = rest[m.end():].lstrip()
            continue
        break
    return tokens


def _term_matches(term: tuple, text: str) -> bool:
    """
    Return True if *term* matches anywhere in *text* (case-insensitive).

    Both ``"word"`` and ``"phrase"`` terms use a case-insensitive substring
    check.  A ``"phrase"`` term matches when the quoted string appears as a
    contiguous substring (spaces included).

    .. note::
        Future versions may tighten ``"phrase"`` matching to require exact
        word-boundary anchoring (e.g. via a regex) for stricter phrase
        semantics.
    """
    _kind, value = term
    if not value:
        return False
    return value.lower() in text.lower()


def _rule_matches(tokens: list, text: str) -> bool:
    """
    Evaluate a tokenized rule against *text*.

    Operator precedence: AND binds tighter than OR, so
    ``a OR b AND c`` is parsed as ``(a) OR (b AND c)``.
    Adjacent terms without an explicit operator are treated as AND.
    """
    if not tokens:
        return False
    # Split by OR into clauses; each clause is the AND of its terms
    clauses: list[list] = []
    current: list = []
    for t in tokens:
        if t == "OR":
            if current:
                clauses.append(current)
            current = []
        elif t == "AND":
            # Explicit AND: terms are already collected sequentially, skip token
            continue
        else:
            current.append(t)
    if current:
        clauses.append(current)

    for clause in clauses:
        if not clause:
            continue
        # Clause matches when every term in it matches (implicit AND).
        # Collect tuple terms first to avoid all([]) == True on an empty sequence.
        terms = [t for t in clause if isinstance(t, tuple)]
        if terms and all(_term_matches(term, text) for term in terms):
            return True
    return False


def load_rules(path: str | None) -> list[list]:
    """
    Load and parse the exclusion rule file at *path*.

    Returns a list of tokenized rules (each rule is a list of tokens as
    produced by :func:`tokenize_rule`).  Returns an empty list when *path*
    is ``None``, the file doesn't exist, or the file cannot be read.
    """
    if not path or not os.path.isfile(path):
        return []
    rules = []
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                tokens = tokenize_rule(line)
                if tokens:
                    rules.append(tokens)
    except (OSError, UnicodeDecodeError) as e:
        _logger.warning(
            "Failed to read exclusion rules from %s (%s)",
            path,
            e.__class__.__name__,
            exc_info=True,
        )
        return []
    return rules


def is_excluded_by_rules(rules: list[list], searchable_text: str) -> bool:
    """
    Return ``True`` if *searchable_text* matches any exclusion rule.

    *searchable_text* is typically a combination of project name, chat title,
    model names, etc., joined by newlines via :func:`build_searchable_text`.
    Returns ``False`` when *rules* is empty or *searchable_text* is empty.
    """
    if not searchable_text or not rules:
        return False
    for tokenized in rules:
        if _rule_matches(tokenized, searchable_text):
            return True
    return False


def build_searchable_text(
    *,
    project_name: str | None = None,
    chat_title: str | None = None,
    model_names: list[str] | None = None,
    chat_content_snippet: str | None = None,
) -> str:
    """
    Combine chat/project metadata into a single string for rule matching.

    All non-empty, non-None parts are joined with newlines.

    The full *chat_content_snippet* is preserved so exclusion matching can
    catch terms anywhere in rendered output, including long transcripts and
    tool outputs.
    """
    parts = []
    if project_name:
        parts.append(project_name)
    if chat_title:
        parts.append(chat_title)
    if model_names:
        parts.extend(model_names)
    if chat_content_snippet:
        parts.append(chat_content_snippet)
    return "\n".join(p for p in parts if p)
