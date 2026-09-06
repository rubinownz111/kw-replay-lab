"""Exercise Desync Lab with synthetic files, including saved and offline cases."""
import json
import sys
import tempfile
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import app
from replay_analyzer.demo import build_desync_replay, build_desync_capture


def main():
    output = ROOT / '.artifacts'; output.mkdir(exist_ok=True)
    server = make_server('127.0.0.1', 0, app)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    errors = []
    try:
        with sync_playwright() as pw, tempfile.TemporaryDirectory() as directory:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(f'http://127.0.0.1:{server.server_port}')
            page.get_by_role('button', name='Try Desync Lab', exact=True).click()
            expect(page.get_by_role('heading', name='Recorded states disagree')).to_be_visible()
            expect(page.locator('#panel')).to_contain_text('Frames 451-900')
            expect(page.locator('#panel')).to_contain_text('Object 204[1]/Health[1]')
            page.locator('[data-action="desync-checkpoint"]').first.click()
            expect(page.get_by_role('heading', name='Frame 450, epoch 0')).to_be_visible()
            page.get_by_role('button', name='Show commands', exact=True).click()
            expect(page.locator('#range-start')).to_have_value('451')
            expect(page.locator('#range-end')).to_have_value('900')
            expect(page.locator('#data-table tbody tr')).to_have_count(4)
            page.locator('[data-action="inspect"][data-index="2"]').click()
            expect(page.locator('dialog')).to_contain_text('Destination')
            page.locator('dialog').evaluate('(d)=>d.close()')
            page.get_by_role('button', name='Desync Lab', exact=True).click()
            page.locator('[data-action="desync-object"]').first.click()
            expect(page.locator('#object-id')).to_have_value('204')
            page.get_by_role('button', name='Desync Lab', exact=True).click()
            peer = build_desync_replay().replace(b'\xc9\x00\x00\x00', b'\xca\x00\x00\x00')
            # The checksum changes while the declared match identity stays the same.
            page.locator('#desync-peers').set_input_files({'name': 'peer.KWReplay', 'mimeType': 'application/octet-stream', 'buffer': peer})
            expect(page.locator('#panel')).to_contain_text('Session metadata matches')
            expect(page.locator('#panel')).to_contain_text('1 player/checkpoint difference')
            page.locator('[data-action="desync-remove-diagnostic"]').first.click()
            page.locator('[data-action="desync-remove-diagnostic"]').first.click()
            page.locator('#desync-diagnostics').set_input_files([
                {'name': f'capture-{label}-Frame900.bin', 'mimeType': 'application/octet-stream', 'buffer': build_desync_capture(value)}
                for label, value in [('A', 100), ('B', 75)]])
            expect(page.locator('#panel')).to_contain_text('1 differing or missing fields')
            page.locator('#diagnostic-search').fill('Health')
            expect(page.locator('#panel')).to_contain_text('Object 204[1]/Health[1]')
            page.locator('#desync-notes').fill('Checked Object 204. Compare health update next.')
            for action, suffix in [('desync-export', '.md'), ('json', '.json'), ('html', '.html')]:
                with page.expect_download() as event:
                    page.locator(f'[data-action="{action}"]').first.click()
                path = Path(directory) / ('case' + suffix); event.value.save_as(path)
                text = path.read_text(encoding='utf-8')
                assert 'Checked Object 204' in text
                if suffix == '.json':
                    case = json.loads(text)['desync_case']
                    assert len(case['peers']) == 1 and len(case['diagnostics']) == 2
                if suffix == '.html':
                    offline = browser.new_page(); requests = []
                    offline.on('pageerror', lambda error: errors.append(str(error)))
                    offline.on('request', lambda r: requests.append(r.url) if r.url.startswith(('http:', 'https:')) else None)
                    offline.goto(path.as_uri())
                    offline.get_by_role('button', name='Desync Lab', exact=True).click()
                    expect(offline.locator('#desync-notes')).to_have_value('Checked Object 204. Compare health update next.')
                    expect(offline.locator('#panel')).to_contain_text('Object 204[1]/Health[1]')
                    expect(offline.locator('#panel')).to_contain_text('peer.KWReplay')
                    assert not requests, requests
                    offline.close()
            page.get_by_role('button', name='Save case in library', exact=True).click()
            expect(page.locator('#notice')).to_contain_text('Saved in this browser')
            page.reload(); page.get_by_role('button', name='Library', exact=True).click()
            page.get_by_role('button', name='Open', exact=True).click()
            page.get_by_role('button', name='Desync Lab', exact=True).click()
            expect(page.locator('#desync-notes')).to_have_value('Checked Object 204. Compare health update next.')
            for width in (1440, 390):
                page.set_viewport_size({'width': width, 'height': 900})
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2'), 'Overflow'
                page.screenshot(path=str(output / f'desync-{width}.png'), full_page=True)
            page.get_by_role('button', name='Analyze replay', exact=True).click()
            page.locator('#replay-input').set_input_files({'name': 'empty.KWReplay', 'mimeType': 'application/octet-stream', 'buffer': b'bad'})
            expect(page.locator('#notice')).to_have_class('notice error')
            browser.close()
        assert not errors, errors
        result = {'passed': True, 'javascript_errors': errors, 'checks': ['checkpoint_window', 'verified_labels', 'object_context', 'player_comparison', 'native_field_upload_and_diff', 'case_exports', 'offline_without_network', 'saved_case_reload', 'mobile_layout']}
        (output / 'desync-browser-validation.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
        print('Desync Lab browser checks passed.')
    finally:
        server.shutdown(); thread.join(timeout=5)


if __name__ == '__main__':
    main()
