"""Validation and summarization for KW Replay Lab live telemetry sidecars."""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SUPPORTED_EXECUTABLE_SHA256 = {
    "8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5",
}
MAX_TELEMETRY_LINES = 250_000
MAX_OBJECTS_PER_SNAPSHOT = 25_000
MAX_VISIBILITY_TARGET_CHECKS = 25_000
OBJECT_SHROUD_STATUS_NAMES = {
    0: "invalid",
    1: "clear",
    2: "partial_clear",
    3: "fogged",
    4: "shrouded",
}


class TelemetryFormatError(ValueError):
    pass


def _load_lines(path: Path) -> tuple[list[dict[str, Any]], str]:
    with path.open("rb") as source:
        payload = source.read(64 * 1024 * 1024 + 1)
    if len(payload) > 64 * 1024 * 1024:
        raise TelemetryFormatError("Telemetry exceeds the 64 MiB file limit.")
    digest = hashlib.sha256(payload).hexdigest()
    records: list[dict[str, Any]] = []
    for line_number, raw_line in enumerate(payload.splitlines(), 1):
        if not raw_line.strip():
            continue
        if len(records) >= MAX_TELEMETRY_LINES:
            raise TelemetryFormatError(
                f"Telemetry exceeds the {MAX_TELEMETRY_LINES:,}-line safety limit."
            )
        try:
            record = json.loads(raw_line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TelemetryFormatError(
                f"Invalid telemetry JSON on line {line_number}: {exc}"
            ) from exc
        if not isinstance(record, dict):
            raise TelemetryFormatError(
                f"Telemetry line {line_number} is not a JSON object."
            )
        if record.get("type") == "snapshot":
            for field in ("frame", "sequence"):
                if type(record.get(field)) is not int or record[field] < 0:
                    raise TelemetryFormatError(f"Snapshot {field} must be a nonnegative integer.")
            for field in ("objects", "players"):
                rows = record.get(field, [])
                if not isinstance(rows, list) or len(rows) > MAX_OBJECTS_PER_SNAPSHOT or any(not isinstance(row, dict) for row in rows):
                    raise TelemetryFormatError(f"Snapshot {field} must be a bounded list of objects.")
                for row in rows:
                    if row.get("position") is not None and (not isinstance(row["position"], list) or len(row["position"]) != 3 or any(type(v) not in (int, float) or not math.isfinite(v) for v in row["position"])):
                        raise TelemetryFormatError("Object position must contain three finite numbers.")
                    visibility = row.get("visibility", {})
                    if not isinstance(visibility, dict) or any(not isinstance(v, dict) for v in visibility.values()):
                        raise TelemetryFormatError("Object visibility must map player IDs to status objects.")
            if not isinstance(record.get("warnings", []), list):
                raise TelemetryFormatError("Snapshot warnings must be a list.")
        records.append(record)
    if not records:
        raise TelemetryFormatError("Telemetry file is empty.")
    return records, digest


def _event_from_rows(
    event: str,
    frame: int,
    object_id: int,
    row: dict[str, Any],
) -> dict[str, Any]:
    result = {
        "event": event,
        "frame": frame,
        "object_id": object_id,
        "template_hash": row.get("template_hash"),
        "player_address": row.get("player_address"),
        "position": row.get("position"),
    }
    return result


def analyze_telemetry(
    path: Path,
    *,
    replay_sha256: str | None = None,
    replay_first_frame: int | None = None,
    replay_last_frame: int | None = None,
    target_checks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    records, digest = _load_lines(path)
    header = records[0]
    if header.get("type") != "kw_telemetry_header":
        raise TelemetryFormatError(
            "Telemetry must begin with a kw_telemetry_header record."
        )
    if header.get("schema_version") != 1:
        raise TelemetryFormatError(
            f"Unsupported telemetry schema {header.get('schema_version')!r}."
        )

    snapshots = [
        record for record in records[1:] if record.get("type") == "snapshot"
    ]
    waiting_count = sum(
        record.get("type") == "waiting" for record in records[1:]
    )
    if not snapshots:
        raise TelemetryFormatError(
            "Telemetry contains no snapshots. GameLogic was probably not "
            "initialized during capture."
        )

    integrity_issues: list[str] = []
    executable_hash = str(header.get("executable_sha256", "")).upper()
    verified_build = (
        header.get("verified_build") is True
        and executable_hash in SUPPORTED_EXECUTABLE_SHA256
    )
    if not verified_build:
        raise TelemetryFormatError("Telemetry must declare the exact supported KW 1.02 executable hash.")
    association_matches = bool(replay_sha256 and str(header.get("replay_sha256", "")).lower() == replay_sha256.lower())
    if header.get("replay_sha256") and replay_sha256 and not association_matches:
        raise TelemetryFormatError("Telemetry replay SHA-256 does not match this replay.")
    if not association_matches:
        integrity_issues.append("Sidecar has no matching replay hash; cross-file correlations are disabled.")
    if header.get("read_only") is not True:
        integrity_issues.append(
            "The telemetry header does not declare a read-only collector."
        )

    prior_sequence: int | None = None
    prior_frame: int | None = None
    previous_objects: dict[int, dict[str, Any]] | None = None
    lifecycle_events: list[dict[str, Any]] = []
    player_samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    object_peak = 0
    object_min: int | None = None
    template_counts: Counter[str] = Counter()
    owner_counts: Counter[str] = Counter()
    object_presence: dict[int, dict[str, Any]] = {}
    requested_target_checks = (target_checks or [])[:MAX_VISIBILITY_TARGET_CHECKS] if association_matches else []
    requested_object_ids = {
        int(check["target_object_id"])
        for check in requested_target_checks
        if isinstance(check.get("target_object_id"), int)
    }
    target_observations: dict[int, list[dict[str, Any]]] = defaultdict(list)
    visibility_status_counts: Counter[str] = Counter()
    visibility_evidence_counts: Counter[str] = Counter()
    visibility_snapshot_count = 0
    warning_count = 0

    for snapshot_index, snapshot in enumerate(snapshots):
        sequence = snapshot.get("sequence")
        frame = snapshot.get("frame")
        objects = snapshot.get("objects")
        players = snapshot.get("players", [])
        if not isinstance(sequence, int) or not isinstance(frame, int):
            raise TelemetryFormatError(
                f"Snapshot {snapshot_index} has an invalid sequence or frame."
            )
        if not isinstance(objects, list):
            raise TelemetryFormatError(
                f"Snapshot {snapshot_index} has no Object list."
            )
        if len(objects) > MAX_OBJECTS_PER_SNAPSHOT:
            raise TelemetryFormatError(
                f"Snapshot {snapshot_index} exceeds the Object safety limit."
            )
        if snapshot.get("object_count") != len(objects):
            integrity_issues.append(
                f"Snapshot {sequence} object_count does not match its Object array."
            )
        if prior_sequence is not None and sequence <= prior_sequence:
            integrity_issues.append("Snapshot sequences are not strictly increasing.")
        if prior_frame is not None and frame < prior_frame:
            integrity_issues.append("Logic frames move backwards in the sidecar.")
        prior_sequence = sequence
        prior_frame = frame

        current_objects: dict[int, dict[str, Any]] = {}
        for row in objects:
            if not isinstance(row, dict) or not isinstance(row.get("id"), int):
                raise TelemetryFormatError(
                    f"Snapshot {sequence} contains an invalid Object row."
                )
            object_id = row["id"]
            if object_id == 0:
                continue
            if object_id in current_objects:
                integrity_issues.append(
                    f"Snapshot {sequence} repeats ObjectID {object_id}."
                )
            current_objects[object_id] = row
            visibility = row.get("visibility")
            if isinstance(visibility, dict):
                if visibility:
                    visibility_snapshot_count += 1
                for value in visibility.values():
                    if not isinstance(value, dict):
                        continue
                    status = value.get("status")
                    evidence = value.get("evidence")
                    if isinstance(status, int):
                        visibility_status_counts[
                            OBJECT_SHROUD_STATUS_NAMES.get(
                                status, f"unknown_{status}"
                            )
                        ] += 1
                    if isinstance(evidence, str):
                        visibility_evidence_counts[evidence] += 1
                if object_id in requested_object_ids:
                    target_observations[object_id].append(
                        {
                            "frame": frame,
                            "visibility": visibility,
                            "template_hash": row.get("template_hash"),
                            "player_address": row.get("player_address"),
                        }
                    )
            presence = object_presence.setdefault(
                object_id,
                {
                    "object_id": object_id,
                    "first_frame": frame,
                    "last_frame": frame,
                    "template_hash": row.get("template_hash"),
                    "first_player_address": row.get("player_address"),
                    "last_player_address": row.get("player_address"),
                },
            )
            presence["last_frame"] = frame
            presence["last_player_address"] = row.get("player_address")
            if row.get("template_hash"):
                template_counts[str(row["template_hash"])] += 1
            if row.get("player_address"):
                owner_counts[str(row["player_address"])] += 1

        if previous_objects is not None:
            for object_id, row in current_objects.items():
                prior = previous_objects.get(object_id)
                if prior is None:
                    lifecycle_events.append(
                        _event_from_rows(
                            "object_created", frame, object_id, row
                        )
                    )
                elif prior.get("player_address") != row.get("player_address"):
                    lifecycle_events.append(
                        {
                            "event": "owner_changed",
                            "frame": frame,
                            "object_id": object_id,
                            "template_hash": row.get("template_hash"),
                            "old_player_address": prior.get("player_address"),
                            "new_player_address": row.get("player_address"),
                        }
                    )
            for object_id, row in previous_objects.items():
                if object_id not in current_objects:
                    lifecycle_events.append(
                        {
                            **_event_from_rows(
                                "object_removed", frame, object_id, row
                            ),
                            "position": row.get("position"),
                        }
                    )
        previous_objects = current_objects

        object_peak = max(object_peak, len(current_objects))
        object_min = (
            len(current_objects)
            if object_min is None
            else min(object_min, len(current_objects))
        )
        warning_count += len(snapshot.get("warnings") or [])

        if not isinstance(players, list):
            raise TelemetryFormatError(
                f"Snapshot {sequence} has an invalid Player list."
            )
        for player in players:
            if not isinstance(player, dict) or not player.get("address"):
                continue
            resource_value = player.get("resources")
            if resource_value is not None and (
                not isinstance(resource_value, int) or resource_value < 0
            ):
                integrity_issues.append(
                    f"Snapshot {sequence} has an invalid resource balance."
                )
                continue
            player_samples[str(player["address"])].append(
                {
                    "frame": frame,
                    "player_id": player.get("player_id"),
                    "resources": resource_value,
                    "primary_resources": player.get("primary_resources"),
                    "secondary_resources": player.get("secondary_resources"),
                }
            )

    first_frame = int(snapshots[0]["frame"])
    last_frame = int(snapshots[-1]["frame"])
    overlap_start = max(
        first_frame,
        replay_first_frame if replay_first_frame is not None else first_frame,
    )
    overlap_end = min(
        last_frame,
        replay_last_frame if replay_last_frame is not None else last_frame,
    )
    frame_overlap = max(0, overlap_end - overlap_start + 1)
    if (
        replay_last_frame is not None
        and replay_first_frame is not None
        and frame_overlap == 0
    ):
        integrity_issues.append(
            "Telemetry frames do not overlap the uploaded replay."
        )

    economy_players: list[dict[str, Any]] = []
    resource_timeline: list[dict[str, Any]] = []
    for address, samples in sorted(player_samples.items()):
        usable = [
            sample for sample in samples if isinstance(sample.get("resources"), int)
        ]
        if not usable:
            continue
        positive_delta = 0
        negative_delta = 0
        for prior, current in zip(usable, usable[1:]):
            delta = current["resources"] - prior["resources"]
            if delta > 0:
                positive_delta += delta
            elif delta < 0:
                negative_delta += -delta
        economy_players.append(
            {
                "player_address": address,
                "player_id": usable[-1].get("player_id"),
                "first_resources": usable[0]["resources"],
                "last_resources": usable[-1]["resources"],
                "minimum_resources": min(
                    sample["resources"] for sample in usable
                ),
                "maximum_resources": max(
                    sample["resources"] for sample in usable
                ),
                "observed_positive_delta": positive_delta,
                "observed_negative_delta": negative_delta,
                "sample_count": len(usable),
            }
        )
        stride = max(1, len(usable) // 600)
        resource_timeline.extend(
            {
                "frame": sample["frame"],
                "player_address": address,
                "player_id": sample.get("player_id"),
                "resources": sample["resources"],
            }
            for sample in usable[::stride]
        )

    event_counts = Counter(
        event["event"] for event in lifecycle_events
    )
    sample_interval_ms = header.get("sample_interval_ms")
    interval_frames = (
        max(1, round(float(sample_interval_ms) * 15 / 1000))
        if isinstance(sample_interval_ms, (int, float))
        and sample_interval_ms > 0
        else 3
    )
    maximum_gap = max(2, interval_frames * 2)
    visibility_checks: list[dict[str, Any]] = []
    for check in requested_target_checks:
        object_id = check.get("target_object_id")
        player_index = check.get("player_index")
        command_frame = check.get("frame")
        result = {**check, "observed": False}
        if not all(
            isinstance(value, int)
            for value in (object_id, player_index, command_frame)
        ):
            visibility_checks.append(result)
            continue
        observations = target_observations.get(int(object_id), [])
        usable: list[dict[str, Any]] = []
        for observation in observations:
            value = observation["visibility"].get(str(player_index))
            if not isinstance(value, dict) or not isinstance(
                value.get("status"), int
            ):
                continue
            usable.append({**observation, **value})
        if not usable:
            visibility_checks.append(result)
            continue

        prior = max(
            (
                observation
                for observation in usable
                if observation["frame"] <= command_frame
            ),
            key=lambda observation: observation["frame"],
            default=None,
        )
        following = min(
            (
                observation
                for observation in usable
                if observation["frame"] >= command_frame
            ),
            key=lambda observation: observation["frame"],
            default=None,
        )
        nearest = min(
            usable,
            key=lambda observation: abs(
                observation["frame"] - command_frame
            ),
        )
        prior_gap = (
            command_frame - prior["frame"] if prior is not None else None
        )
        following_gap = (
            following["frame"] - command_frame
            if following is not None
            else None
        )
        bracketed = bool(
            prior is not None
            and following is not None
            and prior_gap is not None
            and following_gap is not None
            and prior_gap <= maximum_gap
            and following_gap <= maximum_gap
            and prior["status"] == following["status"]
        )
        selected = prior if bracketed and prior is not None else nearest
        result.update(
            {
                "observed": abs(selected["frame"] - command_frame)
                <= maximum_gap,
                "status": selected["status"],
                "status_name": OBJECT_SHROUD_STATUS_NAMES.get(
                    selected["status"], "unknown"
                ),
                "evidence": selected.get("evidence"),
                "sample_frame": selected["frame"],
                "frame_gap": abs(selected["frame"] - command_frame),
                "bracketed": bracketed,
                "prior_frame": prior["frame"] if prior else None,
                "following_frame": following["frame"] if following else None,
                "maximum_gap_frames": maximum_gap,
                "template_hash": selected.get("template_hash"),
                "target_player_address": selected.get("player_address"),
            }
        )
        result["high_confidence_shrouded"] = bool(
            verified_build and association_matches and not integrity_issues
            and result["observed"]
            and result["status"] == 4
            and result["evidence"] == "cached"
            and bracketed
        )
        visibility_checks.append(result)

    observed_visibility_checks = [
        check for check in visibility_checks if check.get("observed")
    ]
    shrouded_visibility_checks = [
        check
        for check in observed_visibility_checks
        if check.get("status") == 4
    ]
    high_confidence_shrouded_checks = [
        check
        for check in shrouded_visibility_checks
        if check.get("high_confidence_shrouded")
    ]
    return {
        "present": True,
        "file": {
            "name": path.name,
            "size": path.stat().st_size,
            "sha256": digest,
        },
        "capture": {
            "schema_version": header["schema_version"],
            "read_only": header.get("read_only") is True,
            "verified_build": verified_build,
            "replay_hash_matches": association_matches,
            "evidence_status": "experimental_unauthenticated_sidecar",
            "executable_sha256": executable_hash,
            "process_id": header.get("process_id"),
            "sample_interval_ms": header.get("sample_interval_ms"),
            "sample_count": len(snapshots),
            "waiting_record_count": waiting_count,
            "first_frame": first_frame,
            "last_frame": last_frame,
            "frame_overlap": frame_overlap,
            "warning_count": warning_count,
            "integrity_issues": list(dict.fromkeys(integrity_issues)),
        },
        "objects": {
            "minimum_live": object_min or 0,
            "peak_live": object_peak,
            "event_counts": dict(event_counts),
            "lifecycle_events": lifecycle_events[:10_000],
            "lifecycle_events_truncated": len(lifecycle_events) > 10_000,
            "observed_template_count": len(template_counts),
            "observed_owner_count": len(owner_counts),
            "presence": list(object_presence.values())[:25_000],
            "presence_truncated": len(object_presence) > 25_000,
        },
        "economy": {
            "players": economy_players,
            "timeline": sorted(
                resource_timeline,
                key=lambda sample: (
                    sample["frame"],
                    sample["player_address"],
                ),
            )[:5_000],
            "timeline_truncated": len(resource_timeline) > 5_000,
        },
        "visibility": {
            "captured": visibility_snapshot_count > 0,
            "object_player_observation_count": sum(
                visibility_status_counts.values()
            ),
            "status_counts": dict(visibility_status_counts),
            "evidence_counts": dict(visibility_evidence_counts),
            "target_check_count": len(visibility_checks),
            "observed_target_count": len(observed_visibility_checks),
            "shrouded_target_count": len(shrouded_visibility_checks),
            "high_confidence_shrouded_target_count": len(
                high_confidence_shrouded_checks
            ),
            "maximum_sample_gap_frames": maximum_gap,
            "target_checks": visibility_checks,
            "target_checks_truncated": len(target_checks or [])
            > MAX_VISIBILITY_TARGET_CHECKS,
        },
    }
