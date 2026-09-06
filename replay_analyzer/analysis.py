"""High-level, explainable analysis built on the recovered KWReplay decoder."""

from __future__ import annotations

import hashlib
import json
import math
import re
import statistics
from collections import Counter, defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any

from tools.kwreplay_inspect import (
    CHANNEL_NAMES,
    MAX_RECORDS, MAX_FRAME, read_replay_bytes, read_body_record,
    GAME_MESSAGE_NAMES,
    Reader,
    ReplayFormatError,
    parse_camera_payload,
    parse_footer,
    parse_game_command_payload,
    parse_header,
    parse_metadata_stream_payload,
    parse_telestrator_payload,
    parse_voice_payload,
)
from replay_analyzer.telemetry import analyze_telemetry
from replay_analyzer.command_semantics import annotate_command
from replay_analyzer.desync import analyze_desync


# Exact KW 1.02: GameLogic_SecondsToLogicFrames 0x472FC1, g_logicFPS 0xB72ADC.
# Time labels assume the initial 15 Hz logic clock; not wall-clock duration.
SIMULATION_FPS = 15
ASSET_CATALOG_PATH = Path(__file__).with_name("data") / "asset_catalog.json"

VERIFIED_PLAYER_TEMPLATES = {
    1: "Random",
    7: "Steel Talons",
    8: "ZOCOM",
    11: "Marked of Kane",
}

FACTION_DISPLAY_NAMES = {
    "ALIEN": "Scrin",
    "SteelTalons": "Steel Talons",
    "MarkedOfKane": "Marked of Kane",
    "BlackHand": "Black Hand",
    "Reaper17": "Reaper-17",
    "Traveler59": "Traveler-59",
}

ASSET_COMMANDS = {
    "MSG_QUEUE_STRUCTURE_CREATE": ("queue_structure", 1),
    "MSG_CANCEL_STRUCTURE_CREATE": ("cancel_structure", 2),
    "MSG_PLACE_STRUCTURE": ("place_structure", 1),
    "MSG_QUEUE_UNIT_CREATE": ("queue_unit", 2),
    "MSG_CANCEL_UNIT_CREATE": ("cancel_unit", 2),
    "MSG_QUEUE_UPGRADE": ("queue_upgrade", 0),
    "MSG_CANCEL_UPGRADE": ("cancel_upgrade", 0),
    "MSG_DO_SPECIAL_POWER": ("special_power", 0),
    "MSG_DO_SPECIAL_POWER_AT_LOCATION": ("special_power_at_location", 0),
    "MSG_DO_SPECIAL_POWER_AT_OBJECT": ("special_power_at_object", 0),
}

BUILD_ORDER_EVENTS = {
    "queue_structure",
    "cancel_structure",
    "place_structure",
    "queue_unit",
    "cancel_unit",
    "queue_upgrade",
    "cancel_upgrade",
}

VISIBILITY_TARGET_COMMANDS = {
    "MSG_DO_ATTACK_OBJECT": 0,
    "MSG_DO_FORCE_ATTACK_OBJECT": 0,
    "MSG_DO_ATTACKMOVETO_OBJECT": 0,
    "MSG_DO_FORCEATTACKMOVETO_OBJECT": 0,
}

SYSTEM_MESSAGES = {
    "MSG_CLEAR_GAME_DATA",
    "MSG_LOGIC_CRC",
    "MSG_STATS_SESSION",
    "MSG_STATS_AUTH",
    "MSG_ANNOUNCE_RTT",
    "MSG_DESTRUCT_PLAYER",
}

SELECTION_MESSAGES = {
    "MSG_CREATE_SELECTED_GROUP",
    "MSG_CREATE_SELECTED_GROUP_NO_SOUND",
    "MSG_DESTROY_SELECTED_GROUP",
    "MSG_REMOVE_FROM_SELECTED_GROUP",
    "MSG_CREATE_SELECT_ALL_GROUP",
}

CATEGORY_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("Production", ("QUEUE_", "CANCEL_", "PLACE_STRUCTURE", "SELL")),
    ("Combat", ("ATTACK", "SPECIAL_POWER", "STANCE", "ORDERMODE")),
    ("Movement", ("MOVETO", "MOVE_TO", "REVERSE", "FORMATION", "SCATTER")),
    ("Control", ("SELECT", "TEAM", "RALLY_POINT", "STOP")),
    ("Economy", ("HARVEST", "DOCK", "REPAIR", "UPGRADE", "POWER")),
    ("Network", ("CRC", "RTT", "STATS_", "DESTRUCT_PLAYER")),
)


@lru_cache(maxsize=1)
def _asset_catalog() -> dict[str, dict[str, Any]]:
    try:
        payload = json.loads(ASSET_CATALOG_PATH.read_text(encoding="utf-8"))
        return payload.get("assets", {})
    except (OSError, ValueError, TypeError):
        return {}


def _compact_asset(asset_hash: str, asset: dict[str, Any] | None) -> dict[str, Any]:
    if not asset:
        return {
            "hash": asset_hash,
            "id": None,
            "name": f"Unknown asset 0x{asset_hash}",
            "resolved": False,
        }
    keys = (
        "id",
        "name",
        "type",
        "side",
        "build_cost",
        "build_time",
        "unit_category",
        "production_queue",
        "required_objects",
    )
    return {
        "hash": asset_hash,
        "resolved": True,
        **{key: asset[key] for key in keys if key in asset},
    }


def _display_faction(value: str | None) -> str | None:
    if not value:
        return None
    return FACTION_DISPLAY_NAMES.get(value, value)


def _parse_int(value: str, default: int | None = None) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def parse_match_configuration(value: str | None) -> dict[str, Any]:
    """Decode the stable portions of the semicolon/comma lobby descriptor."""
    if not value:
        return {"fields": {}, "slots": []}
    prefix, separator, slot_text = value.partition(";S=")
    fields: dict[str, str] = {}
    for item in prefix.split(";"):
        key, equals, field_value = item.partition("=")
        if equals:
            fields[key] = field_value

    map_descriptor = fields.get("M", "")
    map_contents_mask = None
    map_path = map_descriptor
    if len(map_descriptor) >= 2:
        try:
            map_contents_mask = int(map_descriptor[:2], 16)
            map_path = map_descriptor[2:]
        except ValueError:
            pass
    map_path_schema = None
    if re.match(r"^\ddata/", map_path):
        map_path_schema = int(map_path[0])
        map_path = map_path[1:]

    slots: list[dict[str, Any]] = []
    if separator:
        slot_blob = slot_text.partition(";")[0]
        for slot_index, encoded in enumerate(slot_blob.split(":")):
            if not encoded or encoded == "X":
                continue
            if not encoded.startswith("H"):
                slots.append(
                    {
                        "slot": slot_index,
                        "kind": encoded[:1],
                        "raw": encoded,
                    }
                )
                continue
            parts = encoded[1:].split(",")
            if len(parts) < 9:
                continue
            slots.append(
                {
                    "slot": slot_index,
                    "kind": "human",
                    "name": parts[0],
                    "network_id_hash": parts[1],
                    "port": _parse_int(parts[2]),
                    "accepted_and_map_flags": parts[3],
                    "color_index": _parse_int(parts[4]),
                    "player_template_index": _parse_int(parts[5]),
                    "declared_faction": VERIFIED_PLAYER_TEMPLATES.get(
                        _parse_int(parts[5], -999), f"Template {parts[5]}"
                    ),
                    "start_position": _parse_int(parts[6]),
                    "lobby_team": _parse_int(parts[7]),
                    "nat_behavior": _parse_int(parts[8]),
                    "extra_fields": parts[9:],
                }
            )

    rules = [_parse_int(item) for item in fields.get("RU", "").split()]
    return {
        "fields": fields,
        "map_contents_mask": map_contents_mask,
        "map_path_schema": map_path_schema,
        "map_path": map_path,
        "map_crc": fields.get("MC"),
        "map_size": _parse_int(fields.get("MS", "")),
        "seed": _parse_int(fields.get("SD", "")),
        "game_session_id": fields.get("GSID"),
        "game_type": _parse_int(fields.get("GT", "")),
        "player_count": _parse_int(fields.get("PC", "")),
        "rules": rules,
        "slots": slots,
    }


def _category(name: str | None) -> str:
    if not name:
        return "Unknown"
    for category, fragments in CATEGORY_RULES:
        if any(fragment in name for fragment in fragments):
            return category
    return "Other"


def _is_action(name: str | None) -> bool:
    if not name or name in SYSTEM_MESSAGES or name in SELECTION_MESSAGES:
        return False
    if name.startswith(("MSG_CREATE_TEAM", "MSG_SELECT_TEAM", "MSG_ADD_TEAM")):
        return False
    return True


def _safe_argument(argument: dict[str, Any], message_name: str | None) -> dict[str, Any]:
    value = argument["value"]
    result = {"type": argument["type_name"]}
    if (
        isinstance(value, str)
        and message_name in {"MSG_STATS_SESSION", "MSG_STATS_AUTH"}
    ):
        result["value"] = f"<{len(value.encode('latin-1'))}-byte opaque blob>"
        result["opaque"] = True
    elif isinstance(value, str) and (
        len(value) > 180 or any(ord(character) < 32 for character in value)
    ):
        raw = value.encode("latin-1", errors="replace")
        result["value"] = f"<{len(raw)}-byte binary/string value>"
        result["sha256"] = hashlib.sha256(raw).hexdigest()
    else:
        result["value"] = value
    return result


def _source_label(
    source_index: int, participants: list[dict[str, Any]]
) -> tuple[str, int | None]:
    slot = source_index - 3
    for index, participant in enumerate(participants):
        if participant.get("native_slot", index) == slot:
            return participant["name"] or f"Player {slot + 1}", slot
    return f"Source {source_index}", None


def _duration_label(seconds: float) -> str:
    total = max(0, round(seconds))
    minutes, seconds = divmod(total, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def _percentile(values: list[int], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return float(ordered[lower])
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _starting_structure_ids(
    side: str | None, asset_catalog: dict[str, dict[str, Any]]
) -> list[str]:
    """Return the faction's normal deployed starting construction structure."""
    if not side:
        return []
    result = []
    for asset in asset_catalog.values():
        asset_id = asset.get("id", "")
        if (
            asset.get("side") == side
            and "CONSTRUCTION_YARD" in asset.get("kind_of", [])
            and asset_id.endswith(("ConstructionYard", "DronePlatform"))
        ):
            result.append(asset_id)
    return sorted(result)


def _analyze_production_state(
    build_order: list[dict[str, Any]],
    asset_catalog: dict[str, dict[str, Any]],
    inferred_sides: dict[int, str],
    sources: list[int],
    rules: list[int | None],
) -> dict[str, Any]:
    """Reconcile command attempts with confirmed structure placements.

    Replays contain player inputs rather than every resulting game-state event.
    A structure placement is therefore our strongest production-completion
    confirmation; unit and upgrade completion remain intentionally unclaimed.
    """
    pending: dict[tuple[int, int | None, str], list[dict[str, Any]]] = defaultdict(list)
    owned_structures: dict[int, set[str]] = defaultdict(set)
    starting_assets: dict[int, list[str]] = {}
    prerequisite_checks: list[dict[str, Any]] = []
    lifecycles: list[dict[str, Any]] = []
    def empty_metrics() -> dict[str, Any]:
        return {
            "queue_attempt_value": 0,
            "cancel_attempt_value": 0,
            "confirmed_structure_value": 0,
            "matched_structures": 0,
            "cancelled_structure_attempts": 0,
            "pending_structure_attempts": 0,
            "unmatched_placements": 0,
            "timing_checks": 0,
            "fastest_base_ratio": None,
            "prerequisite_checks": 0,
            "missing_prerequisite_attempts": 0,
            "confirmed_missing_prerequisites": 0,
        }

    metrics: dict[int, dict[str, Any]] = {
        source: empty_metrics() for source in sources
    }

    for source in sources:
        seeds = _starting_structure_ids(inferred_sides.get(source), asset_catalog)
        starting_assets[source] = seeds
        owned_structures[source].update(seeds)

    for event in build_order:
        source = event["source_index"]
        source_metrics = metrics.setdefault(source, empty_metrics())
        asset = event["asset"]
        event_name = event["event"]
        asset_hash = asset["hash"]
        producer = event.get("producer_object_id")
        build_cost = asset.get("build_cost")
        if isinstance(build_cost, (int, float)):
            if event_name in {"queue_structure", "queue_unit"}:
                source_metrics["queue_attempt_value"] += int(build_cost)
            elif event_name in {"cancel_structure", "cancel_unit"}:
                source_metrics["cancel_attempt_value"] += int(build_cost)
            elif event_name == "place_structure":
                source_metrics["confirmed_structure_value"] += int(build_cost)

        if event_name.startswith("queue_"):
            required = [
                item
                for item in asset.get("required_objects", [])
                if isinstance(item, str) and item
            ]
            missing = [
                item for item in required if item not in owned_structures[source]
            ]
            if required:
                check = {
                    "frame": event["frame"],
                    "time": event["time"],
                    "source_index": source,
                    "player": event["player"],
                    "event": event_name,
                    "asset": asset,
                    "required_objects": required,
                    "missing_objects": missing,
                    "status": "missing_at_attempt" if missing else "satisfied",
                    "completion_confirmed": False,
                }
                prerequisite_checks.append(check)
                event["prerequisite"] = {
                    "status": check["status"],
                    "required_objects": required,
                    "missing_objects": missing,
                }
                source_metrics["prerequisite_checks"] += 1
                source_metrics["missing_prerequisite_attempts"] += bool(missing)

            if event_name == "queue_structure":
                pending[(source, producer, asset_hash)].append(
                    {
                        "event": event,
                        "missing_objects": missing,
                        "prerequisite_check": (
                            prerequisite_checks[-1] if required else None
                        ),
                    }
                )

        elif event_name == "cancel_structure":
            queue = pending[(source, producer, asset_hash)]
            queued = queue.pop(0) if queue else None
            lifecycle = {
                "source_index": source,
                "player": event["player"],
                "asset": asset,
                "producer_object_id": producer,
                "status": "cancelled_attempt" if queued else "unmatched_cancel",
                "queue_frame": queued["event"]["frame"] if queued else None,
                "end_frame": event["frame"],
                "end_time": event["time"],
            }
            if queued:
                lifecycle["queue_time"] = queued["event"]["time"]
                lifecycle["elapsed_frames"] = (
                    event["frame"] - queued["event"]["frame"]
                )
                lifecycle["elapsed_seconds"] = round(
                    lifecycle["elapsed_frames"] / SIMULATION_FPS, 3
                )
                source_metrics["cancelled_structure_attempts"] += 1
            lifecycles.append(lifecycle)

        elif event_name == "place_structure":
            queue = pending[(source, producer, asset_hash)]
            queued = queue.pop(0) if queue else None
            lifecycle = {
                "source_index": source,
                "player": event["player"],
                "asset": asset,
                "producer_object_id": producer,
                "status": "completed" if queued else "unmatched_placement",
                "queue_frame": queued["event"]["frame"] if queued else None,
                "end_frame": event["frame"],
                "end_time": event["time"],
            }
            if queued:
                queue_event = queued["event"]
                elapsed_frames = event["frame"] - queue_event["frame"]
                lifecycle["queue_time"] = queue_event["time"]
                lifecycle["elapsed_frames"] = elapsed_frames
                lifecycle["elapsed_seconds"] = round(
                    elapsed_frames / SIMULATION_FPS, 3
                )
                source_metrics["matched_structures"] += 1
                build_time = asset.get("build_time")
                if isinstance(build_time, (int, float)) and build_time > 0:
                    base_frames = round(float(build_time) * SIMULATION_FPS)
                    ratio = elapsed_frames / base_frames
                    lifecycle["catalog_base_frames"] = base_frames
                    lifecycle["catalog_base_seconds"] = build_time
                    lifecycle["base_time_ratio"] = round(ratio, 3)
                    if ratio < 0.5:
                        lifecycle["timing_status"] = "extreme_fast_reference"
                    elif ratio < 0.9:
                        lifecycle["timing_status"] = "faster_than_catalog_reference"
                    else:
                        lifecycle["timing_status"] = "at_or_slower_than_catalog_reference"
                    source_metrics["timing_checks"] += 1
                    previous = source_metrics["fastest_base_ratio"]
                    source_metrics["fastest_base_ratio"] = (
                        round(ratio, 3)
                        if previous is None
                        else min(previous, round(ratio, 3))
                    )
                if queued["prerequisite_check"]:
                    queued["prerequisite_check"]["completion_confirmed"] = True
                    if queued["missing_objects"]:
                        queued["prerequisite_check"]["status"] = (
                            "missing_at_confirmed_queue"
                        )
                        source_metrics["confirmed_missing_prerequisites"] += 1
                production_lifecycle = {
                    "queue_frame": queue_event["frame"],
                    "elapsed_frames": elapsed_frames,
                    "elapsed_seconds": lifecycle["elapsed_seconds"],
                }
                if lifecycle.get("base_time_ratio") is not None:
                    production_lifecycle["base_time_ratio"] = lifecycle[
                        "base_time_ratio"
                    ]
                    production_lifecycle["timing_status"] = lifecycle[
                        "timing_status"
                    ]
                event["production_lifecycle"] = production_lifecycle
            else:
                source_metrics["unmatched_placements"] += 1
            asset_id = asset.get("id")
            if asset_id:
                owned_structures[source].add(asset_id)
            lifecycles.append(lifecycle)

    for queue in pending.values():
        for queued in queue:
            event = queued["event"]
            metrics[event["source_index"]]["pending_structure_attempts"] += 1
            lifecycles.append(
                {
                    "source_index": event["source_index"],
                    "player": event["player"],
                    "asset": event["asset"],
                    "producer_object_id": event.get("producer_object_id"),
                    "status": "pending_or_rejected_attempt",
                    "queue_frame": event["frame"],
                    "queue_time": event["time"],
                    "end_frame": None,
                }
            )

    starting_cash = (
        rules[2]
        if len(rules) > 2 and isinstance(rules[2], int) and rules[2] >= 0
        else None
    )
    timing_ratios = [
        item["base_time_ratio"]
        for item in lifecycles
        if item.get("base_time_ratio") is not None
    ]
    return {
        "engine_model": {
            "logic_frames_per_second": SIMULATION_FPS,
            "payment": "unverified_for_1.02",
            "payment_formula": "Replay commands do not establish payments or cash balance.",
            "insufficient_funds": "No 1.02 payment model is asserted by this release.",
            "cancellation": "Cancellation commands are recorded attempts; refunds are not measured.",
            "evidence": "Historical production model depended on 1.03 evidence and is excluded.",
        },
        "lobby_economy": {
            "starting_cash": starting_cash,
            "source": "RU[2]" if starting_cash is not None else None,
            "confidence": "medium" if starting_cash is not None else "unavailable",
            "note": (
                "RU[2] is the observed starting-cash lobby field; native field "
                "naming is still under recovery."
            ),
        },
        "players": {str(source): value for source, value in metrics.items()},
        "starting_assets": {
            str(source): assets for source, assets in starting_assets.items()
        },
        "structure_lifecycles": sorted(
            lifecycles,
            key=lambda item: (
                item.get("queue_frame")
                if item.get("queue_frame") is not None
                else item.get("end_frame", 0)
            ),
        ),
        "prerequisite_checks": prerequisite_checks,
        "summary": {
            "matched_structures": sum(
                item["matched_structures"] for item in metrics.values()
            ),
            "cancelled_structure_attempts": sum(
                item["cancelled_structure_attempts"] for item in metrics.values()
            ),
            "pending_structure_attempts": sum(
                item["pending_structure_attempts"] for item in metrics.values()
            ),
            "unmatched_placements": sum(
                item["unmatched_placements"] for item in metrics.values()
            ),
            "timing_checks": len(timing_ratios),
            "fastest_base_ratio": min(timing_ratios) if timing_ratios else None,
            "prerequisite_checks": len(prerequisite_checks),
            "missing_prerequisite_attempts": sum(
                bool(item["missing_objects"]) for item in prerequisite_checks
            ),
            "confirmed_missing_prerequisites": sum(
                item["status"] == "missing_at_confirmed_queue"
                for item in prerequisite_checks
            ),
        },
        "limitations": [
            "Replay commands are requests; placement commands do not prove successful execution.",
            "Unit and upgrade completion are not emitted as replay commands.",
            "Harvester deposits, captures, losses, powers, and all income are not yet reconstructed.",
            "Catalog base time excludes live player, handicap, power, and upgrade modifiers.",
            "Owned prerequisite state is monotonic, so destroyed or sold tech can only hide violations, not create them.",
        ],
    }


def analyze_replay(
    path: Path,
    display_name: str | None = None,
    telemetry_path: Path | None = None,
    telemetry_display_name: str | None = None,
    reference_catalog: bool = False,
) -> dict[str, Any]:
    """Decode a replay and return a UI-oriented report with explainable signals."""
    data = read_replay_bytes(path)
    header = parse_header(data)
    footer = parse_footer(data)
    if header["body_offset"] > footer["sentinel_offset"]:
        raise ReplayFormatError("Header overlaps footer or end sentinel.")
    if footer["last_frame"] > MAX_FRAME:
        raise ReplayFormatError("Footer frame exceeds the seven-day analysis limit.")
    participants = header["participants"]
    metadata = header.get("metadata_decoded") or {}
    match_configuration = parse_match_configuration(metadata.get("source_28_ascii"))
    lobby_slots_by_name = {
        slot["name"]: slot
        for slot in match_configuration["slots"]
        if slot.get("kind") == "human" and slot.get("name")
    }
    # Header roster excludes observers; command sources use native lobby slots + 3.
    # Match only unique names. Ambiguity is left explicit rather than guessed.
    lobby_by_name = defaultdict(list)
    for lobby in match_configuration["slots"]:
        if lobby.get("kind") == "human":
            lobby_by_name[lobby.get("name")].append(lobby["slot"])
    for index, participant in enumerate(participants):
        matches = lobby_by_name.get(participant["name"], [])
        unique_header_name = sum(p["name"] == participant["name"] for p in participants) == 1
        participant["native_slot"] = matches[0] if len(matches) == 1 and unique_header_name else (None if lobby_by_name else index)
        participant["source_mapping"] = "unique_lobby_name" if len(matches) == 1 and unique_header_name else ("unresolved" if lobby_by_name else "header_order_fallback")
    asset_catalog = _asset_catalog() if reference_catalog else {}
    body = Reader(data, header["body_offset"])
    body_end = footer["sentinel_offset"]

    channel_counts: Counter[int] = Counter()
    command_counts: Counter[str] = Counter()
    category_counts: Counter[str] = Counter()
    source_command_counts: Counter[int] = Counter()
    source_action_counts: Counter[int] = Counter()
    source_category_counts: dict[int, Counter[str]] = defaultdict(Counter)
    action_frames: dict[int, list[int]] = defaultdict(list)
    action_signatures: dict[int, list[tuple[int, str]]] = defaultdict(list)
    per_frame_actions: dict[int, Counter[int]] = defaultdict(Counter)
    bucket_actions: dict[int, Counter[int]] = defaultdict(Counter)
    commands: list[dict[str, Any]] = []
    build_order: list[dict[str, Any]] = []
    sell_events: list[dict[str, Any]] = []
    visibility_target_checks: list[dict[str, Any]] = []
    eliminations: list[dict[str, Any]] = []
    asset_side_counts: dict[int, Counter[str]] = defaultdict(Counter)
    known_spend: Counter[int] = Counter()
    known_cancelled_value: Counter[int] = Counter()
    source_sell_counts: Counter[int] = Counter()
    asset_reference_count = 0
    resolved_asset_count = 0
    command_decode_errors: list[dict[str, Any]] = []
    stream_decode_errors: list[dict[str, Any]] = []
    rtt_points: list[dict[str, Any]] = []
    camera_path: list[dict[str, Any]] = []
    camera_stream_states: dict[int, dict[str, Any]] = {}
    camera_frame_order_violations: list[dict[str, Any]] = []
    camera_updates = 0
    camera_rotations = 0
    voice_events: list[dict[str, Any]] = []
    voice_stream_state: dict[int, dict[str, int]] = {}
    voice_missing_packet_count = 0
    voice_duplicate_packet_count = 0
    telestrator_events: list[dict[str, Any]] = []
    telestrator_operation_counts: Counter[str] = Counter()
    metadata_events: list[dict[str, Any]] = []
    metadata_type_counts: Counter[str] = Counter()
    forensic_records: list[dict[str, Any]] = []
    frame_order_violations: list[dict[str, Any]] = []
    undeclared_channel_counts: Counter[int] = Counter()
    unknown_channel_counts: Counter[int] = Counter()
    command_schema_anomalies: list[dict[str, Any]] = []
    previous_record_frame: int | None = None
    unknown_message_count = 0
    reserved_nonzero = 0
    first_frame: int | None = None
    last_frame = 0
    record_count = 0

    while body.offset < body_end:
        record_offset = body.offset
        if record_count >= MAX_RECORDS:
            raise ReplayFormatError("Replay exceeds the 250,000-record analysis limit.")
        reserved, frame, channel, payload_size, payload = read_body_record(body, body_end)
        record_count += 1
        channel_counts[channel] += 1
        reserved_nonzero += reserved != 0
        if previous_record_frame is not None and frame < previous_record_frame:
            frame_order_violations.append(
                {
                    "record_index": record_count - 1,
                    "offset": record_offset,
                    "previous_frame": previous_record_frame,
                    "frame": frame,
                }
            )
        previous_record_frame = frame
        if channel not in CHANNEL_NAMES:
            unknown_channel_counts[channel] += 1
        if channel < 0xFD and channel not in header["channels"]:
            undeclared_channel_counts[channel] += 1
        first_frame = frame if first_frame is None else first_frame
        last_frame = max(last_frame, frame)
        forensic_record = {
            "index": record_count - 1,
            "offset": record_offset,
            "frame": frame,
            "time_seconds": round(frame / SIMULATION_FPS, 3),
            "time": _duration_label(frame / SIMULATION_FPS),
            "channel": channel,
            "channel_name": CHANNEL_NAMES.get(channel, f"unknown_{channel}"),
            "reserved": reserved,
            "payload_size": payload_size,
            "payload_sha256": hashlib.sha256(payload).hexdigest(),
            "payload_prefix_hex": payload[:16].hex(),
            "decoded_message_count": None,
            "decoded_types": [],
        }
        forensic_records.append(forensic_record)

        if channel == 1:
            try:
                decoded = parse_game_command_payload(payload)
                forensic_record["decoded_message_count"] = decoded["message_count"]
                forensic_record["decoded_types"] = sorted(
                    {
                        message["message_name"] or message["message_type_hex"]
                        for message in decoded["messages"]
                    }
                )
                for message in decoded["messages"]:
                    source = message["source_index"]
                    name = message["message_name"]
                    display_message_name = name or message["message_type_hex"]
                    category = _category(name)
                    is_action = _is_action(name)
                    source_label, participant_slot = _source_label(
                        source, participants
                    )
                    command_counts[display_message_name] += 1
                    source_command_counts[source] += 1
                    if name is None:
                        unknown_message_count += 1
                    if is_action:
                        source_action_counts[source] += 1
                        action_frames[source].append(frame)
                        action_signatures[source].append((frame, display_message_name))
                        per_frame_actions[source][frame] += 1
                        bucket_actions[frame // (SIMULATION_FPS * 30)][source] += 1

                    raw_values = [
                        argument["value"] for argument in message["arguments"]
                    ]
                    safe_arguments = [
                        _safe_argument(argument, name)
                        for argument in message["arguments"]
                    ]
                    command = {
                        "index": len(commands),
                        "record_index": record_count - 1,
                        "record_offset": record_offset,
                        "wire_offset": record_offset + 13 + message["wire_offset"],
                        "wire_size": message["wire_size"],
                        "wire_sha256": hashlib.sha256(payload[message["wire_offset"]:message["wire_offset"] + message["wire_size"]]).hexdigest(),
                        "argument_sha256": hashlib.sha256(payload[message["wire_offset"] + 2:message["wire_offset"] + message["wire_size"]]).hexdigest(),
                        "frame": frame,
                        "time_seconds": round(frame / SIMULATION_FPS, 3),
                        "time": _duration_label(frame / SIMULATION_FPS),
                        "source_index": source,
                        "participant_slot": participant_slot,
                        "player": source_label,
                        "message_type": message["message_type"],
                        "message_type_hex": message["message_type_hex"],
                        "name": display_message_name,
                        "category": category,
                        "is_action": is_action,
                        "arguments": safe_arguments,
                    }

                    annotate_command(command)
                    category_counts[command["category"]] += 1
                    source_category_counts[source][command["category"]] += 1
                    asset_spec = ASSET_COMMANDS.get(name)
                    production_index = name in ("MSG_QUEUE_UNIT_CREATE", "MSG_CANCEL_UNIT_CREATE") and len(raw_values) > 1 and raw_values[1] is True
                    if asset_spec:
                        event_name, argument_index = asset_spec
                        if (
                            argument_index < len(raw_values)
                            and type(raw_values[argument_index]) is int
                            and safe_arguments[argument_index]["type"] == "integer"
                        ):
                            asset_hash = f"{raw_values[argument_index] & 0xFFFFFFFF:08X}"
                            asset = None if production_index else asset_catalog.get(asset_hash)
                            compact_asset = _compact_asset(asset_hash, asset)
                            if production_index:
                                compact_asset = {"hash": None, "name": f"Production index {raw_values[argument_index]}", "resolved": False, "production_index": raw_values[argument_index]}
                            command["asset"] = compact_asset
                            command["event"] = event_name
                            asset_reference_count += not production_index
                            resolved_asset_count += bool(asset)

                            if (
                                asset
                                and asset.get("type") == "GameObject"
                                and asset.get("side")
                                and asset.get("side") != "Neutral"
                                and event_name in {
                                    "queue_structure",
                                    "place_structure",
                                    "queue_unit",
                                }
                            ):
                                asset_side_counts[source][asset["side"]] += 1

                            build_cost = (
                                asset.get("build_cost") if asset else None
                            )
                            if isinstance(build_cost, (int, float)):
                                if event_name in {"queue_structure", "queue_unit"}:
                                    known_spend[source] += int(build_cost)
                                elif event_name in {
                                    "cancel_structure",
                                    "cancel_unit",
                                }:
                                    known_cancelled_value[source] += int(build_cost)

                            if event_name in BUILD_ORDER_EVENTS:
                                build_event = {
                                    "frame": frame,
                                    "time_seconds": round(
                                        frame / SIMULATION_FPS, 3
                                    ),
                                    "time": _duration_label(
                                        frame / SIMULATION_FPS
                                    ),
                                    "source_index": source,
                                    "participant_slot": participant_slot,
                                    "player": source_label,
                                    "event": event_name,
                                    "asset": compact_asset,
                                }
                                if (
                                    event_name
                                    in {
                                        "queue_structure",
                                        "cancel_structure",
                                        "place_structure",
                                        "queue_unit",
                                        "cancel_unit",
                                    }
                                    and raw_values
                                    and isinstance(raw_values[0], int)
                                ):
                                    build_event["producer_object_id"] = int(
                                        raw_values[0]
                                    )
                                if (
                                    event_name == "queue_unit"
                                    and len(raw_values) >= 6
                                ):
                                    build_event["production_id"] = raw_values[3]
                                    build_event["queue_id"] = raw_values[5]
                                elif (
                                    event_name == "cancel_unit"
                                    and len(raw_values) >= 5
                                ):
                                    build_event["queue_id"] = raw_values[4]
                                if (
                                    event_name == "place_structure"
                                    and len(raw_values) >= 5
                                ):
                                    build_event["location"] = raw_values[3]
                                    build_event["rotation"] = raw_values[4]
                                build_order.append(build_event)
                        else:
                            command_schema_anomalies.append(
                                {
                                    "frame": frame,
                                    "offset": record_offset,
                                    "source_index": source,
                                    "message": display_message_name,
                                    "reason": (
                                        f"expected an integer asset hash at "
                                        f"argument {argument_index}"
                                    ),
                                }
                            )

                    if (
                        name == "MSG_SELL"
                        and len(raw_values) == 1
                        and isinstance(raw_values[0], int)
                    ):
                        sell_event = {
                            "frame": frame,
                            "time_seconds": round(frame / SIMULATION_FPS, 3),
                            "time": _duration_label(frame / SIMULATION_FPS),
                            "source_index": source,
                            "participant_slot": participant_slot,
                            "player": source_label,
                            "object_id": int(raw_values[0]),
                        }
                        command["event"] = "sell_object"
                        command["object_id"] = sell_event["object_id"]
                        sell_events.append(sell_event)
                        source_sell_counts[source] += 1
                    elif name == "MSG_SELL":
                        command_schema_anomalies.append(
                            {
                                "frame": frame,
                                "offset": record_offset,
                                "source_index": source,
                                "message": display_message_name,
                                "reason": "expected exactly one ObjectID argument",
                            }
                        )

                    target_argument = VISIBILITY_TARGET_COMMANDS.get(name)
                    if (
                        target_argument is not None
                        and target_argument < len(raw_values)
                        and isinstance(raw_values[target_argument], int)
                    ):
                        check_id = len(visibility_target_checks)
                        target_object_id = int(raw_values[target_argument])
                        visibility_target_checks.append(
                            {
                                "check_id": check_id,
                                "command_index": len(commands),
                                "frame": frame,
                                "source_index": source,
                                "player_index": source - 3,
                                "player": source_label,
                                "message": name,
                                "target_object_id": target_object_id,
                            }
                        )
                        command["target_object_id"] = target_object_id
                        command["visibility_check_id"] = check_id
                    elif target_argument is not None:
                        command_schema_anomalies.append(
                            {
                                "frame": frame,
                                "offset": record_offset,
                                "source_index": source,
                                "message": display_message_name,
                                "reason": (
                                    f"expected an ObjectID at argument "
                                    f"{target_argument}"
                                ),
                            }
                        )

                    commands.append(command)

                    if name == "MSG_ANNOUNCE_RTT" and len(raw_values) == 9:
                        valid_rtts = [
                            int(value)
                            for value in raw_values[1:]
                            if isinstance(value, int) and value >= 0
                        ]
                        rtt_points.append(
                            {
                                "frame": frame,
                                "time_seconds": round(frame / SIMULATION_FPS, 3),
                                "source_index": source,
                                "player": source_label,
                                "local_slot": raw_values[0],
                                "average_ms": (
                                    round(statistics.fmean(valid_rtts), 1)
                                    if valid_rtts
                                    else None
                                ),
                                "maximum_ms": max(valid_rtts) if valid_rtts else None,
                                "values": raw_values[1:],
                            }
                        )
                    elif name == "MSG_DESTRUCT_PLAYER" and len(raw_values) >= 2:
                        target_slot = raw_values[0]
                        if isinstance(target_slot, int):
                            target_name = (
                                participants[target_slot]["name"]
                                if 0 <= target_slot < len(participants)
                                else f"Slot {target_slot}"
                            )
                            eliminations.append(
                                {
                                    "frame": frame,
                                    "time_seconds": round(
                                        frame / SIMULATION_FPS, 3
                                    ),
                                    "time": _duration_label(
                                        frame / SIMULATION_FPS
                                    ),
                                    "target_slot": target_slot,
                                    "target_player": target_name,
                                    "confirmed": bool(raw_values[1]),
                                    "recorded_by_source": source,
                                    "recorded_by_player": source_label,
                                }
                            )
            except ReplayFormatError as exc:
                command_decode_errors.append(
                    {"offset": record_offset, "frame": frame, "error": str(exc)}
                )
        elif channel == 2:
            try:
                decoded = parse_camera_payload(payload)
                forensic_record["decoded_message_count"] = decoded["message_count"]
                forensic_record["decoded_types"] = ["NetCameraDataMsg"]
                for message in decoded["messages"]:
                    camera_updates += 1
                    camera_rotations += "rotation_quaternion" in message
                    stream_id = int(message["stream_id"])
                    previous_state = camera_stream_states.get(stream_id)
                    state = {
                        "position": (
                            list(previous_state["position"])
                            if previous_state
                            else [0.0, 0.0, 0.0]
                        ),
                        "rotation_quaternion": (
                            list(previous_state["rotation_quaternion"])
                            if previous_state
                            else [0.0, 0.0, 0.0, 1.0]
                        ),
                        "camera_frame": int(message["camera_frame"]),
                    }
                    if (
                        previous_state
                        and state["camera_frame"] < previous_state["camera_frame"]
                    ):
                        camera_frame_order_violations.append(
                            {
                                "record_frame": frame,
                                "stream_id": stream_id,
                                "previous_camera_frame": previous_state["camera_frame"],
                                "camera_frame": state["camera_frame"],
                            }
                        )
                    position_inherited = "position" not in message
                    rotation_inherited = "rotation_quaternion" not in message
                    if not position_inherited:
                        state["position"] = list(message["position"])
                    if not rotation_inherited:
                        state["rotation_quaternion"] = list(
                            message["rotation_quaternion"]
                        )
                    camera_stream_states[stream_id] = state
                    camera_path.append(
                        {
                            "frame": frame,
                            "camera_frame": state["camera_frame"],
                            "time_seconds": round(frame / SIMULATION_FPS, 3),
                            "stream_id": stream_id,
                            "base_key": message["base_key"],
                            "x": round(state["position"][0], 3),
                            "y": round(state["position"][1], 3),
                            "z": round(state["position"][2], 3),
                            "rotation_quaternion": [
                                round(value, 6)
                                for value in state["rotation_quaternion"]
                            ],
                            "position_inherited": position_inherited,
                            "rotation_inherited": rotation_inherited,
                        }
                    )
            except ReplayFormatError as exc:
                stream_decode_errors.append(
                    {
                        "offset": record_offset,
                        "frame": frame,
                        "channel": channel,
                        "error": str(exc),
                    }
                )
        elif channel in (3, 4, 0xFD, 0xFE, 0xFF):
            try:
                if channel == 3:
                    decoded = parse_voice_payload(payload)
                    forensic_record["decoded_types"] = ["NetVoiceDataMsg"]
                    for message in decoded["messages"]:
                        stream_id = int(message["stream_id"])
                        serial = int(message["serial"])
                        previous = voice_stream_state.get(stream_id)
                        serial_delta = (
                            (serial - previous["serial"]) & 0xFFFF
                            if previous
                            else None
                        )
                        missing_packets = (
                            serial_delta - 1
                            if serial_delta is not None and 1 < serial_delta < 0x8000
                            else 0
                        )
                        duplicate = serial_delta == 0
                        voice_missing_packet_count += missing_packets
                        voice_duplicate_packet_count += duplicate
                        event = {
                            "record_frame": frame,
                            "frame": int(message["frame"]),
                            "time_seconds": round(frame / SIMULATION_FPS, 3),
                            "stream_id": stream_id,
                            "base_key": message["base_key"],
                            "serial": serial,
                            "serial_delta": serial_delta,
                            "missing_packets_before": missing_packets,
                            "duplicate_serial": duplicate,
                            "data_size": int(message["data_size"]),
                            "data_sha256": hashlib.sha256(
                                bytes.fromhex(message["data_hex"])
                            ).hexdigest(),
                        }
                        voice_events.append(event)
                        voice_stream_state[stream_id] = {
                            "serial": serial,
                            "frame": int(message["frame"]),
                        }
                elif channel == 4:
                    decoded = parse_telestrator_payload(payload)
                    forensic_record["decoded_types"] = ["NetTelestratorDataMsg"]
                    for message in decoded["messages"]:
                        operations = message["operations"]
                        for operation in operations:
                            telestrator_operation_counts[operation["name"]] += 1
                        telestrator_events.append(
                            {
                                "record_frame": frame,
                                "frame": int(message["frame"]),
                                "time_seconds": round(frame / SIMULATION_FPS, 3),
                                "stream_id": int(message["stream_id"]),
                                "base_key": message["base_key"],
                                "data_size": int(message["data_size"]),
                                "operations": operations,
                            }
                        )
                else:
                    decoded = parse_metadata_stream_payload(payload)
                    for message in decoded["messages"]:
                        type_name = message["type_name"]
                        metadata_type_counts[type_name] += 1
                        metadata_events.append(
                            {
                                "record_frame": frame,
                                "time_seconds": round(frame / SIMULATION_FPS, 3),
                                "channel": channel,
                                "channel_name": CHANNEL_NAMES[channel],
                                **{
                                    key: value
                                    for key, value in message.items()
                                    if key != "raw_payload_hex"
                                },
                            }
                        )
                    forensic_record["decoded_types"] = sorted(
                        {message["type_name"] for message in decoded["messages"]}
                    )
                forensic_record["decoded_message_count"] = decoded["message_count"]
            except ReplayFormatError as exc:
                stream_decode_errors.append(
                    {
                        "offset": record_offset,
                        "frame": frame,
                        "channel": channel,
                        "error": str(exc),
                    }
                )

    if body.offset != body_end:
        raise ReplayFormatError(
            f"record stream ended at 0x{body.offset:X}, expected 0x{body_end:X}"
        )

    duration_seconds = max(last_frame, footer["last_frame"]) / SIMULATION_FPS
    duration_minutes = max(duration_seconds / 60, 1 / 60)
    telemetry = (
        analyze_telemetry(
            telemetry_path,
            replay_sha256=hashlib.sha256(data).hexdigest(),
            replay_first_frame=first_frame,
            replay_last_frame=last_frame,
            target_checks=visibility_target_checks,
        )
        if telemetry_path is not None
        else {"present": False}
    )
    if telemetry.get("present") and telemetry_display_name:
        telemetry["file"]["name"] = telemetry_display_name
    if telemetry.get("present"):
        presence_by_id = {
            int(item["object_id"]): item
            for item in telemetry["objects"].get("presence", [])
        }
        removals_by_id: dict[int, list[int]] = defaultdict(list)
        for event in telemetry["objects"].get("lifecycle_events", []):
            if event.get("event") == "object_removed":
                removals_by_id[int(event["object_id"])].append(
                    int(event["frame"])
                )
        capture_first = telemetry["capture"]["first_frame"]
        capture_last = telemetry["capture"]["last_frame"]
        for event in sell_events:
            object_id = event["object_id"]
            presence = presence_by_id.get(object_id)
            removals = [
                frame
                for frame in removals_by_id.get(object_id, [])
                if frame >= event["frame"]
            ]
            event["telemetry_observed"] = bool(
                presence
                and presence["first_frame"] <= event["frame"] <= presence["last_frame"]
            )
            event["telemetry_outcome"] = (
                "removed_after_request"
                if removals and removals[0] <= event["frame"] + SIMULATION_FPS * 30
                else (
                    "still_present_or_sell_rejected"
                    if event["telemetry_observed"]
                    and event["frame"] + SIMULATION_FPS * 30 <= capture_last
                    else (
                        "outside_capture"
                        if not capture_first <= event["frame"] <= capture_last
                        else "not_observed"
                    )
                )
            )
            if removals:
                event["removed_frame"] = removals[0]
                event["removed_delay_frames"] = removals[0] - event["frame"]
        for check in telemetry.get("visibility", {}).get(
            "target_checks", []
        ):
            command_index = check.get("command_index")
            if (
                isinstance(command_index, int)
                and 0 <= command_index < len(commands)
            ):
                commands[command_index]["visibility"] = {
                    key: value
                    for key, value in check.items()
                    if key
                    not in {
                        "check_id",
                        "command_index",
                        "message",
                        "player",
                        "source_index",
                    }
                }

    confirmed_eliminations = {
        event["target_slot"] for event in eliminations if event["confirmed"]
    }
    elimination_by_slot = {
        event["target_slot"]: event
        for event in eliminations
        if event["confirmed"]
    }
    players: list[dict[str, Any]] = []
    known_sources = sorted(source_command_counts)
    for header_index, participant in enumerate(participants):
        slot = participant["native_slot"]
        source = slot + 3 if slot is not None else -(header_index + 1)
        action_total = source_action_counts[source]
        action_apm = action_total / duration_minutes
        frame_bursts = list(per_frame_actions[source].values())
        intervals = [
            later - earlier
            for earlier, later in zip(
                action_frames[source], action_frames[source][1:]
            )
        ]
        player_name = participant["name"] or f"Player {header_index + 1}"
        lobby_slot = lobby_slots_by_name.get(player_name, {})
        side_counts = asset_side_counts[source]
        side_total = sum(side_counts.values())
        inferred_side, inferred_count = (
            side_counts.most_common(1)[0] if side_counts else (None, 0)
        )
        players.append(
            {
                "slot": slot,
                "header_index": header_index,
                "source_mapping": participant["source_mapping"],
                "source_index": source,
                "id": participant["id"],
                "name": player_name,
                "team": participant.get("team"),
                "lobby_team": lobby_slot.get("lobby_team"),
                "start_position": lobby_slot.get("start_position"),
                "color_index": lobby_slot.get("color_index"),
                "player_template_index": lobby_slot.get(
                    "player_template_index"
                ),
                "declared_faction": lobby_slot.get("declared_faction"),
                "inferred_faction": _display_faction(inferred_side),
                "faction_evidence_count": side_total,
                "faction_confidence": (
                    round(inferred_count / side_total, 3) if side_total else None
                ),
                "faction_evidence": [
                    {
                        "faction": _display_faction(side),
                        "count": count,
                    }
                    for side, count in side_counts.most_common()
                ],
                "eliminated": slot in confirmed_eliminations,
                "elimination": elimination_by_slot.get(slot),
                "known_queued_value": known_spend[source],
                "known_cancelled_value": known_cancelled_value[source],
                "sell_command_count": source_sell_counts[source],
                "command_count": source_command_counts[source],
                "action_count": action_total,
                "action_apm": round(action_apm, 1),
                "peak_actions_same_frame": max(frame_bursts, default=0),
                "median_action_interval_frames": (
                    round(statistics.median(intervals), 2) if intervals else None
                ),
                "p10_action_interval_frames": (
                    round(_percentile(intervals, 0.10), 2) if intervals else None
                ),
                "categories": dict(source_category_counts[source].most_common()),
            }
        )

    inferred_sides = {
        source: counts.most_common(1)[0][0]
        for source, counts in asset_side_counts.items()
        if counts
    }
    production_state = _analyze_production_state(
        build_order,
        asset_catalog,
        inferred_sides,
        [player["source_index"] for player in players],
        match_configuration.get("rules", []),
    )
    for player in players:
        production = production_state["players"].get(
            str(player["source_index"]), {}
        )
        player["production"] = production
        player["confirmed_structure_value"] = production.get(
            "confirmed_structure_value", 0
        )

    result: dict[str, Any] = {
        "status": "unknown",
        "label": "Result not recorded",
        "winning_team": None,
        "winners": [],
        "basis": (
            "The replay has no complete player-elimination sequence, so the "
            "winner is not inferred."
        ),
    }
    if confirmed_eliminations and all(player["slot"] is not None for player in players):
        survivors = [
            player for player in players if not player["eliminated"]
        ]
        surviving_teams = {
            player["team"]
            for player in survivors
            if player["team"] is not None
        }
        if len(surviving_teams) == 1:
            winning_team = next(iter(surviving_teams))
            other_slots = {
                player["slot"]
                for player in players
                if player["team"] != winning_team
            }
            if other_slots and other_slots.issubset(confirmed_eliminations):
                winners = [
                    player["name"]
                    for player in survivors
                    if player["team"] == winning_team
                ]
                result = {
                    "status": "recorded_elimination",
                    "label": (
                        f"{winners[0]} is the inferred survivor"
                        if len(winners) == 1
                        else f"Team {winning_team} is the inferred surviving team"
                    ),
                    "winning_team": winning_team,
                    "winners": winners,
                    "basis": (
                        "Derived from MSG_DESTRUCT_PLAYER records eliminating "
                        "every player outside the surviving header team."
                    ),
                }

    orphan_sources = [
        {
            "source_index": source,
            "label": _source_label(source, participants)[0],
            "command_count": source_command_counts[source],
        }
        for source in known_sources
        if _source_label(source, participants)[1] is None
    ]
    post_elimination_commands: list[dict[str, Any]] = []
    for slot, elimination in elimination_by_slot.items():
        eliminated_source = slot + 3
        grace_frame = int(elimination["frame"]) + SIMULATION_FPS
        post_elimination_commands.extend(
            {
                "frame": command["frame"],
                "time": command["time"],
                "source_index": eliminated_source,
                "player": command["player"],
                "message": command["name"],
                "elimination_frame": elimination["frame"],
            }
            for command in commands
            if command["source_index"] == eliminated_source
            and command["is_action"]
            and command["frame"] > grace_frame
        )

    footer_frame_mismatch = footer["last_frame"] != last_frame
    invalid_camera_streams = sorted(
        stream_id for stream_id in camera_stream_states if not 0 <= stream_id < 8
    )
    metadata_padding_anomalies = [
        event
        for event in metadata_events
        if isinstance(event.get("unused_nonzero_bytes"), int)
        and event["unused_nonzero_bytes"] > 0
    ]
    metadata_duplicate_channels = [
        event for event in metadata_events if event.get("duplicate_channel_ids")
    ]

    findings: list[dict[str, Any]] = []

    def add_finding(
        detector: str,
        severity: str,
        confidence: str,
        title: str,
        evidence: str,
        interpretation: str,
    ) -> None:
        if detector.startswith(("production_", "faction")):
            confidence = "low"
            interpretation = "Unverified R22 reference comparison, not a stock-1.02 finding. " + interpretation
        findings.append(
            {
                "detector": detector,
                "severity": severity,
                "confidence": confidence,
                "title": title,
                "evidence": evidence,
                "interpretation": interpretation,
            }
        )

    if telemetry.get("present") and telemetry["capture"]["integrity_issues"]:
        issues = telemetry["capture"]["integrity_issues"]
        add_finding(
            "telemetry_integrity",
            "high",
            "high",
            "Live telemetry failed integrity checks",
            f"{len(issues)} issue(s): " + "; ".join(issues[:4]),
            (
                "Object, ownership, and resource conclusions from this "
                "sidecar should not be trusted until the capture is repeated "
                "with the exact 1.02 collector profile and matching replay."
            ),
        )

    visibility = telemetry.get("visibility", {})
    shrouded_target_checks = [
        check
        for check in visibility.get("target_checks", [])
        if check.get("high_confidence_shrouded")
    ]
    shrouded_by_source: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for check in shrouded_target_checks:
        if isinstance(check.get("source_index"), int):
            shrouded_by_source[int(check["source_index"])].append(check)
    for source, checks in sorted(shrouded_by_source.items()):
        player_name = _source_label(source, participants)[0]
        examples = ", ".join(
            (
                f"{check['message']} -> ObjectID {check['target_object_id']} "
                f"at frame {check['frame']}"
            )
            for check in checks[:4]
        )
        add_finding(
            f"shrouded_target:{source}",
            "medium",
            "low",
            f"Sidecar reports shrouded object targeting by {player_name}",
            (
                f"{len(checks)} object-target command(s) were bracketed by "
                f"user-supplied cached OBJECTSHROUD_SHROUDED state. {examples}"
            ),
            (
                "Experimental correlation only. The sidecar is unauthenticated and "
                "collector offsets and synchronization are not live-validated by this release."
            ),
        )

    if frame_order_violations:
        examples = ", ".join(
            (
                f"0x{item['offset']:X}: "
                f"{item['previous_frame']}->{item['frame']}"
            )
            for item in frame_order_violations[:5]
        )
        add_finding(
            "record_frame_order",
            "critical",
            "high",
            "Replay record frames move backwards",
            (
                f"{len(frame_order_violations)} record(s) violate the writer's "
                f"monotonic frame invariant. {examples}"
            ),
            (
                "The recovered native writer rejects a record whose frame is "
                "older than the last accepted frame. This strongly indicates "
                "corruption, manual editing, or an unsupported writer."
            ),
        )
    if footer_frame_mismatch:
        add_finding(
            "footer_last_frame",
            "high",
            "high",
            "Footer frame does not match the final body record",
            (
                f"The final body record is frame {last_frame}, but the footer "
                f"declares frame {footer['last_frame']}."
            ),
            (
                "Known native file finalization writes the completed last-frame "
                "state into the footer. A mismatch is a strong truncation, "
                "editing, or unsupported-format indicator."
            ),
        )
    if footer["sentinel_reserved"] != 0:
        add_finding(
            "sentinel_reserved",
            "high",
            "high",
            "End sentinel has an unexpected reserved value",
            f"The end sentinel reserved dword is {footer['sentinel_reserved']}.",
            (
                "Native replay files use zero before the 0x7FFFFFFF sentinel "
                "frame; a nonzero value deserves forensic review."
            ),
        )
    if undeclared_channel_counts:
        details = ", ".join(
            f"channel {channel}: {count}"
            for channel, count in sorted(undeclared_channel_counts.items())
        )
        add_finding(
            "channel_mask",
            "high",
            "high",
            "Records use channels absent from the header mask",
            details,
            (
                "The recovered header mask and channel-list conversion define "
                "the enabled ordinary channels. This can indicate editing, "
                "damage, or an unsupported recorder."
            ),
        )
    if unknown_channel_counts:
        details = ", ".join(
            f"0x{channel:02X}: {count}"
            for channel, count in sorted(unknown_channel_counts.items())
        )
        add_finding(
            "unknown_record_channels",
            "medium",
            "high",
            "Unknown replay channels are present",
            details,
            (
                "These channel identifiers are outside the recovered command, "
                "camera, voice, telestrator, and metadata channel set."
            ),
        )
    if command_schema_anomalies:
        examples = "; ".join(
            f"{item['message']} at frame {item['frame']}: {item['reason']}"
            for item in command_schema_anomalies[:5]
        )
        add_finding(
            "command_schema",
            "high",
            "high",
            "Known commands violate recovered argument schemas",
            f"{len(command_schema_anomalies)} violation(s). {examples}",
            (
                "These checks cover only commands with recovered high-confidence "
                "asset, ObjectID, or target argument positions."
            ),
        )
    if post_elimination_commands:
        examples = ", ".join(
            f"{item['player']} {item['message']} at frame {item['frame']}"
            for item in post_elimination_commands[:5]
        )
        add_finding(
            "post_elimination_actions",
            "high",
            "medium",
            "Player actions continue after recorded elimination",
            (
                f"{len(post_elimination_commands)} action(s) occur more than "
                f"one second after elimination. {examples}"
            ),
            (
                "The elimination record is engine-generated, but delayed lockstep "
                "delivery and unusual game modes should be ruled out before "
                "treating this as malicious."
            ),
        )
    if invalid_camera_streams:
        add_finding(
            "camera_stream_id",
            "high",
            "high",
            "Camera stream identifiers exceed the native player range",
            "Invalid stream IDs: " + ", ".join(map(str, invalid_camera_streams)),
            (
                "The recovered camera application routine accepts only stream "
                "IDs 0 through 7."
            ),
        )
    if camera_frame_order_violations:
        add_finding(
            "camera_frame_order",
            "medium",
            "medium",
            "Camera substream frames move backwards",
            (
                f"{len(camera_frame_order_violations)} per-stream camera "
                "frame regression(s) were observed."
            ),
            (
                "This can indicate an edited or unusual camera stream, but it "
                "does not affect synchronized game state."
            ),
        )

    if command_decode_errors or stream_decode_errors:
        add_finding(
            "decoder_integrity",
            "high",
            "high",
            "Replay records failed structural decoding",
            (
                f"{len(command_decode_errors)} command and "
                f"{len(stream_decode_errors)} stream payloads failed."
            ),
            "This indicates corruption, an unsupported format, or possible modification.",
        )
    if reserved_nonzero:
        add_finding(
            "record_envelope",
            "medium",
            "medium",
            "Unexpected record-envelope values",
            f"{reserved_nonzero} records have a nonzero reserved field.",
            "Known validation replays use zero; this deserves manual inspection.",
        )
    if unknown_message_count:
        add_finding(
            "unknown_commands",
            "medium",
            "high",
            "Unknown game-message identifiers",
            f"{unknown_message_count} decoded messages do not have recovered names.",
            "They may come from another build or mod and should be understood before judging the match.",
        )
    if orphan_sources:
        add_finding(
            "source_mapping",
            "medium",
            "medium",
            "Commands from unmapped sources",
            ", ".join(
                f"source {item['source_index']} ({item['command_count']} commands)"
                for item in orphan_sources
            ),
            "The sources do not match the observed multiplayer slot-plus-three mapping.",
        )

    confirmed_prerequisite_gaps = [
        check
        for check in production_state["prerequisite_checks"]
        if check["status"] == "missing_at_confirmed_queue"
    ]
    if confirmed_prerequisite_gaps:
        examples = ", ".join(
            f"{item['player']}: {item['asset']['name']}"
            for item in confirmed_prerequisite_gaps[:5]
        )
        add_finding(
            "production_prerequisites",
            "high",
            "medium",
            "Confirmed structures queued without observed prerequisites",
            (
                f"{len(confirmed_prerequisite_gaps)} completed structure(s) had "
                f"missing catalog prerequisites when queued: {examples}."
            ),
            (
                "The queue-to-placement link confirms production, but captures, "
                "map-provided structures, and incomplete prerequisite semantics "
                "must be ruled out before treating this as a cheat."
            ),
        )

    extreme_timing = [
        lifecycle
        for lifecycle in production_state["structure_lifecycles"]
        if lifecycle.get("timing_status") == "extreme_fast_reference"
    ]
    if extreme_timing:
        fastest = min(
            extreme_timing, key=lambda item: item["base_time_ratio"]
        )
        add_finding(
            "production_timing",
            "high",
            "medium",
            "Structure completed far below catalog base time",
            (
                f"{len(extreme_timing)} structure(s) completed in under half "
                f"their catalog base time; fastest was {fastest['player']}'s "
                f"{fastest['asset']['name']} at "
                f"{fastest['base_time_ratio']:.2f}x."
            ),
            (
                "The native 15 Hz clock and queue/placement producer identity "
                "are recovered, but live build-speed modifiers are not yet "
                "reconstructed. This is a review signal, not proof."
            ),
        )

    for player in players:
        source = player["source_index"]
        declared_faction = player["declared_faction"]
        inferred_faction = player["inferred_faction"]
        faction_confidence = player["faction_confidence"] or 0
        if (
            player["player_template_index"] in VERIFIED_PLAYER_TEMPLATES
            and player["player_template_index"] != 1
            and inferred_faction
            and inferred_faction != declared_faction
            and player["faction_evidence_count"] >= 5
            and faction_confidence >= 0.80
        ):
            add_finding(
                f"faction_identity:{source}",
                "medium",
                "medium",
                f"Faction evidence differs for {player['name']}",
                (
                    f"The lobby declares {declared_faction}, while "
                    f"{player['faction_evidence_count']} decoded production "
                    f"events identify {inferred_faction} at "
                    f"{faction_confidence:.0%} confidence."
                ),
                (
                    "This may indicate modified game data or an incomplete asset "
                    "catalog. It is a review signal, not proof of cheating."
                ),
            )
        if player["action_apm"] >= 300 and player["action_count"] >= 100:
            add_finding(
                f"apm:{source}",
                "medium",
                "low",
                f"Very high action rate for {player['name']}",
                (
                    f"{player['action_count']} filtered actions, averaging "
                    f"{player['action_apm']} APM."
                ),
                "High APM can be legitimate and is only a statistical signal.",
            )
        if player["peak_actions_same_frame"] >= 20:
            add_finding(
                f"burst:{source}",
                "medium",
                "medium",
                f"Large same-frame action burst from {player['name']}",
                (
                    f"{player['peak_actions_same_frame']} action commands were "
                    "issued in one simulation frame."
                ),
                "This may indicate automation, but queued actions and engine batching must be ruled out.",
            )

        signatures = action_signatures[source]
        by_name: dict[str, list[int]] = defaultdict(list)
        for frame, name in signatures:
            by_name[name].append(frame)
        best: tuple[str, int, int, float] | None = None
        for name, frames in by_name.items():
            intervals = [
                later - earlier for earlier, later in zip(frames, frames[1:])
                if later > earlier
            ]
            if len(intervals) < 24:
                continue
            counts = Counter(intervals)
            interval, repeats = counts.most_common(1)[0]
            share = repeats / len(intervals)
            if interval <= 15 and share >= 0.80:
                candidate = (name, interval, repeats, share)
                if best is None or candidate[2] > best[2]:
                    best = candidate
        if best:
            name, interval, repeats, share = best
            add_finding(
                f"regularity:{source}",
                "medium",
                "low",
                f"Highly regular command timing for {player['name']}",
                (
                    f"{name} repeated at a {interval}-frame interval "
                    f"{repeats} times ({share:.0%} of its positive intervals)."
                ),
                "Mechanical timing can suggest a macro, but normal hotkeys and command batching can create similar patterns.",
            )

    integrity_deductions = (
        len(command_decode_errors) * 8
        + len(stream_decode_errors) * 8
        + min(reserved_nonzero, 10)
        + len(frame_order_violations) * 20
        + (20 if footer_frame_mismatch else 0)
        + (10 if footer["sentinel_reserved"] != 0 else 0)
        + min(sum(undeclared_channel_counts.values()) * 5, 20)
        + min(sum(unknown_channel_counts.values()) * 3, 15)
        + min(len(command_schema_anomalies) * 5, 20)
        + min(unknown_message_count, 10)
    )
    integrity_score = max(0, 100 - integrity_deductions)
    high_confidence = [
        finding
        for finding in findings
        if finding["confidence"] == "high"
        and finding["severity"] in {"critical", "high"}
    ]
    statistical = [
        finding
        for finding in findings
        if finding["detector"].startswith(("apm:", "burst:", "regularity:"))
    ]
    if high_confidence:
        assessment = "Deterministic or structural anomalies detected"
        assessment_tone = "danger"
    elif statistical:
        assessment = "Statistical signals need review"
        assessment_tone = "warning"
    else:
        assessment = "Findings available for review" if findings else "No checked structural anomalies detected"
        assessment_tone = "warning" if findings else "clear"

    timeline = []
    final_bucket = math.ceil(duration_seconds / 30)
    sources_for_timeline = [
        player["source_index"] for player in players if player["command_count"]
    ]
    for bucket in range(final_bucket + 1):
        timeline.append(
            {
                "start_seconds": bucket * 30,
                "time": _duration_label(bucket * 30),
                "players": {
                    str(source): bucket_actions[bucket][source]
                    for source in sources_for_timeline
                },
            }
        )

    camera_sample_step = max(1, math.ceil(len(camera_path) / 800))
    rtt_sample_step = max(1, math.ceil(len(rtt_points) / 800))
    voice_by_stream: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for event in voice_events:
        voice_by_stream[event["stream_id"]].append(event)
    voice_streams = [
        {
            "stream_id": stream_id,
            "player": (
                _source_label(stream_id + 3, participants)[0]
                if _source_label(stream_id + 3, participants)[1] is not None
                else None
            ),
            "packet_count": len(events),
            "encoded_bytes": sum(event["data_size"] for event in events),
            "first_frame": min(event["frame"] for event in events),
            "last_frame": max(event["frame"] for event in events),
            "missing_packet_count": sum(
                event["missing_packets_before"] for event in events
            ),
            "duplicate_serial_count": sum(
                event["duplicate_serial"] for event in events
            ),
        }
        for stream_id, events in sorted(voice_by_stream.items())
    ]
    camera_by_stream: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for point in camera_path:
        camera_by_stream[point["stream_id"]].append(point)
    camera_streams = [
        {
            "stream_id": stream_id,
            "player": (
                _source_label(stream_id + 3, participants)[0]
                if _source_label(stream_id + 3, participants)[1] is not None
                else None
            ),
            "update_count": len(points),
            "first_frame": min(point["frame"] for point in points),
            "last_frame": max(point["frame"] for point in points),
            "position_update_count": sum(
                not point["position_inherited"] for point in points
            ),
            "rotation_update_count": sum(
                not point["rotation_inherited"] for point in points
            ),
        }
        for stream_id, points in sorted(camera_by_stream.items())
    ]
    report = {
        "schema_version": 8,
        "target": "KW 1.02",
        "evidence": {
            "format": "exact_kw102_static_binary_and_fixture_evidence",
            "game_version_provenance": "declared_by_replay_header_not_authenticated",
            "native_oracle_verified": False,
            "asset_catalog": "unverified_R22_reference_opt_in" if reference_catalog else "disabled",
            "telemetry": "experimental_user_supplied_not_authenticated",
            "timing": "15_hz_initial_logic_clock",
            "limitations": ["No engine simulation or rendered playback", "No authoritative economy, fog or cheating verdict", "Malformed oversized records are rejected, not emulated"],
        },
        "generated_by": "KW Replay Lab",
        "file": {
            "name": display_name or path.name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        },
        "match": {
            "title": header["match_title"],
            "description": header["match_description"],
            "map_name": header["map_name"],
            "map_id": header["map_id"],
            "game_version": header["game_version"],
            "container_version": header["container_version"],
            "recorded_at": metadata.get("recorded_system_time"),
            "duration_seconds": round(duration_seconds, 3),
            "duration": _duration_label(duration_seconds),
            "first_frame": first_frame,
            "last_frame": last_frame,
            "footer_last_frame": footer["last_frame"],
            "simulation_fps_assumption": SIMULATION_FPS,
            "map_crc": match_configuration.get("map_crc"),
            "map_size": match_configuration.get("map_size"),
            "seed": match_configuration.get("seed"),
            "game_session_id": match_configuration.get("game_session_id"),
            "game_type": match_configuration.get("game_type"),
            "result": result,
        },
        "players": players,
        "observer": header["observer"],
        "summary": {
            "assessment": assessment,
            "assessment_tone": assessment_tone,
            "integrity_score": integrity_score,
            "score_kind": "heuristic_not_authenticity_or_cheating_probability",
            "record_count": record_count,
            "command_count": len(commands),
            "action_count": sum(source_action_counts.values()),
            "camera_update_count": camera_updates,
            "camera_rotation_count": camera_rotations,
            "voice_packet_count": len(voice_events),
            "telestrator_operation_count": sum(
                telestrator_operation_counts.values()
            ),
            "metadata_event_count": len(metadata_events),
            "sell_command_count": len(sell_events),
            "telemetry_sample_count": (
                telemetry["capture"]["sample_count"]
                if telemetry.get("present")
                else 0
            ),
            "crc_checkpoint_count": 0,
            "crc_disagreement_count": 0,
            "rtt_sample_count": len(rtt_points),
            "unknown_message_count": unknown_message_count,
            "decode_error_count": (
                len(command_decode_errors) + len(stream_decode_errors)
            ),
        },
        "integrity": {
            "header_valid": True,
            "footer_valid": True,
            "sentinel_valid": True,
            "footer_version": footer["footer_version"],
            "footer_size": footer["footer_size"],
            "reserved_nonzero_count": reserved_nonzero,
            "sentinel_reserved": footer["sentinel_reserved"],
            "footer_last_frame_matches_body": not footer_frame_mismatch,
            "frame_order_violation_count": len(frame_order_violations),
            "frame_order_violations": frame_order_violations[:50],
            "undeclared_channel_records": {
                str(channel): count
                for channel, count in sorted(undeclared_channel_counts.items())
            },
            "unknown_channel_records": {
                str(channel): count
                for channel, count in sorted(unknown_channel_counts.items())
            },
            "command_schema_anomaly_count": len(command_schema_anomalies),
            "command_schema_anomalies": command_schema_anomalies[:100],
            "post_elimination_action_count": len(post_elimination_commands),
            "post_elimination_actions": post_elimination_commands[:100],
            "camera_frame_order_violation_count": len(
                camera_frame_order_violations
            ),
            "metadata_padding_anomaly_count": len(metadata_padding_anomalies),
            "metadata_duplicate_channel_count": len(metadata_duplicate_channels),
            "command_decode_errors": command_decode_errors[:50],
            "stream_decode_errors": stream_decode_errors[:50],
            "orphan_sources": orphan_sources,
        },
        "cheat_analysis": {
            "assessment": assessment,
            "tone": assessment_tone,
            "finding_count": len(findings),
            "findings": findings,
            "scope": [
                {
                    "name": "Recorded CRC and structure",
                    "status": "active",
                    "detail": (
                        "CRC agreement, monotonic writer frames, footer/sentinel "
                        "finalization, header channel masks, source mapping, "
                        "known command schemas, and payload decoding."
                    ),
                },
                {
                    "name": "Replay stream forensics",
                    "status": "active",
                    "detail": (
                        "Camera state reconstruction, voice serial gaps, all "
                        "four telestrator operations, broadcast metadata, and "
                        "per-record payload fingerprints."
                    ),
                },
                {
                    "name": "Automation signals",
                    "status": "experimental",
                    "detail": "APM, same-frame bursts, and highly regular command timing. These are not proof.",
                },
                {
                    "name": "Experimental visibility correlation",
                    "status": (
                        "telemetry_available"
                        if visibility.get("captured")
                        else "sidecar_required"
                    ),
                    "detail": (
                        "Attack-object commands are compared with associated user-supplied visibility samples. These experimental correlations do not establish what a player could see or prove cheating."
                        if visibility.get("captured")
                        else "Target commands are decoded. Run the read-only telemetry collector during replay playback to capture per-player ObjectShroudStatus."
                    ),
                },
                {
                    "name": "Resource legality",
                    "status": (
                        "telemetry_available"
                        if telemetry.get("present")
                        else "experimental"
                    ),
                    "detail": (
                        "The sidecar provides user-supplied balance samples and deltas. No verified 1.02 payment/refund model or resource-legality verdict is applied."
                        if telemetry.get("present")
                        else "Replay requests do not establish spending or refunds. Optional catalog comparisons use unverified R22 reference values."
                    ),
                },
                {
                    "name": "Object lifecycle and ownership",
                    "status": (
                        "telemetry_available"
                        if telemetry.get("present")
                        else "sidecar_required"
                    ),
                    "detail": (
                        "The sidecar reports sampled object IDs and ownership changes. Sampling gaps and unauthenticated input limit lifecycle and sell-outcome interpretations."
                        if telemetry.get("present")
                        else "MSG_SELL requests are decoded. Replay commands alone do not establish creation, loss, capture or successful sale; experimental sidecars add sampled observations."
                    ),
                },
                {
                    "name": "Faction identity",
                    "status": "experimental",
                    "detail": "Compares fixed lobby faction choices with faction-specific production assets.",
                },
            ],
            "disclaimer": (
                "A replay can show deterministic violations and behavioral "
                "anomalies, but it cannot prove every form of cheating. "
                "No anomaly detected does not certify a player as legitimate."
            ),
        },
        "commands": {
            "items": commands,
            "type_counts": [
                {"name": name, "count": count}
                for name, count in command_counts.most_common()
            ],
            "category_counts": [
                {"name": name, "count": count}
                for name, count in category_counts.most_common()
            ],
        },
        "strategy": {
            "result": result,
            "eliminations": eliminations,
            "build_order": build_order,
            "sell_events": sell_events,
            "production_state": production_state,
            "asset_resolution": {
                "references": asset_reference_count,
                "resolved": resolved_asset_count,
                "coverage_percent": (
                    round(resolved_asset_count / asset_reference_count * 100, 1)
                    if asset_reference_count
                    else 0.0
                ),
                "catalog_size": len(asset_catalog),
            },
        },
        "timeline": timeline,
        "network": {
            "crc_checkpoints": [],
            "rtt_points": rtt_points[::rtt_sample_step],
            "rtt_points_total": len(rtt_points),
            "rtt_points_sampled": rtt_sample_step > 1,
        },
        "camera": {
            "path": camera_path[::camera_sample_step],
            "samples": camera_path,
            "path_points_total": len(camera_path),
            "path_sampled": camera_sample_step > 1,
            "streams": camera_streams,
            "frame_order_violations": camera_frame_order_violations[:100],
        },
        "auxiliary_streams": {
            "voice": {
                "packet_count": len(voice_events),
                "encoded_bytes": sum(
                    event["data_size"] for event in voice_events
                ),
                "missing_packet_count": voice_missing_packet_count,
                "duplicate_serial_count": voice_duplicate_packet_count,
                "timing_basis": (
                    "Packet frame and serial sequencing are exact. Millisecond "
                    "audio duration is not claimed because the runtime codec "
                    "framing configuration is not stored in the replay."
                ),
                "streams": voice_streams,
                "events": voice_events,
            },
            "telestrator": {
                "message_count": len(telestrator_events),
                "operation_count": sum(telestrator_operation_counts.values()),
                "operation_counts": dict(telestrator_operation_counts),
                "events": telestrator_events,
            },
            "metadata": {
                "event_count": len(metadata_events),
                "type_counts": dict(metadata_type_counts),
                "padding_anomaly_count": len(metadata_padding_anomalies),
                "duplicate_channel_event_count": len(
                    metadata_duplicate_channels
                ),
                "events": metadata_events,
            },
        },
        "forensics": {
            "record_count": len(forensic_records),
            "records": forensic_records,
            "fields": {
                "offset": "Absolute byte offset of the native 13-byte record envelope.",
                "frame": "Writer-enforced nondecreasing logic frame.",
                "payload_sha256": "Per-record payload fingerprint for evidence comparison.",
            },
        },
        "telemetry": telemetry,
        "channels": [
            {
                "id": channel,
                "name": CHANNEL_NAMES.get(channel, f"unknown_{channel}"),
                "record_count": count,
                "declared_in_header": (
                    channel in header["channels"] if channel < 0xFD else None
                ),
            }
            for channel, count in sorted(channel_counts.items())
        ],
        "metadata": {
            "schema": metadata.get("schema"),
            "tag": header.get("metadata_tag"),
            "size": header["metadata_size"],
            "sha256": header["metadata_sha256"],
            "engine_version_text": metadata.get("engine_version_text"),
            "match_configuration": metadata.get("source_28_ascii"),
            "match_configuration_decoded": match_configuration,
            "decoded": metadata,
        },
        "methodology": {
            "facts": (
                "Native formats and selected argument meanings were reviewed against KW 1.02. "
                "Unknown meanings remain explicit. Optional asset hints are unverified. "
                "Replay and synthetic fixture validation do not establish full game-state agreement."
            ),
            "action_definition": (
                "APM excludes CRC, RTT, stats/auth, player destruction, "
                "selection-group maintenance, and team-selection messages."
            ),
            "duration_definition": (
                f"Displayed time uses the recovered Kane's Wrath logic rate of "
                f"{SIMULATION_FPS} simulation frames per second."
            ),
            "production_definition": (
                "Queue and placement commands are requests, not proof of completion. "
                "Reference costs and timing remain unverified."
            ),
        },
    }

    report["desync"] = analyze_desync(report)
    report["summary"]["verified_command_count"] = sum(c["semantic_status"] == "verified_fields" for c in commands)
    report["summary"]["command_schema_issue_count"] = sum(bool(c["schema_issues"]) for c in commands)
    return report
