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
"""

from __future__ import annotations

import os
import re
from pathlib import Path


# Default path when no --exclude-rules is given: ~/.cursor-chat-browser/exclusion-rules.txt
DEFAULT_EXCLUSION_RULES_FILENAME = "exclusion-rules.txt"


def get_default_exclusion_rules_path() -> str:
    """Path to the default exclusion rules file in user config dir."""
    return os.path.join(str(Path.home()), ".cursor-chat-browser", DEFAULT_EXCLUSION_RULES_FILENAME)


def resolve_exclusion_rules_path(cli_path: str | None) -> str | None:
    """
    Resolve the exclusion rules file path.
    - If cli_path is given and the file exists, return it (absolute or cwd-relative).
    - Else if the default file exists in ~/.cursor-chat-browser/, return that path.
    - Else return None (no filtering).
    """
    if cli_path:
        p = os.path.abspath(os.path.expanduser(cli_path))
        if os.path.isfile(p):
            return p
        return p  # still use it; loader will report missing file
    default = get_default_exclusion_rules_path()
    if os.path.isfile(default):
        return default
    return None


def _tokenize_rule(line: str) -> list[str]:
    """
    Tokenize a rule line into terms and operators.
    Returns a list of tokens: "AND", "OR", or term (keyword or "phrase").
    """
    tokens = []
    rest = line.strip()
    while rest:
        # Skip whitespace
        m = re.match(r"\s+", rest)
        if m:
            rest = rest[m.end() :]
            continue
        # AND (word boundary)
        if re.match(r"\bAND\b", rest, re.IGNORECASE):
            tokens.append("AND")
            rest = rest[3:].lstrip()
            continue
        # OR (word boundary)
        if re.match(r"\bOR\b", rest, re.IGNORECASE):
            tokens.append("OR")
            rest = rest[2:].lstrip()
            continue
        # Double-quoted phrase
        if rest.startswith('"'):
            end = rest.find('"', 1)
            if end == -1:
                # Unclosed quote: treat remainder as one word term
                tokens.append(("word", rest[1:].strip()))
                break
            tokens.append(("phrase", rest[1:end]))
            rest = rest[end + 1 :].lstrip()
            continue
        # Single word (until space or end)
        m = re.match(r"\S+", rest)
        if m:
            tokens.append(("word", m.group(0)))
            rest = rest[m.end() :].lstrip()
            continue
        break
    return tokens


def _term_matches(term: tuple[str, str], text: str) -> bool:
    """Check if a term (word or phrase) matches in text (case-insensitive)."""
    kind, value = term
    if not value:
        return False
    text_lower = text.lower()
    if kind == "word":
        return value.lower() in text_lower
    # phrase: exact substring (case-insensitive)
    return value.lower() in text_lower


def _rule_matches(tokens: list, text: str) -> bool:
    """
    Evaluate a tokenized rule against text.
    AND has higher precedence: a OR b AND c => (a) OR (b AND c).
    """
    if not tokens:
        return False
    # Split by OR into clauses; each clause is AND of terms
    clauses = []
    current = []
    for t in tokens:
        if t == "OR":
            if current:
                clauses.append(current)
            current = []
        elif t == "AND":
            # just skip; we collect terms, AND is implicit between them
            continue
        else:
            current.append(t)
    if current:
        clauses.append(current)

    for clause in clauses:
        if not clause:
            continue
        # Clause matches if all terms match (AND)
        if all(_term_matches(term, text) for term in clause if isinstance(term, tuple)):
            return True
    return False


def load_rules(path: str | None) -> list[list]:
    """
    Load and parse the exclusion rule file.
    Returns a list of tokenized rules (each is a list of tokens).
    If path is None or file is missing/unreadable, returns [].
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
                tokens = _tokenize_rule(line)
                if tokens:
                    rules.append(tokens)
    except Exception:
        return []
    return rules


def is_excluded_by_rules(rules: list[list], searchable_text: str) -> bool:
    """
    Return True if searchable_text should be excluded (any rule matches).
    searchable_text is typically a combination of project name, chat title, model names, etc.
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
    """Build a single string to run exclusion rules against (e.g. for a chat or project)."""
    parts = []
    if project_name:
        parts.append(project_name)
    if chat_title:
        parts.append(chat_title)
    if model_names:
        parts.extend(model_names)
    if chat_content_snippet:
        # Limit size to avoid huge strings; first N chars is enough for keyword/phrase match
        snippet = chat_content_snippet
        parts.append(snippet[:50_000] if len(snippet) > 50_000 else snippet)
    return "\n".join(p for p in parts if p)
