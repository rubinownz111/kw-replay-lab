"""Exercise the built TypeScript UI, including offline exports and persistence."""
from pathlib import Path
import argparse
import json
import sys
import tempfile
from threading import Thread

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from playwright.sync_api import sync_playwright, expect
from werkzeug.serving import make_server
from app import app
from replay_analyzer.demo import build_auxiliary_replay, build_replay


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', help='Use an existing Replay Lab server.')
    args = parser.parse_args()
    server = None
    if not args.url:
        server = make_server('127.0.0.1', 0, app)
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
    url = args.url or f'http://127.0.0.1:{server.server_port}'
    output = ROOT / '.artifacts'
    output.mkdir(exist_ok=True)
    errors = []
    try:
        with sync_playwright() as playwright, tempfile.TemporaryDirectory() as directory:
            browser = playwright.chromium.launch(headless=True)
            page = browser.new_page(viewport={'width':1486,'height':1058}, color_scheme='light')
            page.on('pageerror', lambda error: errors.append(str(error)))
            page.goto(url)
            expect(page.get_by_role('heading', name='Inspect your replay.')).to_be_visible()
            page.screenshot(path=str(output/'replay-landing.png'), full_page=True)
            page.get_by_role('button', name='Explore a synthetic demo').click()
            expect(page.get_by_role('heading', name='Synthetic 1.02 stream demo')).to_be_visible()
            tabs = page.locator('[data-action="tab"]').all()
            for tab in tabs:
                tab.click()
                expect(tab).to_have_attribute('aria-current','page')
                expect(page.locator('#panel')).to_be_visible()
            page.get_by_role('button', name='Overview', exact=True).click()
            page.screenshot(path=str(output/'replay-report.png'),full_page=True)
            page.get_by_role('button', name='Analyze replay', exact=True).click()
            page.locator('#replay-input').set_input_files({'name':'broken.KWReplay','mimeType':'application/octet-stream','buffer':b'bad'})
            expect(page.locator('#notice')).to_have_class('notice error')
            data=build_auxiliary_replay()
            page.locator('#replay-input').set_input_files({'name':'valid.KWReplay','mimeType':'application/octet-stream','buffer':data})
            expect(page.get_by_role('heading', name='Synthetic 1.02 stream demo')).to_be_visible()
            page.get_by_role('button', name='Commands', exact=True).click()
            expect(page.locator('#data-table tbody tr')).to_have_count(2)
            page.locator('#search').fill('MOVETO')
            expect(page.locator('#data-table tbody tr')).to_have_count(1)
            page.locator('#search').fill('')
            page.locator('[data-action="inspect"]').first.click()
            expect(page.locator('dialog')).to_be_visible()
            expect(page.locator('#raw-bytes')).not_to_contain_text('Loading')
            expect(page.locator('#raw-bytes')).not_to_contain_text('not included')
            page.get_by_role('button',name='Find references').click()
            expect(page.locator('#object-id')).to_have_value('204')
            expect(page.locator('#data-table tbody tr')).to_have_count(1)
            page.locator('#object-id').fill('999')
            expect(page.locator('#data-table')).to_contain_text('No matching records')
            page.get_by_role('button', name='Commands', exact=True).click()
            page.locator('#cursor').fill('29')
            expect(page.locator('#data-table')).to_contain_text('No matching records')
            page.get_by_role('button', name='Full match',exact=True).click()
            expect(page.locator('#data-table tbody tr')).to_have_count(2)
            page.get_by_role('button', name='Camera',exact=True).click()
            expect(page.locator('#camera-plot svg')).to_be_visible()
            page.locator('#camera-mode').select_option('density')
            expect(page.locator('#camera-plot')).to_contain_text('Darker cells')
            page.get_by_role('button', name='Save to library',exact=True).click()
            expect(page.locator('#notice')).to_contain_text('Saved in this browser')
            page.get_by_role('button', name='Save to library',exact=True).click()
            expect(page.locator('#notice')).to_contain_text('Existing library entry updated')
            page.get_by_role('button', name='Library',exact=True).click()
            expect(page.locator('#library-rows tbody tr')).to_have_count(1)
            page.locator('#library-date').fill('2026-07-27')
            expect(page.get_by_role('button',name='Open',exact=True)).to_be_visible()
            page.locator('#library-date').fill('2025-01-01')
            expect(page.locator('#library-rows')).to_contain_text('No matching records')
            page.locator('#library-date').fill('')
            page.locator('#library-import').set_input_files([{'name':'duplicate.KWReplay','mimeType':'application/octet-stream','buffer':data},{'name':'invalid.KWReplay','mimeType':'application/octet-stream','buffer':b'bad'}])
            expect(page.locator('#notice')).to_contain_text('invalid.KWReplay:')
            expect(page.locator('#library-rows tbody tr')).to_have_count(1)
            page.reload()
            page.get_by_role('button', name='Library',exact=True).click()
            expect(page.get_by_role('button',name='Open',exact=True)).to_be_visible()
            page.get_by_role('button',name='Open',exact=True).click()
            page.get_by_role('button',name='Compare',exact=True).click()
            page.locator('#compare-input').set_input_files({'name':'changed.KWReplay','mimeType':'application/octet-stream','buffer':build_replay(include_attack=True)})
            expect(page.locator('#panel')).to_contain_text('Sequence index 2')
            expect(page.locator('#panel')).to_contain_text('Different or unconfirmed session')
            for action,extension in [('json','.json'),('commands-csv','.csv'),('records-csv','.csv'),('html','.html')]:
                with page.expect_download() as event:
                    page.locator(f'[data-action="{action}"]').click()
                download=event.value
                assert download.suggested_filename.endswith(extension)
                path=Path(directory)/download.suggested_filename
                download.save_as(path)
                assert path.stat().st_size>50
                if extension=='.html':
                    offline=browser.new_page()
                    offline.on('pageerror',lambda e:errors.append(str(e)))
                    requests=[]
                    offline.on('request',lambda req:requests.append(req.url) if req.url.startswith(('http:','https:')) else None)
                    offline.goto(path.as_uri())
                    expect(offline.get_by_role('heading',name='Synthetic 1.02 stream demo')).to_be_visible()
                    offline.get_by_role('button',name='Commands',exact=True).click()
                    expect(offline.locator('#data-table tbody tr')).to_have_count(2)
                    offline.locator('[data-action="inspect"]').first.click()
                    expect(offline.locator('#raw-bytes')).to_contain_text('not included')
                    assert requests==[],requests
                    offline.close()
            page.get_by_role('button', name='Overview',exact=True).click()
            for theme in ['light','dark']:
                for _ in range(3):
                    if page.locator('[data-action="theme"]').inner_text()==f'Theme: {theme}':break
                    page.locator('[data-action="theme"]').click()
                expect(page.locator('.lab')).to_have_attribute('data-theme',theme)
                page.screenshot(path=str(output/f'replay-{theme}.png'),full_page=True)
            page.set_viewport_size({'width':390,'height':844})
            for tab in page.locator('[data-action="tab"]').all():
                tab.click()
                assert page.evaluate('document.documentElement.scrollWidth <= innerWidth+2'),'Mobile overflow'
            page.get_by_role('button',name='Overview',exact=True).click()
            page.screenshot(path=str(output/'replay-mobile.png'),full_page=True)
            page.get_by_role('button',name='Analyze replay',exact=True).click()
            hostile=data.replace('Demo player'.encode('utf-16-le'),'<img onerror=bad()>'.encode('utf-16-le'))
            page.locator('#replay-input').set_input_files({'name':'markup.KWReplay','mimeType':'application/octet-stream','buffer':hostile})
            expect(page.get_by_role('heading',name='Synthetic 1.02 stream demo')).to_be_visible()
            assert page.locator('img[onerror]').count()==0
            page.get_by_role('button',name='Library',exact=True).click()
            page.get_by_role('button',name='Remove',exact=True).click()
            expect(page.locator('#library-rows')).to_contain_text('No matching records')
            browser.close()
        assert not errors,errors
        checks=['all_14_tabs','malformed_upload_recovery','command_filters','byte_inspector','typed_object_search','synchronized_time_range','camera_density','library_save_duplicate_reload_remove','replay_comparison','json_csv_exports','portable_html_without_network','light_dark_themes','mobile_all_tabs','html_escaping']
        (output/'browser-validation.json').write_text(json.dumps({'passed':True,'url':url,'javascript_errors':errors,'checks':checks},indent=2),encoding='utf-8')
        print('Browser workspace checks passed: '+', '.join(checks))
    finally:
        if server:
            server.shutdown()
            thread.join(timeout=5)

if __name__=='__main__':main()
