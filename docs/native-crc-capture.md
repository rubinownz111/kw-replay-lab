# Enable KW's native CRC diagnostics

The Python tool creates a separate KW 1.02 executable with the game's built-in deep CRC writer enabled. It can force a diagnostic report while playing a replay. Your installed executable stays unchanged. No DLL or Python package installation is needed.

This is an experimental replay test, not a stock launch option. Multiplayer use has not been validated.

## Run a capture

Use Windows, Python 3.11 or newer, and an English KW 1.02 installation. Close KW first. Get the current tool from the repository's **Code > Download ZIP**, extract it, and open PowerShell in that folder.

1. Put your replay in the game's `Replays` folder, normally under `Documents\Command & Conquer 3 Kane's Wrath\Replays`.
2. Find the checkpoint frame in Replay Lab's Desync Lab. For example, frame 7650 is 8:30 at 15 frames per second.
3. Run this command, changing the game path, replay filename and frame:

```powershell
python scripts/native_crc_capture.py `
  --game-dir "C:\Program Files (x86)\Steam\steamapps\common\Command and Conquer 3 - Kane's Wrath" `
  --output "$env:USERPROFILE\Desktop\KW-CRC-7650" `
  --frame 7650 `
  --replay "match.KWReplay"
```

Use a new output folder for each run. Let playback reach the chosen frame, then close the game. Without `--replay`, the tool only builds the capture copy.

The tool verifies this exact executable SHA-256 before patching:

```text
8225bb6ce15f7d34467e7fd55ed1ad60706e62e9470a877ab3db6aa47addfdf5
```

A different hash is rejected, even if the file is labelled 1.02. Version 1.03 is unsupported.

## Modded replays

Use the exact map and mod revision from the match. The generated configuration loads stock English assets; it does not automatically load your installed mods or DLL plugins.

For a modded replay, add the matching BIG archives to the command with repeated `--asset-big "C:\path\archive.big"` arguments. They are loaded before stock assets, in the order supplied. For the tested R25g map pack, these were `102Scripts.big`, `102texturefix.big`, and the archive containing the replay's map. Their paths depend on where you installed or extracted that revision.

Changing the assets can change the simulation. A newer map pack is not a substitute for the recorded revision.

## Where the files go

The launch command sets the working directory to your `--output` folder. The native writer places its files there:

- `DESYNC-Frame<frame>-<map>-ReplayObserver.txt`: readable state dump.
- `BIN_DESYNC-Frame<frame>-<map>-ReplayObserver.bin`: tagged binary state dump.
- `capture-settings.json` and `cnc3ep1.patch.json`: patch settings and file hashes.

Successful tests retained three snapshots immediately before the requested frame. For example, a trigger at 7650 produced files for 7647, 7648 and 7649. The text header names the trigger frame; filenames name the snapshot frames. Keep the paired text and settings with the binary so the forced trigger remains documented.

Expect large files. One eight-player test produced about 426 MiB of text and binary output combined. The patch raises the native per-stream diagnostic limit from 28 MiB to 128 MiB; this is not a total disk limit. Captures are generated locally and are not included in this repository.

## What this tells you

A forced report records a new playback's state. It does not prove that a natural desync happened at that frame or recover the original players' missing states. The forced-frame setting also changes the CRC comparison path, so this run is not a stock checksum-agreement test.

Replay Lab currently limits capture imports to 8 MiB per file and 20,000 fields. Full native dumps from our tests exceed those limits. Capture generation works, but large-dump analysis is not yet integrated into the app. Do not truncate a binary dump to make it fit.

## How the patch works

The builder adds a small executable section named `.kwcrc`, redirects the entry point through it, writes the settings below, restores CPU registers and flags, then jumps to the original entry point.

| Runtime address | Setting | Value |
| --- | --- | --- |
| `0xBE4F42` | Deep CRC | 1 |
| `0xBE4F45` | Binary output | 1 |
| `0xB6FBB4` | Forced report frame | Your `--frame` value |
| `0xBE4F48` | Diagnostic stream size limit | 134217728 bytes |

These are addresses for the hash-verified executable, not file offsets. Native stream allocation and bounds checks remain in place. To return to normal play, close the capture copy and use your usual game launcher.
