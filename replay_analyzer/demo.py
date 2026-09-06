"""Synthetic parser fixture, not an engine-recorded or playable match."""
import struct
from tools.kwreplay_inspect import HEADER_MAGIC, FOOTER_MAGIC, END_FRAME


def build_desync_capture(health=100.0):
    """Synthetic tagged capture for exploring field comparison."""
    def tag(value):
        return value.encode().rjust(4, b'\0')[::-1]
    data = bytearray(b'ALAE2STR' + struct.pack('<II', 1, 1))
    data += tag('BLOK') + b'\x0aObject 204'
    boundary = len(data)
    data += b'\0' * 4
    data += tag('DSCR') + b'\x06Health' + tag('real') + struct.pack('<f', health)
    data += tag('EBLK')
    struct.pack_into('<I', data, boundary, len(data))
    return bytes(data) + tag('END')


def build_desync_replay():
    metadata = build_metadata()
    config = b'M=00test-map;MC=123;SD=42;GSID=700;S=HPlayer A,0,0,TT,0,1,0,0,0,:HPlayer B,0,0,TT,0,1,0,0,0,:;'
    metadata = metadata.replace(struct.pack('<I', 3) + b'cfg', struct.pack('<I', len(config)) + config)
    header = (HEADER_MAGIC + b'\x05' + struct.pack('<4IH', 1, 2, 0, 0, 2)
              + utf16z('Synthetic desync investigation') + utf16z('Generated test data')
              + utf16z('Test Map') + utf16z('test-map') + b'\x02'
              + struct.pack('<I', 1) + utf16z('Player A') + b'\x01'
              + struct.pack('<I', 2) + utf16z('Player B') + b'\x02'
              + struct.pack('<I', 0) + utf16z('') + b'\x00'
              + struct.pack('<I', len(metadata)) + metadata)
    records = []
    for frame, source, value in [(450, 3, 100), (450, 4, 100), (900, 3, 200), (900, 4, 201)]:
        command = struct.pack('<H', (source << 11) | 609) + b'\x00' + struct.pack('<I', value) + b'\x19' + struct.pack('<II', 0, frame) + b'\x12\x00\x00\xff'
        records.append((frame + 3, command))
    records += [(520, struct.pack('<H', (3 << 11) | 582) + b'\x06' + struct.pack('<3f', 10, 20, 0) + b'\xff'),
                (780, struct.pack('<H', (4 << 11) | 572) + b'\x03' + struct.pack('<I', 204) + b'\x06' + struct.pack('<3f', 10, 20, 0) + b'\xff')]
    body = b''
    for frame, command in sorted(records):
        payload = b'\x01' + struct.pack('<I', 1) + command
        body += struct.pack('<IIBI', 0, frame, 1, len(payload)) + payload
    footer = FOOTER_MAGIC + struct.pack('<IB', 1024, 2)
    return header + body + struct.pack('<II', 0, END_FRAME) + footer + struct.pack('<I', len(footer) + 4)

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
