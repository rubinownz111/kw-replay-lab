"""Synthetic parser fixture, not an engine-recorded or playable match."""
import struct
from tools.kwreplay_inspect import HEADER_MAGIC, FOOTER_MAGIC, END_FRAME

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
            utf16z("Synthetic 1.02 stream demo"),
            utf16z("Synthetic replay"),
            utf16z("Test Map"),
            utf16z("map-id"),
            b"\x01",
            struct.pack("<I", 7),
            utf16z("Demo player"),
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
