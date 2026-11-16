"""Tests for remote_input.py: this daemon owns Menu only (no hardware)."""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
import remote_input


class TestConsumerUsage(unittest.TestCase):
    def test_two_byte_payload(self):
        self.assertEqual(remote_input.consumer_usage(b"\x21\x02"), 0x0221)

    def test_short_payload(self):
        self.assertIsNone(remote_input.consumer_usage(b"\x21"))


class TestOwnership(unittest.TestCase):
    """remote_input.py handles Menu only. CEC actions belong to the observer."""

    def test_volume_and_mute_not_mapped(self):
        for usage in (remote_input.VOLUME_UP, remote_input.VOLUME_DOWN, remote_input.MUTE):
            self.assertNotIn(usage, remote_input.BUTTON_ACTIONS)

    def test_voice_code_not_mapped(self):
        # 0x0221 is the voice button on this remote; it must never wake the TV.
        self.assertNotIn(0x0221, remote_input.BUTTON_ACTIONS)
        self.assertNotIn(0x0221, remote_input.ACTION_COOLDOWN)

    def test_no_power_mapping(self):
        # The real power button is KEY_POWER on evdev, handled by the observer.
        self.assertFalse(hasattr(remote_input, "send_wake"))
        self.assertFalse(hasattr(remote_input, "POWER_KEYS"))
        self.assertNotIn(0x0030, remote_input.BUTTON_ACTIONS)

    def test_menu_not_in_button_table(self):
        self.assertNotIn(remote_input.MENU_KEY, remote_input.BUTTON_ACTIONS)

    def test_no_volume_dispatch_function(self):
        self.assertFalse(hasattr(remote_input, "send_volume"))

    def test_no_cec_dispatch_at_all(self):
        with open(remote_input.__file__, encoding="utf-8") as fh:
            src = fh.read()
        code = "\n".join(line for line in src.splitlines() if not line.lstrip().startswith("#"))
        self.assertNotIn("cec-policy", code)


class TestMenuMapping(unittest.TestCase):
    """Menu goes through the shared data-driven keymap, default D-Bus only."""

    def test_menu_maps_to_dbus_not_keystrokes(self):
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
        from receiver.actions import describe_action, load_keymap, parse_action

        action = parse_action(load_keymap()["keys"]["menu"])
        self.assertEqual(action.kind, "dbus")
        self.assertEqual(action.dbus[3], "activateLauncherMenu")
        self.assertNotIn("key", describe_action(action))
        self.assertNotIn("ydotool", describe_action(action))


class TestSingleton(unittest.TestCase):
    """Only one copy may hold the Menu/power daemon at a time."""

    def test_second_copy_is_refused_and_first_keeps_lock(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            first = remote_input.acquire_singleton(d)
            self.assertIsNotNone(first)
            self.assertIsNone(remote_input.acquire_singleton(d))
            first.close()
            again = remote_input.acquire_singleton(d)
            self.assertIsNotNone(again, "lock must be free after the first copy exits")
            again.close()


if __name__ == "__main__":
    unittest.main()
