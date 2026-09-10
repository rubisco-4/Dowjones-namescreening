"""调试: 查找 code_verifier 存储位置, 以便手动重试 token exchange。"""
import os, sys, time
from pathlib import Path
from urllib.parse import urlparse, parse_qs

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

from playwright.sync_api import sync_playwright
from dowjones_screening import DJ_USER_ID, DJ_PASSWORD, DJ_BASE_URL, TIMEOUT_MS

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
    context = browser.new_context()
    page = context.new_page()
    page.set_default_timeout(TIMEOUT_MS)

    # 登录
    print("打开 riskcenter...")
    page.goto(DJ_BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

    okbtn_clicked = False
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            if not okbtn_clicked and page.locator("#okBtn").count() > 0 and page.locator("#okBtn").first.is_visible(timeout=400):
                page.locator("#okBtn").first.click(timeout=5000)
                okbtn_clicked = True
                page.wait_for_timeout(2500)
                continue
        except Exception:
            pass
        try:
            if page.locator("#email").count() > 0:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    page.wait_for_selector("#email", state="visible", timeout=30000)
    page.locator("#email").click()
    page.locator("#email").press("Control+a")
    page.locator("#email").type(DJ_USER_ID, delay=30)
    pass_input = page.locator("#password-form-item")
    pass_input.wait_for(state="visible", timeout=10000)
    pass_input.click()
    pass_input.press("Control+a")
    pass_input.type(DJ_PASSWORD, delay=30)
    page.wait_for_timeout(400)
    page.locator("#signin-btn").first.click()

    # 等待 oauthcallback
    for _ in range(120):
        if "oauthcallback" in page.url and "code=" in page.url:
            break
        page.wait_for_timeout(500)

    print(f"到达 oauthcallback: {page.url[:150]}")

    # 提取 code
    parsed = urlparse(page.url)
    params = parse_qs(parsed.query)
    code = params.get('code', [None])[0]
    print(f"code: {code[:40]}..." if code else "no code")

    # 查找 code_verifier: 检查 sessionStorage, localStorage, cookie
    print("\n=== 查找 code_verifier ===")
    storage = page.evaluate("""() => {
        const result = {sessionStorage: {}, localStorage: {}, cookies: {}};
        try {
            for (let i = 0; i < sessionStorage.length; i++) {
                const k = sessionStorage.key(i);
                const v = sessionStorage.getItem(k);
                if (k.toLowerCase().includes('verif') || k.toLowerCase().includes('code') || 
                    k.toLowerCase().includes('pkce') || k.toLowerCase().includes('oauth') ||
                    k.toLowerCase().includes('challenge') || (v && v.length > 20 && v.length < 200)) {
                    result.sessionStorage[k] = v.slice(0, 100);
                }
            }
        } catch(e) {}
        try {
            for (let i = 0; i < localStorage.length; i++) {
                const k = localStorage.key(i);
                const v = localStorage.getItem(k);
                if (k.toLowerCase().includes('verif') || k.toLowerCase().includes('code') || 
                    k.toLowerCase().includes('pkce') || k.toLowerCase().includes('oauth') ||
                    k.toLowerCase().includes('challenge') || (v && v.length > 20 && v.length < 200)) {
                    result.localStorage[k] = v.slice(0, 100);
                }
            }
        } catch(e) {}
        try {
            result.cookies = document.cookie;
        } catch(e) {}
        return result;
    }""")
    print(f"sessionStorage 相关: {storage['sessionStorage']}")
    print(f"localStorage 相关: {storage['localStorage']}")

    # 查找全局 JS 变量中的 code_verifier
    js_vars = page.evaluate("""() => {
        const found = [];
        const keys = Object.keys(window);
        for (const k of keys) {
            if (k.toLowerCase().includes('verif') || k.toLowerCase().includes('pkce') || 
                k.toLowerCase().includes('code_verif') || k.toLowerCase().includes('challenge')) {
                try {
                    const v = window[k];
                    found.push({key: k, value: String(v).slice(0, 100)});
                } catch(e) {}
            }
        }
        return found;
    }""")
    print(f"\n全局变量 (verifier/pkce): {js_vars}")

    # 检查页面中是否有 code_verifier 字符串
    page_text = page.evaluate("() => document.body ? document.body.innerHTML : ''")
    import re
    verifier_matches = re.findall(r'code_verifier["\s:=]+([A-Za-z0-9._-]{20,})', page_text)
    if verifier_matches:
        print(f"\n在页面 HTML 中找到 code_verifier: {verifier_matches[0][:50]}...")

    # 尝试手动 token exchange (用从请求中获取的 client_id)
    if code:
        print("\n=== 尝试手动 token exchange ===")
        # 尝试从 sessionStorage 获取 code_verifier
        verifier = None
        for k, v in storage['sessionStorage'].items():
            if 'verif' in k.lower() or len(v) > 40:
                verifier = v
                break

        if not verifier:
            # 尝试从 localStorage
            for k, v in storage['localStorage'].items():
                if 'verif' in k.lower() or len(v) > 40:
                    verifier = v
                    break

        if verifier:
            print(f"找到 code_verifier: {verifier[:40]}...")
            try:
                resp = page.request.post(
                    "https://accounts.dowjones.com/oauth2/v1/token",
                    data={
                        "client_id": "XlPAJdTyz04FwJsOpygmlWS2wknzqgUu",
                        "code": code,
                        "code_verifier": verifier,
                        "grant_type": "authorization_code",
                        "redirect_uri": "https://riskcenter.dowjones.com/oauthcallback",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                print(f"手动 token exchange 状态: {resp.status}")
                body = resp.text()
                print(f"响应: {body[:300]}")
                if resp.status == 200:
                    print("Token exchange 成功! 尝试导航到 dashboard...")
                    page.goto(DJ_BASE_URL + "/dashboard", wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    page.wait_for_timeout(3000)
                    print(f"导航后 URL: {page.url[:150]}")
            except Exception as e:
                print(f"手动 token exchange 失败: {e}")
        else:
            print("未找到 code_verifier, 无法手动 token exchange")

    print(f"\n最终 URL: {page.url[:150]}")
    browser.close()
