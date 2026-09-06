import hashlib
import tempfile
import unittest
from pathlib import Path
from replay_analyzer import analyze_replay
from replay_analyzer.demo import build_auxiliary_replay
from tools.kwreplay_inspect import parse_game_command_payload


class WorkspaceTests(unittest.TestCase):
    def test_command_byte_links_redecode_original_bytes(self):
        data = build_auxiliary_replay()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.KWReplay'
            path.write_bytes(data)
            report = analyze_replay(path)
        for index, command in enumerate(report['commands']['items']):
            self.assertEqual(command['index'], index)
            encoded = data[command['wire_offset']:command['wire_offset'] + command['wire_size']]
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), command['wire_sha256'])
            decoded = parse_game_command_payload(b'\x01\x01\x00\x00\x00' + encoded)['messages'][0]
            self.assertEqual(decoded['message_type'], command['message_type'])
            self.assertEqual(decoded['source_index'], command['source_index'])
            record = report['forensics']['records'][command['record_index']]
            self.assertEqual(record['offset'], command['record_offset'])
            self.assertGreaterEqual(command['wire_offset'], record['offset'] + 13)
            self.assertLessEqual(command['wire_offset'] + command['wire_size'], record['offset'] + 13 + record['payload_size'])

    def test_full_camera_samples_keep_incremental_state(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'fixture.KWReplay'
            path.write_bytes(build_auxiliary_replay())
            report = analyze_replay(path)
        samples = report['camera']['samples']
        self.assertEqual(len(samples), report['camera']['path_points_total'])
        self.assertEqual(samples[0]['x'], samples[1]['x'])
        self.assertTrue(samples[1]['position_inherited'])
        self.assertEqual(samples[1]['rotation_quaternion'], [0, 0, 0, 1])
