"""Confirm that this isolated run imports the candidate in this directory.

This is a name check only: the candidate remains unaccepted and is not
promoted into ``runtime/pdd.py`` by the test suite.
"""
import unittest
from pathlib import Path

import pdd


class CandidateVersionIdentityTests(unittest.TestCase):
    def test_imported_module_is_candidate_pdd(self):
        expected = (Path(__file__).resolve().parents[1] / "pdd.py").resolve()
        self.assertEqual(Path(pdd.__file__).resolve(), expected)


if __name__ == "__main__":
    unittest.main()