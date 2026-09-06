#!/usr/bin/env python3
"""Inspect Kane's Wrath .KWReplay containers without modifying them."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import struct
import sys
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HEADER_MAGIC = b"C&C3 REPLAY HEADER"
FOOTER_MAGIC = b"C&C3 REPLAY FOOTER"
END_FRAME = 0x7FFFFFFF

CHANNEL_NAMES = {
    1: "game_commands",
    2: "camera",
    3: "voice",
    4: "telestrator",
    0xFD: "metadata_fd",
    0xFE: "metadata_fe",
    0xFF: "metadata_ff",
}

TELESTRATOR_OPERATION_NAMES = {
    0: "begin_stroke",
    1: "append_point",
    2: "update_point",
    3: "end_stroke",
}

METADATA_TYPE_NAMES = {
    1: "frame_marker",
    2: "channel_schedule",
}

ARGUMENT_TYPE_NAMES = {
    0: "integer",
    1: "real",
    2: "boolean",
    3: "object_id",
    4: "drawable_id",
    5: "team_id",
    6: "coord3d",
    7: "icoord2d",
    8: "iregion2d",
    9: "timestamp",
    10: "wide_char",
    11: "unknown_no_data",
    12: "ascii_string",
    13: "unicode_string",
}

# Exact KW 1.02 command-name switch; provenance accompanies the generated table.
_catalog_payload = json.loads(
    Path(__file__).with_name("kw_game_message_names.json").read_text(encoding="utf-8-sig")
)
GAME_MESSAGE_NAMES = {
    int(key, 16): value for key, value in _catalog_payload["message_names"].items()
}
SUPPORTED_GAME_VERSION = (1, 2, 0, 0)
TARGET_EXECUTABLE_SHA256 = "8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5"
MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_RECORDS = 250_000
MAX_FRAME = 15 * 86400 * 7  # Explicit analysis resource limit: seven days at 15 Hz.


def read_replay_bytes(path: Path) -> bytes:
    with path.open("rb") as source:
        data = source.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ReplayFormatError("Replay exceeds the 64 MiB analysis limit.")
    return data


def read_body_record(reader, end: int):
    """Read a bounded envelope; reject lengths with ambiguous native semantics."""
    offset = reader.offset
    if not 0 <= offset <= end <= len(reader.data) or end - offset < 13:
        raise ReplayFormatError(f"Truncated record envelope at 0x{offset:X}.")
    reserved, frame, channel, size = reader.u32(), reader.u32(), reader.u8(), reader.u32()
    if size > 0xFFFF:
        raise ReplayFormatError(
            f"Record at 0x{offset:X} has length {size}; KW 1.02 narrows it to "
            "16 bits. Oversized records are rejected to avoid ambiguous decoding."
        )
    if frame > MAX_FRAME:
        raise ReplayFormatError("Record frame exceeds the seven-day analysis limit.")
    if reader.offset + size > end:
        raise ReplayFormatError(f"Record at 0x{offset:X} crosses the replay body boundary.")
    return reserved, frame, channel, size, reader.take(size)


class ReplayFormatError(ValueError):
    pass


@dataclass
class Reader:
    data: bytes
    offset: int = 0

    def take(self, size: int) -> bytes:
        if size < 0 or self.offset + size > len(self.data):
            raise ReplayFormatError(
                f"read of {size} bytes at 0x{self.offset:X} exceeds file size"
            )
        result = self.data[self.offset : self.offset + size]
        self.offset += size
        return result

    def u8(self) -> int:
        return self.take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self.take(2))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self.take(4))[0]

    def f32(self) -> float:
        value = struct.unpack("<f", self.take(4))[0]
        if not math.isfinite(value):
            raise ReplayFormatError("Non-finite floating-point value in replay payload.")
        return value

    def utf16z(self, max_units: int = 500) -> str:
        start = self.offset
        while self.offset - start <= max_units * 2:
            code_unit = self.take(2)
            if code_unit == b"\0\0":
                return self.data[start : self.offset - 2].decode(
                    "utf-16-le", errors="replace"
                )
        raise ReplayFormatError(f"UTF-16 string exceeds {max_units} code units.")


def _xfer_length(reader: Reader) -> int:
    short_length = reader.u8()
    return reader.u32() if short_length == 0xFF else short_length


def _parse_command_argument(reader: Reader, argument_type: int) -> Any:
    if argument_type == 0:
        return reader.i32()
    if argument_type == 1:
        return reader.f32()
    if argument_type == 2:
        return bool(reader.u8())
    if argument_type in (3, 4, 5, 9):
        return reader.u32()
    if argument_type == 6:
        return [reader.f32(), reader.f32(), reader.f32()]
    if argument_type == 7:
        return [reader.i32(), reader.i32()]
    if argument_type == 8:
        return [reader.i32(), reader.i32(), reader.i32(), reader.i32()]
    if argument_type == 10:
        return chr(reader.u16())
    if argument_type == 11:
        return None
    if argument_type == 12:
        return reader.take(_xfer_length(reader)).decode("latin-1", errors="replace")
    if argument_type == 13:
        character_count = _xfer_length(reader)
        return reader.take(character_count * 2).decode(
            "utf-16-le", errors="replace"
        )
    raise ReplayFormatError(f"unsupported game-command argument type {argument_type}")


def parse_game_command_payload(payload: bytes) -> dict[str, Any]:
    """Decode a channel-1 GameCommandStreamData payload."""
    reader = Reader(payload)
    xfer_version = reader.u8()
    if xfer_version != 1:
        raise ReplayFormatError(f"Unsupported command transfer version {xfer_version}.")
    message_count = reader.u32()
    if message_count > (len(payload) - 5) // 3:
        raise ReplayFormatError("Command count exceeds available payload bytes.")
    messages: list[dict[str, Any]] = []

    for _ in range(message_count):
        message_offset = reader.offset
        packed_type = reader.u16()
        message_type = packed_type & 0x7FF
        source_index = packed_type >> 11
        arguments: list[dict[str, Any]] = []

        while True:
            run_header = reader.u8()
            if run_header == 0xFF:
                break
            argument_type = run_header & 0xF
            run_count = (run_header >> 4) + 1
            for _ in range(run_count):
                arguments.append(
                    {
                        "type": argument_type,
                        "type_name": ARGUMENT_TYPE_NAMES.get(
                            argument_type, f"unknown_{argument_type}"
                        ),
                        "value": _parse_command_argument(reader, argument_type),
                    }
                )

        messages.append(
            {
                "wire_offset": message_offset,
                "wire_size": reader.offset - message_offset,
                "message_type": message_type,
                "message_type_hex": f"0x{message_type:03X}",
                "message_name": GAME_MESSAGE_NAMES.get(message_type),
                "source_index": source_index,
                "arguments": arguments,
            }
        )

    if reader.offset != len(payload):
        raise ReplayFormatError(
            f"game-command payload has {len(payload) - reader.offset} trailing bytes"
        )
    return {
        "xfer_version": xfer_version,
        "message_count": message_count,
        "messages": messages,
    }


def parse_object_stream_payload(
    payload: bytes, expected_stream_type: int | None = None
) -> dict[str, Any]:
    """Decode the shared Camera/Voice/Telestrator object-stream payload."""
    reader = Reader(payload)
    message_count = reader.u16()
    messages: list[dict[str, Any]] = []
    inherited_base_key = 0

    for _ in range(message_count):
        stream_id = reader.u32()
        type_and_flags = reader.u8()
        stream_type = type_and_flags & 0x3F
        if type_and_flags & 0x40:
            inherited_base_key = reader.u32()

        message: dict[str, Any] = {
            "stream_id": stream_id,
            "stream_type": stream_type,
            "base_key": inherited_base_key,
            "type_and_flags": type_and_flags,
            "base_key_present": bool(type_and_flags & 0x40),
        }
        if expected_stream_type is not None and stream_type != expected_stream_type:
            raise ReplayFormatError(
                f"expected stream object type {expected_stream_type}, got {stream_type}"
            )

        if stream_type == 13:
            message["type_name"] = "NetVoiceDataMsg"
            message["frame"] = reader.u32()
            message["serial"] = reader.u16()
            byte_count = reader.u16()
            message["data_size"] = byte_count
            message["data_hex"] = reader.take(byte_count).hex()
        elif stream_type == 14:
            message["type_name"] = "NetCameraDataMsg"
            message["camera_frame"] = reader.u32()
            update_flags = reader.u8()
            message["update_flags"] = update_flags
            if update_flags & 1:
                message["position"] = [reader.f32(), reader.f32(), reader.f32()]
            if update_flags & 2:
                message["rotation_quaternion"] = [
                    reader.f32(),
                    reader.f32(),
                    reader.f32(),
                    reader.f32(),
                ]
            if update_flags & ~3:
                raise ReplayFormatError(
                    f"unsupported NetCameraDataMsg update flags "
                    f"0x{update_flags:02X}"
                )
        elif stream_type == 15:
            message["type_name"] = "NetTelestratorDataMsg"
            message["frame"] = reader.u32()
            byte_count = reader.u8()
            message["data_size"] = byte_count
            operation_data = reader.take(byte_count)
            message["data_hex"] = operation_data.hex()
            message["operations"] = parse_telestrator_operations(operation_data)
        else:
            raise ReplayFormatError(
                f"unsupported replay object stream type {stream_type}"
            )
        messages.append(message)

    if reader.offset != len(payload):
        raise ReplayFormatError(
            f"camera payload has {len(payload) - reader.offset} trailing bytes"
        )
    return {
        "message_count": message_count,
        "messages": messages,
    }


def parse_telestrator_operations(data: bytes) -> list[dict[str, Any]]:
    """Decode the four operation forms used by NetTelestratorDataMsg.

    The exact layouts come from the KW 1.02 native operation dispatchers:
    type 0 is two u32 identifiers plus one u16/u16 point, types 1 and 2
    carry one point, and type 3 is a one-byte terminator.
    """
    reader = Reader(data)
    operations: list[dict[str, Any]] = []
    while reader.offset < len(data):
        operation_offset = reader.offset
        operation_type = reader.u8()
        operation: dict[str, Any] = {
            "offset": operation_offset,
            "type": operation_type,
            "name": TELESTRATOR_OPERATION_NAMES.get(
                operation_type, f"unknown_{operation_type}"
            ),
        }
        if operation_type == 0:
            operation["identifier_a"] = reader.u32()
            operation["identifier_b"] = reader.u32()
            point = [reader.u16(), reader.u16()]
            operation["point_u16"] = point
            operation["point_fixed_16_16"] = [value << 16 for value in point]
            operation["encoded_size"] = 13
        elif operation_type in (1, 2):
            point = [reader.u16(), reader.u16()]
            operation["point_u16"] = point
            operation["point_fixed_16_16"] = [value << 16 for value in point]
            operation["encoded_size"] = 5
        elif operation_type == 3:
            operation["encoded_size"] = 1
        else:
            raise ReplayFormatError(
                f"unsupported telestrator operation {operation_type} "
                f"at payload offset 0x{operation_offset:X}"
            )
        operations.append(operation)
    return operations


def parse_camera_payload(payload: bytes) -> dict[str, Any]:
    return parse_object_stream_payload(payload, expected_stream_type=14)


def parse_voice_payload(payload: bytes) -> dict[str, Any]:
    return parse_object_stream_payload(payload, expected_stream_type=13)


def parse_telestrator_payload(payload: bytes) -> dict[str, Any]:
    return parse_object_stream_payload(payload, expected_stream_type=15)


def parse_metadata_stream_payload(payload: bytes) -> dict[str, Any]:
    """Decode the fixed-size BroadcastMetadata stream used by 0xFD-0xFF."""
    reader = Reader(payload)
    message_count = reader.u8()
    messages: list[dict[str, Any]] = []
    for _ in range(message_count):
        metadata_type = reader.u32()
        raw_payload = reader.take(56)
        message: dict[str, Any] = {
            "metadata_type": metadata_type,
            "type_name": METADATA_TYPE_NAMES.get(
                metadata_type, f"unknown_{metadata_type}"
            ),
            "raw_payload_hex": raw_payload.hex(),
        }
        if metadata_type == 1:
            message["frame_or_timestamp"] = struct.unpack_from("<I", raw_payload)[0]
            message["unused_nonzero_bytes"] = sum(value != 0 for value in raw_payload[4:])
        elif metadata_type == 2:
            value = struct.unpack_from("<I", raw_payload)[0]
            channel_count = raw_payload[4]
            if channel_count > 12:
                raise ReplayFormatError(
                    f"BroadcastMetadata type 2 channel count {channel_count} exceeds 12"
                )
            channel_ids = list(
                struct.unpack_from(f"<{channel_count}I", raw_payload, 8)
            )
            message["value"] = value
            message["channel_count"] = channel_count
            message["channel_ids"] = channel_ids
            used_end = 8 + channel_count * 4
            message["unused_nonzero_bytes"] = sum(
                value != 0 for value in raw_payload[used_end:]
            )
            message["duplicate_channel_ids"] = sorted(
                channel
                for channel, count in Counter(channel_ids).items()
                if count > 1
            )
        else:
            message["unused_nonzero_bytes"] = None
        messages.append(message)

    if reader.offset != len(payload):
        raise ReplayFormatError(
            f"metadata stream has {len(payload) - reader.offset} trailing bytes"
        )
    return {
        "message_count": message_count,
        "messages": messages,
    }


def parse_cnc3rpl_metadata(
    metadata: bytes, game_version_parts: list[int] | None = None
) -> dict[str, Any]:
    """Decode exact KW 1.02 metadata; unknown semantics retain source offsets."""
    reader = Reader(metadata)
    tag_size = reader.u32()
    tag_bytes = reader.take(tag_size)
    tag = tag_bytes.rstrip(b"\0").decode("ascii", errors="replace")
    if tag != "CNC3RPL":
        raise ReplayFormatError(f"unsupported replay metadata tag {tag!r}")

    result: dict[str, Any] = {
        "tag_size": tag_size,
        "tag": tag,
    }
    if game_version_parts and tuple(game_version_parts) != SUPPORTED_GAME_VERSION:
        raise ReplayFormatError("Only KW 1.02 (1.2.0.0) metadata is supported.")
    result["schema"] = "kw_1_2"

    result["source_00_to_18_u32"] = [reader.u32() for _ in range(7)]
    result["source_1c_u8"] = reader.u8()
    result["source_1d_u32"] = reader.u32()
    result["source_21_u32"] = reader.u32()

    ascii_size = reader.u32()
    result["source_28_ascii"] = reader.take(ascii_size).decode(
        "latin-1", errors="replace"
    )
    result["source_2c_u32"] = reader.u32()

    wide_count = reader.u32()
    result["source_30_utf16"] = reader.take(wide_count * 2).decode(
        "utf-16-le", errors="replace"
    )
    result["source_34_u8"] = reader.u8()

    wide_count = reader.u32()
    result["source_38_utf16"] = reader.take(wide_count * 2).decode(
        "utf-16-le", errors="replace"
    )
    system_time_raw = reader.take(16)
    system_time_parts = struct.unpack("<8H", system_time_raw)
    result["recorded_system_time"] = {
        "year": system_time_parts[0],
        "month": system_time_parts[1],
        "day_of_week": system_time_parts[2],
        "day": system_time_parts[3],
        "hour": system_time_parts[4],
        "minute": system_time_parts[5],
        "second": system_time_parts[6],
        "milliseconds": system_time_parts[7],
    }

    generated_ascii_size = reader.u32()
    result["engine_version_text"] = reader.take(generated_ascii_size).decode(
        "latin-1", errors="replace"
    )
    result["source_7c_u32"] = reader.u32()
    result["source_80_u8"] = reader.u8()
    result["source_84_u32"] = reader.u32()
    result["source_88_u32"] = reader.u32()
    channel_ids_raw = [reader.u32() for _ in range(12)]
    result["source_8c_to_b8_u32"] = channel_ids_raw
    if result["source_88_u32"] <= len(channel_ids_raw):
        result["channel_ids_candidate"] = channel_ids_raw[
            : result["source_88_u32"]
        ]
    result["source_bc_u32"] = reader.u32()
    result["source_c0_u32"] = reader.u32()
    result["source_c4_to_c8_u32"] = [reader.u32() for _ in range(2)]

    if reader.offset != len(metadata):
        raise ReplayFormatError(
            f"CNC3RPL metadata has {len(metadata) - reader.offset} trailing bytes"
        )
    result["decoded_size"] = reader.offset
    return result


def parse_header(data: bytes) -> dict[str, Any]:
    reader = Reader(data)
    if reader.take(len(HEADER_MAGIC)) != HEADER_MAGIC:
        raise ReplayFormatError("missing C&C3 replay header magic")

    container_version = reader.u8()
    if container_version not in (4, 5):
        raise ReplayFormatError(f"unsupported header version {container_version}")

    game_version = [reader.u32() for _ in range(4)]
    if tuple(game_version) != SUPPORTED_GAME_VERSION:
        version = ".".join(map(str, game_version))
        raise ReplayFormatError(f"Unsupported game version {version}; this tool supports KW 1.02 (1.2.0.0) only.")
    channel_mask = reader.u16()
    channels = [channel for channel in range(12) if channel_mask & (1 << channel)]

    result: dict[str, Any] = {
        "container_version": container_version,
        "game_version": ".".join(str(value) for value in game_version),
        "game_version_parts": game_version,
        "channel_mask": channel_mask,
        "channels": channels,
        "match_title": reader.utf16z(200),
        "match_description": reader.utf16z(500),
        "map_name": reader.utf16z(200),
        "map_id": reader.utf16z(250),
    }

    participant_count = reader.u8()
    if participant_count > 8:
        raise ReplayFormatError("Participant count exceeds the eight native header slots.")
    participants = []
    for _ in range(participant_count):
        participant = {
            "id": reader.u32(),
            "name": reader.utf16z(20),
        }
        if container_version >= 5:
            participant["team"] = reader.u8()
        participants.append(participant)
    result["participants"] = participants

    observer = {
        "id": reader.u32(),
        "name": reader.utf16z(20),
    }
    if container_version >= 5:
        observer["team"] = reader.u8()
    result["observer"] = observer

    metadata_size = reader.u32()
    metadata = reader.take(metadata_size)
    result["metadata_size"] = metadata_size
    result["metadata_sha256"] = hashlib.sha256(metadata).hexdigest()
    result["metadata_prefix_hex"] = metadata[:32].hex()
    if len(metadata) >= 12:
        tag_size = struct.unpack_from("<I", metadata)[0]
        if 0 < tag_size <= len(metadata) - 4:
            result["metadata_tag"] = metadata[4 : 4 + tag_size].rstrip(b"\0").decode(
                "ascii", errors="replace"
            )
            if result["metadata_tag"] == "CNC3RPL":
                try:
                    result["metadata_decoded"] = parse_cnc3rpl_metadata(
                        metadata, game_version
                    )
                except ReplayFormatError as exc:
                    result["metadata_decode_error"] = str(exc)

    result["body_offset"] = reader.offset
    return result


def parse_footer(data: bytes) -> dict[str, Any]:
    if len(data) < 4:
        raise ReplayFormatError("file is too short to contain a footer")
    footer_size = struct.unpack_from("<I", data, len(data) - 4)[0]
    footer_offset = len(data) - footer_size
    if footer_size < len(FOOTER_MAGIC) + 9 or footer_offset < 8:
        raise ReplayFormatError(f"invalid footer size {footer_size}")

    reader = Reader(data, footer_offset)
    if reader.take(len(FOOTER_MAGIC)) != FOOTER_MAGIC:
        raise ReplayFormatError(
            f"missing C&C3 replay footer magic at 0x{footer_offset:X}"
        )

    result: dict[str, Any] = {
        "footer_offset": footer_offset,
        "footer_size": footer_size,
        "last_frame": reader.u32(),
    }
    footer_version = reader.u8()
    result["footer_version"] = footer_version
    if footer_version == 1:
        result["metadata_type"] = reader.u8()
        payload_size = reader.u32()
        payload = reader.take(payload_size)
        result["metadata_size"] = payload_size
        result["metadata_hex"] = payload.hex()
    elif footer_version != 2:
        raise ReplayFormatError(f"unsupported footer version {footer_version}")

    encoded_size = reader.u32()
    if encoded_size != footer_size:
        raise ReplayFormatError(
            f"footer size mismatch: trailer={footer_size}, parsed={encoded_size}"
        )
    if reader.offset != len(data):
        raise ReplayFormatError("footer did not consume the end of the file")

    sentinel_offset = footer_offset - 8
    reserved, frame = struct.unpack_from("<II", data, sentinel_offset)
    result["sentinel_offset"] = sentinel_offset
    result["sentinel_reserved"] = reserved
    result["sentinel_frame"] = frame
    if frame != END_FRAME:
        raise ReplayFormatError(
            f"missing 0x7FFFFFFF end-frame sentinel at 0x{sentinel_offset + 4:X}"
        )
    return result


def parse_records(
    data: bytes,
    start: int,
    end: int,
    declared_channels: set[int] | None = None,
) -> dict[str, Any]:
    reader = Reader(data, start)
    counts: Counter[int] = Counter()
    message_type_counts: Counter[int] = Counter()
    source_index_counts: Counter[int] = Counter()
    argument_type_counts: Counter[int] = Counter()
    command_payload_versions: Counter[int] = Counter()
    first_frame: int | None = None
    last_frame: int | None = None
    records = 0
    command_payloads = 0
    command_messages = 0
    command_decode_errors: list[dict[str, Any]] = []
    camera_payloads = 0
    camera_messages = 0
    camera_position_updates = 0
    camera_rotation_updates = 0
    camera_decode_errors: list[dict[str, Any]] = []
    auxiliary_payloads: Counter[int] = Counter()
    auxiliary_messages: Counter[int] = Counter()
    auxiliary_types: Counter[tuple[int, int]] = Counter()
    auxiliary_decode_errors: list[dict[str, Any]] = []
    logic_crc_checkpoints: list[dict[str, Any]] = []
    logic_crc_checkpoint_count = 0
    logic_crc_values_by_frame: dict[int, set[int]] = {}
    logic_crc_source_counts: Counter[int] = Counter()
    rtt_announcement_count = 0
    rtt_samples: list[dict[str, Any]] = []
    rtt_source_slot_counts: Counter[tuple[int, int]] = Counter()
    stats_session_sizes: Counter[int] = Counter()
    stats_auth_sizes: Counter[int] = Counter()
    destruct_player_messages: list[dict[str, Any]] = []
    samples: list[dict[str, Any]] = []
    previous_frame: int | None = None
    frame_order_violations: list[dict[str, Any]] = []
    frame_order_violation_count = 0
    undeclared_channel_records: Counter[int] = Counter()
    unknown_channel_records: Counter[int] = Counter()
    reserved_nonzero_count = 0
    zero_payload_count = 0

    while reader.offset < end:
        record_offset = reader.offset
        if records >= MAX_RECORDS:
            raise ReplayFormatError("Replay exceeds the 250,000-record analysis limit.")
        reserved, frame, channel, payload_size, payload = read_body_record(reader, end)
        payload_offset = record_offset + 13

        if previous_frame is not None and frame < previous_frame:
            frame_order_violation_count += 1
            if len(frame_order_violations) < 50:
                frame_order_violations.append(
                    {
                        "offset": record_offset,
                        "previous_frame": previous_frame,
                        "frame": frame,
                    }
                )
        previous_frame = frame
        reserved_nonzero_count += reserved != 0
        zero_payload_count += payload_size == 0
        if channel not in CHANNEL_NAMES:
            unknown_channel_records[channel] += 1
        if (
            declared_channels is not None
            and channel < 0xFD
            and channel not in declared_channels
        ):
            undeclared_channel_records[channel] += 1

        if first_frame is None:
            first_frame = frame
        last_frame = frame
        counts[channel] += 1
        records += 1
        decoded_commands = None
        decoded_camera = None
        decoded_auxiliary = None
        if channel == 1:
            try:
                decoded_commands = parse_game_command_payload(payload)
                command_payloads += 1
                command_payload_versions[decoded_commands["xfer_version"]] += 1
                command_messages += decoded_commands["message_count"]
                for message in decoded_commands["messages"]:
                    message_type_counts[message["message_type"]] += 1
                    source_index_counts[message["source_index"]] += 1
                    for argument in message["arguments"]:
                        argument_type_counts[argument["type"]] += 1
                    argument_values = [
                        argument["value"] for argument in message["arguments"]
                    ]
                    if (
                        message["message_type"] == 0x261
                        and len(argument_values) == 5
                    ):
                        logic_crc_checkpoint_count += 1
                        checkpoint_frame = argument_values[2]
                        crc_u32 = argument_values[0] & 0xFFFFFFFF
                        logic_crc_values_by_frame.setdefault(
                            checkpoint_frame, set()
                        ).add(crc_u32)
                        logic_crc_source_counts[message["source_index"]] += 1
                        if len(logic_crc_checkpoints) < 64:
                            logic_crc_checkpoints.append(
                                {
                                    "record_frame": frame,
                                    "source_index": message["source_index"],
                                    "crc_u32": crc_u32,
                                    "timestamp_0": argument_values[1],
                                    "checkpoint_frame": checkpoint_frame,
                                    "playback": argument_values[3],
                                    "mismatch_reporting": argument_values[4],
                                }
                            )
                    elif message["message_type"] == 0x28F:
                        rtt_announcement_count += 1
                        if len(argument_values) == 9:
                            rtt_source_slot_counts[
                                (message["source_index"], argument_values[0])
                            ] += 1
                        if len(argument_values) == 9 and len(rtt_samples) < 16:
                            rtt_samples.append(
                                {
                                    "record_frame": frame,
                                    "source_index": message["source_index"],
                                    "local_slot": argument_values[0],
                                    "rtt_values": argument_values[1:],
                                }
                            )
                    elif (
                        message["message_type"] in (0x28C, 0x28D)
                        and len(argument_values) == 1
                        and isinstance(argument_values[0], str)
                    ):
                        byte_size = len(argument_values[0].encode("latin-1"))
                        if message["message_type"] == 0x28C:
                            stats_session_sizes[byte_size] += 1
                        else:
                            stats_auth_sizes[byte_size] += 1
                    elif (
                        message["message_type"] == 0x291
                        and len(argument_values) == 2
                    ):
                        destruct_player_messages.append(
                            {
                                "record_frame": frame,
                                "source_index": message["source_index"],
                                "slot": argument_values[0],
                                "destruct": argument_values[1],
                            }
                        )
            except ReplayFormatError as exc:
                if len(command_decode_errors) < 20:
                    command_decode_errors.append(
                        {
                            "offset": record_offset,
                            "frame": frame,
                            "error": str(exc),
                        }
                    )
        elif channel == 2:
            try:
                decoded_camera = parse_camera_payload(payload)
                camera_payloads += 1
                camera_messages += decoded_camera["message_count"]
                for message in decoded_camera["messages"]:
                    camera_position_updates += "position" in message
                    camera_rotation_updates += "rotation_quaternion" in message
            except ReplayFormatError as exc:
                if len(camera_decode_errors) < 20:
                    camera_decode_errors.append(
                        {
                            "offset": record_offset,
                            "frame": frame,
                            "error": str(exc),
                        }
                    )
        elif channel in (3, 4, 0xFD, 0xFE, 0xFF):
            try:
                if channel == 3:
                    decoded_auxiliary = parse_voice_payload(payload)
                elif channel == 4:
                    decoded_auxiliary = parse_telestrator_payload(payload)
                else:
                    decoded_auxiliary = parse_metadata_stream_payload(payload)
                auxiliary_payloads[channel] += 1
                auxiliary_messages[channel] += decoded_auxiliary["message_count"]
                for message in decoded_auxiliary["messages"]:
                    object_type = message.get(
                        "stream_type", message.get("metadata_type", -1)
                    )
                    auxiliary_types[(channel, object_type)] += 1
            except ReplayFormatError as exc:
                if len(auxiliary_decode_errors) < 20:
                    auxiliary_decode_errors.append(
                        {
                            "offset": record_offset,
                            "frame": frame,
                            "channel": channel,
                            "error": str(exc),
                        }
                    )

        if len(samples) < 12:
            sample = {
                "offset": record_offset,
                "reserved": reserved,
                "frame": frame,
                "channel": channel,
                "channel_name": CHANNEL_NAMES.get(channel, f"unknown_{channel}"),
                "payload_size": payload_size,
                "payload_prefix_hex": data[
                    payload_offset : payload_offset + min(payload_size, 64)
                ].hex(),
            }
            if decoded_commands is not None:
                sample["game_commands"] = decoded_commands
            if decoded_camera is not None:
                sample["camera"] = decoded_camera
            if decoded_auxiliary is not None:
                sample["auxiliary_stream"] = decoded_auxiliary
            samples.append(sample)

    if reader.offset != end:
        raise ReplayFormatError(
            f"record stream ended at 0x{reader.offset:X}, expected 0x{end:X}"
        )

    return {
        "record_count": records,
        "first_frame": first_frame,
        "last_frame": last_frame,
        "channel_counts": {
            f"{key}:{CHANNEL_NAMES.get(key, f'unknown_{key}')}": counts[key]
            for key in sorted(counts)
        },
        "game_commands": {
            "decoded_payload_count": command_payloads,
            "decoded_message_count": command_messages,
            "payload_version_counts": {
                str(key): command_payload_versions[key]
                for key in sorted(command_payload_versions)
            },
            "message_type_counts": {
                (
                    f"0x{key:03X}:{GAME_MESSAGE_NAMES[key]}"
                    if key in GAME_MESSAGE_NAMES
                    else f"0x{key:03X}:unknown"
                ): message_type_counts[key]
                for key in sorted(message_type_counts)
            },
            "source_index_counts": {
                str(key): source_index_counts[key]
                for key in sorted(source_index_counts)
            },
            "argument_type_counts": {
                f"{key}:{ARGUMENT_TYPE_NAMES.get(key, f'unknown_{key}')}": (
                    argument_type_counts[key]
                )
                for key in sorted(argument_type_counts)
            },
            "decode_error_count": len(command_decode_errors),
            "decode_errors": command_decode_errors,
        },
        "camera": {
            "decoded_payload_count": camera_payloads,
            "decoded_message_count": camera_messages,
            "position_update_count": camera_position_updates,
            "rotation_update_count": camera_rotation_updates,
            "decode_error_count": len(camera_decode_errors),
            "decode_errors": camera_decode_errors,
        },
        "multiplayer_control": {
            "logic_crc_checkpoint_count": logic_crc_checkpoint_count,
            "logic_crc_checkpoint_frame_count": len(logic_crc_values_by_frame),
            "logic_crc_source_counts": {
                str(source): count
                for source, count in sorted(logic_crc_source_counts.items())
            },
            "logic_crc_disagreement_frames": [
                frame
                for frame, values in sorted(logic_crc_values_by_frame.items())
                if len(values) > 1
            ],
            "logic_crc_checkpoint_samples": logic_crc_checkpoints,
            "logic_crc_checkpoint_samples_truncated": (
                logic_crc_checkpoint_count > len(logic_crc_checkpoints)
            ),
            "rtt_announcement_count": rtt_announcement_count,
            "rtt_source_slot_counts": {
                f"source_{source}:slot_{slot}": count
                for (source, slot), count in sorted(rtt_source_slot_counts.items())
            },
            "rtt_samples": rtt_samples,
            "stats_session_blob_size_counts": {
                str(size): count
                for size, count in sorted(stats_session_sizes.items())
            },
            "stats_auth_blob_size_counts": {
                str(size): count
                for size, count in sorted(stats_auth_sizes.items())
            },
            "destruct_player_messages": destruct_player_messages,
        },
        "auxiliary_streams": {
            "decoded_payload_counts": {
                f"{channel}:{CHANNEL_NAMES.get(channel, f'unknown_{channel}')}": (
                    auxiliary_payloads[channel]
                )
                for channel in sorted(auxiliary_payloads)
            },
            "decoded_message_counts": {
                f"{channel}:{CHANNEL_NAMES.get(channel, f'unknown_{channel}')}": (
                    auxiliary_messages[channel]
                )
                for channel in sorted(auxiliary_messages)
            },
            "type_counts": {
                f"channel_{channel}:type_{object_type}": count
                for (channel, object_type), count in sorted(auxiliary_types.items())
            },
            "decode_error_count": len(auxiliary_decode_errors),
            "decode_errors": auxiliary_decode_errors,
        },
        "structural_integrity": {
            "frame_order_violation_count": frame_order_violation_count,
            "frame_order_violations": frame_order_violations,
            "reserved_nonzero_count": reserved_nonzero_count,
            "zero_payload_count": zero_payload_count,
            "unknown_channel_records": {
                str(channel): count
                for channel, count in sorted(unknown_channel_records.items())
            },
            "undeclared_channel_records": {
                str(channel): count
                for channel, count in sorted(undeclared_channel_records.items())
            },
        },
        "sample_records": samples,
    }


def inspect(path: Path, include_records: bool = True) -> dict[str, Any]:
    data = read_replay_bytes(path)
    header = parse_header(data)
    footer = parse_footer(data)
    if header["body_offset"] > footer["sentinel_offset"]:
        raise ReplayFormatError("Header overlaps footer or end sentinel.")
    result: dict[str, Any] = {
        "path": str(path.resolve()),
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "header": header,
        "footer": footer,
    }
    if include_records:
        result["records"] = parse_records(
            data,
            header["body_offset"],
            footer["sentinel_offset"],
            declared_channels=set(header["channels"]),
        )
    return result


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path)
    parser.add_argument(
        "--header-only",
        action="store_true",
        help="skip command/stream record enumeration",
    )
    args = parser.parse_args()

    try:
        result = inspect(args.replay, include_records=not args.header_only)
    except (OSError, ReplayFormatError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
