"""pyproject.toml lists the frozen v0.1 files twice (lint exemptions, no-reformat). Keep them equal."""

from __future__ import annotations

import glob
import os
import tomllib
import unittest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def _lists() -> tuple[set[str], set[str]]:
    with open(os.path.join(ROOT, "pyproject.toml"), "rb") as fh:
        ruff = tomllib.load(fh)["tool"]["ruff"]
    keys = [k for k in ruff["lint"]["per-file-ignores"] if k.startswith("{")]
    assert len(keys) == 1, keys
    lint = set(keys[0].strip("{}").split(","))
    fmt = {p for p in ruff["format"]["exclude"] if p != "*.md"}
    # the formatter's exclude uses a directory glob where the linter names the files
    fmt = {"relay/*.py" if p == "relay/**" else p for p in fmt}
    return lint, fmt


class TestFrozenV01List(unittest.TestCase):
    def test_lint_and_format_lists_match(self):
        lint, fmt = _lists()
        self.assertEqual(lint - fmt, set(), "linted as v0.1 but reformatted")
        self.assertEqual(fmt - lint, set(), "kept unformatted but linted as v0.2")

    def test_every_listed_file_exists(self):
        lint, _ = _lists()
        for pattern in sorted(lint):
            with self.subTest(pattern=pattern):
                self.assertTrue(glob.glob(os.path.join(ROOT, pattern)), f"{pattern} matches nothing")


if __name__ == "__main__":
    unittest.main()
