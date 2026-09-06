# Contributing

Keep changes specific to KW 1.02. For decoder changes, include the target address, supporting instructions or data, and a reproducible test. Other builds can suggest where to look, but cannot confirm 1.02 behavior.

Keep unknown fields and IDs in reports. Separate recorded facts from inferences. Use synthetic test replays; do not commit game files or player captures.

## Setup

```sh
python -m venv .venv
```

Activate with `.venv\Scripts\activate` on Windows Command Prompt, `.venv\Scripts\Activate.ps1` in PowerShell, or `source .venv/bin/activate` on Linux/macOS. Then:

```sh
python -m pip install -r requirements-dev.txt
npm ci
npm run build
```

## Tests

```sh
python -m unittest discover -s tests -v
npm run test:frontend
python -m playwright install chromium
python scripts/smoke_browser.py
```

Add a regression test for parser changes. Run browser checks for UI/API changes. Screenshots go in `.artifacts/`.

The UI source is in `frontend/src/`; builds go in `static/replay-lab/`. It uses system fonts and local CSS. Node is only needed for development.

## Release

```sh
python scripts/package_release.py
python scripts/verify_release.py
```

The ZIP includes prebuilt frontend files. Verification checks its manifest, runs tests from a fresh extraction and starts the extracted app.

Write short, direct documentation. Keep technical detail in `docs/` and evidence in `evidence/`. Function counts and decompilation results do not prove a working game engine.
