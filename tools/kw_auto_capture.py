#!/usr/bin/env python3
"""Launch a staged KW replay and collect read-only runtime telemetry."""

from __future__ import annotations

import ctypes
import json
import shutil
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.kw_live_probe import (
    BUILD_PROFILES,
    GAME_LOGIC_FRAME,
    IDA_IMAGE_BASE,
    REPLAY_CLASS_MODE,
    ProbeError,
    ProcessMemory,
    _close_handle,
    _hex_pointer,
    _write_json_line,
    capture_snapshot,
    derive_events,
    find_main_module,
    list_processes,
    open_process,
    resolve_build_profile,
    sha256_file,
)
from tools.kwreplay_inspect import inspect


ProgressCallback = Callable[[str, str, int | None], None]
DEFAULT_INSTALL_ROOT = Path(
    r"C:\Program Files (x86)\Steam\steamapps\common"
    r"\Command and Conquer 3 - Kane's Wrath"
)
STAGED_PREFIX = "KW Replay Lab "
WM_CLOSE = 0x0010
WM_KEYDOWN = 0x0100
WM_KEYUP = 0x0101
VK_OEM_PERIOD = 0xBE
PROCESS_TERMINATE = 0x0001
SYNCHRONIZE = 0x00100000


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CaptureResult:
    telemetry_path: Path
    build_key: str
    build_label: str
    launcher_pid: int
    engine_pid: int
    completion_reason: str
    elapsed_seconds: float
    fast_forward_requested: bool
    fast_forward_sent: bool
    fast_forward_verified: bool
    observed_frames_per_second: float | None
    final_frame: int | None
    replay_last_frame: int

    def as_dict(self) -> dict[str, Any]:
        return {
            "build_key": self.build_key,
            "build_label": self.build_label,
            "launcher_pid": self.launcher_pid,
            "engine_pid": self.engine_pid,
            "completion_reason": self.completion_reason,
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "fast_forward_requested": self.fast_forward_requested,
            "fast_forward_sent": self.fast_forward_sent,
            "fast_forward_verified": self.fast_forward_verified,
            "observed_frames_per_second": (
                round(self.observed_frames_per_second, 1)
                if self.observed_frames_per_second is not None
                else None
            ),
            "final_frame": self.final_frame,
            "replay_last_frame": self.replay_last_frame,
        }


def _progress(
    callback: ProgressCallback | None,
    stage: str,
    detail: str,
    frame: int | None = None,
) -> None:
    if callback is not None:
        callback(stage, detail, frame)


def replay_directory() -> Path:
    return (
        Path.home()
        / "Documents"
        / "Command & Conquer 3 Kane's Wrath"
        / "Replays"
    )


def find_install_root(explicit: Path | None = None) -> Path:
    candidates = [
        explicit,
        DEFAULT_INSTALL_ROOT,
        Path(r"C:\Program Files\EA Games\Command and Conquer 3 Kane's Wrath"),
        Path(r"C:\Program Files (x86)\Electronic Arts\Kane's Wrath"),
    ]
    for candidate in candidates:
        if candidate is not None and (candidate / "CNC3EP1.exe").is_file():
            return candidate.resolve()
    raise CaptureError(
        "Kane's Wrath was not found. Expected CNC3EP1.exe in the Steam "
        f"installation at {DEFAULT_INSTALL_ROOT}."
    )


def _run_version(game_version: str) -> str:
    if game_version == "1.2.0.0":
        return "1.2"
    raise CaptureError(
        f"Automatic capture does not yet support replay version {game_version}."
    )


def _engine_path(install_root: Path, run_version: str) -> Path:
    return install_root / "RetailExe" / run_version / "cnc3ep1.dat"


def _existing_kw_processes() -> list[int]:
    rows = list_processes()
    return [
        row.pid
        for row in rows
        if row.name.lower() in {"cnc3ep1.exe", "cnc3ep1.dat"}
    ]


def _wait_for_engine(launcher_pid: int, timeout: float) -> int:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        matches = [
            row.pid
            for row in list_processes("cnc3ep1.dat")
            if row.parent_pid == launcher_pid
        ]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise CaptureError(
                "The launcher created more than one game process; capture stopped."
            )
        time.sleep(0.2)
    raise CaptureError(
        "Kane's Wrath started, but its game process did not appear within "
        f"{timeout:.0f} seconds."
    )


def _windows_for_pid(pid: int) -> list[int]:
    if sys.platform != "win32":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    windows: list[int] = []
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    @callback_type
    def visitor(hwnd: int, _lparam: int) -> bool:
        window_pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(window_pid))
        if int(window_pid.value) == pid and user32.IsWindow(hwnd):
            windows.append(int(hwnd))
        return True

    user32.EnumWindows(visitor, 0)
    return windows


def _send_fast_forward(pid: int) -> bool:
    if sys.platform != "win32":
        return False
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    windows = _windows_for_pid(pid)
    if not windows:
        return False
    hwnd = windows[0]
    user32.ShowWindow(hwnd, 9)
    if not user32.SetForegroundWindow(hwnd):
        return False
    time.sleep(0.15)
    foreground = user32.GetForegroundWindow()
    foreground_pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(foreground, ctypes.byref(foreground_pid))
    if int(foreground_pid.value) != pid:
        return False
    user32.keybd_event(VK_OEM_PERIOD, 0, 0, 0)
    time.sleep(0.05)
    user32.keybd_event(VK_OEM_PERIOD, 0, 0x0002, 0)
    return True


def _request_close(pid: int) -> None:
    if sys.platform != "win32":
        return
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    for hwnd in _windows_for_pid(pid):
        user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)


def _process_exists(pid: int) -> bool:
    return any(row.pid == pid for row in list_processes())


def _terminate_owned_process(pid: int) -> None:
    if sys.platform != "win32" or not _process_exists(pid):
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    handle = kernel32.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, pid)
    if not handle:
        return
    try:
        kernel32.TerminateProcess(handle, 0)
        kernel32.WaitForSingleObject(handle, 5000)
    finally:
        kernel32.CloseHandle(handle)


def _write_header(
    target: BinaryIO,
    *,
    pid: int,
    module: Any,
    executable_hash: str,
    build: Any,
    interval_ms: int,
) -> None:
    _write_json_line(
        target,
        {
            "type": "kw_telemetry_header",
            "schema_version": 1,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "automatic_capture": True,
            "process_id": pid,
            "module_name": module.name,
            "module_path": str(module.path),
            "module_base": _hex_pointer(module.base),
            "module_size": module.size,
            "build_key": build.key,
            "build_label": build.label,
            "executable_sha256": executable_hash,
            "supported_executable_sha256": sorted(BUILD_PROFILES),
            "verified_build": True,
            "ida_image_base": _hex_pointer(IDA_IMAGE_BASE),
            "sample_interval_ms": interval_ms,
            "offsets": {
                "game_logic_global_rva": (
                    f"0x{build.game_logic_global_rva:08X}"
                ),
                "shroud_manager_global_rva": (
                    f"0x{build.shroud_manager_global_rva:08X}"
                ),
                "recorder_global_rva": f"0x{build.recorder_global_rva:08X}",
                "game_logic_frame": f"0x{GAME_LOGIC_FRAME:X}",
            },
        },
    )


def capture_replay(
    replay_path: Path,
    output_path: Path,
    *,
    install_root: Path | None = None,
    progress: ProgressCallback | None = None,
    fast_forward: bool = True,
    interval_ms: int = 1000,
    launch_timeout: float = 45,
    playback_timeout: float = 180,
    maximum_runtime: float = 3600,
) -> CaptureResult:
    """Stage, launch, capture, and clean up one replay.

    No existing game process is ever attached to or stopped by this function.
    """
    if sys.platform != "win32":
        raise CaptureError("Automatic replay capture requires Windows.")
    existing = _existing_kw_processes()
    if existing:
        raise CaptureError(
            "Close Kane's Wrath before starting deep capture. Running process "
            f"IDs: {', '.join(map(str, existing))}."
        )

    metadata = inspect(replay_path, include_records=False)
    game_version = metadata["header"]["game_version"]
    run_version = _run_version(game_version)
    replay_last_frame = int(metadata["footer"]["last_frame"])
    root = find_install_root(install_root)
    launcher_path = root / "CNC3EP1.exe"
    expected_engine = _engine_path(root, run_version).resolve()
    if not expected_engine.is_file():
        raise CaptureError(
            f"The selected {run_version} engine is missing: {expected_engine}"
        )
    expected_hash = sha256_file(expected_engine)
    expected_build = resolve_build_profile(expected_hash)

    destination_dir = replay_directory()
    destination_dir.mkdir(parents=True, exist_ok=True)
    staged_path = destination_dir / (
        f"{STAGED_PREFIX}{uuid.uuid4().hex[:10]}.KWReplay"
    )
    launcher: subprocess.Popen[bytes] | None = None
    engine_pid: int | None = None
    process_handle: int | None = None
    started = time.monotonic()
    fast_forward_sent = False
    fast_forward_verified = False
    observed_frames_per_second: float | None = None
    fast_forward_sample: tuple[int, float] | None = None
    final_frame: int | None = None
    completion_reason = "unknown"

    try:
        _progress(progress, "staging", "Copying replay into Kane's Wrath Replays folder")
        shutil.copy2(replay_path, staged_path)
        _progress(
            progress,
            "launching",
            f"Launching Kane's Wrath {run_version} with {staged_path.name}",
        )
        sku_definition = root / f"CNC3EP1_english_{run_version}.SkuDef"
        if not sku_definition.is_file():
            raise CaptureError(
                f"The {run_version} configuration is missing: {sku_definition}"
            )
        launcher = subprocess.Popen(
            [
                str(expected_engine),
                "-replayGame",
                staged_path.name,
                "-config",
                str(sku_definition),
            ],
            cwd=root,
            shell=False,
        )
        engine_pid = launcher.pid
        module_deadline = time.monotonic() + launch_timeout
        while True:
            try:
                module = find_main_module(engine_pid)
                break
            except ProbeError:
                if launcher.poll() is not None:
                    raise CaptureError(
                        "Kane's Wrath exited before its engine initialized."
                    )
                if time.monotonic() >= module_deadline:
                    raise CaptureError(
                        "Kane's Wrath did not initialize within "
                        f"{launch_timeout:.0f} seconds."
                    )
                time.sleep(0.2)
        if module.path.resolve() != expected_engine:
            raise CaptureError(
                "The launcher selected an unexpected engine: "
                f"{module.path.resolve()} (expected {expected_engine})."
            )
        executable_hash = sha256_file(module.path)
        build = resolve_build_profile(executable_hash)
        if build.key != expected_build.key:
            raise CaptureError("The running engine does not match the selected build.")

        process_handle = open_process(engine_pid)
        memory = ProcessMemory(process_handle)
        game_logic_global = module.base + build.game_logic_global_rva
        shroud_global = module.base + build.shroud_manager_global_rva
        recorder_global = module.base + build.recorder_global_rva
        interval_seconds = max(interval_ms, 15) / 1000
        playback_deadline = time.monotonic() + playback_timeout
        overall_deadline = time.monotonic() + maximum_runtime
        output_path.parent.mkdir(parents=True, exist_ok=True)
        previous: dict[int, dict[str, Any]] | None = None
        sequence = 0
        playback_started = False
        playback_started_at: float | None = None

        with output_path.open("wb") as target:
            _write_header(
                target,
                pid=engine_pid,
                module=module,
                executable_hash=executable_hash,
                build=build,
                interval_ms=interval_ms,
            )
            while True:
                if not _process_exists(engine_pid):
                    if playback_started:
                        completion_reason = "game_exited"
                        break
                    raise CaptureError("Kane's Wrath exited before replay playback began.")
                if time.monotonic() >= overall_deadline:
                    raise CaptureError(
                        "Automatic capture exceeded its one-hour safety limit."
                    )
                try:
                    game_logic = memory.u32(game_logic_global)
                    shroud_manager = memory.u32(shroud_global)
                    recorder = memory.u32(recorder_global)
                    recorder_mode = (
                        memory.u32(recorder + REPLAY_CLASS_MODE) if recorder else None
                    )
                except ProbeError as exc:
                    _write_json_line(
                        target,
                        {
                            "type": "warning",
                            "sequence": sequence,
                            "captured_utc": datetime.now(timezone.utc).isoformat(),
                            "warning": str(exc),
                        },
                    )
                    time.sleep(interval_seconds)
                    continue

                if recorder_mode == 1 and game_logic:
                    if not playback_started:
                        playback_started = True
                        playback_started_at = time.monotonic()
                        _progress(
                            progress,
                            "capturing",
                            "Replay playback confirmed; collecting live state",
                            0,
                        )
                    try:
                        snapshot = capture_snapshot(
                            memory, game_logic, shroud_manager, sequence, 20000
                        )
                    except ProbeError as exc:
                        _write_json_line(
                            target,
                            {
                                "type": "warning",
                                "sequence": sequence,
                                "captured_utc": datetime.now(timezone.utc).isoformat(),
                                "warning": str(exc),
                            },
                        )
                    else:
                        snapshot["events"] = derive_events(previous, snapshot)
                        snapshot["recorder"] = {
                            "address": _hex_pointer(recorder),
                            "mode": recorder_mode,
                            "mode_name": "playback",
                        }
                        _write_json_line(target, snapshot)
                        previous = {
                            int(row["id"]): row
                            for row in snapshot["objects"]
                            if int(row["id"]) != 0
                        }
                        final_frame = int(snapshot["frame"])
                        percent = min(
                            100, int(final_frame * 100 / max(replay_last_frame, 1))
                        )
                        _progress(
                            progress,
                            "capturing",
                            f"Frame {final_frame:,} of {replay_last_frame:,} ({percent}%)",
                            final_frame,
                        )
                        if final_frame >= replay_last_frame:
                            completion_reason = "last_frame_reached"
                            break
                    if (
                        fast_forward
                        and not fast_forward_sent
                        and playback_started_at is not None
                        and time.monotonic() - playback_started_at >= 1.0
                    ):
                        fast_forward_sent = _send_fast_forward(engine_pid)
                        if fast_forward_sent and final_frame is not None:
                            fast_forward_sample = (final_frame, time.monotonic())
                        _progress(
                            progress,
                            "capturing",
                            (
                                "Fast-forward requested; measuring playback speed"
                                if fast_forward_sent
                                else "Capturing at game playback speed"
                            ),
                            final_frame,
                        )
                    if (
                        fast_forward_sample is not None
                        and final_frame is not None
                        and time.monotonic() - fast_forward_sample[1] >= 4.0
                    ):
                        frame_delta = final_frame - fast_forward_sample[0]
                        time_delta = time.monotonic() - fast_forward_sample[1]
                        observed_frames_per_second = frame_delta / time_delta
                        fast_forward_verified = observed_frames_per_second >= 45
                        _progress(
                            progress,
                            "capturing",
                            (
                                f"Fast-forward verified at "
                                f"{observed_frames_per_second:.0f} frames/sec"
                                if fast_forward_verified
                                else (
                                    "Fast-forward key sent; game is running at "
                                    f"{observed_frames_per_second:.0f} frames/sec"
                                )
                            ),
                            final_frame,
                        )
                        fast_forward_sample = None
                else:
                    _write_json_line(
                        target,
                        {
                            "type": "waiting",
                            "sequence": sequence,
                            "captured_utc": datetime.now(timezone.utc).isoformat(),
                            "reason": "Waiting for replay playback mode.",
                            "recorder_mode": recorder_mode,
                        },
                    )
                    if playback_started:
                        completion_reason = "playback_mode_ended"
                        break
                    if time.monotonic() >= playback_deadline:
                        raise CaptureError(
                            "Kane's Wrath opened, but replay playback did not begin. "
                            "The staged file remains valid only for this capture and "
                            "will now be removed."
                        )
                sequence += 1
                time.sleep(interval_seconds)

        _progress(progress, "finalizing", "Merging live state with replay commands")
        return CaptureResult(
            telemetry_path=output_path,
            build_key=build.key,
            build_label=build.label,
            launcher_pid=launcher.pid,
            engine_pid=engine_pid,
            completion_reason=completion_reason,
            elapsed_seconds=time.monotonic() - started,
            fast_forward_requested=fast_forward,
            fast_forward_sent=fast_forward_sent,
            fast_forward_verified=fast_forward_verified,
            observed_frames_per_second=observed_frames_per_second,
            final_frame=final_frame,
            replay_last_frame=replay_last_frame,
        )
    finally:
        if process_handle is not None:
            _close_handle(process_handle)
        if engine_pid is not None:
            _request_close(engine_pid)
            deadline = time.monotonic() + 6
            while _process_exists(engine_pid) and time.monotonic() < deadline:
                time.sleep(0.2)
            _terminate_owned_process(engine_pid)
        if launcher is not None and launcher.poll() is None:
            launcher.terminate()
            try:
                launcher.wait(timeout=5)
            except subprocess.TimeoutExpired:
                launcher.kill()
        safe_parent = staged_path.parent.resolve() == destination_dir.resolve()
        safe_name = staged_path.name.startswith(STAGED_PREFIX)
        if safe_parent and safe_name:
            staged_path.unlink(missing_ok=True)


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("replay", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--install-root", type=Path)
    parser.add_argument("--no-fast-forward", action="store_true")
    args = parser.parse_args()

    def print_progress(stage: str, detail: str, _frame: int | None) -> None:
        print(f"[{stage}] {detail}", flush=True)

    try:
        result = capture_replay(
            args.replay,
            args.output,
            install_root=args.install_root,
            progress=print_progress,
            fast_forward=not args.no_fast_forward,
        )
    except (CaptureError, ProbeError, OSError, ValueError) as exc:
        print(f"Automatic capture error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result.as_dict(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
