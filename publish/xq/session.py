"""
雪球会话 —— Edge 持久化 profile + 人工过一次滑块。

【最终架构（2026-11-18 实测定型）】
1. **登录态靠 profile 里的持久化 cookie**
   实测：Edge 正常退出时会把 session cookie **转成带过期时间的持久化 cookie**：
       u            → 过期 22 天
       xq_a_token   → 过期  7 天
       xqat         → 过期  7 天
       xq_id_token  → 过期 22 天
   → 每次用 persistent_context 启动（Edge, headless=False）**自动就是已登录**，
     **不需要常驻浏览器，也不需要每次登录**。

2. **滑块（极验拼图）交给人做一次**
   程序化通过率很低（实测 3~10 次重试仍常失败），而且它是平台风控手段。
   → 用 `publish/xq/resident.py` 手动登录一次，cookie 落盘即可。

3. **必须 headless=False**
   headless 下雪球**不认登录态**（首页仍显示「立即登录/注册」），
   与「热门话题在 headless 下抓不到」是同一套反爬。

【为什么用 Edge 而不是 Chrome】
用户的日常浏览器是 Edge（且已登录雪球）。
"""

import socket
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import env_loader  # noqa: E402
from publish.xq.resident import CDP_PORT, CDP_URL, port_alive  # noqa: F401

ROOT = Path(__file__).parent.parent.parent
USER_DATA_DIR = ROOT / ".xq_profile"

HOME = "https://xueqiu.com/"


class XqSession:
    """雪球会话（支持 CDP 接管 / 自行启动两种模式）。"""

    def __init__(self, headless: bool = False, prefer_cdp: bool = False):
        self.headless = headless
        self.prefer_cdp = prefer_cdp
        self._p = None
        self.ctx = None
        self.page = None
        self.mode = None          # 'cdp' | 'launched'
        self._owns_browser = False

    # ---------- 生命周期 ----------
    def __enter__(self):
        self._p = sync_playwright().start()

        # 模式1：接管常驻浏览器
        if self.prefer_cdp and port_alive():
            try:
                browser = self._p.chromium.connect_over_cdp(CDP_URL)
                self.ctx = browser.contexts[0] if browser.contexts else browser.new_context()
                self.mode = "cdp"
                self.page = (self.ctx.pages[0] if self.ctx.pages
                            else self.ctx.new_page())
                self.page.set_viewport_size({"width": 1600, "height": 1000})
                print(f"✅ 已接管常驻浏览器（CDP {CDP_URL}）", flush=True)
                return self
            except Exception as e:
                print(f"⚠️ CDP 接管失败（{str(e)[:60]}），改为自行启动", flush=True)

        # 模式2：自行启动（持久化 profile）
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ctx = self._p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="msedge",
            headless=self.headless,
            args=[
                "--window-size=1600,1000", "--start-maximized",
                "--disable-blink-features=AutomationControlled",
                f"--remote-debugging-port={CDP_PORT}",
            ],
        )
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome = window.chrome || {runtime: {}};"
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_viewport_size({"width": 1600, "height": 1000})
        self.mode = "launched"
        self._owns_browser = True
        print(f"✅ 已启动常驻浏览器（CDP 端口 {CDP_PORT} 可供后续接管）", flush=True)
        return self

    def __exit__(self, *exc):
        # ⚠️ CDP 接管模式下**不要关浏览器** —— 那是常驻实例，登录态在里面
        try:
            if self._owns_browser:
                self.ctx.close()
        except Exception:
            pass
        try:
            self._p.stop()
        except Exception:
            pass

    def release(self) -> None:
        """
        释放浏览器（关闭它，但**保留 profile**）。

        ⚠️ 用途：发布动作要交给**独立子进程**（实测子进程里 100% 成功，
           集成流程里会因页面状态残留而失败）。
           同一个 profile 不能同时开两个浏览器 → 主流程要先 release。
        """
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        try:
            if self._p:
                self._p.stop()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        self._p = None
        time.sleep(2)

    # ---------- 基础操作 ----------
    def open(self) -> None:
        try:
            self.page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            time.sleep(3)
            self.page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)

    def is_logged_in(self) -> bool:
        """
        判定登录态。

        ⚠️⚠️ **判据必须用完整短语「立即登录/注册」**：
        单独用「立即登录」太宽 —— 页面别处（弹窗/下载App挂件）也有这三个字，
        会把**已登录**误判成未登录（我踩过这个坑）。
        """
        # ⚠️⚠️ **页面还是空白页时必须先 open()** ——
        #    新启动的 context 里 page 是 about:blank，body 为空，
        #    直接读会误判成"未登录"（我踩过这个坑）。
        try:
            url = self.page.url
        except Exception:
            url = ""
        if not url or url.startswith("about:") or url == "blank":
            self.open()

        try:
            body = self.page.inner_text("body")
        except Exception:
            return False
        if not body:
            return False
        if "立即登录/注册" in body:
            return False
        return True

    def bring_to_front(self) -> bool:
        """把浏览器窗口置前（让用户能看到）。"""
        try:
            self.page.bring_to_front()
            return True
        except Exception:
            return False

    def wait_for_login(self, timeout: int = 900) -> bool:
        """
        等待用户完成登录（含滑块）。

        每 3 秒检查一次登录态；检测到滑块时给出提示。
        """
        deadline = time.time() + timeout
        last_hint = 0
        while time.time() < deadline:
            if self.is_logged_in():
                print("✅ 检测到登录成功", flush=True)
                return True
            try:
                body = self.page.inner_text("body")
                need_slider = ("拖动滑块完成拼图" in body or "拖动下方滑块" in body)
            except Exception:
                need_slider = False

            if time.time() - last_hint > 20:
                last_hint = time.time()
                self.bring_to_front()
                if need_slider:
                    print("   ⏳ 等你拖动滑块完成拼图验证…", flush=True)
                else:
                    print("   ⏳ 等你完成登录（点「登 录」）…", flush=True)
            time.sleep(3)
        return False


def start_resident_browser() -> bool:
    """
    启动常驻浏览器（供用户登录）。

    单独进程运行，登录后**保持运行**，后续流程用 CDP 接管。
    """
    if port_alive():
        print(f"✅ 常驻浏览器已在运行（CDP {CDP_URL}）", flush=True)
        return True

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=False,
            args=[
                "--window-size=1600,1000", "--start-maximized",
                f"--remote-debugging-port={CDP_PORT}",
            ],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)

        print("=" * 62, flush=True)
        print("  请在弹出的浏览器窗口里完成雪球登录", flush=True)
        print("  （账号密码已填好 → 点「登 录」→ 拖动滑块）", flush=True)
        print("  登录成功后本窗口会自动关闭并保存登录态", flush=True)
        print("=" * 62, flush=True)

        # 自动填表（减少用户操作）
        try:
            _prefill(page)
        except Exception:
            pass

        deadline = time.time() + 1800
        while time.time() < deadline:
            try:
                body = page.inner_text("body")
                if ("立即登录" not in body) and ("登录/注册" not in body) and body:
                    print("\n✅ 登录成功！正在保存登录态…", flush=True)
                    time.sleep(3)
                    # 保持浏览器运行一小段时间让 cookie 落盘
                    ctx.close()
                    print("✅ 已保存（常驻浏览器已启动，端口 9223）", flush=True)
                    return True
            except Exception:
                pass
            time.sleep(3)

        print("⏱️ 等待超时", flush=True)
        ctx.close()
        return False


def _prefill(page) -> None:
    """自动填账号密码并勾协议（用户只需点登录+拖滑块）。"""
    env = env_loader.load_env()

    try:
        page.locator("xpath=//a[normalize-space(text())='账号密码登录']").first.click(
            timeout=5000, force=True)
        time.sleep(2.5)
    except Exception:
        pass

    for name, key in (("username", "xq_user"), ("password", "xq_passwd")):
        try:
            el = page.query_selector(f"input[name='{name}']")
            if el and el.is_visible() and env.get(key):
                el.click()
                time.sleep(0.2)
                el.press("Control+A")
                el.press("Delete")
                el.type(env[key], delay=55)
                time.sleep(0.3)
        except Exception:
            pass

    # 勾协议（判据：含 newLogin_nochecked = 未勾）
    try:
        page.evaluate(r"""() => {
            const lab = document.querySelector('label[for="terms-law"]')
                || [...document.querySelectorAll('label')]
                     .find(e => (e.textContent||'').includes('阅读并同意'));
            if (!lab) return;
            const icons = [...lab.querySelectorAll('i')];
            if (!icons.length) return;
            if ((icons[0].className||'').includes('newLogin_nochecked')) {
                icons[0].click();
            }
        }""")
        time.sleep(0.8)
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    if "--check" in sys.argv:
        alive = port_alive()
        print(f"常驻浏览器: {'运行中' if alive else '未运行'}")
        if alive:
            with XqSession() as s:
                s.open()
                print(f"登录态: {'✅ 已登录' if s.is_logged_in() else '❌ 未登录'}")
    else:
        start_resident_browser()
