"""Tests for receiver.hidraw: MAC and Amazon-vendor discovery."""
import tempfile
import unittest
from pathlib import Path

from receiver.hidraw import RemoteNotFound, find_remote_hidraw


def _hidraw(root: Path, index: int, uevent: str) -> None:
    node = root / f"hidraw{index}" / "device"
    node.mkdir(parents=True)
    (node / "uevent").write_text(uevent, encoding="utf-8")


class TestFindRemote(unittest.TestCase):
    def test_finds_amazon_remote_without_mac(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _hidraw(root, 0, "HID_ID=0003:0000046D:0000C539\nHID_NAME=Logitech")
            _hidraw(
                root,
                10,
                "HID_ID=0005:00000171:00000427\nHID_NAME=AR\n"
                "HID_UNIQ=AA:BB:CC:DD:EE:FF\n",
            )
            self.assertEqual(find_remote_hidraw(sysfs=root), "/dev/hidraw10")

    def test_mac_wins_when_given(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _hidraw(
                root,
                3,
                "HID_ID=0005:00000171:00000427\nHID_NAME=AR\n"
                "HID_UNIQ=AA:BB:CC:DD:EE:FF\n",
            )
            self.assertEqual(
                find_remote_hidraw("aa:bb:cc:dd:ee:ff", sysfs=root), "/dev/hidraw3"
            )

    def test_stale_mac_falls_back_to_connected_amazon_remote(self):
        # Re-pairing can change HID_UNIQ; discovery must still work.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _hidraw(
                root,
                4,
                "HID_ID=0005:00000171:00000427\nHID_NAME=AR\n"
                "HID_UNIQ=11:22:33:44:55:66\n",
            )
            self.assertEqual(
                find_remote_hidraw("aa:bb:cc:dd:ee:ff", sysfs=root), "/dev/hidraw4"
            )

    def test_no_remote(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _hidraw(root, 0, "HID_ID=0003:0000046D:0000C539\nHID_NAME=Receiver")
            with self.assertRaises(RemoteNotFound):
                find_remote_hidraw(sysfs=root)


if __name__ == "__main__":
    unittest.main()
