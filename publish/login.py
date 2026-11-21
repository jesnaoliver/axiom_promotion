"""
小红书创作服务平台登录 —— 在脚本专用浏览器里完成一次登录。

重要：小红书的 www 主站与 creator 创作服务平台是两套独立登录体系。
主站登录成功 ≠ 创作平台登录成功。判据是 creator 域下的 web_session cookie。
必须用「手机号 + 短信验证码」登录 creator 创作服务平台。

用法：
    python publish/login.py            # 打开窗口，等待登录，自动保存
    python publish/login.py --check    # 只检查当前状态
"""

import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from publish.browser import (  # noqa: E402
    PUBLISH_URL, USER_DATA_DIR, on_login_wall, publish_page_ready,
)
from publish.sync_login import has_web_session  # noqa: E402


def check(verbose: bool = True) -> dict:
    """检查脚本 profile 的登录状态（真正打开页面验证）。"""
    result = {"logged_in": False, "has_cookie": False, "detail": ""}

    ok, n = has_web_session(USER_DATA_DIR)
    result["has_cookie"] = ok
    result["cookie_count"] = n

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR), channel="chrome", headless=True,
            viewport={"width": 1440, "height": 900}, locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        time.sleep(3)
        if on_login_wall(page):
            result["detail"] = "未登录（页面显示登录墙）"
        elif publish_page_ready(page):
            result["logged_in"] = True
            result["detail"] = "已登录，发布页就绪"
        else:
            result["detail"] = f"状态不明，当前页: {page.url}"
        ctx.close()

    if verbose:
        icon = "✅" if result["logged_in"] else "❌"
        print(f"{icon} {result['detail']}")
        print(f"   web_session cookie: {result.get('cookie_count', 0)} 个")
    return result


def interactive_login():
    """打开脚本专用 Chrome，等用户完成登录。"""
    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR), channel="chrome", headless=False,
            viewport={"width": 1440, "height": 900}, locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(PUBLISH_URL, wait_until="domcontentloaded", timeout=60000)
        print("=" * 68)
        print("请在打开的浏览器窗口中完成登录")
        print("  · 必须是 创作服务平台（creator.xiaohongshu.com）")
        print("  · 用「手机号 + 短信验证码」登录")
        print("  · 登录后停在任意页面即可，脚本会自动检测并保存")
        print("=" * 68)
        print("（提示：如果你平时用主站登录过，创作平台仍需单独登录一次）")

        deadline = time.time() + 600  # 10 分钟
        logged = False
        while time.time() < deadline:
            time.sleep(3)
            try:
                if not on_login_wall(page):
                    print(f"\n✅ 检测到登录成功，当前页面: {page.url}")
                    time.sleep(5)  # 等 cookie 落盘
                    logged = True
                    break
            except Exception:
                print("\n浏览器窗口已关闭")
                break
        if not logged:
            print("\n⏳ 等待超时（10 分钟）")
        ctx.close()

    return logged


def main():
    if "--check" in sys.argv:
        r = check()
        sys.exit(0 if r["logged_in"] else 1)

    print("=" * 68)
    print("小红书创作服务平台登录")
    print("=" * 68)
    r = check(verbose=False)
    if r["logged_in"]:
        print(f"✅ 已登录，无需重复登录（{r['detail']}）")
        print("\n下一步：python scheduler.py --date <YYYY-MM-DD>")
        return

    print(f"当前状态：{r['detail']}")
    print()
    interactive_login()

    print()
    print("=" * 68)
    print("最终验证：")
    check()
    print("=" * 68)


if __name__ == "__main__":
    main()
