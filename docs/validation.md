# Validation

Recorded on 2026-09-06. Version 0.2.1 passed the 35 Python tests, 8 analytics tests, build, Chromium workflow and extracted-release checks after the standalone UI update. Details and file hashes are in `evidence/app-validation.json` and `evidence/local-corpus-validation.json`.

| Check | Result |
| --- | --- |
| Python parser, API and CLI | 35 tests passed |
| TypeScript analytics | 8 tests passed; strict build passed |
| Chromium | All 14 tabs, uploads, filters, inspector, comparison, library and exports passed |
| Mobile and themes | All tabs at 390 px; light and dark modes passed |
| Offline HTML | Opened from disk with no network requests |
| Release ZIP | Manifest verified; 35 tests and HTTP checks passed from a fresh extraction |
| Real replay corpus | 15 accepted 1.02 files, 96,678 records, 30,191 commands, zero payload decode errors; two unsupported files rejected |

Browser checks covered standalone use, including invalid-upload recovery, text escaping and persistent library storage. Extracted-release tests reused installed Python dependencies, but no source files outside the ZIP.

The earlier schema 6 corpus run covered 16 files and 30,751 commands. Local replay files changed before the schema 7 run; hashes identify each tested file. The corpus contains command/camera streams and may include modified maps. Auxiliary streams have synthetic tests only.

[CI passed](https://github.com/rubinownz111/kw-replay-lab/actions/workflows/test.yml) on Windows and Ubuntu with Python 3.11/3.13, plus the build, browser and release checks on Ubuntu. Local testing used Windows and Python 3.14.6. macOS, Firefox and Safari are untested.

These checks do not verify game-engine equivalence, live telemetry, stock asset values or successful in-game actions. To repeat them, see [Contributing](../CONTRIBUTING.md).
