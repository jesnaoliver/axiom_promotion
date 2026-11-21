"""
淘股吧登录。

用法：
  python publish/tgb/login.py          # 打开浏览器，自动登录或等待扫码
  python publish/tgb/login.py --scan   # 强制等待扫码
  python publish/tgb/login.py --check  # 只检查登录态

【登录方式】
1. .env 里的 xgb_user / xgb_passwd → 账号密码登录（优先，自动）
2. 微信扫码 / 手机验证码（.env 无凭证时兜底，等用户操作）

登录态存 .tgb_profile 长期复用。
"""

import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

ROOT = Path(__file__).parent.parent.parent
USER_DATA_DIR = ROOT / ".tgb_profile"

HOME = "https://www.tgb.cn/"
NEW_TOPIC = "https://www.tgb.cn/topic/newTopic?davFlag=dv"
SSO_LOGIN = "https://sso.tgb.cn/web/login/index"


def load_env() -> dict:
    """读取 .env（不覆盖已存在的环境变量）。"""
    env_file = ROOT / ".env"
    out = {}
    if not env_file.exists():
        return out
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip().strip('"').strip("'")
        out[k] = v
        os.environ.setdefault(k, v)
    return out


def _is_logged_in(page) -> bool:
    """
    判断是否已登录：发帖页不再跳 SSO。

    这是最可靠的判据 —— 不看页面上有没有"登录"字样
    （未登录时页头也有"登录"入口，会误判），
    而是看**发帖页能不能打开**。
    """
    try:
        page.goto(NEW_TOPIC, wait_until="domcontentloaded", timeout=60000)
        time.sleep(4)
    except Exception:
        return False
    if "sso.tgb.cn" in page.url:
        return False
    try:
        body = page.inner_text("body")
    except Exception:
        return False
    return any(k in body for k in ["标题", "发表", "分类", "正文", "发帖"])


def try_sms_login(page, phone: str) -> bool:
    """
    手机号 + 短信验证码登录（**淘股吧唯一支持的密码类方式**）。

    实测结构（2026-11-09）：
      input#userPanelName2  placeholder="输入手机号"
      input#yzmNumber       placeholder="请输入6位验证码"
    ⚠️ **没有密码输入框** —— 淘股吧不支持账号密码登录，
       .env 里的 xgb_passwd 对应字段不存在。
       另一条路是「极速登录」（扫码/微信），见 --scan。
    """
    try:
        page.goto(SSO_LOGIN + "?url=" + NEW_TOPIC,
                  wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
    except Exception:
        return False

    # 填手机号
    try:
        inp = page.query_selector("#userPanelName2")
        if inp and inp.is_visible():
            inp.click(timeout=4000)
            time.sleep(0.2)
            inp.fill("")
            inp.type(phone, delay=45)
            time.sleep(0.5)
            print(f"   已填手机号: {phone}")
    except Exception as e:
        print(f"   填手机号失败: {str(e)[:80]}")
        return False

    # 点「获取验证码」
    try:
        for el in page.query_selector_all("a,span,div,button"):
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if "获取验证码" in t or "发送验证码" in t:
                el.click(timeout=4000)
                time.sleep(2)
                print("   已点击获取验证码")
                break
    except Exception:
        pass
    return True


def wait_code_and_login(page, timeout: int = 600) -> bool:
    """
    等待 6 位验证码并自动提交。

    两种输入方式，都支持：
      A. 在浏览器窗口里手动填（推荐，验证码即时可见）
      B. 把验证码发到终端 —— 脚本会监听 stdin

    B 的实现：把 stdin 设为非阻塞，逐行读。
    这样即使用户不看浏览器，只在终端敲 6 位数字也能完成。
    """
    print("\n" + "=" * 58)
    print("  请输入收到的 6 位短信验证码")
    print("=" * 58)
    print("  方式 A：直接在浏览器窗口的验证码框里输入并提交")
    print("  方式 B：在本终端里输入 6 位数字后回车（脚本会自动填入并提交）")
    print()

    deadline = time.time() + timeout
    last = 0
    stdin_ready = False
    try:
        import select
        stdin_ready = True
    except Exception:
        pass

    while time.time() < deadline:
        # 1) 检查是否已在浏览器里登录成功
        try:
            if _is_logged_in(page):
                return True
        except Exception:
            pass

        # 2) 读终端输入的验证码
        if stdin_ready:
            try:
                r, _, _ = select.select([sys.stdin], [], [], 1.0)
                if r:
                    line = sys.stdin.readline().strip()
                    code = re.sub(r"\D", "", line)
                    if len(code) >= 6:
                        print(f"   收到验证码，提交中…")
                        if submit_code(page, code):
                            time.sleep(4)
                            if _is_logged_in(page):
                                return True
                        else:
                            print("   ⚠️ 提交失败，请改用浏览器窗口手动输入")
                    elif code:
                        print(f"   验证码位数不足（收到 {len(code)} 位）")
            except Exception:
                stdin_ready = False

        if time.time() - last > 20:
            last = time.time()
            print("   等待验证码登录中...", flush=True)
        time.sleep(2)

    return False


def submit_code(page, code: str) -> bool:
    """把验证码填入并提交。"""
    try:
        inp = page.query_selector("#yzmNumber")
        if not inp or not inp.is_visible():
            return False
        inp.click(timeout=4000)
        time.sleep(0.2)
        inp.fill("")
        inp.type(code, delay=45)
        time.sleep(0.4)
        # 点登录按钮
        for el in page.query_selector_all("button,input[type=submit],a,span,div"):
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("value") or "").strip()
                if t in ("登录", "确定", "立即登录", "登 录"):
                    el.click(timeout=4000)
                    return True
            except Exception:
                continue
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
        return True
    except Exception:
        return False


def open_login_popup(page) -> bool:
    """
    打开首页右上角的登录/注册弹窗。

    实测（2026-11-09）：登录是**站内弹窗**，不是独立 SSO 页。
    入口：首页 A.header-a 的「登录/注册」。
    """
    try:
        page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)
    except Exception:
        return False

    for el in page.query_selector_all("a,span,div,button"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip().replace(" ", "")
            if t in ("登录/注册", "注册登录", "登录"):
                el.click(timeout=5000)
                time.sleep(3.5)
                return True
        except Exception:
            continue
    return False


def switch_to_account_tab(page) -> bool:
    """
    切到「账号登录」标签。

    ⚠️ 这是关键一步 —— 弹窗默认停在「极速登录」/「免密登录注册」，
    那个标签下只有手机号+验证码，**没有密码框**。
    切到「账号登录」后才会出现「笔名 + 密码」两个框。
    """
    for el in page.query_selector_all("span,div,a,li"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "").strip()
            if t != "账号登录":
                continue
            el.click(timeout=5000)
            time.sleep(2.5)
            return True
        except Exception:
            continue
    return False


def fill_account_login(page, penname: str, password: str) -> bool:
    """
    填「笔名 + 密码」并提交。

    实测（2026-11-09，切到「账号登录」标签后）：
      · 笔名   input#userPanelName   placeholder="输入手机号/笔名"
      · 密码   input#userPanelPwd    placeholder="请输入6~18位英文或者数字密码"
      · 按钮   .login-button          （300×42）
    ⚠️⚠️ **id 会随标签切换而变**：
      极速登录标签下是 #userPanelName2 / #yzmNumber（手机号+验证码），
      账号登录标签下变成 #userPanelName / #userPanelPwd。
      之前用通用 placeholder 匹配会误填到错误标签的框里。
      → 必须先切标签，再用**精确 id** 定位。
    """
    filled_name = filled_pwd = False

    # 优先用精确 id
    try:
        nm = page.query_selector("#userPanelName")
        if nm and nm.is_visible():
            nm.click(timeout=4000)
            time.sleep(0.2)
            nm.fill("")
            nm.type(penname, delay=45)
            filled_name = True
    except Exception:
        pass
    try:
        pw = page.query_selector("#userPanelPwd")
        if pw and pw.is_visible():
            pw.click(timeout=4000)
            time.sleep(0.2)
            pw.fill("")
            pw.type(password, delay=45)
            filled_pwd = True
    except Exception:
        pass

    # 兜底：按 placeholder
    if not (filled_name and filled_pwd):
        for el in page.query_selector_all("input"):
            try:
                if not el.is_visible():
                    continue
                ph = (el.get_attribute("placeholder") or "").strip()
                ty = (el.get_attribute("type") or "").lower()
                if ty in ("hidden", "button", "submit", "radio", "checkbox"):
                    continue
                if "密码" in ph or ty == "password":
                    if not filled_pwd:
                        el.click(timeout=4000); time.sleep(0.2)
                        el.fill(""); el.type(password, delay=45)
                        filled_pwd = True
                elif "笔名" in ph and not filled_name:
                    el.click(timeout=4000); time.sleep(0.2)
                    el.fill(""); el.type(penname, delay=45)
                    filled_name = True
            except Exception:
                continue

    if not (filled_name and filled_pwd):
        print(f"   表单填充失败: 笔名={filled_name} 密码={filled_pwd}")
        return False

    time.sleep(0.5)
    # 点 .login-button
    clicked = False
    try:
        btn = page.query_selector(".login-button")
        if btn and btn.is_visible():
            btn.click(timeout=5000)
            clicked = True
    except Exception:
        pass
    if not clicked:
        for el in page.query_selector_all("button,a,span,div,input[type=submit]"):
            try:
                if not el.is_visible():
                    continue
                t = (el.inner_text() or el.get_attribute("value") or "").strip()
                if t in ("登录", "立即登录", "确定"):
                    el.click(timeout=5000)
                    clicked = True
                    break
            except Exception:
                continue
    if not clicked:
        try:
            page.keyboard.press("Enter")
        except Exception:
            pass
    return True


def read_login_error(page) -> str:
    """读取登录失败提示（如"密码错误""账号锁定"）。"""
    try:
        body = page.inner_text("body")
    except Exception:
        return ""
    for kw in ("密码错误", "账号不存在", "笔名不存在", "锁定", "错误次数",
               "登录失败", "请输入正确的", "剩余次数", "已被冻结", "帐号异常"):
        if kw in body:
            i = body.find(kw)
            return body[max(0, i - 60): i + 80].replace("\n", " ").strip()
    return ""


def try_account_login(page, penname: str, password: str) -> bool:
    """完整流程：开弹窗 → 切账号登录标签 → 填笔名密码 → 提交 → 验证。"""
    if not open_login_popup(page):
        print("   未找到登录入口")
        return False

    if not switch_to_account_tab(page):
        print("   未找到「账号登录」标签（可能结构已变）")
        # 继续尝试，密码框可能本来就在
    else:
        print("   已切到「账号登录」标签")

    ok = fill_account_login(page, penname, password)
    if not ok:
        return False

    print(f"   已填笔名并提交登录")
    time.sleep(5)
    return _is_logged_in(page)


def main():
    from playwright.sync_api import sync_playwright

    check_only = "--check" in sys.argv
    force_scan = "--scan" in sys.argv
    env = load_env()
    user = env.get("xgb_user", "")
    passwd = env.get("xgb_passwd", "")
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=False,
            args=["--window-size=1600,1000", "--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_viewport_size({"width": 1600, "height": 1000})

        page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(4)

        if check_only:
            ok = _is_logged_in(page)
            print("✅ 已登录，淘股吧就绪" if ok else "❌ 未登录，请运行: python publish/tgb/login.py")
            ctx.close()
            return 0 if ok else 1

        print("=" * 60)
        print("淘股吧登录")
        print("=" * 60)

        # 已登录就直接过
        if _is_logged_in(page):
            print("✅ 已有有效登录态")
            ctx.close()
            return 0

        if user and passwd and not force_scan:
            print(f"检测到 .env 凭证（笔名 {user}），走账号密码登录...")
            try:
                if try_account_login(page, user, passwd):
                    print("✅ 账号密码登录成功")
                    # 登录后探测发帖页结构
                    try:
                        page.goto(NEW_TOPIC, wait_until="domcontentloaded", timeout=90000)
                        time.sleep(6)
                        t = page.inner_text("body")
                        print(f"   发帖页 URL: {page.url}")
                        for kw in ["标题", "分类", "发表", "发布", "正文", "插入图片"]:
                            if kw in t:
                                print(f"   发帖页含 '{kw}'")
                    except Exception as e:
                        print(f"   发帖页探测失败: {str(e)[:80]}")
                    ctx.close()
                    return 0

                err = read_login_error(page)
                if err:
                    print(f"   ❌ 登录被拒: {err}")
                    print("   ⚠️  已停止自动重试（避免触发账号锁定），请确认凭证后手动重跑")
                    ctx.close()
                    return 1
                print("   账号密码登录未成功（无明确报错，可能弹窗未提交成功）")
            except Exception as e:
                print(f"   账号密码登录异常: {str(e)[:100]}")
        else:
            print("走扫码/验证码登录（未提供凭证或指定 --scan）")
            open_login_popup(page)

        print("\n浏览器窗口已打开。若上面未自动完成，请手动：")
        print("  · 点「账号登录」标签 → 填笔名 + 密码 → 登录")
        print("  · 或点「极速登录」用微信扫一扫")
        print("\n等待中（最多 15 分钟）...\n")

        try:
            page.evaluate(r"""
                () => {
                    const m = document.createElement('div');
                    m.id = 'ax-tgb-hint';
                    m.style.cssText = [
                        'position:fixed','top:10px','right:10px','z-index:2147483647',
                        'background:#e34d59','color:#fff','font-size:15px','padding:12px 18px',
                        'border-radius:8px','line-height:1.6','box-shadow:0 4px 16px rgba(0,0,0,.25)'
                    ].join(';');
                    m.innerHTML = 'AXIOM 自动发布<br>请在此页完成淘股吧登录';
                    document.body.appendChild(m);
                }
            """)
        except Exception:
            pass

        deadline = time.time() + 900
        last = 0
        while time.time() < deadline:
            try:
                if _is_logged_in(page):
                    print("\n✅ 登录成功，已进入发帖页")
                    print(f"   URL: {page.url}")
                    body = page.inner_text("body")
                    for kw in ["标题", "分类", "发表", "发布", "正文", "插入图片"]:
                        if kw in body:
                            print(f"   发帖页含 '{kw}'")
                    ctx.close()
                    return 0
            except Exception:
                pass
            if time.time() - last > 30:
                last = time.time()
                print("   等待登录中...", flush=True)
            time.sleep(3)

        print("\n❌ 登录超时")
        ctx.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
