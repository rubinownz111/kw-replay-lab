"""Current replay survives navigation, reload and explicit library operations."""
import json
import sys
from pathlib import Path
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import app
from replay_analyzer.demo import build_desync_replay, build_desync_capture


def main():
    server = make_server('127.0.0.1', 0, app)
    thread = Thread(target=server.serve_forever, daemon=True); thread.start()
    errors, uploads = [], []
    output = ROOT / '.artifacts'; output.mkdir(exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width': 1440, 'height': 1000})
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.on('request', lambda request: uploads.append(request.url) if request.url.endswith('/api/analyze') else None)
            base = f'http://127.0.0.1:{server.server_port}'
            page.goto(base + '/api/health')
            legacy = app.test_client().get('/api/demo').get_json()
            legacy['file']['name'] = 'legacy-library.KWReplay'
            page.evaluate('''report=>new Promise((resolve,reject)=>{
                const request=indexedDB.open('kw-replay-lab-v1',1);
                request.onupgradeneeded=()=>request.result.createObjectStore('replays',{keyPath:'hash'});
                request.onsuccess=()=>{const db=request.result,tx=db.transaction('replays','readwrite');
                    tx.objectStore('replays').put({hash:report.file.sha256,report,savedAt:new Date().toISOString(),bytes:1024});
                    tx.oncomplete=()=>{db.close();resolve(true);};tx.onerror=()=>reject(tx.error);};
                request.onerror=()=>reject(request.error);
            })''', legacy)
            page.goto(base)
            expect(page.locator('#replay-input')).to_be_enabled()
            page.get_by_role('button', name='Library', exact=True).click()
            expect(page.locator('#library-rows')).to_contain_text('legacy-library.KWReplay')
            page.get_by_role('button', name='Remove', exact=True).click()
            expect(page.locator('#library-rows')).to_contain_text('No matching records')
            page.get_by_role('button', name='Analyze replay', exact=True).click()
            page.locator('#replay-input').set_input_files({'name': 'keep-me.KWReplay', 'mimeType': 'application/octet-stream', 'buffer': build_desync_replay()})
            expect(page.locator('#session-status')).to_contain_text('until you close it')
            page.get_by_role('button', name='Desync Lab', exact=True).click()
            page.locator('#desync-notes').fill('Keep these investigation notes.')
            page.locator('#desync-diagnostics').set_input_files([
                {'name': f'capture-{name}-Frame900.bin', 'mimeType': 'application/octet-stream', 'buffer': build_desync_capture(value)}
                for name, value in [('A', 100), ('B', 75)]])
            expect(page.get_by_role('heading', name='First differing captured field')).to_be_visible()
            expect(page.locator('#busy')).to_be_hidden()
            page.locator('[data-action="desync-object"]').first.click()
            expect(page.locator('#object-id')).to_have_value('204')
            expect(page.get_by_role('button', name='Back to Desync Lab', exact=True)).to_be_visible()
            page.go_back()
            expect(page.get_by_role('button', name='Desync Lab', exact=True)).to_have_attribute('aria-current', 'page')
            expect(page.locator('#desync-notes')).to_have_value('Keep these investigation notes.')
            page.go_forward()
            expect(page.locator('#object-id')).to_have_value('204')
            expect(page.locator('#range-start')).to_have_value('451')
            page.reload()
            expect(page.locator('#object-id')).to_have_value('204')
            expect(page.locator('#range-start')).to_have_value('451')
            page.get_by_role('button', name='Back to Desync Lab', exact=True).click()
            expect(page.locator('#desync-notes')).to_have_value('Keep these investigation notes.')
            expect(page.get_by_role('heading', name='First differing captured field')).to_be_visible()
            # An immediate reload also retains the last typed notes.
            page.locator('#desync-notes').fill('Notes entered just before refresh.')
            page.reload()
            expect(page.locator('#desync-notes')).to_have_value('Notes entered just before refresh.')
            page.get_by_role('button', name='Show commands', exact=True).click()
            page.locator('[data-action="inspect"][data-index="2"]').click()
            expect(page.locator('#raw-bytes')).not_to_contain_text('not included')
            expect(page.locator('#raw-bytes')).not_to_contain_text('Loading')
            page.get_by_role('button', name='Close', exact=True).click()
            page.get_by_role('button', name='Analyze replay', exact=True).click()
            expect(page.locator('.active-replay')).to_contain_text('keep-me.KWReplay')
            page.reload()
            page.get_by_role('button', name='Resume replay', exact=True).click()
            expect(page.locator('#data-table tbody tr')).to_have_count(4)
            page.get_by_role('button', name='Save to library', exact=True).click()
            expect(page.locator('#notice')).to_contain_text('Saved in this browser')
            page.get_by_role('button', name='Library', exact=True).click()
            page.get_by_role('button', name='Remove', exact=True).click()
            expect(page.locator('#library-rows')).to_contain_text('No matching records')
            page.get_by_role('button', name='Resume replay', exact=True).click()
            expect(page.locator('#data-table tbody tr')).to_have_count(4)
            page.get_by_role('button', name='Desync Lab', exact=True).click()
            page.set_viewport_size({'width': 390, 'height': 844})
            page.locator('[data-action="desync-object"]').first.click()
            expect(page.get_by_role('button', name='Back to Desync Lab', exact=True)).to_be_visible()
            assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2')
            page.screenshot(path=str(output / 'navigation-mobile.png'), full_page=True)
            page.get_by_role('button', name='Close replay', exact=True).click()
            expect(page.get_by_role('heading', name='Inspect your replay.')).to_be_visible()
            page.go_back()
            expect(page.get_by_role('heading', name='Inspect your replay.')).to_be_visible()
            page.reload()
            expect(page.locator('#replay-input')).to_be_enabled()
            expect(page.get_by_role('button', name='Resume replay', exact=True)).to_have_count(0)
            assert len(uploads) == 1, uploads
            # If the browser refuses persistence, navigation must still work and explain the limit.
            blocked = browser.new_context()
            blocked.add_init_script("IDBObjectStore.prototype.put=function(){throw new DOMException('Full','QuotaExceededError');}")
            limited = blocked.new_page(); limited.on('pageerror', lambda error: errors.append(str(error)))
            limited.goto(base)
            limited.get_by_role('button', name='Try Desync Lab', exact=True).click()
            expect(limited.locator('#session-status')).to_contain_text('recovery is unavailable')
            limited.locator('[data-action="desync-object"]').first.click()
            limited.get_by_role('button', name='Back to Desync Lab', exact=True).click()
            expect(limited.get_by_role('heading', name='Recorded states disagree')).to_be_visible()
            blocked.close()
            browser.close()
        assert not errors, errors
        (output / 'navigation-browser-validation.json').write_text(json.dumps({'passed': True, 'uploads': len(uploads), 'javascript_errors': errors, 'checks': ['existing_library_migration', 'browser_back_forward', 'context_back_button', 'reload_preserves_view_range_object_and_bytes', 'capture_and_note_recovery', 'resume_from_upload_and_library', 'library_removal_keeps_active_replay', 'explicit_close_clears_recovery', 'storage_failure_keeps_live_navigation', 'mobile_navigation']}, indent=2), encoding='utf-8')
        print('Replay navigation and persistence checks passed with one upload.')
    finally:
        server.shutdown(); thread.join(timeout=5)


if __name__ == '__main__':
    main()
