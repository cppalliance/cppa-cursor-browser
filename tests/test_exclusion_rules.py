"""
Tests for exclusion rules (filtering sensitive projects/chats).
Run from project root: python -m pytest tests/test_exclusion_rules.py -v
or: python -m unittest tests.test_exclusion_rules -v
"""

import os
import tempfile
import unittest

# Ensure project root is on path when running tests
import sys
from pathlib import Path

_root = Path(__file__).resolve().parent.parent
if str(_root) not in sys.path:
    sys.path.insert(0, str(_root))

from utils.exclusion_rules import (
    load_rules,
    is_excluded_by_rules,
    build_searchable_text,
    get_default_exclusion_rules_path,
    resolve_exclusion_rules_path,
)


class TestBuildSearchableText(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(build_searchable_text(), "")

    def test_project_only(self):
        self.assertEqual(
            build_searchable_text(project_name="my-project"),
            "my-project",
        )

    def test_project_and_title(self):
        t = build_searchable_text(project_name="proj", chat_title="Chat 1")
        self.assertIn("proj", t)
        self.assertIn("Chat 1", t)

    def test_model_names(self):
        t = build_searchable_text(
            project_name="p",
            chat_title="t",
            model_names=["gpt-4", "claude-3"],
        )
        self.assertIn("gpt-4", t)
        self.assertIn("claude-3", t)


class TestExclusionMatching(unittest.TestCase):
    def test_no_rules(self):
        self.assertFalse(is_excluded_by_rules([], "anything"))
        self.assertFalse(is_excluded_by_rules([], ""))

    def test_single_word_rule(self):
        rules = [[("word", "secret")]]
        self.assertTrue(is_excluded_by_rules(rules, "this is secret stuff"))
        self.assertTrue(is_excluded_by_rules(rules, "SECRET"))
        self.assertFalse(is_excluded_by_rules(rules, "public"))

    def test_phrase_rule(self):
        rules = [[("phrase", "project alpha")]]
        self.assertTrue(is_excluded_by_rules(rules, "Confidential: project alpha internal"))
        self.assertFalse(is_excluded_by_rules(rules, "project and alpha"))

    def test_or_rule(self):
        # secret OR internal
        rules = [[("word", "secret"), "OR", ("word", "internal")]]
        self.assertTrue(is_excluded_by_rules(rules, "secret data"))
        self.assertTrue(is_excluded_by_rules(rules, "internal only"))
        self.assertTrue(is_excluded_by_rules(rules, "secret internal"))
        self.assertFalse(is_excluded_by_rules(rules, "public data"))

    def test_and_rule(self):
        # foo AND bar
        rules = [[("word", "foo"), "AND", ("word", "bar")]]
        self.assertTrue(is_excluded_by_rules(rules, "foo and bar"))
        self.assertFalse(is_excluded_by_rules(rules, "foo only"))
        self.assertFalse(is_excluded_by_rules(rules, "bar only"))

    def test_and_precedence_over_or(self):
        # a OR b AND c  =>  (a) OR (b AND c)
        rules = [[("word", "a"), "OR", ("word", "b"), "AND", ("word", "c")]]
        self.assertTrue(is_excluded_by_rules(rules, "a"))
        self.assertFalse(is_excluded_by_rules(rules, "b"))
        self.assertFalse(is_excluded_by_rules(rules, "c"))
        self.assertTrue(is_excluded_by_rules(rules, "b and c"))
        self.assertTrue(is_excluded_by_rules(rules, "a or b"))

    def test_any_rule_matches(self):
        rules = [
            [("word", "x")],
            [("word", "y")],
        ]
        self.assertTrue(is_excluded_by_rules(rules, "x"))
        self.assertTrue(is_excluded_by_rules(rules, "y"))
        self.assertFalse(is_excluded_by_rules(rules, "z"))

    def test_implicit_and_adjacent_terms(self):
        """Adjacent terms without an explicit AND operator are treated as AND."""
        rules = [[("word", "foo"), ("word", "bar")]]
        self.assertTrue(is_excluded_by_rules(rules, "foo bar"))
        self.assertTrue(is_excluded_by_rules(rules, "bar and foo"))
        self.assertFalse(is_excluded_by_rules(rules, "foo only"))
        self.assertFalse(is_excluded_by_rules(rules, "bar only"))

    def test_unclosed_quote_treated_as_word(self):
        """An unclosed double-quote falls back to a plain word/substring match."""
        # Tokenizer produces ("word", "unclosed phrase") for `"unclosed phrase`
        from utils.exclusion_rules import _tokenize_rule
        tokens = _tokenize_rule('"unclosed phrase')
        self.assertEqual(len(tokens), 1)
        self.assertEqual(tokens[0][0], "word")
        rules = [tokens]
        self.assertTrue(is_excluded_by_rules(rules, "text with unclosed phrase inside"))
        self.assertFalse(is_excluded_by_rules(rules, "something unrelated"))

    def test_quoted_logical_operator_is_literal(self):
        """A quoted "AND" or "OR" is a literal term, not a boolean operator."""
        from utils.exclusion_rules import _tokenize_rule
        # "AND" (quoted) should produce a phrase token, not the "AND" string
        tokens_and = _tokenize_rule('"AND"')
        self.assertEqual(len(tokens_and), 1)
        self.assertIsInstance(tokens_and[0], tuple)
        self.assertEqual(tokens_and[0][1], "AND")

        tokens_or = _tokenize_rule('"OR"')
        self.assertEqual(len(tokens_or), 1)
        self.assertIsInstance(tokens_or[0], tuple)
        self.assertEqual(tokens_or[0][1], "OR")

        # The quoted term matches text containing the literal word
        rules_and = [tokens_and]
        self.assertTrue(is_excluded_by_rules(rules_and, "foo AND bar"))
        self.assertFalse(is_excluded_by_rules(rules_and, "foo bar"))

        rules_or = [tokens_or]
        self.assertTrue(is_excluded_by_rules(rules_or, "foo OR bar"))
        self.assertFalse(is_excluded_by_rules(rules_or, "foo bar"))


class TestLoadRules(unittest.TestCase):
    def test_none_path(self):
        self.assertEqual(load_rules(None), [])

    def test_missing_file(self):
        self.assertEqual(load_rules("/nonexistent/path/rules.txt"), [])

    def test_empty_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("")
            path = f.name
        try:
            self.assertEqual(load_rules(path), [])
        finally:
            os.unlink(path)

    def test_comments_and_blank(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write("# comment\n\n  \nsecret\n")
            path = f.name
        try:
            rules = load_rules(path)
            self.assertEqual(len(rules), 1)
            self.assertEqual(rules[0], [("word", "secret")])
        finally:
            os.unlink(path)

    def test_word_and_phrase(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            f.write('secret OR "project alpha"\n')
            path = f.name
        try:
            rules = load_rules(path)
            self.assertEqual(len(rules), 1)
            self.assertEqual(len(rules[0]), 3)  # (word, secret), OR, (phrase, project alpha)
            self.assertEqual(rules[0][0], ("word", "secret"))
            self.assertEqual(rules[0][1], "OR")
            self.assertEqual(rules[0][2], ("phrase", "project alpha"))
        finally:
            os.unlink(path)

    def test_utf8(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False) as f:
            f.write("секрет\n")
            path = f.name
        try:
            rules = load_rules(path)
            self.assertEqual(len(rules), 1)
            self.assertTrue(is_excluded_by_rules(rules, "документ секрет"))
        finally:
            os.unlink(path)


class TestResolvePath(unittest.TestCase):
    def test_default_none_when_no_file(self):
        # Default path may or may not exist; we only care that when cli_path is None
        # we get None if default file doesn't exist
        result = resolve_exclusion_rules_path(None)
        default_path = get_default_exclusion_rules_path()
        if os.path.isfile(default_path):
            self.assertEqual(result, default_path)
        else:
            self.assertIsNone(result)

    def test_cli_path_returned_when_given(self):
        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
            path = f.name
        try:
            self.assertEqual(resolve_exclusion_rules_path(path), path)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
