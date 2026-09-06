"""Release regressions: version isolation, bounds, attribution and exports."""
import hashlib
import io
import json
import random
import struct
import tempfile
import unittest
from pathlib import Path

from app import app, main, csv_report
from replay_analyzer import analyze_replay
from tools.kwreplay_inspect import (
    HEADER_MAGIC, ReplayFormatError, parse_header, parse_footer,
    parse_records, parse_camera_payload, GAME_MESSAGE_NAMES,
)
from tests.test_replay_analyzer import (
    build_replay, build_visibility_telemetry, build_telemetry, utf16z,
)


class ReleaseTests(unittest.TestCase):
    def upload(self, data, telemetry=None):
        files = {"replay": (io.BytesIO(data), "sample.KWReplay")}
        if telemetry is not None:
            files["telemetry"] = (io.BytesIO(telemetry), "capture.jsonl")
        return app.test_client().post("/api/analyze", data=files)

    def test_rejects_103_in_parser_and_api(self):
        data = bytearray(build_replay())
        struct.pack_into("<I", data, len(HEADER_MAGIC) + 5, 3)
        with self.assertRaisesRegex(ReplayFormatError, "1.02"):
            parse_header(data)
        self.assertEqual(self.upload(bytes(data)).status_code, 422)

    def test_rejects_oversized_native_record(self):
        data = struct.pack("<IIBI", 0, 10, 5, 65536) + bytes(65536)
        with self.assertRaisesRegex(ReplayFormatError, "16 bits"):
            parse_records(data, 0, len(data))

    def test_payload_cannot_consume_sentinel(self):
        data = bytearray(build_replay())
        start = parse_header(data)["body_offset"]
        size = struct.unpack_from("<I", data, start + 9)[0]
        struct.pack_into("<I", data, start + 9, size + 1)
        self.assertIn("body boundary", self.upload(data).get_json()["error"])

    def test_nonfinite_camera_float_is_rejected(self):
        payload = struct.pack("<HIBIBfff", 1, 0, 14, 30, 1, float("nan"), 0, 0)
        with self.assertRaisesRegex(ReplayFormatError, "Non-finite"):
            parse_camera_payload(payload)

    def test_header_roster_native_limit(self):
        data = bytearray(build_replay())
        offset = len(HEADER_MAGIC) + 1 + 16 + 2
        for _ in range(4):
            while data[offset:offset + 2] != b"\0\0":
                offset += 2
            offset += 2
        data[offset] = 9
        with self.assertRaisesRegex(ReplayFormatError, "eight"):
            parse_header(data)

    def test_large_frame_is_bounded_before_timeline_allocation(self):
        data = bytearray(build_replay())
        start = parse_header(data)["body_offset"]
        struct.pack_into("<I", data, start + 4, 0x7FFFFFFE)
        self.assertEqual(self.upload(data).status_code, 422)

    def test_truncated_inputs_have_controlled_errors(self):
        data = build_replay()
        for length in (0, 1, 18, 20, 50, len(data) - 1, len(data) - 8):
            with self.subTest(length=length):
                self.assertEqual(self.upload(data[:length]).status_code, 422)

    def test_reference_values_are_off_by_default(self):
        response = self.upload(build_replay())
        report = response.get_json()
        self.assertEqual(report["evidence"]["asset_catalog"], "disabled")
        self.assertFalse(report["commands"]["items"][0]["asset"]["resolved"])
        self.assertEqual(report["strategy"]["production_state"]["engine_model"]["payment"], "unverified_for_1.02")
        self.assertEqual(report["schema_version"], 7)

    def test_exact_command_names_include_additional_102_case(self):
        self.assertEqual(len(GAME_MESSAGE_NAMES), 233)
        self.assertEqual(GAME_MESSAGE_NAMES[1305], "MSG_OBJECT_JOINED_TEAM")

    def test_observer_gap_preserves_native_command_source(self):
        data = build_replay()
        config = b"M=00map;S=HObserver,0,0,TT,0,3,0,0,0,:X:HTester,0,0,TT,0,1,0,0,0,:X:;"
        header = parse_header(data)
        metadata_size_offset = header["body_offset"] - header["metadata_size"] - 4
        data = data.replace(struct.pack("<I", 3) + b"cfg", struct.pack("<I", len(config)) + config)
        data = bytearray(data)
        struct.pack_into("<I", data, metadata_size_offset, header["metadata_size"] + len(config) - 3)
        start = parse_header(data)["body_offset"] + 13 + 5
        struct.pack_into("<H", data, start, (5 << 11) | 0x22F)
        move = start + 18
        struct.pack_into("<H", data, move, (5 << 11) | 0x246)
        report = self.upload(data).get_json()
        self.assertEqual(report["summary"]["decode_error_count"], 0)
        self.assertEqual(report["players"][0]["source_index"], 5)
        self.assertEqual(report["players"][0]["command_count"], 2)
        self.assertTrue(all(c["player"] == "Tester" for c in report["commands"]["items"]))

    def test_telemetry_wrong_replay_or_build_is_rejected(self):
        payload = build_telemetry().decode()
        wrong = payload.replace(hashlib.sha256(build_replay()).hexdigest(), "0" * 64)
        self.assertEqual(self.upload(build_replay(), wrong.encode()).status_code, 422)
        wrong = payload.replace("8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5", "B" * 64)
        self.assertEqual(self.upload(build_replay(), wrong.encode()).status_code, 422)

    def test_malformed_telemetry_shape_is_a_controlled_error(self):
        lines = [json.loads(line) for line in build_telemetry().splitlines()]
        lines[1]["objects"] = None
        payload = "\n".join(json.dumps(line) for line in lines).encode()
        response = self.upload(build_replay(), payload)
        self.assertEqual(response.status_code, 422)
        self.assertIn("bounded list", response.get_json()["error"])

    def test_unbound_sidecar_does_not_accuse_player(self):
        lines = [json.loads(line) for line in build_visibility_telemetry().splitlines()]
        lines[0].pop("replay_sha256")
        payload = "\n".join(json.dumps(line) for line in lines).encode()
        report = self.upload(build_replay(include_attack=True), payload).get_json()
        self.assertFalse(report["telemetry"]["capture"]["replay_hash_matches"])
        self.assertEqual(report["telemetry"]["visibility"]["target_check_count"], 0)

    def test_cli_json_csv_batch_and_input_preservation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "sample.KWReplay"
            source.write_bytes(build_replay())
            output, commands, records = root / "report.json", root / "commands.csv", root / "records.csv"
            self.assertEqual(main(["--analyze", str(source), "--output", str(output), "--commands-csv", str(commands), "--records-csv", str(records)]), 0)
            self.assertEqual(json.loads(output.read_text())["schema_version"], 7)
            self.assertIn("MSG_DO_MOVETO", commands.read_text(encoding="utf-8-sig"))
            self.assertIn("game_commands", records.read_text(encoding="utf-8-sig"))
            self.assertEqual(main(["--analyze", str(source), "--output", str(source)]), 1)
            self.assertEqual(source.read_bytes(), build_replay())
            (root / "bad.KWReplay").write_bytes(b"broken")
            self.assertEqual(main(["--batch", str(root), "--output", str(root / "reports")]), 1)
            summary = json.loads((root / "reports/batch-summary.json").read_text())
            self.assertEqual({r["status"] for r in summary}, {"ok", "error"})

    def test_csv_user_text_is_not_a_formula(self):
        report = self.upload(build_replay()).get_json()
        report["commands"]["items"][0]["player"] = "=1+1"
        self.assertIn("'=1+1", csv_report(report))

    def test_synthetic_demo_covers_all_streams(self):
        response = app.test_client().get("/api/demo")
        self.assertEqual(response.status_code, 200)
        report = response.get_json()
        self.assertEqual(report["summary"]["decode_error_count"], 0)
        self.assertGreater(report["summary"]["voice_packet_count"], 0)
        self.assertGreater(report["summary"]["telestrator_operation_count"], 0)
        self.assertGreater(report["summary"]["metadata_event_count"], 0)


if __name__ == "__main__":
    unittest.main()
