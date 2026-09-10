"""调试: 查找页面上的 Print 按钮, 点击后捕获弹出的内容。"""
import os, sys, time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

try:
    from dotenv import load_dotenv
except ImportError:
    def load_dotenv(path):
        if not os.path.exists(path): return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line: continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

load_dotenv(BASE_DIR / ".dowjones.env")

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
from dowjones_screening import (
    login, open_advanced_search, select_search_type,
    fill_name_and_search, DJ_USER_ID, DJ_PASSWORD,
    DJ_BASE_URL, DJ_ADVANCED_SEARCH_URL, OUTPUT_DIR, TIMEOUT_MS,
)

proxy_url = (
    os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")
    or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or ""
)
launch_kwargs = {"headless": True}
puppeteer_chrome = "/root/.cache/puppeteer/chrome/linux-151.0.7922.71/chrome-linux64/chrome"
if os.path.exists(puppeteer_chrome):
    launch_kwargs["executable_path"] = puppeteer_chrome
if proxy_url:
    launch_kwargs["proxy"] = {"server": proxy_url}

with sync_playwright() as p:
    browser = p.chromium.launch(**launch_kwargs)
    context = browser.new_context(accept_downloads=True)
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)

    new_pages = []
    def on_new_page(new_page):
        print(f"[新页面] URL: {new_page.url}")
        new_pages.append(new_page)
    context.on("page", on_new_page)

    page.on("console", lambda msg: print(f"[console] {msg.text}") if "print" in msg.text.lower() else None)
    downloads = []
    def on_download(download):
        print(f"[下载] filename={download.suggested_filename}")
        downloads.append(download)
    page.on("download", on_download)

    # 登录
    login_ok = False
    for attempt in range(1, 5):
        try:
            print(f"登录尝试 {attempt}/4")
            login(page, DJ_BASE_URL)
            login_ok = True
            break
        except Exception as e:
            print(f"  登录失败 ({attempt}/4): {e}")
            try: context.clear_cookies()
            except: pass
            try: page.goto(DJ_BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            except: pass
            page.wait_for_timeout(2000)

    if not login_ok:
        print("登录失败, 退出")
        browser.close()
        sys.exit(1)

    # 搜索一个有结果的 entity
    print("\n=== 搜索 entity: 上海国泰海通证券资产有限公司 ===")
    open_advanced_search(page, DJ_ADVANCED_SEARCH_URL)
    select_search_type(page, "entity")
    fill_name_and_search(page, "上海国泰海通证券资产有限公司")
    page.wait_for_timeout(3000)

    # 查找含 print 的元素 (修复 className 非字符串问题)
    print("\n--- 查找含 print 的元素 (搜索结果页) ---")
    elements = page.evaluate("""() => {
        const results = [];
        const all = document.querySelectorAll('button, a, [role="button"], [onclick], i, span, div');
        for (const el of all) {
            const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
            const cls = String(el.className || '');
            const id = el.id || '';
            const onclick = el.getAttribute('onclick') || '';
            const dataTestId = el.getAttribute('data-testid') || '';
            if (text.toLowerCase().includes('print') || cls.toLowerCase().includes('print') ||
                id.toLowerCase().includes('print') || onclick.toLowerCase().includes('print') ||
                dataTestId.toLowerCase().includes('print')) {
                const rect = el.getBoundingClientRect();
                if (el.offsetParent !== null || el.offsetWidth > 0) {
                    results.push({
                        tag: el.tagName,
                        text: text.slice(0, 80),
                        cls: cls.slice(0, 120),
                        id: id,
                        dataTestId: dataTestId,
                        x: Math.round(rect.x), y: Math.round(rect.y),
                        w: Math.round(rect.width), h: Math.round(rect.height),
                    });
                }
            }
        }
        return results;
    }""")
    print(f"找到 {len(elements)} 个可见含 print 的元素:")
    for e in elements:
        print(f"  tag={e['tag']} text='{e['text']}' cls='{e['cls'][:80]}' id='{e['id']}' testid='{e['dataTestId']}' pos=({e['x']},{e['y']}) size={e['w']}x{e['h']}")

    # 尝试点击第一个含 print 的按钮
    if elements:
        target = elements[0]
        print(f"\n--- 点击: tag={target['tag']} text='{target['text']}' ---")
        # 用 JS 点击 (更可靠)
        try:
            page.evaluate(f"""() => {{
                const els = document.querySelectorAll('{target["tag"].toLowerCase()}');
                for (const el of els) {{
                    const t = (el.innerText || el.textContent || '').trim();
                    if (t.includes('{target["text"][:30]}')) {{
                        el.click();
                        return true;
                    }}
                }}
                return false;
            }}""")
            print("  JS click 完成")
        except Exception as e:
            print(f"  JS click 异常: {e}")

        page.wait_for_timeout(5000)
        print(f"  新页面数: {len(new_pages)}")
        for np in new_pages:
            print(f"    新页面 URL: {np.url}")
            try: np.wait_for_load_state("domcontentloaded", timeout=10000)
            except: pass
            print(f"    新页面标题: {np.title()}")

        # 检查 modal/popup
        modals = page.evaluate("""() => {
            const modals = document.querySelectorAll('[class*="modal" i], [class*="dialog" i], [class*="popup" i], [role="dialog"], [class*="overlay" i]');
            return Array.from(modals).filter(m => m.offsetParent !== null || m.offsetWidth > 0).map(m => ({
                cls: String(m.className).slice(0, 150),
                text: (m.innerText || '').slice(0, 300),
            }));
        }""")
        print(f"  可见 modal/popup: {len(modals)}")
        for m in modals:
            print(f"    cls='{m['cls'][:80]}' text='{m['text'][:100]}'")

        # 截图
        out_dir = OUTPUT_DIR / "debug"
        out_dir.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(out_dir / "search_after_print_click.png"), full_page=True)
        print(f"  截图已保存")

    # 也检查详情页
    print("\n=== 进入详情页检查 print 按钮 ===")
    try:
        result_link = page.locator('a[href*="profile?id="]').first
        if result_link.is_visible(timeout=5000):
            result_link.click()
            page.wait_for_timeout(3000)
            print("--- 详情页: 查找含 print 的元素 ---")
            detail_elements = page.evaluate("""() => {
                const results = [];
                const all = document.querySelectorAll('button, a, [role="button"], [onclick], i, span, div');
                for (const el of all) {
                    const text = (el.innerText || el.textContent || el.getAttribute('aria-label') || el.getAttribute('title') || '').trim();
                    const cls = String(el.className || '');
                    const id = el.id || '';
                    if (text.toLowerCase().includes('print') || cls.toLowerCase().includes('print') || id.toLowerCase().includes('print')) {
                        const rect = el.getBoundingClientRect();
                        if (el.offsetParent !== null || el.offsetWidth > 0) {
                            results.push({
                                tag: el.tagName, text: text.slice(0, 80),
                                cls: cls.slice(0, 120), id: id,
                                x: Math.round(rect.x), y: Math.round(rect.y),
                                w: Math.round(rect.width), h: Math.round(rect.height),
                            });
                        }
                    }
                }
                return results;
            }""")
            print(f"详情页找到 {len(detail_elements)} 个含 print 的元素:")
            for e in detail_elements:
                print(f"  tag={e['tag']} text='{e['text']}' cls='{e['cls'][:80]}' id='{e['id']}' pos=({e['x']},{e['y']}) size={e['w']}x{e['h']}")

            out_dir = OUTPUT_DIR / "debug"
            page.screenshot(path=str(out_dir / "detail_page.png"), full_page=True)
            # 右侧区域截图
            page.screenshot(path=str(out_dir / "detail_right.png"), clip={"x": 800, "y": 0, "width": 600, "height": 1000})
            print(f"  详情页截图已保存")

            # 尝试点击详情页 print 按钮
            if detail_elements:
                target = detail_elements[0]
                print(f"\n--- 点击详情页 print: tag={target['tag']} text='{target['text']}' ---")
                try:
                    page.evaluate(f"""() => {{
                        const els = document.querySelectorAll('{target["tag"].toLowerCase()}');
                        for (const el of els) {{
                            const t = (el.innerText || el.textContent || '').trim();
                            if (t.includes('{target["text"][:30]}')) {{
                                el.click();
                                return true;
                            }}
                        }}
                        return false;
                    }}""")
                    print("  JS click 完成")
                except Exception as e:
                    print(f"  JS click 异常: {e}")

                page.wait_for_timeout(5000)
                print(f"  新页面数: {len(new_pages)}")
                for np in new_pages:
                    print(f"    新页面 URL: {np.url}")

                modals = page.evaluate("""() => {
                    const modals = document.querySelectorAll('[class*="modal" i], [class*="dialog" i], [class*="popup" i], [role="dialog"]');
                    return Array.from(modals).filter(m => m.offsetParent !== null || m.offsetWidth > 0).map(m => ({
                        cls: String(m.className).slice(0, 150),
                        text: (m.innerText || '').slice(0, 300),
                    }));
                }""")
                print(f"  可见 modal/popup: {len(modals)}")
                for m in modals:
                    print(f"    cls='{m['cls'][:80]}' text='{m['text'][:100]}'")

                page.screenshot(path=str(out_dir / "detail_after_print_click.png"), full_page=True)
                print(f"  截图已保存")
    except Exception as e:
        print(f"  详情页检查异常: {e}")

    browser.close()
