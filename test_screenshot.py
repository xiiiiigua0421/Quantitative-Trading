from playwright.sync_api import sync_playwright

def run():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto('file:///app/reports/demo/report.html')
        page.screenshot(path='screenshot.png', full_page=True)
        # Test focus on .chart-wrap
        page.evaluate("document.querySelector('.chart-wrap').focus()")
        page.screenshot(path='screenshot_chart_focus.png', full_page=True)
        browser.close()

if __name__ == '__main__':
    run()
