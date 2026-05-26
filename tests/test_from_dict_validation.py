from __future__ import annotations

import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from models.errors import SchemaError
from models.from_dict_validation import (
    require_non_empty_str_field,
    require_non_empty_str_fields,
)


class RequireNonEmptyStrFieldMessages(unittest.TestCase):
    def test_absent_key_raises_missing_required_field(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            require_non_empty_str_field({}, "composerId", model="TestModel")
        self.assertEqual(cm.exception.field, "composerId")
        self.assertIn("missing required field", str(cm.exception))
        self.assertNotIn("invalid field", str(cm.exception))

    def test_wrong_type_raises_invalid_field(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            require_non_empty_str_field(
                {"composerId": 123},
                "composerId",
                model="TestModel",
            )
        self.assertEqual(cm.exception.field, "composerId")
        self.assertIn("invalid field", str(cm.exception))
        self.assertIn("expected non-empty str, got int", str(cm.exception))
        self.assertNotIn("missing required field", str(cm.exception))


class RequireNonEmptyStrFieldsMessages(unittest.TestCase):
    def test_absent_key_raises_missing_required_field(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            require_non_empty_str_fields(
                {"title": "x", "workspace": "w"},
                ("log_id", "title", "workspace"),
                model="ExportEntry",
            )
        self.assertEqual(cm.exception.field, "log_id")
        self.assertIn("missing required field", str(cm.exception))
        self.assertNotIn("invalid field", str(cm.exception))

    def test_wrong_type_raises_invalid_field(self) -> None:
        with self.assertRaises(SchemaError) as cm:
            require_non_empty_str_fields(
                {"log_id": 1, "title": "x", "workspace": "w"},
                ("log_id", "title", "workspace"),
                model="ExportEntry",
            )
        self.assertEqual(cm.exception.field, "log_id")
        self.assertIn("invalid field", str(cm.exception))
        self.assertIn("expected non-empty str, got int", str(cm.exception))
        self.assertNotIn("missing required field", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
