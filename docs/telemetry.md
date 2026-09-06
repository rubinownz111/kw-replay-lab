# Experimental telemetry

Replay files are enough for normal analysis. The Windows collectors in `tools/` are research tools and never start automatically. Live-game behavior has not been validated for this release.

To sample an already running 1.02 game:

```sh
python -m tools.kw_live_probe --replay match.KWReplay --output capture.jsonl --duration 60
```

The collector uses read-only process access and requires the exact executable hash listed in the evidence inventory. You must confirm that the named replay is the one playing. Use `--help` for process and sampling options.

Upload the capture alongside its replay, or run:

```sh
python app.py --analyze match.KWReplay --telemetry capture.jsonl
```

Wrong executable hashes and mismatched replay hashes are rejected. A missing replay hash permits standalone summaries but disables visibility correlation.

Sidecars are editable. Their samples do not prove what a player saw, why resources changed or why an object disappeared. The retained `kw_auto_capture.py` tool can launch and focus the game and perform cleanup; it is outside the tested app workflow.
