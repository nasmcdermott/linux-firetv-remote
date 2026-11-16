"""Tests for hidraw report splitting and consumer usage parsing."""
import importlib
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))
voice = importlib.import_module("06_voice_stt")


class TestReportParsing(unittest.TestCase):
    def test_consumer_usages(self):
        self.assertEqual(voice.consumer_usages(b"\x21\x02\x00\x00"), [0x0221, 0])
        self.assertEqual(voice.consumer_usages(b"\x21\x02"), [0x0221])

    def test_splits_concatenated_reports(self):
        # consumer voice report (5 bytes), audio report (81 bytes), keyboard (4)
        data = (
            b"\x02\x21\x02\x00\x00"
            + b"\xf0" + bytes(range(80))
            + b"\x01\x00\x00\x00"
        )
        sizes = {0x01: 3, 0x02: 4, 0xF0: 80}
        reports = list(voice.iter_reports(data, sizes))
        self.assertEqual([r[0] for r in reports], [0x02, 0xF0, 0x01])
        self.assertEqual(reports[0][1], b"\x21\x02\x00\x00")
        self.assertEqual(len(reports[1][1]), 80)
        self.assertEqual(reports[2][1], b"\x00\x00\x00")

    def test_unknown_report_is_dropped_not_misparsed_as_audio(self):
        sizes = {0x02: 4}
        reports = list(voice.iter_reports(b"\xee\x01\x02\x03\x04", sizes))
        self.assertEqual(reports, [])


if __name__ == "__main__":
    unittest.main()
