"""Confirm that this isolated run imports the working operator, not the candidate.

The synthetic environment is prepared by ``runtime/tests/__init__.py`` before
``pdd`` is imported. This test makes the checked file explicit so a wrong
``PYTHONPATH`` or working directory cannot silently verify another version.
"""
import unittest
from pathlib import Path

import pdd


class WorkingVersionIdentityTests(unittest.TestCase):
    def test_imported_module_is_runtime_pdd(self):
        expected = (Path(__file__).resolve().parents[1] / "pdd.py").resolve()
        self.assertEqual(Path(pdd.__file__).resolve(), expected)


if __name__ == "__main__":
    unittest.main()