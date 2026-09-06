import io
import hashlib
import json
import struct
import tempfile
import unittest
from pathlib import Path

from app import app
from replay_analyzer import analyze_replay
from replay_analyzer.analysis import parse_match_configuration
from tools.kwreplay_inspect import END_FRAME, FOOTER_MAGIC, HEADER_MAGIC


def utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\0\0"


def build_metadata() -> bytes:
    result = bytearray(struct.pack("<I", 8) + b"CNC3RPL\0")
    result += struct.pack("<7I", *range(7))
    result += b"\x01" + struct.pack("<II", 29, 33)
    result += struct.pack("<I", 3) + b"cfg"
    result += struct.pack("<I", 44)
    result += struct.pack("<I", 3) + "map".encode("utf-16-le")
    result += b"\x01"
    result += struct.pack("<I", 4) + "test".encode("utf-16-le")
    result += struct.pack("<8H", 2026, 7, 1, 27, 20, 30, 0, 0)
    result += struct.pack("<I", 7) + b"1.2.0.0"
    result += struct.pack("<IBII", 124, 1, 132, 2)
    result += struct.pack("<12I", 1, 2, *([0] * 10))
    result += struct.pack("<IIII", 188, 192, 196, 200)
    return bytes(result)


def build_replay(*, include_attack: bool = False) -> bytes:
    metadata = build_metadata()
    header = b"".join(
        [
            HEADER_MAGIC,
            b"\x05",
            struct.pack("<4I", 1, 2, 0, 0),
            struct.pack("<H", 0b110),
            utf16z("Analyzer Test"),
            utf16z("Synthetic replay"),
            utf16z("Test Map"),
            utf16z("map-id"),
            b"\x01",
            struct.pack("<I", 7),
            utf16z("Tester"),
            b"\x01",
            struct.pack("<I", 0),
            utf16z(""),
            b"\x00",
            struct.pack("<I", len(metadata)),
            metadata,
        ]
    )
    queue_structure = (
        struct.pack("<H", (3 << 11) | 0x22F)
        + b"\x03"
        + struct.pack("<I", 204)
        + b"\x00"
        + struct.pack("<I", 0xE5F7D19B)
        + b"\x00"
        + struct.pack("<i", 0)
        + b"\xFF"
    )
    move = (
        struct.pack("<H", (3 << 11) | 0x246)
        + b"\x06"
        + struct.pack("<fff", 10.0, 20.0, 0.0)
        + b"\xFF"
    )
    attack = (
        struct.pack("<H", (3 << 11) | 0x23C)
        + b"\x03"
        + struct.pack("<I", 204)
        + b"\x06"
        + struct.pack("<fff", 10.0, 20.0, 0.0)
        + b"\xFF"
    )
    command_payload = (
        b"\x01"
        + struct.pack("<I", 3 if include_attack else 2)
        + queue_structure
        + move
        + (attack if include_attack else b"")
    )
    record = (
        struct.pack("<II", 0, 30)
        + b"\x01"
        + struct.pack("<I", len(command_payload))
        + command_payload
    )
    footer_body = FOOTER_MAGIC + struct.pack("<I", 30) + b"\x02"
    footer = footer_body + struct.pack("<I", len(footer_body) + 4)
    return header + record + struct.pack("<II", 0, END_FRAME) + footer


def add_records(
    replay: bytes, records: list[bytes], channel_mask: int | None = None
) -> bytes:
    sentinel = struct.pack("<II", 0, END_FRAME)
    sentinel_offset = replay.rfind(sentinel)
    result = replay[:sentinel_offset] + b"".join(records) + replay[sentinel_offset:]
    if channel_mask is not None:
        mask_offset = len(HEADER_MAGIC) + 1 + 16
        result = (
            result[:mask_offset]
            + struct.pack("<H", channel_mask)
            + result[mask_offset + 2 :]
        )
    return result


def body_record(frame: int, channel: int, payload: bytes, reserved: int = 0) -> bytes:
    return (
        struct.pack("<II", reserved, frame)
        + bytes([channel])
        + struct.pack("<I", len(payload))
        + payload
    )


def build_auxiliary_replay() -> bytes:
    camera_payload = b"".join(
        [
            struct.pack("<H", 2),
            struct.pack("<I", 0),
            b"\x0E",
            struct.pack("<I", 30),
            b"\x01",
            struct.pack("<fff", 10.0, 20.0, 30.0),
            struct.pack("<I", 0),
            b"\x0E",
            struct.pack("<I", 31),
            b"\x02",
            struct.pack("<ffff", 0.0, 0.0, 0.0, 1.0),
        ]
    )
    voice_payload = b"".join(
        [
            struct.pack("<H", 2),
            struct.pack("<I", 0),
            b"\x0D",
            struct.pack("<IHH", 30, 1, 2),
            b"\xAA\xBB",
            struct.pack("<I", 0),
            b"\x0D",
            struct.pack("<IHH", 30, 3, 2),
            b"\xCC\xDD",
        ]
    )
    telestrator_operations = (
        struct.pack("<BIIHH", 0, 11, 22, 100, 200)
        + struct.pack("<BHH", 1, 101, 201)
        + b"\x03"
    )
    telestrator_payload = b"".join(
        [
            struct.pack("<H", 1),
            struct.pack("<I", 0),
            b"\x0F",
            struct.pack("<I", 30),
            bytes([len(telestrator_operations)]),
            telestrator_operations,
        ]
    )
    metadata_body = bytearray(56)
    struct.pack_into("<I", metadata_body, 0, 30)
    metadata_body[4] = 4
    struct.pack_into("<4I", metadata_body, 8, 1, 2, 3, 4)
    metadata_payload = b"\x01" + struct.pack("<I", 2) + bytes(metadata_body)
    return add_records(
        build_replay(),
        [
            body_record(30, 2, camera_payload),
            body_record(30, 3, voice_payload),
            body_record(30, 4, telestrator_payload),
            body_record(30, 0xFD, metadata_payload),
        ],
        channel_mask=sum(1 << channel for channel in (1, 2, 3, 4)),
    )


def build_telemetry() -> bytes:
    records = [
        {
            "type": "kw_telemetry_header",
            "schema_version": 1,
            "read_only": True,
            "verified_build": True,
            "executable_sha256": (
                "8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5"
            ),
            "replay_sha256": hashlib.sha256(build_replay()).hexdigest(),
        "process_id": 1234,
            "sample_interval_ms": 200,
        },
        {
            "type": "snapshot",
            "sequence": 0,
            "frame": 30,
            "object_count": 1,
            "objects": [
                {
                    "id": 204,
                    "template_hash": "E5F7D19B",
                    "player_address": "0x01002000",
                    "position": [10.0, 20.0, 0.0],
                }
            ],
            "players": [
                {
                    "address": "0x01002000",
                    "player_id": 0,
                    "resources": 1000,
                    "primary_resources": 1000,
                    "secondary_resources": 0,
                }
            ],
            "warnings": [],
        },
        {
            "type": "snapshot",
            "sequence": 1,
            "frame": 31,
            "object_count": 0,
            "objects": [],
            "players": [
                {
                    "address": "0x01002000",
                    "player_id": 0,
                    "resources": 700,
                    "primary_resources": 700,
                    "secondary_resources": 0,
                }
            ],
            "warnings": [],
        },
    ]
    return (
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
        + "\n"
    ).encode()


def build_visibility_telemetry() -> bytes:
    header = {
        "type": "kw_telemetry_header",
        "schema_version": 1,
        "read_only": True,
        "verified_build": True,
        "executable_sha256": (
            "8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5"
        ),
        "replay_sha256": hashlib.sha256(build_replay(include_attack=True)).hexdigest(),
        "process_id": 1234,
        "sample_interval_ms": 200,
    }
    snapshots = []
    for sequence, frame in enumerate((29, 31)):
        snapshots.append(
            {
                "type": "snapshot",
                "sequence": sequence,
                "frame": frame,
                "object_count": 1,
                "objects": [
                    {
                        "id": 204,
                        "template_hash": "E5F7D19B",
                        "player_address": "0x03004000",
                        "position": [10.0, 20.0, 0.0],
                        "visibility": {
                            "0": {
                                "status": 4,
                                "name": "shrouded",
                                "evidence": "cached",
                            }
                        },
                    }
                ],
                "players": [
                    {
                        "address": "0x01002000",
                        "player_id": 0,
                        "resources": 1000,
                        "primary_resources": 1000,
                        "secondary_resources": 0,
                    }
                ],
                "warnings": [],
            }
        )
    records = [header, *snapshots]
    return (
        "\n".join(json.dumps(record, separators=(",", ":")) for record in records)
        + "\n"
    ).encode()


class ReplayAnalyzerTests(unittest.TestCase):
    def test_match_configuration(self) -> None:
        decoded = parse_match_configuration(
            "M=0F1data/maps/test;MC=ABC123;MS=512;SD=77;"
            "GSID=session;GT=1;PC=2;RU=1 0;S="
            "HAlice,net,16000,TT,2,7,4,0,1,extra:"
            "HBob,net2,16001,TT,5,8,7,1,2,extra;"
        )
        self.assertEqual(decoded["map_path"], "data/maps/test")
        self.assertEqual(decoded["map_contents_mask"], 15)
        self.assertEqual(decoded["seed"], 77)
        self.assertEqual(decoded["slots"][0]["declared_faction"], "Steel Talons")
        self.assertEqual(decoded["slots"][1]["declared_faction"], "ZOCOM")
        self.assertEqual(decoded["slots"][1]["start_position"], 7)

    def test_high_level_report(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".KWReplay", delete=False) as temp:
            temp.write(build_replay())
            path = Path(temp.name)
        try:
            report = analyze_replay(path, display_name="test.KWReplay", reference_catalog=True)
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(report["file"]["name"], "test.KWReplay")
        self.assertEqual(report["summary"]["command_count"], 2)
        self.assertEqual(report["summary"]["action_count"], 2)
        self.assertEqual(report["summary"]["integrity_score"], 100)
        self.assertEqual(report["match"]["simulation_fps_assumption"], 15)
        self.assertEqual(report["match"]["duration_seconds"], 2.0)
        self.assertEqual(
            report["commands"]["items"][0]["name"], "MSG_QUEUE_STRUCTURE_CREATE"
        )
        self.assertEqual(
            report["commands"]["items"][0]["asset"]["id"],
            "SteelTalonsPowerPlant",
        )
        self.assertEqual(
            report["strategy"]["build_order"][0]["asset"]["build_cost"], 700
        )
        self.assertEqual(
            report["strategy"]["build_order"][0]["producer_object_id"], 204
        )
        self.assertEqual(
            report["strategy"]["production_state"]["engine_model"]["payment"],
            "unverified_for_1.02",
        )
        self.assertEqual(
            report["players"][0]["production"]["queue_attempt_value"], 700
        )
        self.assertEqual(
            report["cheat_analysis"]["assessment"],
            "No checked structural or CRC anomalies detected",
        )
        self.assertEqual(report["schema_version"], 7)
        self.assertEqual(report["forensics"]["record_count"], 1)
        self.assertTrue(report["integrity"]["footer_last_frame_matches_body"])

    def test_auxiliary_streams_and_forensic_index(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".KWReplay", delete=False) as temp:
            temp.write(build_auxiliary_replay())
            path = Path(temp.name)
        try:
            report = analyze_replay(path, display_name="auxiliary.KWReplay")
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(report["camera"]["streams"][0]["update_count"], 2)
        self.assertTrue(report["camera"]["path"][1]["position_inherited"])
        self.assertEqual(report["auxiliary_streams"]["voice"]["packet_count"], 2)
        self.assertEqual(
            report["auxiliary_streams"]["voice"]["missing_packet_count"], 1
        )
        self.assertEqual(
            report["auxiliary_streams"]["telestrator"]["operation_count"], 3
        )
        self.assertEqual(
            report["auxiliary_streams"]["metadata"]["events"][0]["channel_ids"],
            [1, 2, 3, 4],
        )
        self.assertEqual(report["forensics"]["record_count"], 5)
        self.assertEqual(report["integrity"]["frame_order_violation_count"], 0)

    def test_structural_forensics_detect_writer_invariant_violations(self) -> None:
        malformed = add_records(
            build_replay(),
            [
                body_record(29, 2, struct.pack("<H", 0), reserved=7),
                body_record(29, 9, b""),
            ],
            channel_mask=1 << 1,
        )
        with tempfile.NamedTemporaryFile(suffix=".KWReplay", delete=False) as temp:
            temp.write(malformed)
            path = Path(temp.name)
        try:
            report = analyze_replay(path, display_name="structural.KWReplay")
        finally:
            path.unlink(missing_ok=True)

        detectors = {
            finding["detector"] for finding in report["cheat_analysis"]["findings"]
        }
        self.assertIn("record_frame_order", detectors)
        self.assertIn("channel_mask", detectors)
        self.assertIn("unknown_record_channels", detectors)
        self.assertEqual(report["integrity"]["reserved_nonzero_count"], 1)

    def test_upload_api(self) -> None:
        client = app.test_client()
        response = client.post(
            "/api/analyze",
            data={"replay": (io.BytesIO(build_replay()), "test.KWReplay")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["players"][0]["name"], "Tester")

    def test_health_reports_analyzer_schema(self) -> None:
        response = app.test_client().get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["report_schema_version"], 7)
        self.assertFalse(response.get_json()["production_analysis"])
        self.assertTrue(response.get_json()["telemetry_sidecars"])

    def test_telemetry_sidecar(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".KWReplay", delete=False) as replay:
            replay.write(build_replay())
            replay_path = Path(replay.name)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as sidecar:
            sidecar.write(build_telemetry())
            sidecar_path = Path(sidecar.name)
        try:
            report = analyze_replay(replay_path, telemetry_path=sidecar_path)
        finally:
            replay_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)

        self.assertTrue(report["telemetry"]["present"])
        self.assertTrue(report["telemetry"]["capture"]["verified_build"])
        self.assertEqual(report["summary"]["telemetry_sample_count"], 2)
        self.assertEqual(
            report["telemetry"]["objects"]["event_counts"]["object_removed"], 1
        )
        self.assertEqual(
            report["telemetry"]["economy"]["players"][0][
                "observed_negative_delta"
            ],
            300,
        )

    def test_upload_api_accepts_telemetry(self) -> None:
        response = app.test_client().post(
            "/api/analyze",
            data={
                "replay": (io.BytesIO(build_replay()), "test.KWReplay"),
                "telemetry": (
                    io.BytesIO(build_telemetry()),
                    "test.jsonl",
                ),
            },
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["telemetry"]["present"])
        self.assertEqual(payload["telemetry"]["file"]["name"], "test.jsonl")

    def test_shrouded_target_detector_requires_bracketed_cached_state(self) -> None:
        with tempfile.NamedTemporaryFile(suffix=".KWReplay", delete=False) as replay:
            replay.write(build_replay(include_attack=True))
            replay_path = Path(replay.name)
        with tempfile.NamedTemporaryFile(suffix=".jsonl", delete=False) as sidecar:
            sidecar.write(build_visibility_telemetry())
            sidecar_path = Path(sidecar.name)
        try:
            report = analyze_replay(replay_path, telemetry_path=sidecar_path)
        finally:
            replay_path.unlink(missing_ok=True)
            sidecar_path.unlink(missing_ok=True)

        visibility = report["telemetry"]["visibility"]
        self.assertTrue(visibility["captured"])
        self.assertEqual(visibility["target_check_count"], 1)
        self.assertEqual(visibility["high_confidence_shrouded_target_count"], 1)
        command = report["commands"]["items"][-1]
        self.assertEqual(command["visibility"]["status_name"], "shrouded")
        self.assertTrue(command["visibility"]["bracketed"])
        self.assertTrue(
            any(
                finding["detector"] == "shrouded_target:3"
                for finding in report["cheat_analysis"]["findings"]
            )
        )

    def test_upload_rejects_wrong_extension(self) -> None:
        client = app.test_client()
        response = client.post(
            "/api/analyze",
            data={"replay": (io.BytesIO(b"not a replay"), "test.txt")},
            content_type="multipart/form-data",
        )
        self.assertEqual(response.status_code, 400)


if __name__ == "__main__":
    unittest.main()
