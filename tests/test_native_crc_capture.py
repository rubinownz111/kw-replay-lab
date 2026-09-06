import hashlib
import struct
import tempfile
import unittest
from pathlib import Path

from scripts.native_crc_capture import build, capture_config, prepare, settings


def pe_fixture():
    data = bytearray(1024)
    struct.pack_into('<I', data, 60, 128)
    data[128:132] = b'PE\0\0'
    struct.pack_into('<HH', data, 132, 0x14c, 1)
    struct.pack_into('<H', data, 148, 224)
    opt = 152
    struct.pack_into('<H', data, opt, 0x10b)
    struct.pack_into('<I', data, opt + 16, 0x1010)
    struct.pack_into('<III', data, opt + 28, 0x400000, 4096, 512)
    struct.pack_into('<I', data, opt + 60, 512)
    struct.pack_into('<8sIIII', data, opt + 224, b'.text', 512, 4096, 512, 512)
    return bytes(data)


class NativeCaptureTests(unittest.TestCase):
    def test_stub_writes_settings_and_returns_to_original_entry(self):
        with tempfile.TemporaryDirectory() as temp:
            source, output = Path(temp) / 'stock.dat', Path(temp) / 'copy.dat'
            original = pe_fixture()
            source.write_bytes(original)
            manifest = settings(7650)
            manifest['input_sha256'] = hashlib.sha256(original).hexdigest()
            receipt = build(source, output, manifest)
            data = output.read_bytes()
            self.assertEqual(source.read_bytes(), original)
            self.assertEqual(data[512:1024], original[512:1024])
            self.assertEqual(struct.unpack_from('<I', data, 168)[0], 0x2000)
            stub = bytes.fromhex(receipt['stub_hex'])
            self.assertEqual(stub[:9], b'\x9c\x60\xe8\0\0\0\0\x58\x2d')
            # Simulate relocation: the call/pop must recover the actual module base.
            loaded_base = 0x500000
            recovered_base = loaded_base + 0x2007 - struct.unpack_from('<I', stub, 9)[0]
            self.assertEqual(recovered_base, loaded_base)
            pos, writes = 13, {}
            while stub[pos] in (0xc6, 0xc7):
                self.assertEqual(stub[pos + 1], 0x80)
                width = 1 if stub[pos] == 0xc6 else 4
                offset = struct.unpack_from('<I', stub, pos + 2)[0]
                writes[recovered_base + offset] = int.from_bytes(stub[pos + 6:pos + 6 + width], 'little')
                pos += 6 + width
            self.assertEqual(writes, {0xce4f42: 1, 0xce4f45: 1, 0xc6fbb4: 7650, 0xce4f48: 134217728})
            self.assertEqual(stub[pos:pos + 3], b'\x61\x9d\xe9')
            destination = 0x2000 + pos + 7 + struct.unpack_from('<i', stub, pos + 3)[0]
            self.assertEqual(destination, 0x1010)
            with self.assertRaises(ValueError):
                build(source, output, manifest)
            with self.assertRaises(ValueError):
                build(source, source, manifest)

    def test_rejects_wrong_build_before_creating_output(self):
        with tempfile.TemporaryDirectory() as temp:
            root, output = Path(temp) / 'game', Path(temp) / 'capture'
            source = root / 'RetailExe/1.2/cnc3ep1.dat'
            source.parent.mkdir(parents=True)
            source.write_bytes(pe_fixture())
            with self.assertRaisesRegex(ValueError, 'Unsupported executable'):
                prepare(root, output, 450)
            self.assertFalse(output.exists())
            with self.assertRaisesRegex(ValueError, 'outside'):
                prepare(root, root / 'capture', 450)

    def test_config_preserves_overlay_order_and_resolves_stock_paths(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('Lang-english/1.2', 'EnglishAudio/1.2', 'Core/1.2', 'Meta/1.2', 'Movies/1.0'):
                folder = root / name
                folder.mkdir(parents=True)
                (folder / 'config.txt').write_text('add-big stock.big\n')
                (folder / 'stock.big').write_bytes(b'fixture')
            overlay = root / 'mod.big'
            overlay.write_bytes(b'fixture')
            text = capture_config(root, root / 'output', [overlay]).decode('ascii')
            self.assertEqual(text.splitlines()[0], f'add-big {overlay.resolve()}')
            self.assertEqual(text.count('add-big '), 6)
            (root / 'Core/1.2/config.txt').write_text('add-config config.txt\n')
            with self.assertRaisesRegex(ValueError, 'cycle'):
                capture_config(root, root / 'output', [])

    def test_invalid_frames(self):
        for frame in (-1, 0, 3, 2147483648):
            with self.assertRaises(ValueError):
                settings(frame)


if __name__ == '__main__':
    unittest.main()
