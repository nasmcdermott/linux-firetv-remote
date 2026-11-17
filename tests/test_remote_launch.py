"""Tests for remote_launch key/scan matching (no hardware)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import remote_launch


def button(name: str, key: int, scan: int | None, action: str = "true") -> dict:
    return {"name": name, "key": key, "scan": scan, "action": action}


class TestMatchButton(unittest.TestCase):
    def test_missing_config_has_no_buttons(self):
        with tempfile.TemporaryDirectory() as tmp:
            cfg = remote_launch.load_config(str(Path(tmp) / "missing.yaml"))
            self.assertEqual(cfg["buttons"], [])

    def test_unknown_hex_key_names_parse(self):
        self.assertEqual(remote_launch.parse_key("KEY_0x0067"), 103)

    def test_distinguishes_app_buttons_by_scan(self):
        buttons = [
            button("prime", 362, 0xC008D),
            button("netflix", 362, 0xC008E),
        ]
        self.assertEqual(
            remote_launch.match_button(buttons, 362, 0xC008E, 10.0, 10.1, 0.2)["name"],
            "netflix",
        )

    def test_stale_scan_does_not_match(self):
        buttons = [button("prime", 362, 0xC008D)]
        self.assertIsNone(
            remote_launch.match_button(buttons, 362, 0xC008D, 10.0, 10.5, 0.2)
        )

    def test_mapping_without_scan_is_fallback(self):
        buttons = [button("generic", 362, None)]
        self.assertEqual(
            remote_launch.match_button(buttons, 362, 0x1234, 10.0, 10.1, 0.2)["name"],
            "generic",
        )

    def test_exact_scan_wins_over_fallback(self):
        buttons = [
            button("generic", 362, None),
            button("prime", 362, 0xC008D),
        ]
        self.assertEqual(
            remote_launch.match_button(buttons, 362, 0xC008D, 10.0, 10.1, 0.2)["name"],
            "prime",
        )

    def test_other_key_does_not_match(self):
        buttons = [button("prime", 362, 0xC008D)]
        self.assertIsNone(
            remote_launch.match_button(buttons, 356, 0xC008D, 10.0, 10.1, 0.2)
        )


class TestClassifyAction(unittest.TestCase):
    def test_executable_file_runs_directly(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.AppImage"
            path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            path.chmod(0o755)
            self.assertEqual(remote_launch.classify_action(str(path)), ("exec", str(path)))

    def test_desktop_file_is_launched(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "app.desktop"
            path.write_text("[Desktop Entry]\nType=Application\n", encoding="utf-8")
            self.assertEqual(
                remote_launch.classify_action(str(path)), ("desktop", str(path))
            )

    def test_directory_and_regular_file_use_opener(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "dir"
            directory.mkdir()
            document = Path(tmp) / "note.txt"
            document.write_text("hi", encoding="utf-8")
            self.assertEqual(
                remote_launch.classify_action(str(directory)), ("open", str(directory))
            )
            self.assertEqual(
                remote_launch.classify_action(str(document)), ("open", str(document))
            )

    def test_command_is_split(self):
        self.assertEqual(
            remote_launch.classify_action("echo hello"), ("command", ["echo", "hello"])
        )


class TestSender(unittest.TestCase):
    def test_cooldown_is_per_button_not_per_key_code(self):
        sender = remote_launch.Sender(cooldown=60.0)
        prime = button("prime", 362, 0xC008D)
        netflix = button("netflix", 362, 0xC008E)
        self.assertTrue(sender.allowed(prime))
        self.assertFalse(sender.allowed(prime))
        self.assertTrue(sender.allowed(netflix))


if __name__ == "__main__":
    unittest.main()
