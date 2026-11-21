"""
抖音创作者中心 —— 会话层。

【与小红书的关键差异】

小红书：creator.xiaohongshu.com/publish/publish（URL 固定直达发布页）
抖音：  creator.douyin.com 是**纯 SPA**，
        未登录时任何 /creator-micro/* 路径都返回 200 但渲染成登录页。
        → **不能靠 URL 猜入口**，必须登录后从导航 DOM 里找。

【登录方式】
抖音创作者中心支持：
  · APP 扫码（最常用）
  · 手机号 + 验证码
  · 密码登录
本模块支持前两种，登录态存 .douyin_profile 长期复用。

【为什么必须用独立 profile】
抖音对"设备指纹 + 登录环境"更敏感：
频繁切换设备/地域的账号容易被判定异常。
固定一个独立 profile 长期使用，比接管日常浏览器更稳。
"""

import socket
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent.parent
USER_DATA_DIR = ROOT / ".douyin_profile"

CREATOR_HOME = "https://creator.douyin.com/creator-micro/home"
ARTICLE_ENTRY_CANDIDATES = [
    "https://creator.douyin.com/creator-micro/content/article",
    "https://creator.douyin.com/creator-micro/content/post",
]

# 登录墙特征
LOGIN_MARKERS = [
    "text=扫码登录",
    "text=验证码登录",
    "text=密码登录",
    "text=获取验证码",
    "text=打开「抖音APP」点击左上角",
]

# 登录成功后的特征（侧边导航）
READY_MARKERS = [
    "text=发布作品",
    "text=内容管理",
    "text=首页",
    "text=数据中心",
    "text=创作灵感",
]


def on_login_wall(page) -> bool:
    """判断是否停在登录墙。"""
    try:
        body = page.inner_text("body")
    except Exception:
        return True
    return any(m.strip("text=") in body for m in LOGIN_MARKERS)


class DouyinSession:
    """抖音创作者中心会话。"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._p = None
        self.ctx = None
        self.page = None
        self.log: list[str] = []

    def __enter__(self):
        self._p = sync_playwright().start()
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ctx = self._p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=self.headless,
            args=["--window-size=1600,1000", "--disable-blink-features=AutomationControlled"],
        )
        # ⚠️ 关键：add_init_script 必须在**任何页面加载之前**注册，
        #    否则对已经加载的页面无效（navigator.webdriver 不会被覆盖）。
        #    这直接导致抖音返回 ERR_CONNECTION_ABORTED —— 实测：
        #    带 init_script 的实例能正常访问，不带的立刻 abort。
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'plugins',{get:()=>[1,2,3]});"
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

    # ---------- 登录 ----------
    def open(self) -> None:
        """
        打开创作者中心首页（建立会话）。

        ⚠️ 实测：直接 goto 深层路由（编辑器）会 ERR_CONNECTION_ABORTED。
        抖音会校验会话初始化流程 —— 必须先访问首页，再走站内导航。
        """
        try:
            self.page.goto(CREATOR_HOME, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            # 偶发 abort 时重试一次
            time.sleep(3)
            self.page.goto(CREATOR_HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)

    def is_logged_in(self) -> bool:
        try:
            body = self.page.inner_text("body")
        except Exception:
            return False
        if on_login_wall(self.page):
            return False
        return any(m.strip("text=") in body for m in READY_MARKERS)

    def wait_for_login(self, timeout: int = 600) -> bool:
        """等待用户扫码/验证码登录完成。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_logged_in():
                self.log.append("登录成功")
                return True
            time.sleep(3)
        return False

    def show_login_hint(self) -> None:
        """在页面上标出登录区域，方便用户操作。"""
        try:
            self.page.evaluate(r"""
                () => {
                    const mark = document.createElement('div');
                    mark.id = 'ax-dy-hint';
                    mark.style.cssText = [
                        'position:fixed', 'top:12px', 'left:12px', 'z-index:2147483647',
                        'background:#ff5c3c', 'color:#fff', 'font-size:15px',
                        'padding:14px 20px', 'border-radius:10px', 'line-height:1.7',
                        'box-shadow:0 6px 24px rgba(0,0,0,.3)', 'max-width:340px'
                    ].join(';');
                    mark.innerHTML = 'AXIOM 自动发布<br>请在此页完成抖音扫码登录<br>登录后本窗口会自动继续';
                    document.body.appendChild(mark);
                }
            """)
        except Exception:
            pass

    # ---------- 文章入口发现 ----------
    def find_article_entry(self) -> str | None:
        """
        登录后从侧边导航里找「文章 / 长图文 / 图文」入口。

        抖音文章功能 2025-12-26 上线，入口在创作者中心
        「高清发布 - 发布文章」，但具体路由需登录后从 DOM 确认。
        """
        try:
            # 1) 先找导航里的文字
            for el in self.page.query_selector_all("a,li,span,div,button"):
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                    if t in ("发布文章", "长图文", "写文章", "文章"):
                        href = el.get_attribute("href")
                        if href:
                            return href
                except Exception:
                    continue

            # 2) 找「高清发布」并展开
            for el in self.page.query_selector_all("a,li,span,div,button"):
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                    if "高清发布" in t or "发布作品" in t:
                        el.click()
                        time.sleep(2.5)
                        break
                except Exception:
                    continue

            # 3) 展开后重新找
            for el in self.page.query_selector_all("a,li,span,div,button"):
                try:
                    if not el.is_visible():
                        continue
                    t = (el.inner_text() or "").strip()
                    if t in ("发布文章", "长图文", "写文章"):
                        href = el.get_attribute("href")
                        if href:
                            return href
                except Exception:
                    continue
        except Exception:
            pass
        return None

    def goto_article_editor(self) -> bool:
        """进入文章编辑器。"""
        href = self.find_article_entry()
        urls = ([href] if href else []) + ARTICLE_ENTRY_CANDIDATES
        for u in urls:
            if not u:
                continue
            if not u.startswith("http"):
                u = "https://creator.douyin.com" + u
            try:
                self.page.goto(u, wait_until="domcontentloaded", timeout=45000)
                time.sleep(5)
                if not on_login_wall(self.page):
                    body = self.page.inner_text("body")
                    if "文章" in body or "标题" in body or "正文" in body:
                        self.log.append(f"已进入文章编辑器: {self.page.url}")
                        return True
            except Exception:
                continue
        return False
