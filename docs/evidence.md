# RE evidence

All supported decoding targets the exact KW 1.02 executable. Its hash and addresses are in the inventory. Names from other builds are leads only.

| File | Contents |
| --- | --- |
| `evidence/replay-inventory.json` | 278 function roots, addresses, prototypes, calls, partial types and known gaps |
| `evidence/reviewed-artifacts/` | Reader, stream and shared-runtime review records; listed in `index.json` |
| `evidence/command-switch-cases.json` | 233 names recovered from the 1.02 switch at `0x575913`, including ID 1305 (`MSG_OBJECT_JOINED_TEAM`) |
| `tools/kw_game_message_names.json` | Runtime command-name table |
| `evidence/local-corpus-validation.json` | Tested file hashes and decoding counts; no player captures |

The tests run the Python parser, not the game. Command names alone do not establish each command's behavior or argument meaning.

## Gaps

The inventory's original name-prefix search missed ReplayClass destructors `0x41FD1F` and `0x41FCF0`, plus context destructors `0x580493` and `0x58A0AB`. It does not cover all indirect or transitive dependencies.

At the audit snapshot, 28 direct targets remained auto-named or unnamed, 98 roots were absent from the reviewed publication index, and none of the 278 roots had registered human C++ implementations. The app does not reconstruct the game engine, render playback or decode voice audio.

## Experimental data

The optional 831-entry R22/R23 asset catalog supplies historical hints. Names, costs, factions and prerequisites are not verified stock-1.02 values. It is disabled by default; the app does not apply the 1.03 payment/refund model.

Telemetry files are editable, unverified observations. Matching hashes do not validate offsets, player visibility or economy conclusions. See [telemetry](telemetry.md).
