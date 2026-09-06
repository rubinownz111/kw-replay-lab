"""Build an isolated, hash-guarded KW 1.02 native CRC experiment.

Never patches the installed file. The added entrypoint sets diagnostic globals
before CRT/game startup, preserves registers/flags, and jumps to the old entry.
The forced frame tests the writer; it is not evidence of a natural desync.
"""
import argparse
import hashlib
import json
import struct
import subprocess
import os
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def build(source, output, manifest):
    original = source.read_bytes()
    if sha(original) != manifest['input_sha256'].lower():
        raise ValueError('Input is not the reviewed KW 1.02 executable')
    if output.resolve() == source.resolve() or output.exists():
        raise ValueError('Output must be a new isolated file')
    data = bytearray(original)
    pe = struct.unpack_from('<I', data, 60)[0]
    opt = pe + 24
    count = struct.unpack_from('<H', data, pe + 6)[0]
    opt_size = struct.unpack_from('<H', data, pe + 20)[0]
    table = opt + opt_size
    header = table + count * 40
    base = struct.unpack_from('<I', data, opt + 28)[0]
    entry = struct.unpack_from('<I', data, opt + 16)[0]
    sa, fa = struct.unpack_from('<II', data, opt + 32)
    headers = struct.unpack_from('<I', data, opt + 60)[0]
    if data[pe:pe+4] != b'PE\0\0' or struct.unpack_from('<H', data, opt)[0] != 0x10b:
        raise ValueError('Expected PE32')
    if header + 40 > headers or any(data[header:header+40]):
        raise ValueError('No spare section header')
    align = lambda n, a: (n + a - 1) // a * a
    ends = []
    for n in range(count):
        vs, va, rs, ro = struct.unpack_from('<IIII', data, table + n * 40 + 8)
        ends.append(va + max(vs, rs))
    rva, raw = align(max(ends), sa), align(len(data), fa)
    # pushfd; pushad; call next; pop eax; sub eax, return RVA.
    stub = bytearray(b'\x9c\x60\xe8\0\0\0\0\x58\x2d')
    stub += struct.pack('<I', rva + 7)
    for setting in manifest['startup_writes']:
        offset = int(setting['va'], 0) - base
        size, value = setting['size'], setting['value']
        if size == 1:
            stub += b'\xc6\x80' + struct.pack('<IB', offset, value)
        elif size == 4:
            stub += b'\xc7\x80' + struct.pack('<Ii', offset, value)
        else:
            raise ValueError('Unsupported write size')
    stub += b'\x61\x9d\xe9'
    stub += struct.pack('<i', entry - (rva + len(stub) + 4))
    raw_size = align(len(stub), fa)
    data.extend(b'\0' * (raw + raw_size - len(data)))
    data[raw:raw+len(stub)] = stub
    struct.pack_into('<8sIIIIIIHHI', data, header, b'.kwcrc\0\0', len(stub), rva,
                     raw_size, raw, 0, 0, 0, 0, 0x60000020)
    struct.pack_into('<H', data, pe + 6, count + 1)
    struct.pack_into('<I', data, opt + 4, struct.unpack_from('<I', data, opt + 4)[0] + raw_size)
    struct.pack_into('<I', data, opt + 16, rva)
    struct.pack_into('<I', data, opt + 56, align(rva + len(stub), sa))
    struct.pack_into('<I', data, opt + 64, 0)  # optional PE checksum
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('xb') as stream:
        stream.write(data)
    result = dict(input_sha256=sha(original), output_sha256=sha(data),
                  original_entry_rva=hex(entry), entry_rva=hex(rva),
                  stub_hex=stub.hex(), output=str(output.resolve()), settings=manifest['startup_writes'])
    output.with_suffix('.patch.json').write_text(json.dumps(result, indent=2)+'\n', encoding='utf-8')
    return result


INPUT_SHA256 = '8225bb6ce15f7d34467e7fd55ed1ad60706e62e9470a877ab3db6aa47addfdf5'


def settings(frame):
    if not 4 <= frame <= 2147483647:
        raise ValueError('Frame must be between 4 and 2147483647')
    return dict(input_sha256=INPUT_SHA256, startup_writes=[
        dict(name='deep_crc', va='0xBE4F42', size=1, value=1),
        dict(name='binary_deep_crc', va='0xBE4F45', size=1, value=1),
        dict(name='forced_report_frame', va='0xB6FBB4', size=4, value=frame),
        dict(name='capture_size_limit', va='0xBE4F48', size=4, value=128 * 1024 * 1024),
    ])


def capture_config(game_root, folder, assets):
    """Resolve stock English configuration and explicitly supplied mod archives."""
    lines, active = [], set()

    def add_asset(path):
        path = path.resolve(strict=True)
        if not path.is_file() or any(c in str(path) for c in '\r\n'):
            raise ValueError(f'Invalid asset path: {path}')
        lines.append(f'add-big {path}')

    def flatten(path):
        path = path.resolve(strict=True)
        if path in active:
            raise ValueError(f'Config cycle: {path}')
        active.add(path)
        for line in path.read_text(encoding='utf-8-sig').splitlines():
            command, _, value = line.strip().partition(' ')
            value = value.strip()
            if command == 'add-config':
                flatten(path.parent / value)
            elif command == 'add-big':
                if value.lower() != 'kw_no_ea_logo.big':
                    add_asset(path.parent / value)
            elif command and command != 'add-search-path':
                raise ValueError(f'Unsupported directive in {path}: {line}')
        active.remove(path)

    for asset in assets:
        add_asset(asset)
    for name in ('Lang-english/1.2', 'EnglishAudio/1.2', 'Core/1.2', 'Meta/1.2', 'Movies/1.0'):
        flatten(game_root / name / 'config.txt')
    lines.extend((f'add-search-path {folder}', 'add-search-path big:'))
    return ('\n'.join(lines) + '\n').encode('ascii')


def prepare(game_root, folder, frame, assets=()):
    manifest = settings(frame)
    game_root = game_root.resolve(strict=True)
    folder = folder.resolve()
    if folder.exists():
        raise ValueError('Choose a new output folder; existing folders are never reused')
    if folder.is_relative_to(game_root):
        raise ValueError('Choose an output folder outside the game installation')
    if any(c in str(folder) for c in '\r\n'):
        raise ValueError('Invalid output folder')
    source = game_root / 'RetailExe/1.2/cnc3ep1.dat'
    if sha(source.read_bytes()) != INPUT_SHA256:
        raise ValueError('Unsupported executable: this tool requires the reviewed KW 1.02 SHA-256')
    config = capture_config(game_root, folder, assets)
    folder.mkdir(parents=True, exist_ok=False)
    result = build(source, folder / 'cnc3ep1.dat', manifest)
    (folder / 'capture.SkuDef').write_bytes(config)
    result['assets'] = [dict(path=str(p.resolve()), sha256=sha(p.read_bytes())) for p in assets]
    result['forced_report'] = True
    result['output_directory'] = str(folder)
    (folder / 'capture-settings.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return folder


def main():
    parser = argparse.ArgumentParser(description='Enable native KW 1.02 CRC dumps in an isolated replay test copy.')
    parser.add_argument('--game-dir', type=Path, required=True, help='English KW installation folder')
    parser.add_argument('--output', type=Path, required=True, help='New folder outside the game installation')
    parser.add_argument('--frame', type=int, required=True, help='Force a report at this simulation frame (15 frames/second)')
    parser.add_argument('--asset-big', type=Path, action='append', default=[], help='Matching mod/map BIG; repeat in load-priority order')
    parser.add_argument('--replay', help='Replay filename already in the game Replays folder; starts playback after building')
    args = parser.parse_args()
    try:
        if args.replay and (os.name != 'nt' or '/' in args.replay or '\\' in args.replay
                            or not args.replay.lower().endswith('.kwreplay')):
            raise ValueError('--replay needs a filename ending .KWReplay and Windows')
        folder = prepare(args.game_dir, args.output, args.frame, args.asset_big)
        print(f'Capture copy: {folder}')
        print(f'Text and binary dumps will be written here at frame {args.frame}.')
        print('This forces a diagnostic report; it does not prove a natural desync.')
        if args.replay:
            command = [str(folder / 'cnc3ep1.dat'), '-config', str(folder / 'capture.SkuDef'),
                       '-replayGame', args.replay, '-win', '-xres', '1024', '-yres', '768',
                       '-noaudio', '-noshellmap']
            process = subprocess.Popen(command, cwd=folder, shell=False)
            print(f'Playback started (PID {process.pid}). Close the game when finished.')
    except (OSError, ValueError, struct.error) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
