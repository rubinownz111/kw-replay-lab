import struct
import unittest

from tools.kwreplay_inspect import (
    END_FRAME,
    ReplayFormatError,
    FOOTER_MAGIC,
    HEADER_MAGIC,
    parse_footer,
    parse_camera_payload,
    parse_cnc3rpl_metadata,
    parse_game_command_payload,
    parse_header,
    parse_metadata_stream_payload,
    parse_records,
    parse_telestrator_payload,
    parse_voice_payload,
)


def utf16z(text: str) -> bytes:
    return text.encode("utf-16-le") + b"\0\0"


class ReplayParserTests(unittest.TestCase):
    @staticmethod
    def build_cnc3rpl_metadata(kw13: bool) -> bytes:
        result = bytearray(struct.pack("<I", 8) + b"CNC3RPL\0")
        if kw13:
            result += struct.pack("<4IIH", 0x33434E43, 0, 0, 0, 3, 1)
        result += struct.pack("<7I", *range(7))
        result += b"\x01" + struct.pack("<II", 29, 33)
        result += struct.pack("<I", 3) + b"cfg"
        result += struct.pack("<I", 44)
        result += struct.pack("<I", 3) + "map".encode("utf-16-le")
        result += b"\x01"
        result += struct.pack("<I", 5) + "title".encode("utf-16-le")
        result += struct.pack("<8H", 2023, 12, 6, 9, 17, 5, 24, 652)
        result += struct.pack("<I", 7) + b"1.2.0.0"
        result += struct.pack("<IBII", 124, 1, 132, 2)
        result += struct.pack("<12I", 1, 2, *([0] * 10))
        result += struct.pack("<IIII", 188, 192, 196, 200)
        return bytes(result)

    def test_version_5_header(self) -> None:
        metadata = struct.pack("<I", 8) + b"CNC3RPL\0" + b"\xAA\xBB"
        data = b"".join(
            [
                HEADER_MAGIC,
                b"\x05",
                struct.pack("<4I", 1, 2, 0, 0),
                struct.pack("<H", 0b110),
                utf16z("Title"),
                utf16z("Description"),
                utf16z("Map"),
                utf16z("MapID"),
                b"\x01",
                struct.pack("<I", 7),
                utf16z("Player"),
                b"\x03",
                struct.pack("<I", 9),
                utf16z("Observer"),
                b"\x08",
                struct.pack("<I", len(metadata)),
                metadata,
            ]
        )

        header = parse_header(data)
        self.assertEqual(header["container_version"], 5)
        self.assertEqual(header["game_version"], "1.2.0.0")
        self.assertEqual(header["channels"], [1, 2])
        self.assertEqual(header["participants"][0]["team"], 3)
        self.assertEqual(header["observer"]["team"], 8)
        self.assertEqual(header["metadata_tag"], "CNC3RPL")

    def test_game_command_payload(self) -> None:
        payload = bytearray()
        payload += b"\x01"
        payload += struct.pack("<I", 2)

        payload += struct.pack("<H", (3 << 11) | 0x22F)
        payload += b"\x03" + struct.pack("<I", 204)
        payload += b"\x10" + struct.pack("<ii", 42, -1)
        payload += b"\xFF"

        payload += struct.pack("<H", (2 << 11) | 0x246)
        payload += b"\x06" + struct.pack("<fff", 1.5, 2.5, 3.5)
        payload += b"\x02\x01"
        payload += b"\x0C\x03abc"
        payload += b"\x0D\x01Z\x00"
        payload += b"\xFF"

        decoded = parse_game_command_payload(bytes(payload))
        self.assertEqual(decoded["xfer_version"], 1)
        self.assertEqual(decoded["message_count"], 2)
        self.assertEqual(
            decoded["messages"][0]["message_name"],
            "MSG_QUEUE_STRUCTURE_CREATE",
        )
        self.assertEqual(decoded["messages"][0]["source_index"], 3)
        self.assertEqual(
            [argument["value"] for argument in decoded["messages"][0]["arguments"]],
            [204, 42, -1],
        )
        self.assertEqual(
            decoded["messages"][1]["message_name"],
            "MSG_DO_MOVETO",
        )
        self.assertEqual(
            decoded["messages"][1]["arguments"][0]["value"],
            [1.5, 2.5, 3.5],
        )
        self.assertEqual(decoded["messages"][1]["arguments"][2]["value"], "abc")
        self.assertEqual(decoded["messages"][1]["arguments"][3]["value"], "Z")

    def test_cnc3rpl_metadata_12_and_13_layouts(self) -> None:
        kw12 = parse_cnc3rpl_metadata(
            self.build_cnc3rpl_metadata(False), [1, 2, 0, 0]
        )
        self.assertEqual(kw12["schema"], "kw_1_2")
        self.assertNotIn("identity_dwords", kw12)
        self.assertEqual(kw12["source_28_ascii"], "cfg")
        self.assertEqual(kw12["source_38_utf16"], "title")
        self.assertEqual(kw12["channel_ids_candidate"], [1, 2])

        with self.assertRaisesRegex(ReplayFormatError, "1.02"):
            parse_cnc3rpl_metadata(self.build_cnc3rpl_metadata(True), [1, 3, 0, 0])


    def test_camera_payload(self) -> None:
        payload = b"".join(
            [
                struct.pack("<H", 1),
                struct.pack("<I", 0),
                b"\x0E",
                struct.pack("<I", 77),
                b"\x03",
                struct.pack("<fff", 10.0, 20.0, 30.0),
                struct.pack("<ffff", 0.0, 0.0, 0.0, 1.0),
            ]
        )
        decoded = parse_camera_payload(payload)
        self.assertEqual(decoded["message_count"], 1)
        message = decoded["messages"][0]
        self.assertEqual(message["type_name"], "NetCameraDataMsg")
        self.assertEqual(message["camera_frame"], 77)
        self.assertEqual(message["position"], [10.0, 20.0, 30.0])
        self.assertEqual(message["rotation_quaternion"], [0.0, 0.0, 0.0, 1.0])

    def test_voice_and_telestrator_payloads(self) -> None:
        voice = b"".join(
            [
                struct.pack("<H", 1),
                struct.pack("<I", 4),
                b"\x0D",
                struct.pack("<IHH", 88, 7, 3),
                b"\xAA\xBB\xCC",
            ]
        )
        voice_message = parse_voice_payload(voice)["messages"][0]
        self.assertEqual(voice_message["frame"], 88)
        self.assertEqual(voice_message["serial"], 7)
        self.assertEqual(voice_message["data_hex"], "aabbcc")

        operations = b"".join(
            [
                struct.pack("<BIIHH", 0, 11, 22, 100, 200),
                struct.pack("<BHH", 1, 101, 201),
                struct.pack("<BHH", 2, 102, 202),
                b"\x03",
            ]
        )
        telestrator = b"".join(
            [
                struct.pack("<H", 1),
                struct.pack("<I", 5),
                b"\x0F",
                struct.pack("<I", 99),
                bytes([len(operations)]),
                operations,
            ]
        )
        telestrator_message = parse_telestrator_payload(telestrator)["messages"][0]
        self.assertEqual(telestrator_message["frame"], 99)
        self.assertEqual(
            [operation["name"] for operation in telestrator_message["operations"]],
            ["begin_stroke", "append_point", "update_point", "end_stroke"],
        )
        self.assertEqual(
            telestrator_message["operations"][0]["point_u16"], [100, 200]
        )
        self.assertEqual(
            telestrator_message["operations"][0]["point_fixed_16_16"],
            [100 << 16, 200 << 16],
        )

    def test_broadcast_metadata_payload(self) -> None:
        metadata_body = bytearray(56)
        struct.pack_into("<I", metadata_body, 0, 1234)
        metadata_body[4] = 4
        struct.pack_into("<4I", metadata_body, 8, 1, 2, 3, 4)
        payload = b"\x01" + struct.pack("<I", 2) + bytes(metadata_body)
        decoded = parse_metadata_stream_payload(payload)
        self.assertEqual(decoded["message_count"], 1)
        message = decoded["messages"][0]
        self.assertEqual(message["metadata_type"], 2)
        self.assertEqual(message["value"], 1234)
        self.assertEqual(message["channel_ids"], [1, 2, 3, 4])

    def test_record_and_footer_envelopes(self) -> None:
        command_payload = (
            b"\x01"
            + struct.pack("<I", 1)
            + struct.pack("<H", (1 << 11) | 0x261)
            + b"\x00"
            + struct.pack("<i", -1)
            + b"\x19"
            + struct.pack("<II", 0, 75)
            + b"\x12\x00\x01"
            + b"\xFF"
        )
        record = (
            struct.pack("<II", 0, 77)
            + b"\x01"
            + struct.pack("<I", len(command_payload))
            + command_payload
        )
        records = parse_records(record, 0, len(record))
        self.assertEqual(records["record_count"], 1)
        self.assertEqual(records["game_commands"]["decoded_message_count"], 1)
        self.assertEqual(records["game_commands"]["decode_error_count"], 0)
        self.assertEqual(
            records["multiplayer_control"]["logic_crc_checkpoint_count"], 1
        )
        self.assertEqual(
            records["multiplayer_control"]["logic_crc_checkpoint_frame_count"], 1
        )
        self.assertEqual(
            records["multiplayer_control"]["logic_crc_disagreement_frames"], []
        )
        checkpoint = records["multiplayer_control"][
            "logic_crc_checkpoint_samples"
        ][0]
        self.assertEqual(checkpoint["crc_u32"], 0xFFFFFFFF)
        self.assertEqual(checkpoint["checkpoint_frame"], 75)
        self.assertFalse(checkpoint["playback"])
        self.assertTrue(checkpoint["mismatch_reporting"])

        footer_body = FOOTER_MAGIC + struct.pack("<I", 77) + b"\x02"
        footer_size = len(footer_body) + 4
        data = (
            b"prefix"
            + struct.pack("<II", 0, END_FRAME)
            + footer_body
            + struct.pack("<I", footer_size)
        )
        footer = parse_footer(data)
        self.assertEqual(footer["last_frame"], 77)
        self.assertEqual(footer["footer_version"], 2)
        self.assertEqual(footer["sentinel_frame"], END_FRAME)


if __name__ == "__main__":
    unittest.main()
