# Replay format: exact KW 1.02

Target hashes are in `evidence/replay-inventory.json`. Addresses below are virtual addresses in the 1.02 executable, based at `0x400000`.

## Container

Integers and floats are little-endian. UTF16Z strings end with a zero UTF-16LE code unit. The native header object is `0xAE0` bytes; the serialized header has variable length.

```text
"C&C3 REPLAY HEADER"             18 bytes, no terminating NUL
u8 container_version            4 or 5
u32[4] game_version             supported by this tool: 1,2,0,0
u16 channel_mask
UTF16Z match_title              at most 200 nonzero code units
UTF16Z match_description        at most 500
UTF16Z map_name                 at most 200
UTF16Z map_id                   at most 250
u8 participant_count           at most 8
repeat participant_count:
    u32 participant_id
    UTF16Z name                at most 20
    u8 team                    only for container version 5
u32 observer_id
UTF16Z observer_name            at most 20
u8 observer_team                only for container version 5
u32 metadata_size
u8[metadata_size] metadata
```

Normal body records are `u32 reserved`, `u32 frame`, `u8 channel`, `u32 payload_size`, then payload. The writer emits reserved zero. The 8-byte end sentinel contains reserved zero and frame `0x7FFFFFFF`, without channel/size/payload.

The native reader at `0x8B088C` narrows u32 sizes to u16 (`0x8B08EA`, `0x8B08EE`). The app rejects sizes above 65,535 and records that overlap the sentinel. Other limits are 250,000 records and seven days of 15 Hz frame values.

The footer begins with `C&C3 REPLAY FOOTER`, followed by u32 last frame and u8 version. Footer v1 adds a u8 metadata type, u32 length, and metadata bytes. Footer v2 has no such payload. A final u32 contains total footer size. The tool validates footer size, complete consumption and the preceding sentinel.

Header read/write: `0x8A4F32` / `0x8B0922`. Body read/write: `0x8B088C` / `0x8B0ABA`. Footer read/write: `0x8A510F` / `0x8B0B02`.

## Match metadata

`CNC3RPL` metadata begins with u32 length 8 and `CNC3RPL\0`. In 1.02, serialized source fields begin immediately afterward; there is no later-build identity preamble. Parser/writer: `0x57B50E` / `0x57D7E0`.

Recovered fields include seven dwords, packed scalar fields, length-prefixed ASCII/UTF-16 strings, a 16-byte SYSTEMTIME, generated engine-version text, and a twelve-entry channel array. Unknown fields keep names such as `source_00_to_18_u32`. Lobby slots, seed/map fields and raw match configuration are retained. Declared versions do not authenticate the executable or assets.

## Streams

| Channel | Wire payload | Evidence |
| --- | --- | --- |
| 1 | Versioned counted GameMessage vector | `0x6D7E51`; save/load entry points in inventory |
| 2 | Shared object stream, camera message type 14 | `0x6C06E4` |
| 3 | Shared object stream, voice type 13 | `0x6C614D` |
| 4 | Shared object stream, telestrator type 15 | `0x6C60D6` |
| FD–FF | u8 count and raw 60-byte broadcast metadata objects | `0x6C3409`, `0x6DB676` |

Command streams start with u8 transfer version 1 and u32 message count. Each message starts with u16 packed ID: low 11 bits are command ID; high 5 bits are command source. Argument-run headers use the low nibble for argument type and high nibble plus one for count; `0xFF` ends the message. Strings use a u8 length unless it is `0xFF`, then u32; Unicode lengths count code units. Arguments include integer, real, boolean, object/drawable/team IDs, coordinates, regions, timestamps, wchar and strings. Type 11 consumes no data and remains semantically unknown. Native GameMessage storage is 132 bytes, not the on-disk message length.

The object stream uses u16 count; each object has u32 stream ID and u8 type/flags. Bit `0x40` introduces a u32 base key; low six bits select the message type. Camera messages contain frame and flags; bit 1 adds three position floats and bit 2 adds a quaternion. Components omitted by later packets inherit earlier state within the stream. Voice has u32 frame, u16 serial, u16 byte count and encoded data. Telestrator has u32 frame, u8 byte count and operations. Operations 0-3 are decoded, but their meanings are not fully verified. Voice audio and game rendering are unsupported.

Observers can leave gaps in the header roster. The app maps sources using unique lobby names and native slot + 3, preserving unresolved source IDs. These labels come from metadata, not verified identities.

## Native runtime notes

The recovered ReplayClass has recording/playback/inactive modes 0/1/2, mode at `+0x18`, contexts at `+0x20` and `+0x1BC8`, and partial recorded size `0x2C38`. `TheRecorder` is `0xBE4FBC`, vtable `0xA122F0`, constructor `0x58E10F`, reset `0x589AD6`, update `0x58BCDD`.

The inventory covers scheduling, queues, reader/writer jobs, storage, network transfers, capture and camera interpolation. These are static RE records, not a replacement runtime. Destructor paths and ownership dependencies remain incomplete.
