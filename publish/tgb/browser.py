"""
淘股吧会话层。

【与抖音/小红书的关键差异】

淘股吧是**老式站点**（jQuery + iframe 编辑器），
但它的风控机制更严（先审后发 + 小黑屋案底制），
所以自动化上的要求反而是**更保守**，不是更激进。

会话策略沿用"独立 profile + 长期复用"：
  · 淘股吧用手机认证发言制，一个手机号最多一个交流笔名
  · 设备指纹频繁变化会增加被风控概率 → 固定一个 profile 长期用
"""

import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent.parent
USER_DATA_DIR = ROOT / ".tgb_profile"

HOME = "https://www.tgb.cn/"
NEW_TOPIC = "https://www.tgb.cn/topic/newTopic?davFlag=dv"
SSO_LOGIN = "https://sso.tgb.cn/web/login/index"

# 未登录标志
LOGIN_MARKERS = ["扫码登录", "账号登录", "免密登录注册", "极速登录"]

# 已登录标志
READY_MARKERS = ["我的关注", "我的股票", "我的收藏", "退出登录", "个人中心"]


class TgbSession:
    """淘股吧会话。"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._p = None
        self.ctx = None
        self.page = None

    def __enter__(self):
        self._p = sync_playwright().start()
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ctx = self._p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=self.headless,
            args=["--window-size=1600,1000", "--disable-blink-features=AutomationControlled"],
        )
        # ⚠️ add_init_script 必须在任何页面加载前注册（抖音踩过这个坑）
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']});"
            "window.chrome = window.chrome || {runtime: {}};"
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_viewport_size({"width": 1600, "height": 1000})
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self._p.stop()
        except Exception:
            pass

    def open(self) -> None:
        """打开首页（建立会话）。"""
        try:
            self.page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            time.sleep(3)
            self.page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)

    def is_logged_in(self) -> bool:
        """判断登录态。"""
        try:
            body = self.page.inner_text("body")
        except Exception:
            return False
        if any(m in body for m in LOGIN_MARKERS):
            # 有登录表单 → 未登录
            for sel in ["input[type='password']", "input[placeholder*='验证码']",
                        "input[placeholder*='手机号']"]:
                try:
                    for el in self.page.query_selector_all(sel):
                        if el.is_visible():
                            return False
                except Exception:
                    continue
        return any(m in body for m in READY_MARKERS)

    def wait_for_login(self, timeout: int = 900) -> bool:
        deadline = time.time() + timeout
        last = 0
        while time.time() < deadline:
            try:
                self.page.goto(NEW_TOPIC, wait_until="domcontentloaded", timeout=60000)
                time.sleep(4)
                if "sso.tgb.cn" not in self.page.url:
                    body = self.page.inner_text("body")
                    if any(k in body for k in ["标题", "发表", "分类", "正文"]):
                        return True
            except Exception:
                pass
            if time.time() - last > 30:
                last = time.time()
                print("   等待登录中...", flush=True)
            time.sleep(3)
        return False

    def show_login_hint(self) -> None:
        try:
            self.page.evaluate(r"""
                () => {
                    const m = document.createElement('div');
                    m.id = 'ax-tgb-hint';
                    m.style.cssText = [
                        'position:fixed','top:10px','right:10px','z-index:2147483647',
                        'background:#e34d59','color:#fff','font-size:15px','padding:12px 18px',
                        'border-radius:8px','line-height:1.6','box-shadow:0 4px 16px rgba(0,0,0,.25)'
                    ].join(';');
                    m.innerHTML = 'AXIOM 自动发布<br>请在此页完成淘股吧登录（微信扫码最快）';
                    document.body.appendChild(m);
                }
            """)
        except Exception:
            pass
