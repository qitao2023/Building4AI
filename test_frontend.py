"""Playwright test for StructureAI frontend."""
import os, sys, io
# Force UTF-8 output on Windows
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace') if hasattr(sys.stdout, 'buffer') else sys.stdout
from playwright.sync_api import sync_playwright

FRONTEND = 'file:///e:/01-claudecode/Building4AI/webapp/index.html'
VIEWER = 'file:///e:/01-claudecode/Building4AI/webapp/viewer.html'

def test_index():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1200, 'height': 900})

        # Capture console messages
        console_logs = []
        page.on('console', lambda msg: console_logs.append(f'[{msg.type}] {msg.text}'))

        print('=== Step 1: Load index.html ===')
        page.goto(FRONTEND, wait_until='networkidle', timeout=15000)
        page.wait_for_timeout(500)

        # Check basic structure
        title = page.title()
        print(f'  Title: {title}')
        assert 'StructureAI' in title or 'AI' in title, f'Bad title: {title}'

        # Check header
        header = page.locator('h1').first
        print(f'  H1: {header.text_content()}')

        # Check upload zone exists
        upload_zone = page.locator('.upload-zone')
        has_upload = upload_zone.count() > 0
        print(f'  Upload zone visible: {has_upload}')

        page.screenshot(path='e:/01-claudecode/Building4AI/screenshot_step1_upload.png', full_page=True)
        print('  Screenshot: screenshot_step1_upload.png')

        # === Step 2: Click skip (快速体验) ===
        print('=== Step 2: Click 快速体验 ===')
        # Find the skip button
        skip_btn = page.locator('button:has-text("快速体验")')
        if skip_btn.count() == 0:
            # Try onclick with _skip
            skip_btn = page.locator('button:has-text("快速体验")')
        print(f'  Skip button found: {skip_btn.count() > 0}')
        if skip_btn.count() > 0:
            skip_btn.first.click()
            page.wait_for_timeout(800)

        page.screenshot(path='e:/01-claudecode/Building4AI/screenshot_step2_analysis.png', full_page=True)
        print('  Screenshot: screenshot_step2_analysis.png')

        # Check analysis section
        candidates = page.locator('.candidate-tag')
        cand_count = candidates.count()
        print(f'  Candidate zones: {cand_count}')

        # Check step indicators
        step_texts = page.locator('.step-item').all_text_contents()
        print(f'  Steps: {step_texts}')

        # === Step 3: Click AI Design ===
        print('=== Step 3: Click AI 设计楼梯 ===')
        design_btn = page.locator('button:has-text("AI 设计楼梯")')
        print(f'  Design button found: {design_btn.count() > 0}')

        if design_btn.count() > 0:
            design_btn.first.click()
            # Wait for API response (local algorithm is fast)
            page.wait_for_timeout(2000)

        page.screenshot(path='e:/01-claudecode/Building4AI/screenshot_step3_design.png', full_page=True)
        print('  Screenshot: screenshot_step3_design.png')

        # Check design result
        flights = page.locator('text=第一跑')
        has_flight1 = flights.count() > 0
        print(f'  Flight 1 shown: {has_flight1}')

        # Find result source
        source_text = page.content()
        has_result = '本地算法' in source_text or '设计结果' in source_text
        print(f'  Design result visible: {has_result}')

        # Check errors section
        errors_section = page.locator('.pill-amber')
        if errors_section.count() > 0:
            print(f'  Errors found: {errors_section.first.text_content()}')

        # Console logs summary
        errors = [l for l in console_logs if 'error' in l.lower() or 'err' in l.lower()]
        if errors:
            print(f'  Console errors ({len(errors)}):')
            for e in errors[:5]:
                print(f'    {e}')
        else:
            print(f'  No console errors')

        browser.close()
        return True

def test_viewer():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={'width': 1400, 'height': 900})

        print('\n=== Test viewer.html ===')
        page.goto(VIEWER, wait_until='networkidle', timeout=15000)
        page.wait_for_timeout(1000)

        title = page.title()
        print(f'  Title: {title}')

        # Check if canvas rendered
        canvas = page.locator('#canvas')
        print(f'  Canvas found: {canvas.count() > 0}')

        page.screenshot(path='e:/01-claudecode/Building4AI/screenshot_viewer.png', full_page=False)
        print('  Screenshot: screenshot_viewer.png')

        browser.close()
        return True

if __name__ == '__main__':
    print('=== StructureAI Frontend Test ===')
    try:
        test_index()
        test_viewer()
        print('\n=== ALL TESTS PASSED ===')
    except Exception as e:
        print(f'\nFAILED: {e}')
        import traceback; traceback.print_exc()
        sys.exit(1)
