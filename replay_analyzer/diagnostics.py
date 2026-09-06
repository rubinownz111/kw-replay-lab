"""Bounded readers for native 1.02 desync text and tagged Xfer captures."""
import hashlib
import math
import re
import struct
from collections import Counter

MAX_BYTES = 8 * 1024 * 1024
MAX_FIELDS = 20000


class DiagnosticError(ValueError):
    pass


class Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, size):
        if size < 0 or self.pos + size > len(self.data):
            raise DiagnosticError(f'Truncated data at byte {self.pos}.')
        value = self.data[self.pos:self.pos + size]
        self.pos += size
        return value

    def integer(self, size=4):
        return int.from_bytes(self.take(size), 'little')

    def string(self, wide=False):
        size = self.integer(1)
        if size == 255:
            size = self.integer()
        data = self.take(size * (2 if wide else 1))
        return data.decode('utf-16-le' if wide else 'cp1252', errors='replace')


def fold_checksum(checksum, data):
    """0x859782: each transfer folds dwords, then remaining bytes, modulo 2^32."""
    at = 0
    while at + 4 <= len(data):
        value = int.from_bytes(data[at:at + 4], 'little')
        checksum = (value + (checksum >> 31) + 2 * checksum) & 0xffffffff
        at += 4
    for value in data[at:]:
        checksum = (value + (checksum >> 31) + 2 * checksum) & 0xffffffff
    return checksum


def parse_diagnostic(data, name):
    if len(data) > MAX_BYTES:
        raise DiagnosticError('Diagnostic file exceeds 8 MiB.')
    name = name.replace('\\', '/').split('/')[-1]
    if not name.lower().endswith(('.txt', '.bin')):
        raise DiagnosticError('Choose a DESYNC .txt or BIN_DESYNC .bin file.')
    result = {'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data),
              'kind': 'binary' if name.lower().endswith('.bin') else 'text', 'fields': [],
              'issues': [], 'complete': False, 'forced': False, 'frame': None, 'settings': {},
              'association': 'User-supplied capture; executable and replay association are not authenticated.'}
    match = re.search(r'Frame[-_ ]?(\d+)', name, re.I)
    if match:
        result['frame'] = int(match[1])
    try:
        if result['kind'] == 'binary':
            _binary(data, result)
        else:
            _text(data, result)
    except (DiagnosticError, UnicodeError, struct.error) as exc:
        result['issues'].append(str(exc))
    return result


def _binary(data, result):
    r = Reader(data)
    if r.take(4) != b'ALAE':
        raise DiagnosticError('Not a native tagged Xfer stream (missing ALAE header).')
    version = r.take(4)
    if version not in (b'1STR', b'2STR'):
        raise DiagnosticError('Unsupported Xfer stream header.')
    result['format_version'] = r.integer() if version == b'2STR' else 0
    flags = r.integer()
    if flags != 1:
        raise DiagnosticError('This capture has no supported field tags. Use a tagged deep-CRC capture.')
    descriptions, stack, counts = [], [], Counter()
    pending = ''

    def description():
        length = r.integer(1)
        if length == 255:
            index = r.integer()
            if index >= len(descriptions):
                raise DiagnosticError(f'Invalid description reference at byte {r.pos - 4}.')
            return descriptions[index]
        text = r.take(length).decode('cp1252', errors='replace')
        if length:
            descriptions.append(text)
        return text

    formats = {'byte': 'b', 'ubyt': 'B', 'bool': 'B', 'shrt': 'h', 'usht': 'H',
               'int': 'i', 'uint': 'I', 'i64': 'q', 'real': 'f', 'ver': 'B',
               'c2d': '2f', 'c3d': '3f', 'ic2d': '2i', 'ic3d': '3i',
               'r2d': '4f', 'r3d': '6f', 'ir2d': '4i', 'ir3d': '6i',
               'rnge': '2f', 'rgb': '3f', 'rgbr': '4f', 'rgbi': '4i'}
    while r.pos < len(data):
        if len(result['fields']) >= MAX_FIELDS:
            raise DiagnosticError(f'Display limit reached ({MAX_FIELDS} fields). Remaining data is not decoded.')
        offset = r.pos
        tag = r.take(4)[::-1].strip(b'\0').decode('ascii', errors='replace')
        if tag == 'END':
            if stack or pending or r.pos != len(data):
                raise DiagnosticError('END has an open block, pending description or trailing data.')
            result['complete'] = True
            return
        if tag == 'BLOK':
            label = description() or 'Unnamed block'
            end = r.integer()  # 0x858DF3 writes an absolute end position, not a length.
            if end < r.pos + 4 or end > len(data) or (stack and end > stack[-1][1]):
                raise DiagnosticError(f'Invalid block boundary at byte {offset}.')
            if len(stack) >= 64:
                raise DiagnosticError('Block nesting exceeds 64 levels.')
            prefix = '/'.join(x[0] for x in stack)
            key = (prefix, label)
            counts[key] += 1
            stack.append((f'{label}[{counts[key]}]', end))
            pending = ''
            continue
        if tag == 'EBLK':
            if not stack or stack[-1][1] != r.pos:
                raise DiagnosticError(f'Block end does not match its boundary at byte {offset}.')
            stack.pop()
            pending = ''
            continue
        if stack and r.pos >= stack[-1][1]:
            raise DiagnosticError(f'Missing block end at byte {offset}.')
        if tag == 'DSCR':
            pending = description()
            continue
        start = r.pos
        if tag in formats:
            fmt = '<' + formats[tag]
            raw = r.take(struct.calcsize(fmt))
            values = struct.unpack(fmt, raw)
            values = [str(x) if isinstance(x, float) and not math.isfinite(x) else x for x in values]
            value = values[0] if len(values) == 1 else values
            if tag == 'i64':
                value = str(value)  # Preserve all 64 bits in JavaScript consumers.
        elif tag in ('astr', 'ustr'):
            value = r.string(tag == 'ustr')
        elif tag.startswith('enu') and tag[-1:] in '1234':
            value = r.integer(int(tag[-1]))
        elif tag == 'raw':
            size = r.integer()
            raw = r.take(size)
            value = {'bytes': size, 'sha256': hashlib.sha256(raw).hexdigest(), 'prefix': raw[:32].hex()}
        else:
            raise DiagnosticError(f'Unknown tag {tag!r} at byte {offset}; decoding stopped.')
        if stack and r.pos > stack[-1][1] - 4:
            raise DiagnosticError(f'Field overlaps its block end at byte {offset}.')
        path = '/'.join([x[0] for x in stack] + [pending or tag])
        counts[('field', path)] += 1
        path += f'[{counts[("field", path)]}]'
        raw = data[start:r.pos]
        result['fields'].append({'path': path, 'label': pending or tag, 'type': tag,
                                 'value': value, 'offset': offset, 'size': r.pos - offset,
                                 'raw_hex': raw[:64].hex(), 'value_sha256': hashlib.sha256(raw).hexdigest()})
        pending = ''
    raise DiagnosticError('Stream ended without END.')


def _text(data, result):
    text = data.decode('utf-16') if data.startswith((b'\xff\xfe', b'\xfe\xff')) else data.decode('utf-8-sig', errors='replace')
    if not re.search(r'GAME REPORT:|REPLAY FILE|CLIENT_DESYNC|Desync detected on frame|FAKE DESYNC|FORCED DESYNC', text, re.I):
        raise DiagnosticError('No native desync-report markers found.')
    if re.search(r'(?:game|engine)\s*version\s*[:=]\s*1\.(?:3|03)\b', text, re.I):
        raise DiagnosticError('This report declares an unsupported game version.')
    result['forced'] = bool(re.search(r'THIS IS A (?:FAKE|FORCED) DESYNC', text))
    frame = re.search(r'(?:Frame #|Desync detected on frame\s+)(\d+)', text, re.I)
    if frame:
        result['frame'] = int(frame[1])
    for key in ['deepCRC', 'liteCRC', 'binaryDeepCRC', 'verifyClientCRC', 'netCRCInterval', 'forceDesyncOnFrame']:
        match = re.search(r'-' + key + r'\b(?:[ \t]+(-?\d+))?', text, re.I)
        if match:
            result['settings'][key] = match[1] or True
    map_name = re.search(r'Map Name:\s*([^\r\n]+)', text)
    if map_name:
        result['settings']['map_name'] = map_name[1].strip()
    stack, seen, section, command = [], Counter(), 'Report', ''
    for line_number, line in enumerate(text.splitlines(), 1):
        if len(result['fields']) >= MAX_FIELDS:
            raise DiagnosticError(f'Display limit reached ({MAX_FIELDS} fields).')
        value = line.strip()
        if not value or set(value) <= {'-', '='}:
            continue
        if len(value) > 8192:
            raise DiagnosticError(f'Line {line_number} exceeds 8,192 characters.')
        if value in ('GAME REPORT:', 'REPLAY FILE'):
            section, command = value.rstrip(':'), ''
        match = re.match(r'Frame:(\d+),\s*(.+?)\((\d+)\):', value)
        if match:
            command = f'Frame {match[1]}/{match[2]}({match[3]})'
        opening = re.fullmatch(r'<([^<>]+)>', value)
        if opening:
            label = opening[1]
            if label.startswith('/'):
                if not stack or stack[-1].rsplit('[', 1)[0] != label[1:]:
                    raise DiagnosticError(f'Unmatched block at line {line_number}.')
                stack.pop()
            else:
                if len(stack) >= 64:
                    raise DiagnosticError('Block nesting exceeds 64 levels.')
                key = '/'.join(stack + [label])
                seen[key] += 1
                stack.append(f'{label}[{seen[key]}]')
            continue
        label = value.split(':', 1)[0] if ':' in value else 'Line'
        path = '/'.join([section] + stack + ([command] if command and not stack else []) + [label])
        seen[path] += 1
        path += f'[{seen[path]}]'
        result['fields'].append({'path': path, 'label': label, 'type': 'text', 'value': value,
                                 'line': line_number, 'value_sha256': hashlib.sha256(value.encode()).hexdigest()})
    if stack:
        raise DiagnosticError('Text ends inside a block; the report may be incomplete.')
    result['complete'] = True
