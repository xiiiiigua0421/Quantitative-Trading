from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto('file:///app/reports/demo/report.html')
        # Test focus on .scroll
        page.evaluate("document.querySelector('.scroll').focus()")
        page.screenshot(path='screenshot_scroll_focus.png', full_page=True)
        browser.close()

if __name__ == '__main__':
    run()
