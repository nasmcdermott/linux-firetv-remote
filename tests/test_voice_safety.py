#!/usr/bin/env python3
"""Tests for the conservative voice command/dictation gate."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import voice_core  # noqa: E402


class FakeHandler:
    app_names = ["Firefox", "Steam", "Konsole", "System Settings"]
    cfg = {"desktop_redirects": {"files": "org.kde.dolphin"}}

    def _find_desktop(self, name: str):
        known = {
            "firefox",
            "steam",
            "konsole",
            "system settings",
            "org.kde.dolphin",
        }
        value = name.strip().lower()
        return value if value in known else None

    def handle(self, text: str) -> str:
        return f"handle:{text}"

    def type_text(self, text: str) -> str:
        return f"type:{text}"


class VoiceSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = FakeHandler()

    def decision(self, text: str) -> str:
        return voice_core.classify_utterance(text, self.handler)[0]

    def test_open_known_app_is_command(self) -> None:
        self.assertEqual(self.decision("Open Firefox"), "command")

    def test_trailing_period_does_not_block_command(self) -> None:
        self.assertEqual(self.decision("Open Firefox."), "command")

    def test_open_unknown_app_is_dictation(self) -> None:
        self.assertEqual(self.decision("Open DefinitelyNotAnApp"), "dictation")

    def test_open_in_middle_is_dictation(self) -> None:
        self.assertEqual(
            self.decision("I want to open Firefox because the tabs are useful"),
            "dictation",
        )

    def test_open_with_clause_is_dictation(self) -> None:
        self.assertEqual(
            self.decision("Open Firefox and tell me the weather"),
            "dictation",
        )

    def test_open_with_please_is_dictation(self) -> None:
        self.assertEqual(self.decision("Open Firefox please"), "dictation")

    def test_long_sentence_is_dictation(self) -> None:
        self.assertEqual(
            self.decision(
                "This is a normal sentence with open Firefox inside it"
            ),
            "dictation",
        )

    def test_short_search_is_command(self) -> None:
        self.assertEqual(self.decision("Search for my notes"), "command")

    def test_long_search_is_dictation(self) -> None:
        self.assertEqual(
            self.decision(
                "Search for the file I was working on yesterday afternoon"
            ),
            "dictation",
        )

    def test_explicit_type_is_command(self) -> None:
        self.assertEqual(self.decision("Type hello world"), "command")

    def test_route_dictation_types_whole_text(self) -> None:
        result = voice_core.route_text(
            "Open DefinitelyNotAnApp", self.handler, "command"
        )
        self.assertEqual(result, "type:Open DefinitelyNotAnApp")

    def test_route_command_executes(self) -> None:
        result = voice_core.route_text("Open Firefox", self.handler, "command")
        self.assertEqual(result, "handle:Open Firefox")

    def test_route_safe_disabled_restores_old_behavior(self) -> None:
        result = voice_core.route_text(
            "Open DefinitelyNotAnApp", self.handler, "command", safe=False
        )
        self.assertEqual(result, "handle:Open DefinitelyNotAnApp")


if __name__ == "__main__":
    unittest.main()
