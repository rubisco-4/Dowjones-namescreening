"""调试: 拦截 oauthcallback 的 token exchange 请求, 获取请求参数。"""
import os, sys, time, json
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

    # 拦截 token 请求
    token_requests = []
    def on_request(request):
        if 'oauth2/v1/token' in request.url:
            try:
                post_data = request.post_data
                token_requests.append({
                    'url': request.url,
                    'method': request.method,
                    'post_data': post_data,
                    'headers': dict(request.headers),
                })
                print(f"[TOKEN REQUEST] {request.method} {request.url}")
                print(f"  post_data: {post_data[:500] if post_data else 'None'}")
            except Exception as e:
                print(f"[TOKEN REQUEST] error: {e}")
    def on_response(response):
        if 'oauth2/v1/token' in response.url:
            try:
                body = response.text()
                print(f"[TOKEN RESPONSE] {response.status}")
                print(f"  body: {body[:500]}")
            except Exception:
                pass
    page.on("request", on_request)
    page.on("response", on_response)

    # 登录
    print("打开 riskcenter...")
    page.goto(DJ_BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

    okbtn_clicked = False
    deadline = time.time() + 45
    while time.time() < deadline:
        try:
            if not okbtn_clicked and page.locator("#okBtn").count() > 0 and page.locator("#okBtn").first.is_visible(timeout=400):
                print("点击 #okBtn")
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

    print("等待登录表单...")
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

    print("等待 oauthcallback...")
    callback_url = None
    for _ in range(120):
        url = page.url
        if "oauthcallback" in url and "code=" in url:
            callback_url = url
            print(f"到达 oauthcallback: {url[:200]}")
            break
        page.wait_for_timeout(500)

    # 提取 code
    code = None
    if callback_url:
        parsed = urlparse(callback_url)
        params = parse_qs(parsed.query)
        code = params.get('code', [None])[0]
        print(f"提取到 code: {code[:50]}..." if code else "未提取到 code")

    # 等待 token exchange (最多 30 秒)
    print("等待 token exchange 请求...")
    for _ in range(60):
        if token_requests:
            break
        page.wait_for_timeout(500)

    if token_requests:
        print(f"\n=== Token exchange 请求详情 ===")
        for tr in token_requests:
            print(f"URL: {tr['url']}")
            print(f"Method: {tr['method']}")
            print(f"Post data: {tr['post_data']}")
            # 解析 post data
            if tr['post_data']:
                try:
                    parsed = parse_qs(tr['post_data'])
                    print(f"Parsed: {json.dumps(parsed, indent=2)}")
                except Exception:
                    pass
    else:
        print("\n未捕获到 token exchange 请求 (可能被网络阻止)")
        # 尝试手动触发
        if code:
            print(f"\n尝试手动 token exchange, code={code[:30]}...")
            # 尝试用 page.request 发起请求
            try:
                resp = page.request.post(
                    "https://accounts.dowjones.com/oauth2/v1/token",
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": "https://riskcenter.dowjones.com/oauthcallback",
                        "client_id": "riskcenter",
                    },
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
                print(f"手动请求状态: {resp.status}")
                print(f"响应: {resp.text()[:500]}")
            except Exception as e:
                print(f"手动请求失败: {e}")

    print(f"\n最终 URL: {page.url[:200]}")
    browser.close()
