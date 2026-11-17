"""Tests for the `firetv-remote remap` config logic (no hardware)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from receiver import remap


class TestCli(unittest.TestCase):
    def test_remap_is_a_cli_command(self):
        from receiver import cli

        args = cli.build_parser().parse_args(["remap", "--list"])
        self.assertIs(args.func, cli.cmd_remap)
        self.assertTrue(args.list_mappings)


class TestParsing(unittest.TestCase):
    def test_key_names_and_numbers(self):
        self.assertEqual(remap.parse_key("KEY_PROGRAM"), 362)
        self.assertEqual(remap.parse_key("362"), 362)
        self.assertEqual(remap.parse_key("0x16a"), 362)
        self.assertEqual(remap.parse_key("KEY_0x0067"), 103)
        self.assertEqual(remap.key_name(362), "KEY_PROGRAM")
        self.assertEqual(remap.key_name(9999), "KEY_0x270f")

    def test_scan_values(self):
        self.assertEqual(remap.parse_scan("0xc008d"), 0xC008D)
        self.assertEqual(remap.parse_scan(0xC008D), 0xC008D)
        self.assertIsNone(remap.parse_scan(None))
        with self.assertRaises(remap.RemapError):
            remap.parse_scan("not-a-scan")


class TestMappings(unittest.TestCase):
    def test_app_buttons_with_different_scans_coexist(self):
        cfg = remap.new_config()
        first, created = remap.add_mapping(
            cfg, key="KEY_PROGRAM", scan="0xc008d", action="echo prime"
        )
        second, also_created = remap.add_mapping(
            cfg, key="KEY_PROGRAM", scan="0xc008e", action="echo netflix"
        )
        self.assertTrue(created)
        self.assertTrue(also_created)
        self.assertEqual(len(cfg["buttons"]), 2)
        self.assertEqual(first["scan"], "0xc008d")
        self.assertEqual(second["scan"], "0xc008e")

    def test_same_scan_updates_instead_of_duplicating(self):
        cfg = remap.new_config()
        remap.add_mapping(cfg, key="KEY_PROGRAM", scan="0xc008d", action="echo old")
        entry, created = remap.add_mapping(
            cfg, key="KEY_PROGRAM", scan="0xc008d", action="echo new"
        )
        self.assertFalse(created)
        self.assertEqual(len(cfg["buttons"]), 1)
        self.assertEqual(entry["action"], "echo new")
        self.assertEqual(entry["name"], "echo-old")

    def test_same_name_updates_key_and_scan(self):
        cfg = remap.new_config()
        remap.add_mapping(cfg, key="KEY_PROGRAM", scan="0xc008d", action="echo old", name="x")
        entry, created = remap.add_mapping(
            cfg, key="KEY_MENU", action="echo new", name="x"
        )
        self.assertFalse(created)
        self.assertEqual(len(cfg["buttons"]), 1)
        self.assertEqual(entry["key"], "KEY_MENU")
        self.assertNotIn("scan", entry)

    def test_name_is_derived_from_action(self):
        cfg = remap.new_config()
        entry, _ = remap.add_mapping(cfg, key="KEY_PROGRAM", action="/tmp/MyApp.sh")
        self.assertEqual(entry["name"], "MyApp")

    def test_remove_by_name_key_or_scan(self):
        cfg = remap.new_config()
        remap.add_mapping(cfg, key="KEY_PROGRAM", scan="0xc008d", action="echo a", name="a")
        remap.add_mapping(cfg, key="KEY_PROGRAM", scan="0xc008e", action="echo b", name="b")
        self.assertEqual(remap.remove_mapping(cfg, "0xc008e"), 1)
        self.assertEqual(remap.remove_mapping(cfg, "a"), 1)
        self.assertEqual(cfg["buttons"], [])

    def test_derived_names_do_not_clobber_each_other(self):
        cfg = remap.new_config()
        first, _ = remap.add_mapping(cfg, key="KEY_PROGRAM", action="echo hello")
        second, _ = remap.add_mapping(cfg, key="KEY_PROGRAM", action="echo hello world")
        self.assertNotEqual(first["name"], second["name"])
        self.assertEqual(len(cfg["buttons"]), 2)

    def test_save_load_roundtrip(self):
        cfg = remap.new_config()
        remap.add_mapping(cfg, key="KEY_PROGRAM", scan="0xc008d", action="echo hi")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "button_launch.yaml"
            remap.save_config(cfg, path)
            loaded = remap.load_config(path)
            self.assertEqual(loaded["buttons"], cfg["buttons"])
            self.assertIn("firetv-remote remap", path.read_text(encoding="utf-8"))

    def test_reset(self):
        cfg = remap.new_config()
        remap.add_mapping(cfg, key="KEY_PROGRAM", action="echo hi")
        remap.reset_config(cfg)
        self.assertEqual(cfg["buttons"], [])


if __name__ == "__main__":
    unittest.main()
