# Report schema 7

JSON contains the full report. Unknown fields and source IDs are retained. A nonzero payload decode-error count means some content could not be read.

| Key | Meaning |
| --- | --- |
| `schema_version`, `target`, `evidence` | Version and evidence/assumption boundaries |
| `file` | Display filename, size and SHA-256 of original bytes |
| `match`, `observer`, `players` | Declared metadata, inferred result, roster, mapping basis and activity totals |
| `summary`, `integrity` | Counts, checked structure/CRC conditions and bounded diagnostic lists |
| `commands.items` | Frame, source, player label, numeric/name ID, arguments and analytical category |
| `strategy` | Recorded build/sell requests, elimination inference and optional reference comparisons |
| `camera` | Sampled display path plus full `samples`, per-stream counts and incremental state |
| `auxiliary_streams` | Voice packet/serial data, telestrator operations and broadcast metadata |
| `network` | Recorded CRC checkpoints and RTT observations |
| `forensics.records` | Every record's index/offset/frame/channel/length, prefix and payload hash |
| `telemetry` | Optional experimental sidecar summaries and association status |
| `cheat_analysis` | Legacy key retained for findings; **not a cheating verdict** |

## Commands and camera

Each command includes `index`, `record_index`, `record_offset`, `wire_offset`, `wire_size` and `wire_sha256`. Offsets are absolute file positions. The hash covers the packed command and its arguments, including opaque data.

`camera.samples` keeps every reconstructed sample. `camera.path` is a smaller display projection. Neither represents a game simulation.

## Exports

Browser JSON and CSV include all rows, regardless of filters. HTML includes one report and its offline runtime, without replay bytes or comparison targets. These exports cannot be turned back into a playable replay.

CLI command CSV columns: `frame,time,player,source_index,name,category,arguments`. Arguments contain JSON. Browser command CSV also includes the numeric command ID and byte-link fields.

Record CSV columns: `index,offset,frame,time,channel,channel_name,payload_size,payload_sha256`.

## Interpretation

`summary.integrity_score` is a heuristic identified by `score_kind`, not an authenticity score. The UI shows decode errors instead. `cheat_analysis` is a legacy key for findings, not a cheating verdict. An empty findings list proves neither authenticity nor fair play.

## HTTP API

`POST /api/analyze` accepts multipart `replay`, optional `telemetry` and optional `reference_catalog=true`. `GET /api/health` returns target, schema and features. `GET /api/demo` returns a synthetic report.

| Status | Meaning |
| --- | --- |
| 400 | Missing file or invalid extension |
| 413 | Upload too large |
| 422 | Malformed or unsupported data |
| 429 | Another analysis is running |

The frontend requires schema 7.
