"""Synthetic wire fixtures exercise recovered contracts, not game execution."""
import copy
import io
import json
import struct
import tempfile
import unittest
from pathlib import Path
from app import app, main
from replay_analyzer import analyze_replay
from replay_analyzer.command_semantics import annotate_command
from replay_analyzer.demo import build_desync_replay, build_replay, add_records, body_record
from replay_analyzer.desync import analyze_desync
from replay_analyzer.diagnostics import parse_diagnostic, fold_checksum, DiagnosticError, MAX_BYTES


def tag(name):
    return name.encode().rjust(4, b'\0')[::-1]


def binary_capture(value=100):
    data = bytearray(b'ALAE2STR' + struct.pack('<II', 1, 1))
    data += tag('BLOK') + b'\x0aObject 204'
    boundary = len(data)
    data += b'\0' * 4
    data += tag('DSCR') + b'\x06Health' + tag('real') + struct.pack('<f', value)
    data += tag('DSCR') + b'\xff' + struct.pack('<I', 1) + tag('uint') + struct.pack('<I', 7)
    data += tag('EBLK')
    struct.pack_into('<I', data, boundary, len(data))
    return bytes(data) + tag('END')


def crc(source, value, frame=900, epoch=0, mode=False, flag=False):
    return {'index': 0, 'name': 'MSG_LOGIC_CRC', 'message_type': 609, 'source_index': source,
            'player': str(source), 'frame': frame+3, 'is_action': False, 'category': 'Network',
            'arguments': [{'type': t, 'value': v} for t, v in zip(
                ['integer', 'timestamp', 'timestamp', 'boolean', 'boolean'], [value, epoch, frame, mode, flag])]}


def investigate(*commands):
    for i, c in enumerate(commands):
        c['index'] = i
    report = {'commands': {'items': list(commands)}, 'players': [{'source_index': s, 'name': str(s)} for s in (3, 4)],
              'match': {'last_frame': 1024}, 'summary': {'decode_error_count': 0}, 'network': {}, 'cheat_analysis': {'findings': []}}
    return analyze_desync(report)


class DesyncTests(unittest.TestCase):
    def test_realistic_window_and_typed_labels(self):
        report = app.test_client().get('/api/desync/demo').get_json()
        d = report['desync']
        self.assertEqual((d['status'], d['incidents'][0]['start_frame'], d['incidents'][0]['frame']), ('disagreement', 451, 900))
        self.assertEqual(d['incidents'][0]['command_count'], 2)
        self.assertEqual(d['incidents'][0]['objects'][0]['id'], 204)
        self.assertEqual(report['summary']['decode_error_count'], 0)
        self.assertEqual(report['commands']['items'][0]['arguments'][2]['label'], 'Checkpoint frame')
        self.assertEqual(sum(x['count'] for x in report['commands']['category_counts']), report['summary']['command_count'])

    def test_duplicate_is_not_a_second_player(self):
        p = investigate(crc(3, 1), crc(3, 1))['checkpoints'][0]
        self.assertIsNone(p['agreement'])
        self.assertEqual(p['duplicate_reports'], 1)

    def test_same_source_conflict_not_player_disagreement(self):
        d = investigate(crc(3, 1), crc(3, 2), crc(4, 1))
        self.assertEqual(d['checkpoints'][0]['status'], 'conflicting_reports')
        self.assertEqual(d['coverage']['disagreement_count'], 0)

    def test_epochs_never_merge(self):
        d = investigate(crc(3, 1), crc(4, 2, epoch=1))
        self.assertEqual(len(d['checkpoints']), 2)
        self.assertEqual(d['coverage']['disagreement_count'], 0)

    def test_mixed_modes_are_inconclusive(self):
        self.assertEqual(investigate(crc(3, 1), crc(4, 2, mode=True))['checkpoints'][0]['status'], 'mixed_modes')

    def test_wrong_wire_type_and_future_checkpoint(self):
        c = crc(3, 1); c['arguments'][0] = {'type': 'coord3d', 'value': [1, 2, 3]}
        d = investigate(c, crc(4, 2, frame=1025))
        self.assertEqual(d['coverage']['invalid_crc_count'], 2)
        self.assertEqual(d['checkpoints'], [])

    def test_flag_without_difference(self):
        d = investigate(crc(3, 1, flag=True), crc(4, 1))
        self.assertEqual(d['status'], 'reported_mismatch')
        self.assertEqual(d['incidents'][0]['start_frame'], 0)

    def test_last_matching_must_cover_incident_sources(self):
        d = investigate(crc(3, 1, 450), crc(5, 1, 450), crc(3, 2), crc(4, 3))
        self.assertIsNone(d['incidents'][0]['last_matching_frame'])

    def test_partial_schema_never_labels_wrong_type(self):
        c = {'message_type': 582, 'name': 'MSG_MOVETO', 'arguments': [{'type': 'integer', 'value': 1}]}
        annotate_command(c)
        self.assertEqual(c['semantic_status'], 'shape_mismatch')
        self.assertEqual(c['arguments'][0]['label_status'], 'unknown')

    def test_cli_case_inputs_and_output_collision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            replay, capture, output = root/'a.KWReplay', root/'a.bin', root/'case.json'
            replay.write_bytes(build_desync_replay()); capture.write_bytes(binary_capture())
            args = ['--analyze', str(replay), '--diagnostic', str(capture), '--peer-replay', str(replay)]
            self.assertEqual(main(args + ['--output', str(output)]), 0)
            case = json.loads(output.read_text(encoding='utf-8'))['desync_case']
            self.assertTrue(case['diagnostics'][0]['complete'])
            self.assertEqual(len(case['peers']), 1)
            before = capture.read_bytes()
            self.assertEqual(main(args + ['--output', str(capture)]), 1)
            self.assertEqual(capture.read_bytes(), before)

    def test_index_form_retains_build_request_without_asset_key(self):
        wire = struct.pack('<H', 3 << 11 | 557) + b'\x03' + struct.pack('<I', 204)
        wire += b'\x02\x01\x00' + struct.pack('<II', 7, 12) + b'\x02\x00\x00' + struct.pack('<I', 0) + b'\xff'
        # Two consecutive integers have a run descriptor (0x10).
        wire = wire.replace(b'\x02\x01\x00' + struct.pack('<II', 7, 12), b'\x02\x01\x10' + struct.pack('<II', 7, 12))
        replay = add_records(build_replay(), [body_record(30, 1, b'\x01' + struct.pack('<I', 1) + wire)])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'index.KWReplay'; path.write_bytes(replay)
            report = analyze_replay(path)
        self.assertEqual(report['summary']['decode_error_count'], 0)
        command = report['commands']['items'][-1]
        self.assertEqual(command['arguments'][2]['label'], 'Production index')
        self.assertIsNone(command['asset']['hash'])
        self.assertEqual(report['strategy']['build_order'][-1]['asset']['production_index'], 7)


class DiagnosticTests(unittest.TestCase):
    def test_absolute_block_end_and_shared_description_dictionary(self):
        d = parse_diagnostic(binary_capture(), 'BIN_DESYNC_Frame900.bin')
        self.assertTrue(d['complete'], d['issues'])
        self.assertEqual(d['frame'], 900)
        self.assertEqual([f['path'] for f in d['fields']], ['Object 204[1]/Health[1]', 'Object 204[1]/Health[2]'])
        self.assertEqual([f['value'] for f in d['fields']], [100.0, 7])

    def test_truncation_and_unknown_tag_preserve_partial_result(self):
        for data in (binary_capture()[:-4], binary_capture()[:-4] + tag('nope')):
            d = parse_diagnostic(data, 'capture.bin')
            self.assertFalse(d['complete']); self.assertEqual(len(d['fields']), 2); self.assertTrue(d['issues'])

    def test_invalid_boundary_and_dictionary_are_rejected(self):
        for offset in (31, 62):
            data = bytearray(binary_capture()); data[offset:offset+4] = b'\xff'*4
            self.assertFalse(parse_diagnostic(bytes(data), 'capture.bin')['complete'])

    def test_primitive_wire_values_and_exact_bytes(self):
        data = b'ALAE1STR' + struct.pack('<I', 1)
        data += tag('i64') + struct.pack('<q', 2**62+1) + tag('real') + struct.pack('<I', 0x80000000)
        data += tag('ustr') + b'\x02' + 'AB'.encode('utf-16-le')
        data += tag('enu3') + b'\xff\xff\x7f' + tag('c3d') + struct.pack('<3f', 1, 2, 3)
        data += tag('raw') + struct.pack('<I', 3) + b'abc' + tag('real') + struct.pack('<I', 0x7fc00001) + tag('END')
        d = parse_diagnostic(data, 'capture.bin')
        self.assertTrue(d['complete'], d['issues'])
        self.assertEqual(d['fields'][0]['value'], str(2**62+1))
        self.assertEqual(d['fields'][1]['raw_hex'], '00000080')
        self.assertEqual(d['fields'][2]['value'], 'AB')
        self.assertEqual(d['fields'][3]['value'], 0x7fffff)
        json.dumps(d, allow_nan=False)

    def test_text_fields_settings_and_forced_marker(self):
        text = b'GAME REPORT:\nTHIS IS A FORCED DESYNC\nDesync detected on frame 900\n-deepCRC -netCRCInterval 450\n<Object 204>\nHealth: 100 [real]\n</Object 204>\n'
        d = parse_diagnostic(text, 'DESYNC.txt')
        self.assertTrue(d['complete']); self.assertTrue(d['forced']); self.assertEqual(d['frame'], 900)
        self.assertEqual(d['settings']['netCRCInterval'], '450')
        self.assertIn('Object 204[1]/Health[1]', d['fields'][-1]['path'])
        self.assertFalse(parse_diagnostic(text.replace(b'</Object 204>', b''), 'DESYNC.txt')['complete'])

    def test_version_size_and_untagged_limits(self):
        self.assertFalse(parse_diagnostic(b'GAME REPORT:\nGame version: 1.03', 'x.txt')['complete'])
        self.assertFalse(parse_diagnostic(b'ALAE2STR' + struct.pack('<II', 1, 0), 'x.bin')['complete'])
        with self.assertRaises(DiagnosticError): parse_diagnostic(b'0'*(MAX_BYTES+1), 'x.txt')

    def test_transfer_boundaries_affect_checksum(self):
        self.assertEqual(fold_checksum(0x80000000, b'\x01\0\0\0'), 2)
        self.assertNotEqual(fold_checksum(0, b'\x01\x02\x03\x04'), fold_checksum(fold_checksum(0, b'\x01'), b'\x02\x03\x04'))

    def test_api_mixed_uploads_and_count_limit(self):
        client = app.test_client()
        response = client.post('/api/desync/diagnostics', data={'diagnostics': [(io.BytesIO(binary_capture()), 'a.bin'), (io.BytesIO(b'x'), 'a.exe')]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json['diagnostics']), 1); self.assertEqual(len(response.json['errors']), 1)
        self.assertEqual(client.post('/api/desync/diagnostics').status_code, 400)


if __name__ == '__main__':
    unittest.main()
