"""
抖音创作者中心登录。

用法：
  python publish/douyin/login.py          # 打开浏览器，等待扫码登录
  python publish/douyin/login.py --check   # 只检查登录态
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
from publish.douyin.browser import DouyinSession, CREATOR_HOME  # noqa: E402


def main():
    check_only = "--check" in sys.argv

    if check_only:
        with DouyinSession(headless=True) as s:
            s.open()
            ok = s.is_logged_in()
            print("✅ 已登录，抖音创作者中心就绪" if ok
                  else "❌ 未登录，请运行: python publish/douyin/login.py")
            return 0 if ok else 1

    print("=" * 60)
    print("抖音创作者中心登录")
    print("=" * 60)
    print("浏览器窗口即将打开。请在页面里完成登录：")
    print("  · APP 扫码（推荐）：打开抖音APP → 左上角扫一扫")
    print("  · 或手机号 + 短信验证码")
    print("\n登录态会保存到 .douyin_profile，长期复用。")
    print("等待中（最多 15 分钟）...\n")

    with DouyinSession(headless=False) as s:
        s.open()
        s.show_login_hint()

        # 轮询等待登录
        deadline = time.time() + 900
        last_hint = 0
        while time.time() < deadline:
            if s.is_logged_in():
                print("\n✅ 登录成功")
                # 顺手探一下文章入口
                try:
                    href = s.find_article_entry()
                    print(f"   文章入口: {href or '未在导航中找到（登录后进入内容管理页再探）'}")
                except Exception as e:
                    print(f"   入口探测: {e}")
                return 0
            if time.time() - last_hint > 30:
                last_hint = time.time()
                print("   等待登录中...", flush=True)
            time.sleep(3)

    print("\n❌ 登录超时")
    return 1


if __name__ == "__main__":
    sys.exit(main())
