"""
浏览器连接层 —— 统一管理小红书会话的获取方式。

支持两种模式，按优先级自动选择：

  模式 A（CDP 接管，优先）
      连接你日常使用的那个已登录 Chrome。
      优点：登录态天然存在、不会污染独立 profile、账号风控友好（固定设备）。
      前置：Chrome 需以 --remote-debugging-port=9222 启动。

  模式 B（独立 profile）
      启动一个独立的 Chrome 实例，用 .browser_profile 存放登录态。
      优点：与日常浏览器完全隔离。
      前置：需要在本窗口内完成一次登录。

检测逻辑：
      先探 9222 端口是否可连 → 可连走 CDP；否则走独立 profile。
"""

import socket
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
USER_DATA_DIR = ROOT / ".browser_profile"
PUBLISH_URL = "https://creator.xiaohongshu.com/publish/publish"

CDP_PORT = 9222
CDP_HOST = "127.0.0.1"

# 登录墙特征元素（创作服务平台的登录页 URL 与发布页相同，只能靠元素判断）
LOGIN_WALL_MARKERS = [
    "text=短信登录",
    "text=验证码登录",
    "text=发送验证码",
    "text=扫码登录",
    "text=登录后可发布",
    "text=加入我们",
    "input[placeholder*='验证码']",
]

# 发布页关键元素（出现即说明已登录且页面已渲染）
PUBLISH_READY_MARKERS = [
    "text=上传图文",
    "text=上传视频",
    "input[type='file']",
]


def is_cdp_available(port: int = CDP_PORT) -> bool:
    """探测远程调试端口是否可用。"""
    try:
        with socket.create_connection((CDP_HOST, port), timeout=2):
            return True
    except OSError:
        return False


def on_login_wall(page) -> bool:
    """
    页面是否仍停留在登录墙。

    判定逻辑：必须同时满足
      1) 出现登录墙特征元素
      2) 未出现任何发布页就绪元素
    否则一律视为已登录 —— 避免页面渲染慢时误判为未登录。
    """
    wall_found = False
    for sel in LOGIN_WALL_MARKERS:
        try:
            el = page.wait_for_selector(sel, timeout=800, state="visible")
            if el:
                wall_found = True
                break
        except Exception:
            continue
    if not wall_found:
        return False
    # 有登录墙特征，但若发布页元素也出现了，说明是残留/过渡态，不算未登录
    for sel in PUBLISH_READY_MARKERS:
        try:
            el = page.wait_for_selector(sel, timeout=600, state="attached")
            if el:
                return False
        except Exception:
            continue
    return True


def publish_page_ready(page) -> bool:
    """发布页是否已就绪（说明登录成功且进入发布流程）。"""
    for sel in PUBLISH_READY_MARKERS:
        try:
            el = page.wait_for_selector(sel, timeout=1500, state="attached")
            if el:
                return True
        except Exception:
            continue
    return False


class BrowserSession:
    """统一封装：无论 CDP 还是独立 profile，都提供 .page 和 .ctx。"""

    def __init__(self, p, ctx, page, mode: str, owns_p: bool = True):
        self._p = p
        self.ctx = ctx
        self.page = page
        self.mode = mode
        self._owns_p = owns_p

    def close(self):
        try:
            # CDP 模式下不能关用户的日常浏览器，只断开连接
            self.ctx.close()
        except Exception:
            pass
        if self._owns_p:
            try:
                self._p.stop()
            except Exception:
                pass


def open_session(headless: bool = False, force_profile: bool = False, p=None):
    """
    打开浏览器会话，返回 BrowserSession。

    p: 可选的外部 Playwright 实例。若传入，调用方负责其生命周期；
       若为 None，本函数会自行创建（此时 session.close() 会一并关闭它）。
    """
    owns_p = p is None
    if owns_p:
        p = sync_playwright().start()

    if not force_profile and is_cdp_available():
        browser = p.chromium.connect_over_cdp(f"http://{CDP_HOST}:{CDP_PORT}")
        ctx = browser.contexts[0] if browser.contexts else browser.new_context(
            locale="zh-CN", timezone_id="Asia/Shanghai", viewport={"width": 1440, "height": 900}
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        return BrowserSession(p, ctx, page, "cdp", owns_p)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(USER_DATA_DIR),
        channel="chrome",
        headless=headless,
        viewport={"width": 1440, "height": 900},
        locale="zh-CN",
        timezone_id="Asia/Shanghai",
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.add_init_script(
        "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
    )
    return BrowserSession(p, ctx, page, "profile", owns_p)
