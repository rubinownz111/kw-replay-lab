# Desync Lab

Open a replay and select **Desync Lab**, or choose **Try Desync Lab** for a synthetic example.

1. Start with the first differing checkpoint. The app shows the preceding matching checkpoint and the commands between them.
2. Choose **Show commands**, **Show camera**, or an object ID to inspect that interval.
3. Add recordings from other players. The comparison finds missing or changed commands and compares each player's CRCs.
4. Add paired DESYNC `.txt` or tagged BIN_DESYNC `.bin` files. The first differing captured field appears at the top, with values, block paths and byte offsets or line numbers below.
5. Add notes, save the case in your library, or export JSON, Markdown notes and offline HTML.

Use **Back to Desync Lab** after following an object or command link. Browser Back/Forward works too. Refresh restores the current replay, view, filters and case data. **Close replay** clears the active workspace; library copies are managed separately.

## What the result means

A replay can bracket a CRC disagreement. It cannot identify the faulty player or reconstruct the state that caused it. Matching commands can produce different states. Matching checksums do not prove identical state.

CRC reports stay separate by epoch and checkpoint frame. Duplicate sources, mixed modes, missing reports and malformed arguments are shown explicitly. The initial roster is a coverage hint; membership can change.

Player comparison uses unique roster names to map source numbers. Ambiguous mappings remain provisional. Alignment looks ahead up to 32 commands per player and excludes CRC, network and replay-camera messages. A metadata match does not authenticate the files.

Captured field differences point to objects or modules worth checking. Capture order is not the order in which bugs occurred. Check frames, settings, forced-report markers and partial decoding before interpreting differences. Capture association with a replay is user supplied.

## Files and limits

To generate your own dumps, see [Enable native CRC diagnostics](native-crc-capture.md). The Python tool activates the game's writer in an isolated executable. Its full native dumps currently exceed the import limits below.

A case holds three additional recordings and eight captures. Captures are limited to 8 MiB each, 32 MiB combined, 20,000 decoded fields and 64 nested blocks. Unsupported tags stop decoding with the partial result retained. Untagged binary captures are unsupported.

JSON and HTML retain decoded case data and notes. Raw file bytes are excluded, but decoded reports can contain player names and machine details. Library storage is local to your browser.

The reader follows the exact 1.02 code. Binary and text diagnostic tests use synthetic fixtures; paired native captures still need end-to-end validation.

CLI analysis includes Desync Lab results automatically. Repeat `--diagnostic FILE` or `--peer-replay FILE` to attach evidence to case JSON. Field and player comparisons run in the browser.
