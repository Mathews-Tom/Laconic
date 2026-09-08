from __future__ import annotations

import unittest

from assignment_parser import AssignmentError, parse_assignment


class AssignmentParserTests(unittest.TestCase):
    def test_trailing_comment_is_removed(self) -> None:
        self.assertEqual(parse_assignment("COLOR=blue # display color"), ("COLOR", "blue"))

    def test_hash_inside_quotes_is_preserved(self) -> None:
        self.assertEqual(parse_assignment('COLOR="#336699" # palette'), ("COLOR", "#336699"))

    def test_escaped_space_is_preserved(self) -> None:
        self.assertEqual(parse_assignment(r"TITLE=hello\ world"), ("TITLE", "hello world"))

    def test_malformed_assignment_is_rejected(self) -> None:
        with self.assertRaises(AssignmentError):
            parse_assignment("not-an-assignment")


if __name__ == "__main__":
    unittest.main()
