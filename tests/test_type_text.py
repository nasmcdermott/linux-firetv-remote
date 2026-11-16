"""Tests for dictation typing safety (no real keystrokes)."""
import importlib
import os
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
type_mod = importlib.import_module("08_command_handler")


def make_handler():
    return type_mod.CommandHandler(
        {"command": {"dictation": {"convert_numbers": False}}},
        app_names=["dummy"],
    )


class TestTypeText(unittest.TestCase):
    def test_ydotool_uses_stdin_and_length_aware_timeout(self):
        handler = make_handler()
        calls = []

        def fake_run(cmd, **kw):
            calls.append((cmd, kw))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(type_mod.shutil, "which",
                          side_effect=lambda n: "/usr/bin/ydotool" if n == "ydotool" else None):
            with patch.object(type_mod.subprocess, "run", side_effect=fake_run):
                out = handler.type_text("hello world")

        self.assertIn("typed", out)
        self.assertEqual(len(calls), 1)
        cmd, kw = calls[0]
        self.assertEqual(cmd[:2], ["ydotool", "type"])
        self.assertIn("--file", cmd)
        self.assertIn("-", cmd)
        self.assertEqual(kw["input"], "hello world")
        self.assertGreaterEqual(kw["timeout"], type_mod.TYPE_TIMEOUT_MIN)

    def test_timeout_releases_keys_and_never_falls_through(self):
        handler = make_handler()
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            if cmd[:2] == ["ydotool", "type"]:
                raise subprocess.TimeoutExpired(cmd, kw.get("timeout", 0))
            return subprocess.CompletedProcess(cmd, 0, "", "")

        with patch.object(type_mod.shutil, "which",
                          side_effect=lambda n: "/usr/bin/ydotool" if n == "ydotool" else None):
            with patch.object(type_mod.subprocess, "run", side_effect=fake_run):
                out = handler.type_text("a" * 400)

        self.assertIn("typing failed", out)
        self.assertTrue(any(c[:2] == ["ydotool", "key"] for c in calls),
                        "must send key-up recovery after a timeout")
        self.assertFalse(any(c[0] in ("wtype", "xdotool") for c in calls),
                         "must not run a second typer after partial typing")

    def test_no_typer_reports_failure(self):
        handler = make_handler()
        with patch.object(type_mod.shutil, "which", return_value=None):
            out = handler.type_text("hello")
        self.assertIn("no typer", out)


if __name__ == "__main__":
    unittest.main()
