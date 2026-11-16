"""Tests for spoken-punctuation normalization."""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
handler_mod = importlib.import_module("08_command_handler")


class TestNormalizeDictation(unittest.TestCase):
    def test_spoken_punctuation_after_automatic_punctuation(self):
        # Whisper emits both the mark and the spoken word; don't double up.
        self.assertEqual(
            handler_mod.normalize_dictation(
                "Yes, comma install root monitor, period Run it now"
            ),
            "Yes, install root monitor. Run it now",
        )

    def test_plain_spoken_punctuation(self):
        self.assertEqual(
            handler_mod.normalize_dictation("hello comma world period"),
            "hello, world.",
        )

    def test_duplicate_punctuation_collapses(self):
        self.assertEqual(
            handler_mod.normalize_dictation("really,,, yes..."),
            "really, yes.",
        )


if __name__ == "__main__":
    unittest.main()
