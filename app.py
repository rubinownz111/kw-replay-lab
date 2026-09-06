"""KW Replay Lab: local, offline inspection of Kane's Wrath 1.02 replays."""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import tempfile
import webbrowser
from pathlib import Path
from threading import BoundedSemaphore, Timer

from flask import Flask, jsonify, render_template, request, Response
from werkzeug.exceptions import RequestEntityTooLarge

from replay_analyzer import analyze_replay
from replay_analyzer.demo import build_auxiliary_replay
from tools.kwreplay_inspect import ReplayFormatError, MAX_FILE_BYTES, inspect

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_FILE_BYTES + 1024 * 1024
app.json.ensure_ascii = True
REPORT_SCHEMA_VERSION = 7
analysis_slot = BoundedSemaphore(1)


def encode_report(report):
    return json.dumps(report, indent=2, ensure_ascii=True, allow_nan=False) + "\n"


def csv_report(report, section="commands"):
    rows = report["commands"]["items"] if section == "commands" else report["forensics"]["records"]
    columns = ["frame", "time", "player", "source_index", "name", "category", "arguments"] if section == "commands" else ["index", "offset", "frame", "time", "channel", "channel_name", "payload_size", "payload_sha256"]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=columns)
    writer.writeheader()
    for row in rows:
        cells = {}
        for key in columns:
            value = row.get(key, "")
            if isinstance(value, (list, dict)):
                value = json.dumps(value, ensure_ascii=True, allow_nan=False)
            # Text fields opened in spreadsheet applications must remain text.
            if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            cells[key] = value
        writer.writerow(cells)
    return output.getvalue()


@app.get("/")
def index():
    return render_template("index.html")


@app.post("/api/analyze")
def analyze():
    upload = request.files.get("replay")
    if upload is None or not upload.filename:
        return jsonify(error="Choose a .KWReplay file to analyze."), 400
    if not upload.filename.lower().endswith(".kwreplay"):
        return jsonify(error="Choose a Kane's Wrath .KWReplay file."), 400
    if not analysis_slot.acquire(blocking=False):
        return jsonify(error="Another analysis is running. Please try again when it finishes."), 429
    try:
        with tempfile.TemporaryDirectory(prefix="kw-replay-") as directory:
            replay = Path(directory) / "input.KWReplay"
            upload.save(replay)
            telemetry = request.files.get("telemetry")
            telemetry_path = None
            if telemetry is not None and telemetry.filename:
                if not telemetry.filename.lower().endswith((".jsonl", ".kwtelemetry")):
                    return jsonify(error="Telemetry must be a .jsonl or .kwtelemetry file."), 400
                telemetry_path = Path(directory) / "sidecar.jsonl"
                telemetry.save(telemetry_path)
            report = analyze_replay(
                replay, display_name=upload.filename,
                telemetry_path=telemetry_path,
                telemetry_display_name=telemetry.filename if telemetry else None,
                reference_catalog=request.form.get("reference_catalog") == "true",
            )
            # Strict JSON validation catches accidental non-finite analytical results.
            return Response(encode_report(report), mimetype="application/json")
    except (ReplayFormatError, OSError, ValueError) as exc:
        return jsonify(error=str(exc)), 422
    finally:
        analysis_slot.release()


@app.get("/api/demo")
def demo():
    with tempfile.TemporaryDirectory(prefix="kw-demo-") as directory:
        path = Path(directory) / "synthetic-demo.KWReplay"
        path.write_bytes(build_auxiliary_replay())
        report = analyze_replay(path)
    report["evidence"]["sample_kind"] = "synthetic_not_engine_recorded"
    return Response(encode_report(report), mimetype="application/json")


@app.errorhandler(RequestEntityTooLarge)
def too_large(_error):
    return jsonify(error="Upload exceeds the 64 MiB replay limit (65 MiB combined request)."), 413


@app.get("/api/health")
def health():
    return jsonify(status="ok", target="KW 1.02", supported_game_version="1.2.0.0",
        report_schema_version=REPORT_SCHEMA_VERSION, production_analysis=False,
        telemetry_sidecars=True, telemetry_status="experimental",
        auxiliary_stream_analysis=True, forensic_record_index=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--analyze", type=Path, help="Analyze a replay and exit.")
    mode.add_argument("--inspect", type=Path, help="Decode low-level container/stream statistics.")
    mode.add_argument("--batch", type=Path, help="Analyze .KWReplay files in a directory.")
    parser.add_argument("--telemetry", type=Path, help="Experimental sidecar for --analyze.")
    parser.add_argument("--reference-catalog", action="store_true", help="Opt into unverified R22 asset hints.")
    parser.add_argument("--output", type=Path, help="JSON file, or report directory for --batch.")
    parser.add_argument("--commands-csv", type=Path, help="Command export for --analyze.")
    parser.add_argument("--records-csv", type=Path, help="Record export for --analyze.")
    args = parser.parse_args(argv)
    if args.batch and not args.output:
        parser.error("--batch requires --output DIRECTORY")
    if (args.telemetry or args.commands_csv or args.records_csv) and not args.analyze:
        parser.error("--telemetry and CSV outputs require --analyze")
    if args.output and not (args.analyze or args.inspect or args.batch):
        parser.error("--output requires --analyze, --inspect or --batch")
    try:
        destinations = [p.resolve() for p in (args.output, args.commands_csv, args.records_csv) if p]
        inputs = {p.resolve() for p in (args.analyze, args.inspect, args.telemetry) if p}
        if len(destinations) != len(set(destinations)) or any(p in inputs for p in destinations):
            raise ValueError("All output paths must be distinct and must not overwrite inputs.")
        if args.batch:
            if not args.batch.is_dir():
                raise ValueError("Batch input must be a directory.")
            args.output.mkdir(parents=True, exist_ok=True)
            results = []
            for path in sorted(args.batch.iterdir()):
                if not path.is_file() or path.suffix.lower() != ".kwreplay":
                    continue
                try:
                    report = analyze_replay(path, reference_catalog=args.reference_catalog)
                    destination = args.output / (path.name + ".json")
                    destination.write_text(encode_report(report), encoding="utf-8")
                    results.append({"file": path.name, "status": "ok", "report": destination.name})
                except (OSError, ValueError) as exc:
                    results.append({"file": path.name, "status": "error", "error": str(exc)})
            (args.output / "batch-summary.json").write_text(encode_report(results), encoding="utf-8")
            print(f"Analyzed {len(results)} files; {sum(x['status'] == 'error' for x in results)} failed.")
            return 1 if not results or any(x["status"] == "error" for x in results) else 0
        if args.analyze or args.inspect:
            report = inspect(args.inspect) if args.inspect else analyze_replay(
                args.analyze, telemetry_path=args.telemetry, reference_catalog=args.reference_catalog)
            payload = encode_report(report)
            if args.output:
                if args.output.resolve() in {p.resolve() for p in (args.analyze, args.inspect, args.telemetry) if p}:
                    raise ValueError("Output must not overwrite an input file.")
                args.output.write_text(payload, encoding="utf-8")
            else:
                print(payload, end="")
            for destination, section in [(args.commands_csv, "commands"), (args.records_csv, "records")]:
                if destination:
                    if destination.resolve() in {p.resolve() for p in (args.analyze, args.telemetry, args.output) if p}:
                        raise ValueError("CSV output must differ from input and JSON paths.")
                    destination.write_text(csv_report(report, section), encoding="utf-8-sig")
            return 0
        if not 1 <= args.port <= 65535:
            raise ValueError("Port must be between 1 and 65535.")
        from waitress import serve
        print(f"KW Replay Lab 1.02: http://{args.host}:{args.port}", flush=True)
        if not args.no_browser:
            timer = Timer(0.8, lambda: webbrowser.open(f"http://{args.host}:{args.port}"))
            timer.daemon = True
            timer.start()
        serve(app, host=args.host, port=args.port, threads=2, max_request_body_size=app.config["MAX_CONTENT_LENGTH"])
    except (OSError, ValueError) as exc:
        print(f"Replay error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
