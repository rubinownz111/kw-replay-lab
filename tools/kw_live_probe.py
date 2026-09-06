#!/usr/bin/env python3
"""Read-only Kane's Wrath 1.02 live telemetry collector.

The collector never injects code or writes process memory. It opens the game
with PROCESS_VM_READ, walks the verified GameLogic Object list, and writes
newline-delimited JSON snapshots that KW Replay Lab can merge with a replay.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import struct
import sys
import time
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO


IDA_IMAGE_BASE = 0x00400000


@dataclass(frozen=True)
class BuildProfile:
    key: str
    label: str
    executable_sha256: str
    game_logic_global_rva: int
    shroud_manager_global_rva: int
    recorder_global_rva: int


BUILD_PROFILES = {
    "8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5": (
        BuildProfile(
            key="kw_1_2",
            label="Kane's Wrath exact 1.02",
            executable_sha256=(
                "8225BB6CE15F7D34467E7FD55ED1AD60706E62E9470A877AB3DB6AA47ADDFDF5"
            ),
            game_logic_global_rva=0x00BE1008 - IDA_IMAGE_BASE,
            shroud_manager_global_rva=0x00BE4F88 - IDA_IMAGE_BASE,
            recorder_global_rva=0x00BE4FBC - IDA_IMAGE_BASE,
        )
    ),
}

PROCESS_VM_READ = 0x0010
PROCESS_QUERY_INFORMATION = 0x0400
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TH32CS_SNAPPROCESS = 0x00000002
TH32CS_SNAPMODULE = 0x00000008
TH32CS_SNAPMODULE32 = 0x00000010
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
MAX_PATH = 260
ERROR_NO_MORE_FILES = 18

GAME_LOGIC_FRAME = 0x38
GAME_LOGIC_FIRST_OBJECT = 0xB8
GAME_LOGIC_SHROUD_ENABLED = 0x128
OBJECT_TEMPLATE = 0x04
OBJECT_POSITION = 0x38
OBJECT_NEXT = 0x78
OBJECT_STATUS = 0x7C
OBJECT_SHROUD_DATA = 0x94
OBJECT_ID = 0xEC
OBJECT_TEAM = 0x348
OBJECT_CREATION_FRAME = 0x43C
THING_TEMPLATE_NAME_KEY = 0x04
THING_TEMPLATE_BEHAVIOR_FLAGS_1 = 0x128
THING_TEMPLATE_ALWAYS_VISIBLE = 0x00800000
TEAM_PROTOTYPE = 0x04
TEAM_PROTOTYPE_PLAYER = 0x08
PLAYER_RESOURCES = 0x60
PLAYER_RESOURCES_PRIMARY = 0x04
PLAYER_RESOURCES_PRIMARY_CAP = 0x08
PLAYER_RESOURCES_PLAYER_ID = 0x0C
PLAYER_RESOURCES_SECONDARY = 0x10
PLAYER_RESOURCES_SECONDARY_CAP = 0x14
SHROUD_MANAGER_IMPLEMENTATION = 0x1C
SHROUD_OBJECT_CELL_INTERSECTIONS = 0x1C
SHROUD_OBJECT_CELL_INTERSECTION_COUNT = 0x20
SHROUD_OBJECT_STATUS_BY_PLAYER = 0x24
SHROUD_OBJECT_PARTIAL_OVERRIDE = 0x151
SHROUD_CELL_SIZE = 0xDC
SHROUD_CELL_FORCE_SHROUDED = 0xD8
SHROUD_CELL_PLAYER_STATE = 0x04
SHROUD_CELL_PLAYER_STRIDE = 0x08
MAX_SHROUD_PLAYERS = 26
MAX_OBJECT_SHROUD_CELLS = 4096
REPLAY_CLASS_MODE = 0x18

OBJECT_SHROUD_STATUS_NAMES = {
    0: "invalid",
    1: "clear",
    2: "partial_clear",
    3: "fogged",
    4: "shrouded",
}
KERNEL32 = (
    ctypes.WinDLL("kernel32", use_last_error=True)
    if sys.platform == "win32"
    else None
)


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * MAX_PATH),
    ]


class MODULEENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("th32ModuleID", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("GlblcntUsage", wintypes.DWORD),
        ("ProccntUsage", wintypes.DWORD),
        ("modBaseAddr", ctypes.POINTER(ctypes.c_byte)),
        ("modBaseSize", wintypes.DWORD),
        ("hModule", wintypes.HMODULE),
        ("szModule", wintypes.WCHAR * 256),
        ("szExePath", wintypes.WCHAR * MAX_PATH),
    ]


@dataclass(frozen=True)
class ModuleInfo:
    base: int
    size: int
    path: Path
    name: str


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent_pid: int
    name: str


class ProbeError(RuntimeError):
    pass


class ProcessMemory:
    """Small ReadProcessMemory wrapper fixed to KW's 32-bit pointer width."""

    def __init__(self, process_handle: int):
        self.process_handle = process_handle

    def read(self, address: int, size: int) -> bytes:
        if not address or address < 0x10000:
            raise ProbeError(f"invalid target address 0x{address:X}")
        buffer = ctypes.create_string_buffer(size)
        transferred = ctypes.c_size_t()
        read_process_memory = _kernel_function(
            "ReadProcessMemory",
            [
                wintypes.HANDLE,
                wintypes.LPCVOID,
                wintypes.LPVOID,
                ctypes.c_size_t,
                ctypes.POINTER(ctypes.c_size_t),
            ],
            wintypes.BOOL,
        )
        if not read_process_memory(
            self.process_handle,
            ctypes.c_void_p(address),
            buffer,
            size,
            ctypes.byref(transferred),
        ):
            error = ctypes.get_last_error()
            raise ProbeError(
                f"ReadProcessMemory(0x{address:08X}, {size}) failed "
                f"with Win32 error {error}"
            )
        if transferred.value != size:
            raise ProbeError(
                f"short process read at 0x{address:08X}: "
                f"{transferred.value}/{size} bytes"
            )
        return buffer.raw

    def u32(self, address: int) -> int:
        return struct.unpack("<I", self.read(address, 4))[0]

    def i32(self, address: int) -> int:
        return struct.unpack("<i", self.read(address, 4))[0]

    def f32(self, address: int) -> float | None:
        value = struct.unpack("<f", self.read(address, 4))[0]
        return round(value, 4) if math.isfinite(value) else None


def _kernel_function(name: str, argtypes: list[Any], restype: Any) -> Any:
    if KERNEL32 is None:
        raise ProbeError("The live collector requires Windows.")
    function = getattr(KERNEL32, name)
    function.argtypes = argtypes
    function.restype = restype
    return function


def _snapshot(flags: int, pid: int = 0) -> int:
    create_snapshot = _kernel_function(
        "CreateToolhelp32Snapshot",
        [wintypes.DWORD, wintypes.DWORD],
        wintypes.HANDLE,
    )
    handle = create_snapshot(flags, pid)
    if handle == INVALID_HANDLE_VALUE:
        raise ProbeError(
            f"CreateToolhelp32Snapshot failed with Win32 error "
            f"{ctypes.get_last_error()}"
        )
    return handle


def _close_handle(handle: int) -> None:
    if handle and KERNEL32 is not None:
        close_handle = _kernel_function(
            "CloseHandle", [wintypes.HANDLE], wintypes.BOOL
        )
        close_handle(handle)


def list_processes(requested_name: str | None = None) -> list[ProcessInfo]:
    target = requested_name.lower() if requested_name else None
    target_stem = Path(target).stem if target else None
    handle = _snapshot(TH32CS_SNAPPROCESS)
    try:
        first = _kernel_function(
            "Process32FirstW",
            [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)],
            wintypes.BOOL,
        )
        following = _kernel_function(
            "Process32NextW",
            [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)],
            wintypes.BOOL,
        )
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not first(handle, ctypes.byref(entry)):
            raise ProbeError("Could not enumerate Windows processes.")
        rows: list[ProcessInfo] = []
        while True:
            current = entry.szExeFile
            lowered = current.lower()
            if (
                target is None
                or lowered == target
                or Path(lowered).stem == target_stem
            ):
                rows.append(
                    ProcessInfo(
                        pid=int(entry.th32ProcessID),
                        parent_pid=int(entry.th32ParentProcessID),
                        name=current,
                    )
                )
            if not following(handle, ctypes.byref(entry)):
                break
        return rows
    finally:
        _close_handle(handle)


def find_process_id(requested_name: str) -> int:
    target = requested_name.lower()
    matches = [row.pid for row in list_processes(target)]
    if not matches:
        raise ProbeError(
            f"{requested_name} is not running. Start Kane's Wrath, begin "
            "replay playback, then run the collector again."
        )
    if len(matches) > 1:
        raise ProbeError(
            f"Multiple {requested_name} processes are running "
            f"({', '.join(map(str, matches))}); pass --pid."
        )
    return matches[0]


def find_main_module(pid: int) -> ModuleInfo:
    handle = _snapshot(TH32CS_SNAPMODULE | TH32CS_SNAPMODULE32, pid)
    try:
        first = _kernel_function(
            "Module32FirstW",
            [wintypes.HANDLE, ctypes.POINTER(MODULEENTRY32W)],
            wintypes.BOOL,
        )
        entry = MODULEENTRY32W()
        entry.dwSize = ctypes.sizeof(entry)
        if not first(handle, ctypes.byref(entry)):
            raise ProbeError(
                f"Could not enumerate modules for process {pid}; Win32 error "
                f"{ctypes.get_last_error()}."
            )
        return ModuleInfo(
            base=int(
                ctypes.cast(entry.modBaseAddr, ctypes.c_void_p).value or 0
            ),
            size=int(entry.modBaseSize),
            path=Path(entry.szExePath),
            name=entry.szModule,
        )
    finally:
        _close_handle(handle)


def open_process(pid: int) -> int:
    open_process_function = _kernel_function(
        "OpenProcess",
        [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD],
        wintypes.HANDLE,
    )
    handle = open_process_function(
        PROCESS_VM_READ
        | PROCESS_QUERY_INFORMATION
        | PROCESS_QUERY_LIMITED_INFORMATION,
        False,
        pid,
    )
    if not handle:
        raise ProbeError(
            f"OpenProcess({pid}) failed with Win32 error "
            f"{ctypes.get_last_error()}."
        )
    return handle


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest().upper()


def resolve_build_profile(
    executable_hash: str, *, allow_unknown_build: bool = False
) -> BuildProfile:
    normalized = executable_hash.upper()
    profile = BUILD_PROFILES.get(normalized)
    if profile is not None:
        return profile
    supported = ", ".join(
        f"{profile.label} ({digest[:12]}…)"
        for digest, profile in BUILD_PROFILES.items()
    )
    raise ProbeError(
        "The running executable is not a verified supported Kane's Wrath "
        f"build. Got {normalized}; supported builds: {supported}. "
        "No fallback offsets are supported."
    )


def _hex_pointer(value: int) -> str | None:
    return f"0x{value:08X}" if value else None


def _read_owner(memory: ProcessMemory, team: int) -> tuple[int, int]:
    if not team:
        return 0, 0
    prototype = memory.u32(team + TEAM_PROTOTYPE)
    if not prototype:
        return 0, 0
    return memory.u32(prototype + TEAM_PROTOTYPE_PLAYER), prototype


def _read_player(
    memory: ProcessMemory, player: int, player_cache: dict[int, dict[str, Any]]
) -> None:
    if not player or player in player_cache:
        return
    resources = memory.u32(player + PLAYER_RESOURCES)
    row: dict[str, Any] = {
        "address": _hex_pointer(player),
        "resources_address": _hex_pointer(resources),
    }
    if resources:
        primary = memory.u32(resources + PLAYER_RESOURCES_PRIMARY)
        secondary = memory.u32(resources + PLAYER_RESOURCES_SECONDARY)
        row.update(
            {
                "player_id": memory.i32(
                    resources + PLAYER_RESOURCES_PLAYER_ID
                ),
                "resources": primary + secondary,
                "primary_resources": primary,
                "secondary_resources": secondary,
                "primary_cap": memory.u32(
                    resources + PLAYER_RESOURCES_PRIMARY_CAP
                ),
                "secondary_cap": memory.u32(
                    resources + PLAYER_RESOURCES_SECONDARY_CAP
                ),
            }
        )
    player_cache[player] = row


def _read_geometric_shroud_statuses(
    memory: ProcessMemory,
    shroud_data: int,
    player_indices: list[int],
) -> dict[int, int]:
    """Reproduce the non-callback portion of ShroudObjectData's status fold."""
    intersection_array = memory.u32(
        shroud_data + SHROUD_OBJECT_CELL_INTERSECTIONS
    )
    intersection_count = memory.u32(
        shroud_data + SHROUD_OBJECT_CELL_INTERSECTION_COUNT
    )
    if (
        not intersection_array
        or intersection_count == 0
        or intersection_count > MAX_OBJECT_SHROUD_CELLS
    ):
        return {player_index: 4 for player_index in player_indices}

    records = memory.read(intersection_array, intersection_count * 16)
    cell_addresses = {
        struct.unpack_from("<I", records, index * 16)[0]
        for index in range(intersection_count)
    }
    cells = [
        memory.read(address, SHROUD_CELL_SIZE)
        for address in cell_addresses
        if address
    ]
    if not cells:
        return {player_index: 4 for player_index in player_indices}

    partial_override = bool(
        memory.read(shroud_data + SHROUD_OBJECT_PARTIAL_OVERRIDE, 1)[0]
    )
    results: dict[int, int] = {}
    for player_index in player_indices:
        cell_statuses: list[int] = []
        for cell in cells:
            if cell[SHROUD_CELL_FORCE_SHROUDED]:
                cell_statuses.append(3)
                continue
            value = struct.unpack_from(
                "<h",
                cell,
                SHROUD_CELL_PLAYER_STATE
                + player_index * SHROUD_CELL_PLAYER_STRIDE,
            )[0]
            # KW's native cell fold uses 1=clear, 0=fogged, 3=shrouded.
            cell_statuses.append(1 if value not in (0, -1) else 3 if value == -1 else 0)

        shrouded_count = cell_statuses.count(3)
        fogged_count = cell_statuses.count(0)
        count = len(cell_statuses)
        if shrouded_count == count:
            status = 4
        elif fogged_count + shrouded_count == count:
            status = 3
        elif shrouded_count or fogged_count or partial_override:
            status = 2
        else:
            status = 1
        results[player_index] = status
    return results


def _read_object_visibility(
    memory: ProcessMemory,
    row: dict[str, Any],
    player_indices: list[int],
    shroud_enabled: bool,
) -> dict[str, dict[str, Any]]:
    template_flags = row.pop("_template_flags", 0)
    shroud_data = row.pop("_shroud_data", 0)
    if not player_indices:
        return {}
    if (
        not shroud_enabled
        or template_flags & THING_TEMPLATE_ALWAYS_VISIBLE
    ):
        return {
            str(player_index): {
                "status": 1,
                "name": "clear",
                "evidence": "engine_bypass",
            }
            for player_index in player_indices
        }

    if not shroud_data:
        return {
            str(player_index): {
                "status": 1,
                "name": "clear",
                "evidence": "no_shroud_object_data",
            }
            for player_index in player_indices
        }

    raw_statuses = memory.read(
        shroud_data + SHROUD_OBJECT_STATUS_BY_PLAYER,
        MAX_SHROUD_PLAYERS * 4,
    )
    cached = {
        player_index: struct.unpack_from(
            "<I", raw_statuses, player_index * 4
        )[0]
        for player_index in player_indices
    }
    invalid_indices = [
        player_index
        for player_index, status in cached.items()
        if status not in OBJECT_SHROUD_STATUS_NAMES or status == 0
    ]
    geometric = (
        _read_geometric_shroud_statuses(
            memory, shroud_data, invalid_indices
        )
        if invalid_indices
        else {}
    )
    visibility: dict[str, dict[str, Any]] = {}
    for player_index in player_indices:
        raw_status = cached[player_index]
        if raw_status in OBJECT_SHROUD_STATUS_NAMES and raw_status != 0:
            status = raw_status
            evidence = "cached"
        else:
            status = geometric.get(player_index, 0)
            evidence = "geometric" if status else "unavailable"
        visibility[str(player_index)] = {
            "status": status,
            "name": OBJECT_SHROUD_STATUS_NAMES.get(status, "unknown"),
            "evidence": evidence,
        }
    return visibility


def capture_snapshot(
    memory: ProcessMemory,
    game_logic: int,
    shroud_manager: int,
    sequence: int,
    max_objects: int,
) -> dict[str, Any]:
    frame = memory.u32(game_logic + GAME_LOGIC_FRAME)
    shroud_enabled = bool(
        memory.u32(game_logic + GAME_LOGIC_SHROUD_ENABLED)
        and shroud_manager
        and memory.u32(shroud_manager + SHROUD_MANAGER_IMPLEMENTATION)
    )
    object_address = memory.u32(game_logic + GAME_LOGIC_FIRST_OBJECT)
    objects: list[dict[str, Any]] = []
    players: dict[int, dict[str, Any]] = {}
    visited: set[int] = set()
    warnings: list[str] = []

    while object_address:
        if object_address in visited:
            warnings.append(
                f"Object list cycle detected at {_hex_pointer(object_address)}."
            )
            break
        if len(objects) >= max_objects:
            warnings.append(
                f"Object list exceeded the safety limit of {max_objects}."
            )
            break
        visited.add(object_address)

        template = memory.u32(object_address + OBJECT_TEMPLATE)
        template_flags = (
            memory.u32(template + THING_TEMPLATE_BEHAVIOR_FLAGS_1)
            if template
            else 0
        )
        team = memory.u32(object_address + OBJECT_TEAM)
        player, team_prototype = _read_owner(memory, team)
        _read_player(memory, player, players)
        status = [
            memory.u32(object_address + OBJECT_STATUS + index * 4)
            for index in range(6)
        ]
        objects.append(
            {
                "id": memory.u32(object_address + OBJECT_ID),
                "address": _hex_pointer(object_address),
                "template_address": _hex_pointer(template),
                "template_hash": (
                    f"{memory.u32(template + THING_TEMPLATE_NAME_KEY):08X}"
                    if template
                    else None
                ),
                "team_address": _hex_pointer(team),
                "team_prototype_address": _hex_pointer(team_prototype),
                "player_address": _hex_pointer(player),
                "position": [
                    memory.f32(object_address + OBJECT_POSITION + index * 4)
                    for index in range(3)
                ],
                "creation_frame": memory.u32(
                    object_address + OBJECT_CREATION_FRAME
                ),
                "status_words": [f"0x{word:08X}" for word in status],
                "destroyed": bool(status[0] & 1),
                "_shroud_data": memory.u32(
                    object_address + OBJECT_SHROUD_DATA
                ),
                "_template_flags": template_flags,
            }
        )
        object_address = memory.u32(object_address + OBJECT_NEXT)

    player_indices = sorted(
        {
            int(player["player_id"])
            for player in players.values()
            if isinstance(player.get("player_id"), int)
            and 0 <= int(player["player_id"]) < MAX_SHROUD_PLAYERS
        }
    )
    for row in objects:
        row["visibility"] = _read_object_visibility(
            memory,
            row,
            player_indices,
            shroud_enabled,
        )

    return {
        "type": "snapshot",
        "sequence": sequence,
        "captured_utc": datetime.now(timezone.utc).isoformat(),
        "frame": frame,
        "object_count": len(objects),
        "objects": objects,
        "players": list(players.values()),
        "shroud": {
            "manager_address": _hex_pointer(shroud_manager),
            "enabled": shroud_enabled,
            "player_indices": player_indices,
        },
        "warnings": warnings,
    }


def derive_events(
    previous: dict[int, dict[str, Any]] | None,
    current_snapshot: dict[str, Any],
) -> list[dict[str, Any]]:
    current = {
        int(row["id"]): row
        for row in current_snapshot["objects"]
        if int(row["id"]) != 0
    }
    if previous is None:
        return []
    events: list[dict[str, Any]] = []
    frame = current_snapshot["frame"]
    for object_id, row in current.items():
        prior = previous.get(object_id)
        if prior is None:
            events.append(
                {
                    "event": "object_created",
                    "frame": frame,
                    "object_id": object_id,
                    "template_hash": row["template_hash"],
                    "player_address": row["player_address"],
                    "position": row["position"],
                }
            )
        elif prior["player_address"] != row["player_address"]:
            events.append(
                {
                    "event": "owner_changed",
                    "frame": frame,
                    "object_id": object_id,
                    "template_hash": row["template_hash"],
                    "old_player_address": prior["player_address"],
                    "new_player_address": row["player_address"],
                }
            )
    for object_id, row in previous.items():
        if object_id not in current:
            events.append(
                {
                    "event": "object_removed",
                    "frame": frame,
                    "object_id": object_id,
                    "template_hash": row["template_hash"],
                    "player_address": row["player_address"],
                    "last_position": row["position"],
                }
            )
    return events


def _write_json_line(target: BinaryIO, payload: dict[str, Any]) -> None:
    target.write(
        (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        .encode("utf-8")
    )
    target.flush()


def _default_output() -> Path:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path.cwd() / f"kw-telemetry-{timestamp}.jsonl"


def collect(args: argparse.Namespace) -> Path:
    if sys.platform != "win32":
        raise ProbeError("The live collector requires Windows.")
    pid = args.pid or find_process_id(args.process_name)
    module = find_main_module(pid)
    executable_hash = sha256_file(module.path)
    build = resolve_build_profile(
        executable_hash
    )

    handle = open_process(pid)
    output = args.output or _default_output()
    output.parent.mkdir(parents=True, exist_ok=True)
    interval_seconds = max(args.interval_ms, 15) / 1000.0
    deadline = (
        time.monotonic() + args.duration
        if args.duration is not None
        else None
    )
    try:
        memory = ProcessMemory(handle)
        game_logic_pointer_address = (
            module.base + build.game_logic_global_rva
        )
        shroud_manager_pointer_address = (
            module.base + build.shroud_manager_global_rva
        )
        recorder_pointer_address = (
            module.base + build.recorder_global_rva
        )
        with output.open("wb") as target:
            _write_json_line(
                target,
                {
                    "type": "kw_telemetry_header",
                    "schema_version": 1,
                    "created_utc": datetime.now(timezone.utc).isoformat(),
                    "read_only": True,
                    "process_id": pid,
                    "module_name": module.name,
                    "module_path": str(module.path),
                    "module_base": _hex_pointer(module.base),
                    "module_size": module.size,
                    "build_key": build.key,
                    "build_label": build.label,
                    "executable_sha256": executable_hash,
                    "supported_executable_sha256": sorted(BUILD_PROFILES),
                    "verified_build": executable_hash in BUILD_PROFILES,
                    "replay_sha256": sha256_file(args.replay) if args.replay else None,
                    "evidence_status": "experimental_not_live_validated",
                    "ida_image_base": _hex_pointer(IDA_IMAGE_BASE),
                    "sample_interval_ms": args.interval_ms,
                    "offsets": {
                        "game_logic_global_rva": (
                            f"0x{build.game_logic_global_rva:08X}"
                        ),
                        "shroud_manager_global_rva": (
                            f"0x{build.shroud_manager_global_rva:08X}"
                        ),
                        "recorder_global_rva": (
                            f"0x{build.recorder_global_rva:08X}"
                        ),
                        "game_logic_frame": f"0x{GAME_LOGIC_FRAME:X}",
                        "game_logic_first_object": (
                            f"0x{GAME_LOGIC_FIRST_OBJECT:X}"
                        ),
                        "object_next": f"0x{OBJECT_NEXT:X}",
                        "object_id": f"0x{OBJECT_ID:X}",
                        "object_team": f"0x{OBJECT_TEAM:X}",
                    },
                },
            )
            previous: dict[int, dict[str, Any]] | None = None
            sequence = 0
            try:
                while True:
                    game_logic = memory.u32(game_logic_pointer_address)
                    shroud_manager = memory.u32(
                        shroud_manager_pointer_address
                    )
                    recorder = memory.u32(recorder_pointer_address)
                    recorder_mode = (
                        memory.u32(recorder + REPLAY_CLASS_MODE)
                        if recorder
                        else None
                    )
                    if not game_logic:
                        _write_json_line(
                            target,
                            {
                                "type": "waiting",
                                "sequence": sequence,
                                "captured_utc": datetime.now(
                                    timezone.utc
                                ).isoformat(),
                                "reason": (
                                    "GameLogic is not initialized; start or "
                                    "play the replay."
                                ),
                                "recorder_mode": recorder_mode,
                            },
                        )
                    else:
                        snapshot = capture_snapshot(
                            memory,
                            game_logic,
                            shroud_manager,
                            sequence,
                            args.max_objects,
                        )
                        snapshot["events"] = derive_events(
                            previous, snapshot
                        )
                        snapshot["recorder"] = {
                            "address": _hex_pointer(recorder),
                            "mode": recorder_mode,
                            "mode_name": {
                                0: "record",
                                1: "playback",
                                2: "inactive",
                            }.get(recorder_mode, "unknown"),
                        }
                        _write_json_line(target, snapshot)
                        previous = {
                            int(row["id"]): row
                            for row in snapshot["objects"]
                            if int(row["id"]) != 0
                        }
                    sequence += 1
                    if args.once:
                        break
                    if (
                        deadline is not None
                        and time.monotonic() >= deadline
                    ):
                        break
                    time.sleep(interval_seconds)
            except KeyboardInterrupt:
                pass
    finally:
        _close_handle(handle)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--pid", type=int, help="Attach to this process ID.")
    target.add_argument(
        "--process-name",
        default="cnc3ep1.dat",
        help="Process executable name when --pid is omitted.",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Output .jsonl path (default: timestamped file in this folder).",
    )
    parser.add_argument(
        "--interval-ms",
        type=int,
        default=200,
        help="Snapshot interval in milliseconds (default: 200).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        help="Stop after this many seconds; otherwise run until Ctrl+C.",
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Write one snapshot and exit.",
    )
    parser.add_argument(
        "--max-objects",
        type=int,
        default=20000,
        help="Safety limit for one Object-list walk (default: 20000).",
    )
    parser.add_argument("--replay", type=Path, help="Replay being played; binds its hash to the sidecar.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.interval_ms < 15:
        raise SystemExit("--interval-ms must be at least 15")
    if args.duration is not None and args.duration <= 0:
        raise SystemExit("--duration must be positive")
    if args.max_objects <= 0:
        raise SystemExit("--max-objects must be positive")
    try:
        output = collect(args)
    except (OSError, ProbeError) as exc:
        print(f"Telemetry collector error: {exc}", file=sys.stderr)
        return 1
    print(f"Saved read-only telemetry to {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
