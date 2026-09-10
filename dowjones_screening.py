"""
Dow Jones Risk Center Name Screening 自动化工作流

读取 screening_list.csv 模板, 逐行登录 riskcenter.dowjones.com/search/advanced,
按 person/entity 搜索, 将搜索结果页打印为 PDF, 若有结果则点击每个结果进入详情页,
提取 result_name + profile_id 并将详情页打印为 PDF。最终输出 run_summary.csv。

用法:
    python3 dowjones_screening.py [--template screening_list.csv] [--headless]

凭据与配置从 .dowjones.env 读取 (见 .dowjones.env)。
"""

import argparse
import csv
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

try:
    from dotenv import load_dotenv
except ImportError:  # python-dotenv 可选, 失败则用 os.environ
    def load_dotenv(path):
        if not os.path.exists(path):
            return
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip())

from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# 配置加载
# ---------------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".dowjones.env")

DJ_USER_ID = os.environ.get("DJ_USER_ID", "")
DJ_PASSWORD = os.environ.get("DJ_PASSWORD", "")
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "output"))
TIMEOUT_MS = int(os.environ.get("TIMEOUT_MS", "60000"))
HEADLESS = os.environ.get("HEADLESS", "true").lower() == "true"
DJ_BASE_URL = os.environ.get("DJ_BASE_URL", "https://riskcenter.dowjones.com")
DJ_ADVANCED_SEARCH_URL = os.environ.get(
    "DJ_ADVANCED_SEARCH_URL", "https://riskcenter.dowjones.com/search/simple"
)

# 文件名非法字符
ILLEGAL_FN_CHARS = re.compile(r'[/\\:*?"<>|]')


def sanitize(name: str) -> str:
    """替换文件名非法字符并去除首尾空白。"""
    return ILLEGAL_FN_CHARS.sub("_", name).strip()


def _get_proxy_url() -> str:
    """读取当前代理配置 (沙箱环境通过本地代理出网)。"""
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    )


def _build_proxy_opener():
    """构建 urllib opener (走代理, 沙箱环境必须通过代理出网)。"""
    import urllib.request
    proxy_url = _get_proxy_url()
    if proxy_url:
        proxy_handler = urllib.request.ProxyHandler({
            "http": proxy_url,
            "https": proxy_url,
        })
        return urllib.request.build_opener(proxy_handler)
    return urllib.request.build_opener()


def log(msg: str):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# 模板读取
# ---------------------------------------------------------------------------
def read_template(path: Path):
    """读取模板, 返回 [{'type','name','remark'}, ...]; 跳过空行/注释/非法 type。"""
    rows = []
    # 先过滤注释行与空行, 再交给 DictReader 解析表头
    cleaned = []
    with open(path, newline="", encoding="utf-8-sig") as f:
        for line in f:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            cleaned.append(line if line.endswith("\n") else line + "\n")
    if not cleaned:
        return rows
    reader = csv.DictReader(cleaned)
    if not reader.fieldnames or "type" not in [h.lower() for h in reader.fieldnames]:
        raise ValueError(f"模板缺少 type/name 表头: {reader.fieldnames}")
    lower_map = {h.lower(): h for h in reader.fieldnames}
    for row in reader:
        t = (row.get(lower_map.get("type", "type")) or "").strip()
        n = (row.get(lower_map.get("name", "name")) or "").strip()
        r = (row.get(lower_map.get("remark", "remark")) or "").strip() if "remark" in lower_map else ""
        if not t or not n:
            continue
        if t.lower() not in ("person", "entity"):
            log(f"  跳过非法 type={t!r} (name={n!r})")
            continue
        rows.append({"type": t.lower(), "name": n, "remark": r})
    return rows


# ---------------------------------------------------------------------------
# 登录
# ---------------------------------------------------------------------------
def login(page, base_url: str):
    """登录 Dow Jones Risk Center。

    实际流程: riskcenter.dowjones.com → djlogin 中转页 (#okBtn)
    → sso.accounts.dowjones.com 登录表单 (#email / #password-form-item / #signin-btn)
    → oauth 回调 → riskcenter dashboard。
    """
    log(f"打开 {base_url}")
    page.goto(base_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)

    # 设置 token exchange 请求重试路由: 网络抖动时 code 交换 XHR 易失败,
    # 用 urllib 走代理重试 (沙箱环境必须通过代理出网)
    try:
        import urllib.request
        import urllib.error

        def _token_route(route):
            request = route.request
            post_data = request.post_data or ""
            headers = dict(request.headers)
            # 移除可能导致问题的 header
            for h in ["host", "content-length", "connection"]:
                headers.pop(h, None)
            for attempt in range(3):
                try:
                    req = urllib.request.Request(
                        request.url,
                        data=post_data.encode("utf-8") if isinstance(post_data, str) else post_data,
                        headers=headers,
                        method=request.method,
                    )
                    opener = _build_proxy_opener()
                    with opener.open(req, timeout=30) as resp:
                        body = resp.read()
                        route.fulfill(
                            status=resp.status,
                            headers=dict(resp.headers),
                            body=body,
                        )
                    return
                except Exception as e:
                    if attempt < 2:
                        log(f"  token exchange 第 {attempt+1} 次失败, 重试: {str(e)[:100]}")
                        continue
                    else:
                        route.abort()
                        return
        page.route("**/oauth2/v1/token", _token_route)
    except Exception:
        pass

    # 1) 多跳重定向: riskcenter → accounts.dowjones.com/oauth2 → djlogin/login.asp(#okBtn)
    #    → 点击 #okBtn → sso.accounts.dowjones.com 登录表单。
    #    用轮询兼容重定向链 (wait_for / expect_navigation 会被中间跳转打断)。
    okbtn_clicked = False
    deadline = time.time() + 45
    last_url_log = 0.0
    while time.time() < deadline:
        url = (page.url or "").lower()
        # 每 ~10s 记录当前 URL, 便于排查重定向链中页面所处状态
        now = time.time()
        if now - last_url_log >= 10:
            log(f"  轮询中 (剩余 {int(deadline - now)}s), 当前 URL: {url[:120]}")
            last_url_log = now
        # 已登录则直接返回
        if "riskcenter.dowjones.com" in url and "login" not in url and "signin" not in url and "oauthcallback" not in url:
            if _is_logged_in(page):
                log("已处于登录后状态, 跳过登录")
                return
        # 中转页 #okBtn
        try:
            if not okbtn_clicked and page.locator("#okBtn").count() > 0 and page.locator("#okBtn").first.is_visible(timeout=400):
                log("发现中转页 #okBtn, 点击继续")
                try:
                    page.locator("#okBtn").first.click(timeout=5000)
                except Exception:
                    pass
                okbtn_clicked = True
                page.wait_for_timeout(2500)
                continue
        except Exception:
            pass
        # 登录表单出现 (#email 存在)
        try:
            if page.locator("#email").count() > 0:
                break
        except Exception:
            pass
        page.wait_for_timeout(500)

    # 2) 若已登录, 直接返回
    if _is_logged_in(page):
        log("已处于登录后状态, 跳过登录")
        return

    # 2b) 宽松判定: 已在 riskcenter 但 _is_logged_in 未找到预期元素 (SPA 慢渲染),
    #     尝试直接导航到搜索页再探测一次; 若仍停在 riskcenter 则视为可能已登录。
    cur_url = (page.url or "").lower()
    if "riskcenter.dowjones.com" in cur_url and "login" not in cur_url and "signin" not in cur_url and "oauthcallback" not in cur_url:
        log(f"  已在 riskcenter 但探测元素未找到, 尝试直接导航到搜索页: {DJ_ADVANCED_SEARCH_URL}")
        try:
            page.goto(DJ_ADVANCED_SEARCH_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
            page.wait_for_timeout(2000)
        except Exception as e:
            log(f"  导航搜索页失败: {e}")
        if _is_logged_in(page):
            log("导航到搜索页后确认已登录, 跳过登录")
            return
        after_url = (page.url or "").lower()
        if "riskcenter.dowjones.com" in after_url and "login" not in after_url and "signin" not in after_url and "oauthcallback" not in after_url:
            log("仍在 riskcenter 搜索页, 视为可能已登录, 跳过登录")
            return

    # 3) 等待登录表单稳定 (SPA 可能重渲染 #email, 直接 fill 会因元素消失超时)
    log("等待登录表单稳定")
    try:
        page.wait_for_selector("#email", state="visible", timeout=30000)
    except PWTimeout:
        if _is_logged_in(page):
            log("已处于登录后状态, 跳过登录")
            return
        raise RuntimeError("找不到用户名输入框, 页面结构可能变化 (或已登录/需要人工介入)")

    # 4) 填登录表单 (用 type 逐字符输入, 兼容 React 受控输入; fill 偶发不触发 onChange)
    user_input = page.locator("#email")
    log("填入用户名")
    user_input.click()
    user_input.press("Control+a")
    user_input.type(DJ_USER_ID, delay=30)
    pass_input = page.locator("#password-form-item")
    try:
        pass_input.wait_for(state="visible", timeout=10000)
    except PWTimeout:
        pass_input = _first_visible(page, [
            'input[type="password"]', 'input[name="password"]', 'input[id*="password" i]',
        ])
        if pass_input is None:
            raise RuntimeError("找不到密码输入框")
    log("填入密码")
    pass_input.click()
    pass_input.press("Control+a")
    pass_input.type(DJ_PASSWORD, delay=30)
    page.wait_for_timeout(400)
    # 校验值已写入 (防止 SPA 重置)
    if not user_input.input_value() or not pass_input.input_value():
        log("  表单值被重置, 重新输入")
        user_input.click(); user_input.press("Control+a"); user_input.type(DJ_USER_ID, delay=30)
        pass_input.click(); pass_input.press("Control+a"); pass_input.type(DJ_PASSWORD, delay=30)
        page.wait_for_timeout(400)

    submit_selectors = [
        "#signin-btn",
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Sign In")',
        'button:has-text("Sign in")',
    ]
    submit = _first_visible(page, submit_selectors)
    if submit is None:
        log("未找到提交按钮, 改用回车提交")
        pass_input.press("Enter")
    else:
        submit.click()

    # 5) 等待 oauth 回调跳回 riskcenter (URL 不再含 login/signin/oauthcallback)
    #    用轮询以兼容客户端重定向 (wait_for_url 对 SPA 跳转不可靠)
    deadline = time.time() + TIMEOUT_MS / 1000 + 60
    landed = False
    saw_oauthcallback = False
    while time.time() < deadline:
        try:
            url = (page.url or "").lower()
        except Exception:
            url = ""
        if (
            "riskcenter.dowjones.com" in url
            and "oauthcallback" not in url
            and "login" not in url
            and "signin" not in url
        ):
            landed = True
            break
        # oauthcallback 拿到 code 后, SPA 偶发不自动跳 dashboard (code 交换 XHR 网络失败)
        if "oauthcallback" in url and "code=" in url:
            if not saw_oauthcallback:
                saw_oauthcallback = True
                log("  检测到 oauthcallback (已获 code), 等待 SPA 处理")
                # 等待 SPA 自行处理 code 交换 (最多 30 秒, 网络慢时需更长)
                for _ in range(60):
                    page.wait_for_timeout(500)
                    cur = (page.url or "").lower()
                    if "riskcenter.dowjones.com" in cur and "oauthcallback" not in cur and "login" not in cur and "signin" not in cur:
                        landed = True
                        break
                if landed:
                    break
                # 仍未跳走 → reload 一次重试 SPA 的 code 交换 XHR
                if "oauthcallback" in (page.url or "").lower():
                    log("  oauthcallback 未自动跳转, reload 重试 SPA code 交换")
                    try:
                        page.reload(wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    except Exception:
                        pass
                    # reload 后等待 SPA 处理 (最多 45 秒)
                    for _ in range(90):
                        page.wait_for_timeout(500)
                        cur = (page.url or "").lower()
                        if "riskcenter.dowjones.com" in cur and "oauthcallback" not in cur and "login" not in cur and "signin" not in cur:
                            landed = True
                            break
                    if landed:
                        break
                    # reload 仍未跳走 → 直接导航到搜索页 (可能触发 code 交换)
                    if "oauthcallback" in (page.url or "").lower():
                        log("  reload 无效, 导航到搜索页重试")
                        try:
                            page.goto(DJ_ADVANCED_SEARCH_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                        except Exception:
                            pass
                        page.wait_for_timeout(5000)
                        cur = (page.url or "").lower()
                        if "riskcenter.dowjones.com" in cur and "oauthcallback" not in cur and "login" not in cur and "signin" not in cur:
                            landed = True
                            break
                    continue
        page.wait_for_timeout(500)
    # 严格判定: 仍在 oauthcallback 则视为未登录
    if "oauthcallback" in (page.url or "").lower():
        raise RuntimeError("登录后仍停留在 oauthcallback, code 交换失败")
    if not landed and not _is_logged_in(page):
        raise RuntimeError("登录后未跳转回 riskcenter, 可能凭据失效或出现验证码/2FA")

    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeout:
        log("  登录后 networkidle 超时, 继续")
    log(f"登录成功, 当前 URL: {page.url}")


def _first_visible(page, selectors):
    for sel in selectors:
        loc = page.locator(sel).first
        try:
            if loc.is_visible(timeout=2000):
                return loc
        except Exception:
            continue
    return None


def _is_logged_in(page):
    """探测是否已登录 (URL 不在 login 区且能找到搜索/账户元素)。"""
    url = page.url.lower()
    if "login" in url or "signin" in url:
        return False
    # 探测登录后才有的元素: 搜索框/账户菜单
    probes = [
        'input[name*="search" i]',
        'input[placeholder*="search" i]',
        'a:has-text("Sign Out")',
        'a:has-text("Log Out")',
        '[data-testid*="search" i]',
    ]
    for sel in probes:
        try:
            if page.locator(sel).first.is_visible(timeout=1500):
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Advanced Search
# ---------------------------------------------------------------------------
def open_advanced_search(page, search_url: str):
    log(f"导航到 advanced search: {search_url}")
    page.goto(search_url, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
    page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)


def select_search_type(page, search_type: str):
    """选择 searching type = person / entity。

    Dow Jones Risk Center 的 Person/Entity 类型切换按钮位于 /search/simple 页:
    `button:has-text("Person")` 与 `button:has-text("Entity")`。
    """
    label = "Person" if search_type == "person" else "Entity"
    log(f"选择 searching type = {label}")

    # 主选择器: simple search 页的 Person/Entity 按钮 (用长超时等渲染)
    primary = [
        f'button:has-text("{label}")',
        f'[role="tab"]:has-text("{label}")',
        f'div[role="button"]:has-text("{label}")',
        f'a:has-text("{label}")',
    ]
    for sel in primary:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=10000)
            loc.click()
            page.wait_for_timeout(800)
            log(f"  已点击 {label} 按钮 (sel={sel})")
            return
        except Exception:
            continue

    # 回退: radio / dropdown
    for sel in [
        f'input[type="radio"][value*="{label}" i]',
        f'input[type="radio"][id*="{label}" i]',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=1000):
                loc.click()
                return
        except Exception:
            continue
    sel_elems = page.locator("select").all()
    for s in sel_elems:
        try:
            options = s.locator("option").all_text_contents()
            if any(label.lower() in (o or "").lower() for o in options):
                s.select_option(label=label)
                return
        except Exception:
            continue

    log(f"  警告: 未定位到 {label} 选择控件 (可能已在该类型)")


def fill_name_and_search(page, name: str):
    """填入 name 并触发搜索, 等待结果页加载。"""
    log(f"填入 name={name!r} 并搜索")
    # 等待 name 输入框稳定 (用长超时, 兼容 SPA 渲染延迟)
    name_selectors = [
        "input[name='name']",
        "input[placeholder='Enter a name']",
        'input[name*="name" i]',
        'input[placeholder*="name" i]',
        'input[id*="name" i]',
        'input[type="search"]',
        'input[name*="search" i]',
        'input[placeholder*="search" i]',
    ]
    name_input = None
    for sel in name_selectors:
        try:
            loc = page.locator(sel).first
            loc.wait_for(state="visible", timeout=8000)
            name_input = loc
            break
        except Exception:
            continue
    if name_input is None:
        # 回退: 取第一个可见 text input
        try:
            name_input = page.locator('input[type="text"]:visible').first
            name_input.wait_for(timeout=5000)
        except Exception:
            raise RuntimeError("找不到 name 输入框")
    name_input.click()
    name_input.fill("")
    # 用 type 触发 React onChange (与登录同理)
    name_input.type(name, delay=40)
    page.wait_for_timeout(600)

    # 触发搜索: 优先回车 (simple search 回车即可触发), 再尝试按钮
    search_btn_selectors = [
        'button[type="submit"]',
        'button:has-text("Search")',
        'button:has-text("SEARCH")',
        'button[id*="search" i]',
        'a:has-text("Search")',
        '[data-testid*="search" i]',
    ]
    btn = _first_visible(page, search_btn_selectors)
    if btn is not None:
        try:
            btn.click()
        except Exception:
            name_input.press("Enter")
    else:
        name_input.press("Enter")

    # 等待结果页加载
    try:
        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
    except PWTimeout:
        log("  等待 networkidle 超时, 继续")
    # 额外等待, 让结果渲染稳定
    page.wait_for_timeout(3000)


# ---------------------------------------------------------------------------
# 结果检测
# ---------------------------------------------------------------------------
NO_RESULT_PATTERNS = [
    "no results", "no result", "no matching", "no records",
    "0 results", "no matches found", "no data found",
]


def detect_results(page):
    """返回结果项的 locator 列表 (可点击的 name 链接); 空列表表示无结果。"""
    # 先检查无结果提示
    body_text = (page.inner_text("body") or "").lower()
    for pat in NO_RESULT_PATTERNS:
        if pat in body_text:
            # 进一步确认: 没有结果项
            if not _collect_result_links(page):
                return []
    return _collect_result_links(page)


def _collect_result_links(page):
    """收集搜索结果中可点击的 name 链接 (指向 profile 详情页)。返回 locator 列表。

    精确匹配 href 含 profile id 的链接, 过滤导航/分页/页眉页脚等噪音。
    """
    # 真实结果链接: href 含 profile 详情页路径/参数
    profile_href_selectors = [
        'a[href*="/profile/"]',
        'a[href*="/riskentities/profiles/"]',
        'a[href*="profile?id="]',
        'a[href*="profileId="]',
        'a[href*="profile_id="]',
        'a[data-testid*="result" i]',
        'a[data-testid*="profile" i]',
    ]
    NAV_WORDS = (
        "next", "previous", "prev", "sort", "filter", "export", "print",
        "search", "clear", "reset", "summary", "dashboard", "home",
        "sign out", "log out", "help", "feedback", "terms", "privacy",
    )
    links = []
    seen_href = set()
    for sel in profile_href_selectors:
        try:
            loc_list = page.locator(sel).all()
        except Exception:
            continue
        for loc in loc_list:
            try:
                if not loc.is_visible(timeout=400):
                    continue
            except Exception:
                continue
            try:
                href = loc.get_attribute("href") or ""
                text = (loc.inner_text(timeout=400) or "").strip()
            except Exception:
                continue
            if not text or len(text) > 200:
                continue
            if any(w in text.lower() for w in NAV_WORDS):
                continue
            if not href:
                continue
            # 去重 (同一 href 只取一次)
            if href in seen_href:
                continue
            # 必须看起来像 profile 详情链接 (含 id 段)
            if not _looks_like_profile_href(href):
                continue
            seen_href.add(href)
            links.append(loc)
        if links:
            # 已找到真实结果, 不再用更宽的选择器兜底
            return links
    return links


def _looks_like_profile_href(href: str) -> bool:
    """判断 href 是否指向 profile 详情页 (含 profile id 段)。"""
    h = href.lower()
    if "/profile/" in h or "/riskentities/profiles/" in h:
        # 取末段, 要求非空且像 id (字母数字/连字符, 长度>=3)
        last = href.rstrip("/").rsplit("/", 1)[-1].split("?")[0]
        if len(last) >= 3 and re.match(r"^[A-Za-z0-9_\-]+$", last):
            return True
    # /search/profile?id=12345678 或 profileId= / profile_id= 形式
    if re.search(r"profile\?id=\d{3,}", h) or "profileid=" in h or "profile_id=" in h:
        return True
    return False


# ---------------------------------------------------------------------------
# PDF 留痕
# ---------------------------------------------------------------------------
def stabilize_for_pdf(page):
    """等待页面稳定并滚动触发懒加载, 以保证整页 PDF 完整。"""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    # 滚动到底再回顶, 触发懒加载
    try:
        page.evaluate("() => { window.scrollTo(0, document.body.scrollHeight); }")
        page.wait_for_timeout(800)
        page.evaluate("() => { window.scrollTo(0, 0); }")
        page.wait_for_timeout(500)
    except Exception:
        pass


def download_pdf(page, target_path: Path, page_type: str = "detail") -> Path:
    """点击页面 Download 按钮, 选择 PDF 格式, 下载并保存到 target_path。

    用 expect_download 包裹整个交互流程: 点击 Download → 等 dialog/dropdown → 选 PDF → 点 Continue。
    若 Download 按钮直接触发下载 (无 dialog), expect_download 也能捕获。
    """
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 记录点击 Download 前页面上可见的 "PDF" 元素数量 (用于检测新增的 PDF 选项)
    try:
        pdf_count_before = page.locator('text="PDF"').count()
    except Exception:
        pdf_count_before = 0

    # 查找页面上方的 Download 按钮
    # Dow Jones Risk Center 的 Download 按钮是图标按钮 (svg[aria-label="file-download"])
    # 其父元素是可点击的 button/div, 但 SVG 不在 button:has-text("Download") 内部
    dl_btn = None
    dl_btn_sel = ""
    # 优先: 通过 SVG 图标找 Download 按钮 (这是页面上真正的 Download 图标按钮)
    try:
        svg_loc = page.locator('svg[aria-label="file-download"]').first
        if svg_loc.is_visible(timeout=2000):
            # SVG 的父元素是可点击的按钮/div
            dl_btn = svg_loc
            dl_btn_sel = 'svg[aria-label="file-download"]'
            log("  找到 Download 图标按钮 (svg file-download)")
    except Exception:
        pass

    # 回退: 文字按钮 / aria-label 按钮
    if dl_btn is None:
        for sel in [
            'button[aria-label="Download"]',
            'button[aria-label*="download" i]',
            'a[aria-label*="download" i]',
            'button:has(svg[aria-label="file-download"])',
            'button:has-text("Download")',
            'a:has-text("Download")',
        ]:
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1000):
                    dl_btn = loc
                    dl_btn_sel = sel
                    break
            except Exception:
                continue

    if dl_btn is None:
        raise RuntimeError("未找到 Download 按钮")

    # 用 expect_download 包裹整个流程 (Download 点击 + dialog 交互 + Continue 点击)
    try:
        with page.expect_download(timeout=120000) as dl_info:
            # 点击 Download 按钮
            # SVG 图标按钮常被 overlay 拦截 pointer events
            if dl_btn_sel == 'svg[aria-label="file-download"]':
                # 方案 1: Playwright force=True 直接点击 SVG (跳过 actionability 检查)
                try:
                    dl_btn.click(force=True, timeout=5000)
                    log("  Download SVG 点击成功 (force=True)")
                except Exception as e1:
                    log(f"  SVG force click 失败, 尝试坐标点击: {str(e1)[:80]}")
                    # 方案 2: 获取 SVG 坐标, 用 mouse.click 直接点击
                    try:
                        box = dl_btn.bounding_box()
                        if box:
                            cx = box["x"] + box["width"] / 2
                            cy = box["y"] + box["height"] / 2
                            log(f"  SVG 坐标: ({cx:.0f}, {cy:.0f}), 用 mouse.click")
                            page.mouse.click(cx, cy)
                            log("  mouse.click 完成")
                    except Exception as e2:
                        log(f"  坐标点击失败, 尝试 JS: {str(e2)[:80]}")
                        # 方案 3: JS dispatchEvent
                        js_result = page.evaluate("""() => {
                            const svg = document.querySelector('svg[aria-label="file-download"]');
                            if (!svg) return 'svg not found';
                            let el = svg;
                            for (let i = 0; i < 5; i++) {
                                el = el.parentElement;
                                if (!el) break;
                                const tag = el.tagName.toLowerCase();
                                const role = el.getAttribute('role') || '';
                                if (tag === 'button' || tag === 'a' || role === 'button') {
                                    el.click();
                                    return 'clicked: ' + tag;
                                }
                            }
                            const parent = svg.parentElement;
                            if (parent) {
                                parent.dispatchEvent(new MouseEvent('click', {bubbles: true, cancelable: true}));
                                return 'dispatchEvent on ' + parent.tagName;
                            }
                            return 'failed';
                        }""")
                        log(f"  JS 点击 Download: {js_result}")
            else:
                # 普通按钮: 先 scroll, 再 click, 再 force, 再 JS
                try:
                    dl_btn.scroll_into_view_if_needed(timeout=3000)
                except Exception:
                    pass
                clicked = False
                try:
                    dl_btn.click(timeout=5000)
                    clicked = True
                    log("  Download 按钮点击成功 (普通 click)")
                except Exception as e1:
                    log(f"  普通 click 失败, 尝试 force=True: {str(e1)[:80]}")
                    try:
                        dl_btn.click(force=True, timeout=5000)
                        clicked = True
                        log("  Download 按钮点击成功 (force=True)")
                    except Exception:
                        pass
                if not clicked:
                    page.evaluate("""() => {
                        const btns = document.querySelectorAll('button, a, [role="button"]');
                        for (const b of btns) {
                            const aria = b.getAttribute('aria-label') || '';
                            const t = (b.innerText || b.textContent || '').trim();
                            if (aria.toLowerCase() === 'download' || t.toLowerCase() === 'download') {
                                b.click();
                                return true;
                            }
                        }
                        return false;
                    }""")

            # 等待 dialog / dropdown / popover 出现 (轮询, 最多 10 秒)
            dialog = None
            for wait_round in range(20):
                page.wait_for_timeout(500)
                for dlg_sel in [
                    '[role="dialog"]',
                    '[role="alertdialog"]',
                    '[role="menu"]',
                    '[class*="modal" i]',
                    '[class*="dialog" i]',
                    '[class*="overlay" i]',
                    '[class*="popover" i]',
                    '[class*="dropdown" i]',
                    '[class*="popup" i]',
                    'mat-dialog-container',
                    '.MuiDialog-container',
                    '.MuiPopover-paper',
                    '[class*="ant-modal" i]',
                    '[class*="ant-dropdown" i]',
                    '[class*="ant-popover" i]',
                ]:
                    try:
                        loc = page.locator(dlg_sel).first
                        if loc.is_visible(timeout=200):
                            dialog = loc
                            log(f"  找到对话框容器: {dlg_sel} (第 {wait_round+1} 轮)")
                            break
                    except Exception:
                        continue
                if dialog is not None:
                    break
                # 也检测是否有新增的 "PDF" 或 "Continue" 文本
                try:
                    pdf_now = page.locator('text="PDF"').count()
                    cont_now = page.locator('text="Continue"').count() + page.locator('text="CONTINUE"').count()
                    if pdf_now > pdf_count_before or cont_now > 0:
                        log(f"  第 {wait_round+1} 轮: 检测到 PDF({pdf_now}) 或 Continue({cont_now}) 文本")
                        break
                except Exception:
                    pass

            container = dialog if dialog is not None else page

            # 诊断: 若未找到 dialog, dump 页面 body 的前 1500 字符并截图
            if dialog is None:
                try:
                    body_html = page.evaluate("""() => {
                        return document.body.innerHTML.substring(0, 1500);
                    }""")
                    log(f"  未找到 dialog, 页面 body 前 1500 字符: {body_html[:800]}")
                except Exception:
                    pass
                # 截图用于排查
                try:
                    screenshot_path = OUTPUT_DIR / "debug_download.png"
                    page.screenshot(path=str(screenshot_path), full_page=True)
                    log(f"  截图已保存: {screenshot_path}")
                except Exception:
                    pass
                # dump 所有可见按钮的完整信息 (含父元素 class)
                try:
                    all_btns = page.evaluate("""() => {
                        const btns = document.querySelectorAll('button, [role="button"], a');
                        const results = [];
                        for (const b of btns) {
                            const rect = b.getBoundingClientRect();
                            if (rect.width === 0 || rect.height === 0) continue;
                            if (b.offsetParent === null) continue;
                            const t = (b.innerText || b.textContent || '').trim();
                            const aria = b.getAttribute('aria-label') || '';
                            const cls = (b.className || '').toString().substring(0, 60);
                            const parent = b.parentElement;
                            const pcls = parent ? (parent.className || '').toString().substring(0, 60) : '';
                            const disabled = b.disabled || false;
                            results.push({text: t.substring(0, 30), aria: aria.substring(0, 20), cls: cls, pcls: pcls, disabled: disabled});
                        }
                        return results;
                    }""")
                    log(f"  所有可见按钮 (含 class/disabled): {all_btns}")
                except Exception:
                    pass

            # 检测是否有新增的 "PDF" 选项 (点击 Download 后新出现的)
            try:
                pdf_count_after = page.locator('text="PDF"').count()
            except Exception:
                pdf_count_after = 0

            if pdf_count_after > pdf_count_before:
                log(f"  检测到新增 PDF 选项 (before={pdf_count_before}, after={pdf_count_after})")

            # 选择 PDF 格式 (在整个页面范围内搜索, 不局限于 dialog)
            pdf_clicked = False
            for sel in ['text="PDF"', 'button:has-text("PDF")', '[role="radio"]:has-text("PDF")',
                        'label:has-text("PDF")', '[role="option"]:has-text("PDF")',
                        'span:has-text("PDF")', 'li:has-text("PDF")']:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1000):
                        log("  选择 PDF 格式")
                        try:
                            loc.click(timeout=3000)
                        except Exception:
                            loc.click(force=True, timeout=3000)
                        page.wait_for_timeout(1000)
                        pdf_clicked = True
                        break
                except Exception:
                    continue

            if not pdf_clicked:
                log("  未找到 PDF 选项 (可能使用默认格式)")

            # 点击动作按钮 (Continue/CONTINUE/Download 等) 触发下载
            # 排除原始 Download 按钮 (如果它仍然可见)
            action_btn = None
            for sel in [
                'button:has-text("Continue")',
                'button:has-text("CONTINUE")',
                'button:has-text("OK")',
                'button:has-text("Confirm")',
                'button:has-text("Submit")',
                'button:has-text("Export")',
                'button:has-text("GENERATE")',
                'button:has-text("Generate")',
                'a:has-text("Continue")',
                'a:has-text("CONTINUE")',
                '[role="button"]:has-text("Continue")',
                '[role="button"]:has-text("CONTINUE")',
                'button[type="submit"]',
            ]:
                try:
                    loc = page.locator(sel).first
                    if loc.is_visible(timeout=1000):
                        action_btn = loc
                        log(f"  找到动作按钮: {sel}")
                        break
                except Exception:
                    continue

            if action_btn is None:
                # JS 遍历所有按钮找动作关键词 (排除原始 Download 按钮)
                log("  CSS 选择器未找到动作按钮, 尝试 JS 遍历")
                found = page.evaluate("""() => {
                    const btns = document.querySelectorAll('button, [role="button"], a, [type="submit"]');
                    // 优先级: continue > ok > confirm > submit > export > generate > download
                    const priorities = [
                        ['continue', '继续'],
                        ['ok', 'confirm', '确定'],
                        ['submit', 'export', 'generate', '导出', '生成'],
                        ['download', '下载'],
                    ];
                    for (const group of priorities) {
                        for (const b of btns) {
                            const t = (b.innerText || b.textContent || '').trim().toLowerCase();
                            const aria = (b.getAttribute('aria-label') || '').toLowerCase();
                            // 排除工具栏的原始 Download 按钮 (它旁边有 Print/Email)
                            if (group.includes('download')) {
                                const parent = b.parentElement;
                                if (parent) {
                                    const siblings = Array.from(parent.children);
                                    const hasPrint = siblings.some(s =>
                                        (s.innerText || '').toLowerCase().includes('print'));
                                    if (hasPrint) continue;  // 这是工具栏的 Download
                                }
                            }
                            for (const w of group) {
                                if (t === w || t.startsWith(w) || aria === w || aria.includes(w)) {
                                    b.click();
                                    return t || aria;
                                }
                            }
                        }
                    }
                    return '';
                }""")
                if found:
                    log(f"  JS 点击了按钮: {found}")
                else:
                    # 诊断: dump 页面上所有可见按钮
                    try:
                        btn_texts = page.evaluate("""() => {
                            const btns = document.querySelectorAll('button, [role="button"], a, [type="submit"]');
                            const texts = [];
                            for (const b of btns) {
                                const rect = b.getBoundingClientRect();
                                if (rect.width === 0 || rect.height === 0) continue;
                                if (b.offsetParent === null) continue;
                                const t = (b.innerText || b.textContent || '').trim();
                                const aria = b.getAttribute('aria-label') || '';
                                if (t || aria) texts.push(t.substring(0, 40) + '|' + aria.substring(0, 20));
                            }
                            return texts;
                        }""")
                        log(f"  当前可见按钮: {btn_texts}")
                    except Exception:
                        pass
                    raise RuntimeError("未找到动作按钮 (Continue/Download)")

            if action_btn is not None:
                log("  点击动作按钮触发下载")
                try:
                    action_btn.click(timeout=5000)
                except Exception:
                    try:
                        action_btn.click(force=True, timeout=5000)
                    except Exception:
                        pass

        # expect_download 成功捕获下载
        download = dl_info.value
        log(f"  下载成功: {download.suggested_filename}")
        download.save_as(str(target_path))

    except Exception as e:
        # expect_download 超时, 检查是否有新 tab 打开
        log(f"  expect_download 超时: {e}")
        pages = page.context.pages if page.context else []
        downloaded = False
        for p in reversed(pages):
            if p is not page:
                try:
                    p.wait_for_load_state("domcontentloaded", timeout=10000)
                except Exception:
                    pass
                new_url = p.url or ""
                if new_url.lower().endswith(".pdf") or "application/pdf" in new_url.lower():
                    try:
                        opener = _build_proxy_opener()
                        req = __import__("urllib.request", fromlist=["Request"]).Request(new_url)
                        with opener.open(req, timeout=60) as resp:
                            data = resp.read()
                        with open(target_path, "wb") as f:
                            f.write(data)
                        log(f"  从新 tab URL 下载 PDF: {target_path}")
                        p.close()
                        downloaded = True
                        break
                    except Exception as e2:
                        log(f"  从新 tab URL 下载 PDF 失败: {e2}")
                try:
                    pdf_src = p.evaluate("""() => {
                        const el = document.querySelector('iframe[src*=".pdf"], embed[src*=".pdf"], object[data*=".pdf"]');
                        if (el) return el.src || el.data;
                        return '';
                    }""")
                    if pdf_src:
                        opener = _build_proxy_opener()
                        import urllib.request
                        req = urllib.request.Request(pdf_src)
                        with opener.open(req, timeout=60) as resp:
                            data = resp.read()
                        with open(target_path, "wb") as f:
                            f.write(data)
                        log(f"  从 iframe/embed 下载 PDF: {target_path}")
                        p.close()
                        downloaded = True
                        break
                except Exception:
                    pass
                p.close()
        if not downloaded:
            raise RuntimeError(f"下载失败: {e}")

    # 关闭可能残留的 dialog
    try:
        dismiss_modal(page)
    except Exception:
        pass

    log(f"  保存 PDF: {target_path}")
    return target_path


def dismiss_modal(page):
    """关闭页面上的 modal/popup (按 Escape, 点击关闭按钮, 点击 overlay)。"""
    # 1) 按 Escape
    try:
        page.keyboard.press("Escape")
        page.wait_for_timeout(500)
    except Exception:
        pass
    # 2) 查找并点击关闭按钮
    for sel in [
        'button[aria-label*="close" i]',
        'button[aria-label*="Close" i]',
        'button:has-text("Close")',
        'button:has-text("×")',
        '[class*="close" i] button',
        'button[class*="close" i]',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=500):
                loc.click(timeout=2000)
                page.wait_for_timeout(500)
                break
        except Exception:
            continue
    # 3) 点击 overlay (modal 外区域)
    try:
        page.evaluate("""() => {
            const overlays = document.querySelectorAll(
                '[class*="overlay" i], [class*="backdrop" i], [class*="scrim" i]'
            );
            for (const o of overlays) {
                if (o.offsetParent !== null || o.offsetWidth > 0) {
                    o.click();
                    return true;
                }
            }
            return false;
        }""")
        page.wait_for_timeout(500)
    except Exception:
        pass


def save_search_result_pdf(page, input_name: str, context=None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUTPUT_DIR / f"{sanitize(input_name)} - search result.pdf"
    stabilize_for_pdf(page)
    # 点击页面 Download 按钮, 选择 PDF, 下载保存
    download_pdf(page, path, page_type="search")
    return path


def extract_profile_id(page) -> str:
    """从详情页 URL 或页面字段提取 profile id。"""
    url = page.url
    # /search/profile?id=12345678 (simple search 详情页)
    m = re.search(r"/search/profile\?id=([^&#]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    # /riskentities/profiles/{id}
    m = re.search(r"/riskentities/profiles/([^/?#]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    # 回退: URL 中任意 profile id 段 (profileId= / profile_id= / profile?id=)
    m = re.search(r"profile[_-]?id[=/?]([A-Za-z0-9-]+)", url, re.IGNORECASE)
    if m:
        return m.group(1)
    # 回退: 页面字段
    for sel in [
        '[data-testid*="profile" i][data-testid*="id" i]',
        'span:has-text("Profile ID")',
        'dt:has-text("Profile ID") + dd',
        'text="Profile ID: *"',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                txt = (loc.inner_text(timeout=800) or "")
                m = re.search(r"([A-Za-z0-9-]{4,})", txt)
                if m:
                    return m.group(1)
        except Exception:
            continue
    # 最后回退: 当前 URL 末段
    last = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    return last if last else "unknown"


def extract_result_name(page, clicked_text: str) -> str:
    """详情页正式名称优先, 否则用点击的文本。"""
    for sel in [
        'h1', 'h2', '[data-testid*="name" i]', '[class*="title" i]',
    ]:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=800):
                txt = (loc.inner_text(timeout=800) or "").strip()
                if txt and len(txt) < 300:
                    return txt
        except Exception:
            continue
    return clicked_text


def save_detail_pdf(page, input_name: str, result_name: str, profile_id: str, context=None) -> Path:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    # 用 input_name (模板输入名) 命名, 与 search result PDF 保持一致
    path = OUTPUT_DIR / f"{sanitize(input_name)} - {sanitize(profile_id)}.pdf"
    stabilize_for_pdf(page)
    # 点击页面 Download 按钮, 选择 PDF, 下载保存
    download_pdf(page, path, page_type="detail")
    return path


# ---------------------------------------------------------------------------
# 单行处理
# ---------------------------------------------------------------------------
def process_one(page, row, search_url: str, context=None):
    """处理单行名单, 返回 summary dict。"""
    input_name = row["name"]
    log(f"=== 处理: type={row['type']} name={input_name!r} ===")
    summary = {
        "type": row["type"],
        "name": input_name,
        "status": "failed",
        "pdfs": "",
        "error": "",
    }

    try:
        open_advanced_search(page, search_url)
        select_search_type(page, row["type"])
        fill_name_and_search(page, input_name)
        save_search_result_pdf(page, input_name, context)
        summary["pdfs"] = f"{sanitize(input_name)} - search result.pdf"

        results = detect_results(page)
        if not results:
            summary["status"] = "no_result"
            log(f"  无结果, 跳过详情页")
            return summary

        log(f"  找到 {len(results)} 个结果, 依次进入详情页")
        detail_pdfs = []
        for idx in range(len(results)):
            # 重新收集以避免 stale locator
            current_results = _collect_result_links(page)
            if idx >= len(current_results):
                break
            link = current_results[idx]
            clicked_text = ""
            try:
                clicked_text = (link.inner_text(timeout=1000) or "").strip()
            except Exception:
                pass
            url_before = page.url
            try:
                with page.expect_navigation(timeout=TIMEOUT_MS):
                    try:
                        link.click()
                    except Exception:
                        link.click(force=True)
            except PWTimeout:
                # SPA 可能不触发整页导航, 用点击 + 等待 URL 变化
                pass
            try:
                page.wait_for_load_state("domcontentloaded", timeout=TIMEOUT_MS)
            except PWTimeout:
                pass
            try:
                page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
            except PWTimeout:
                pass
            page.wait_for_timeout(1500)

            # 校验确实进入了详情页 (URL 含 profile id); 否则跳过此项
            profile_id = extract_profile_id(page)
            if not _looks_like_profile_href(page.url) and profile_id in ("unknown", "", "profile", "simple", "search"):
                log(f"  结果 {idx+1} 未进入详情页 (URL={page.url[:80]}), 跳过")
                # 回到结果页继续下一项
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    page.wait_for_timeout(1500)
                except Exception:
                    open_advanced_search(page, search_url)
                    select_search_type(page, row["type"])
                    fill_name_and_search(page, input_name)
                continue

            result_name = extract_result_name(page, clicked_text)
            detail_pdf = save_detail_pdf(page, input_name, result_name, profile_id, context)
            detail_pdfs.append(detail_pdf.name)

            # 返回结果页以便点击下一项 (若仍有)
            if idx < len(current_results) - 1:
                try:
                    page.go_back(wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    try:
                        page.wait_for_load_state("networkidle", timeout=TIMEOUT_MS)
                    except PWTimeout:
                        pass
                    page.wait_for_timeout(1500)
                except Exception:
                    # 回退: 重新搜索
                    open_advanced_search(page, search_url)
                    select_search_type(page, row["type"])
                    fill_name_and_search(page, input_name)

        summary["status"] = "success"
        summary["pdfs"] = summary["pdfs"] + "; " + "; ".join(detail_pdfs)
        return summary

    except Exception as e:
        summary["error"] = f"{type(e).__name__}: {e}"
        log(f"  处理失败: {summary['error']}")
        return summary


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def write_summary(records, path: Path):
    # 用 utf-8-sig (带 BOM) 写入, Excel 打开不乱码
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(
            f, fieldnames=["type", "name", "status", "pdfs", "error"]
        )
        writer.writeheader()
        for r in records:
            writer.writerow(r)


def print_stats(records):
    s = sum(1 for r in records if r["status"] == "success")
    n = sum(1 for r in records if r["status"] == "no_result")
    fail = sum(1 for r in records if r["status"] == "failed")
    log(f"=== 汇总: 成功 {s} 条 / 无结果 {n} 条 / 失败 {fail} 条 (共 {len(records)} 条) ===")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Dow Jones Name Screening 自动化")
    parser.add_argument("--template", default=str(BASE_DIR / "screening_list.csv"))
    parser.add_argument("--headless", default=None, help="覆盖 .dowjones.env 的 HEADLESS (true/false)")
    args = parser.parse_args()

    headless = HEADLESS if args.headless is None else (args.headless.lower() == "true")

    if not DJ_USER_ID or not DJ_PASSWORD:
        log("错误: 未配置 DJ_USER_ID / DJ_PASSWORD, 请检查 .dowjones.env")
        sys.exit(2)

    template_path = Path(args.template)
    if not template_path.exists():
        log(f"错误: 模板不存在: {template_path}")
        sys.exit(2)

    rows = read_template(template_path)
    if not rows:
        log("模板无有效行, 退出")
        sys.exit(0)
    log(f"模板载入 {len(rows)} 行待筛查")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    records = []

    # 自动读取 egress 代理 (沙箱环境通过本地代理出网)
    proxy_url = (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    )
    launch_kwargs = {"headless": headless}
    # 使用 puppeteer 已安装的 chrome (playwright 自带 chromium 未安装时)
    puppeteer_chrome = "/root/.cache/puppeteer/chrome/linux-151.0.7922.71/chrome-linux64/chrome"
    if os.path.exists(puppeteer_chrome):
        launch_kwargs["executable_path"] = puppeteer_chrome
        log(f"使用 chrome: {puppeteer_chrome}")
    if proxy_url:
        launch_kwargs["proxy"] = {"server": proxy_url}
        log(f"使用 egress 代理: {proxy_url}")

    with sync_playwright() as p:
        browser = p.chromium.launch(**launch_kwargs)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()
        page.set_default_timeout(TIMEOUT_MS)

        # 登录 (整个流程复用同一会话); 带重试应对网络抖动 / oauthcallback 偶发不跳转
        login_ok = False
        last_login_err = None
        for attempt in range(1, 6):
            try:
                log(f"登录尝试 {attempt}/5")
                login(page, DJ_BASE_URL)
                login_ok = True
                break
            except Exception as e:
                last_login_err = e
                log(f"  登录失败 ({attempt}/5): {type(e).__name__}: {e}")
                if attempt < 5:
                    # 重试前清空 cookies, 确保 SSO 重定向链从干净状态重新触发
                    # (上次失败可能残留半截 SSO cookie, 导致 riskcenter 不再展示 #okBtn / 登录表单)
                    try:
                        context.clear_cookies()
                        log("  已清空 cookies, 准备重试")
                    except Exception as ce:
                        log(f"  清空 cookies 失败: {ce}")
                    # 重置: 回到首页重新走流程
                    try:
                        page.goto(DJ_BASE_URL, wait_until="domcontentloaded", timeout=TIMEOUT_MS)
                    except Exception:
                        pass
                    page.wait_for_timeout(2000)
        if not login_ok:
            log(f"登录重试耗尽, 终止流程: {last_login_err}")
            for row in rows:
                records.append({
                    "type": row["type"], "name": row["name"],
                    "status": "failed", "pdfs": "",
                    "error": f"login_failed: {last_login_err}",
                })
            write_summary(records, OUTPUT_DIR / "run_summary.csv")
            print_stats(records)
            browser.close()
            sys.exit(1)

        # 逐行处理
        for row in rows:
            rec = process_one(page, row, DJ_ADVANCED_SEARCH_URL, context)
            records.append(rec)

        browser.close()

    write_summary(records, OUTPUT_DIR / "run_summary.csv")
    print_stats(records)


if __name__ == "__main__":
    main()
