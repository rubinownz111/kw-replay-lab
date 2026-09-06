# KW Replay Lab

Inspect Kane's Wrath **1.02** replays in your browser or from the command line. No game installation needed. Other versions, including 1.03, are rejected.

## Start

[Download the latest release](https://github.com/rubinownz111/kw-replay-lab/releases/latest) and extract it. Install Python 3.11 or newer, then double-click **Start KW Replay Lab.bat** on Windows.

For manual setup, create and activate a virtual environment, then run:

```sh
python -m pip install -r requirements.txt
python app.py
```

Open http://127.0.0.1:8765. Choose a `.KWReplay` file or try **Explore a synthetic demo**. Setup needs internet access; analysis runs locally. Node is only needed to rebuild the frontend.

## Features

- Filter commands, build requests and checkpoints by time.
- Inspect command arguments, raw bytes and ObjectID references.
- Read verified argument labels for 36 command types, with unknown fields kept explicit.
- Use **Desync Lab** to find CRC investigation windows and compare player recordings or captured fields.
- View camera paths and sample density.
- Compare commands, openings and recorded CRCs across two replays.
- Save and search replays in a browser library.
- Export JSON, CSV or an HTML report that opens offline.
- Switch between light and dark mode.

The library stores up to 50 entries / 256 MiB. Clearing browser data removes it, so export reports you want to keep.

## Command line

```sh
python app.py --analyze match.KWReplay --output report.json
python app.py --analyze match.KWReplay --commands-csv commands.csv
python app.py --inspect match.KWReplay --output container.json
python app.py --batch ./replays --output ./reports
python app.py --analyze match.KWReplay --diagnostic DESYNC.txt --output case.json
```

Batch mode continues after invalid files and lists results in `batch-summary.json`.

## Limits

Supports complete files up to 64 MiB with headers declaring `1.2.0.0`. Commands are recorded requests; they do not prove successful actions, cash balances or cheating. The tool does not simulate or render the game. Asset hints and telemetry are experimental and off by default.

## Documentation

- [Setup, development and tests](CONTRIBUTING.md)
- [Desync Lab](docs/desync-lab.md)
- [1.02 format](docs/format-1.02.md) and [report schema](docs/report-schema.md)
- [RE evidence](docs/evidence.md), [validation](docs/validation.md) and [telemetry](docs/telemetry.md)

No game executable, IDA database or captured player replay is included. This project is not affiliated with the game's publisher.
