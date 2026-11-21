"""
雪球常驻浏览器 —— 用 subprocess 独立启动 Edge/Chrome。

【为什么必须用 subprocess 而不是 Playwright 的 launch_persistent_context】
Playwright 启动的浏览器是**Playwright 的子进程**：
  · `ctx.close()` → 浏览器被关掉
  · Python 进程退出 → Playwright 清理 → 浏览器也被关掉
  · 而 Chrome 的 **session cookie（xq_a_token）在进程退出时不持久化**
    → 登录态随之丢失
→ 所以必须让浏览器**独立于任何 Python 进程**存活。

【架构】
1. `start_resident()` 用 subprocess 启动 Edge（独立进程，带 --remote-debugging-port=9223）
2. 用 CDP 连上去**自动预填账号密码 + 勾协议**（用户只需点登录+拖滑块）
3. 用户登录成功 → 浏览器**继续运行**，登录态常驻
4. 后续所有流程 `connect_over_cdp("http://127.0.0.1:9223")` 接管

【浏览器选择】
优先用 **Microsoft Edge**（用户日常用的那个，且已登录雪球），
没有则用 Chrome。
"""

import os
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import env_loader  # noqa: E402

ROOT = Path(__file__).parent.parent.parent
USER_DATA_DIR = ROOT / ".xq_profile"
CDP_PORT = 9223
CDP_URL = f"http://127.0.0.1:{CDP_PORT}"
HOME = "https://xueqiu.com/"

BROWSERS = [
    ("Edge", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    ("Edge", r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
    ("Chrome", r"C:\Program Files\Google\Chrome\Application\chrome.exe"),
    ("Chrome", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe"),
]


def find_browser():
    for name, path in BROWSERS:
        if os.path.exists(path):
            return name, path
    return None, None


def port_alive(port: int = CDP_PORT) -> bool:
    import socket
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1.5):
            return True
    except OSError:
        return False


def start_resident(wait_for_login: bool = True, timeout: int = 1800) -> bool:
    """
    启动常驻浏览器（独立进程）。

    wait_for_login=True 时：连上去预填表单，等用户完成登录+滑块。
    """
    if port_alive():
        print(f"✅ 常驻浏览器已在运行（CDP {CDP_URL}）", flush=True)
        if wait_for_login:
            return _wait_and_prefill(timeout)
        return True

    name, path = find_browser()
    if not path:
        print("❌ 未找到 Chrome/Edge 可执行文件", flush=True)
        return False

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    args = [
        path,
        f"--remote-debugging-port={CDP_PORT}",
        f"--user-data-dir={USER_DATA_DIR}",
        "--no-first-run",
        "--no-default-browser-check",
        "--new-window",
        HOME,
    ]
    print(f"🚀 启动常驻浏览器: {name}", flush=True)
    print(f"   {path}", flush=True)
    print(f"   CDP: {CDP_URL}", flush=True)

    # DETACHED_PROCESS：让它独立于本 Python 进程存活
    DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        args, creationflags=DETACHED, close_fds=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )

    # 等端口起来
    for _ in range(40):
        time.sleep(0.5)
        if port_alive():
            print("   ✅ 调试端口已就绪\n", flush=True)
            break
    else:
        print("   ❌ 调试端口未就绪", flush=True)
        return False

    if wait_for_login:
        return _wait_and_prefill(timeout)
    return True


def _wait_and_prefill(timeout: int = 1800) -> bool:
    """连上 CDP，预填表单，等用户完成登录。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(CDP_URL)
        except Exception as e:
            print(f"❌ CDP 连接失败: {str(e)[:80]}", flush=True)
            return False

        ctx = browser.contexts[0] if browser.contexts else browser.new_context()
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        try:
            page.bring_to_front()
        except Exception:
            pass
        page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(6)

        def logged_in() -> bool:
            try:
                body = page.inner_text("body")
                return bool(body) and ("立即登录" not in body) and ("登录/注册" not in body)
            except Exception:
                return False

        if logged_in():
            print("✅ 已是登录态", flush=True)
            return True

        _prefill(page)

        print("=" * 62, flush=True)
        print("  👉 请在浏览器窗口里完成雪球登录", flush=True)
        print("     ① 点蓝色的「登 录」", flush=True)
        print("     ② 拖动滑块完成拼图验证", flush=True)
        print("  完成后会自动保存，浏览器保持运行供后续复用", flush=True)
        print("=" * 62 + "\n", flush=True)

        deadline = time.time() + timeout
        last = 0
        while time.time() < deadline:
            if logged_in():
                print("\n✅ 登录成功！登录态已保存在常驻浏览器中", flush=True)
                try:
                    ck = sorted(c["name"] for c in ctx.cookies())
                    auth = [n for n in ck if n in ("u", "xq_a_token", "xqat", "xq_id_token")]
                    print(f"   登录 cookie: {auth}", flush=True)
                except Exception:
                    pass
                return True
            if time.time() - last > 20:
                last = time.time()
                try:
                    page.bring_to_front()
                except Exception:
                    pass
                body = page.inner_text("body")
                need = "拖动滑块完成拼图" in body or "拖动下方滑块" in body
                print(f"   ⏳ {'等你拖动滑块…' if need else '等你点登录…'}", flush=True)
            time.sleep(3)

        print("⏱️ 等待超时", flush=True)
        return False


def _prefill(page) -> None:
    """自动填账号密码 + 勾协议。"""
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


def stop_resident() -> bool:
    """关闭常驻浏览器。"""
    import psutil
    killed = 0
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cl = " ".join(proc.info["cmdline"] or [])
            if f"--remote-debugging-port={CDP_PORT}" in cl:
                proc.terminate()
                killed += 1
        except Exception:
            pass
    time.sleep(2)
    return killed > 0


if __name__ == "__main__":
    if "--stop" in sys.argv:
        print("已关闭常驻浏览器" if stop_resident() else "未找到常驻浏览器")
    elif "--check" in sys.argv:
        print(f"常驻浏览器: {'运行中' if port_alive() else '未运行'}")
    else:
        start_resident(wait_for_login=True)
