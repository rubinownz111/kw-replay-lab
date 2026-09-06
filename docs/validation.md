# Validation

Version 0.3.0 was checked on Windows with Python 3.14.6 on 2026-09-06. Results and source-file hashes are in `evidence/app-validation.json` and `evidence/local-corpus-validation.json`.

| Check | Result |
| --- | --- |
| Python parser, API and CLI | 54 tests passed |
| TypeScript comparisons | 19 tests passed; strict build passed |
| Chromium | All 15 tabs, uploads, filters, inspector, library and exports passed |
| Desync Lab | Windows, player comparisons, capture uploads, field differences and saved cases passed |
| Mobile and themes | 390 px layout and light/dark modes passed |
| Offline HTML | Case recordings, captures and notes worked without network requests |
| Release ZIP | Manifest, 54 extracted tests and local HTTP checks passed |
| Replay corpus | 16 accepted files, 96,694 records, 30,193 commands, zero payload errors |
| Argument labels | 18,079 commands had verified fields; no checked field-type mismatches |
| CRC evidence | 261 checkpoints; two recordings had a disagreement, both first at frame 900 |

One unfinished or malformed local replay was rejected. Separate tests reject 1.03. Header declarations do not authenticate a stock installation; the corpus may contain modified maps.

Diagnostic readers follow reviewed 1.02 instructions and have synthetic fixtures. Paired native captures still need end-to-end validation. Auxiliary streams also use synthetic tests.

The [CI workflow](https://github.com/rubinownz111/kw-replay-lab/actions/workflows/test.yml) runs on Windows and Ubuntu with Python 3.11/3.13. Check the current commit's run for its status. macOS, Firefox and Safari are untested.

These checks do not prove engine equivalence, successful game actions, stock asset values or a desync's root cause. See [Contributing](../CONTRIBUTING.md) to repeat them.
