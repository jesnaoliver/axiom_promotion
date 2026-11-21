"""
雪球登录。

用法：
  python publish/xq/login.py          # 登录（含滑动验证处理）
  python publish/xq/login.py --check  # 只检查登录态

凭证来自 .env：xq_user / xq_passwd
登录态存 .xq_profile 长期复用。

【滑动验证】
雪球登录会触发滑块（第三方验证码，在 iframe 内）。
脚本会先尝试程序化拖动（三次贝塞尔轨迹）；
失败则保留窗口请人工拖一次 —— 登录态保存后，
后续定时任务完全不需要再过验证。
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

ROOT = Path(__file__).parent.parent.parent


def load_env() -> dict:
    out = {}
    p = ROOT / ".env"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    return out


def main():
    from publish.xq.browser import XqSession

    check_only = "--check" in sys.argv
    env = load_env()
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")

    if check_only:
        # ⚠️ 2026-11-11 修复（三处）：
        #   1) 原实现**只查 cookie，从不访问页面** → cookie 残留但服务端 token
        #      已过期时**假阳性报「已登录」**（实测首页直接弹登录框，
        #      /v4/statuses/user_timeline.json 返回 400，cookie 仍在）。
        #   2) 必须**有头**模式：无头会被雪球反爬拦截
        #      （「请求异常已被安全策略拦截」，HTTP 567 非 JSON），
        #      页面根本渲染不出来 → 无头判定永远为「未登录」。
        #   3) 判据以**服务端 API** 为准（user 字段），不是页面文案。
        with XqSession(headless=False) as s:
            s.open()                      # ← 关键：先落地真实页面
            ok = s.is_logged_in()
            if ok:
                print("✅ 已登录，雪球就绪")
            else:
                print("❌ 雪球登录态失效（服务端已判定未登录）")
                print("   需人工运行: python publish/xq/login_manual.py")
                print("   （点右上角「登 录」+ 拖动滑块，登录态存 .xq_profile）")
            return 0 if ok else 1

    if not user or not passwd:
        print("❌ .env 缺少 xq_user / xq_passwd")
        return 1

    print("=" * 60)
    print("雪球登录")
    print("=" * 60)
    print(f"账号: {user}")
    print("登录态将保存到 .xq_profile，长期复用\n")

    with XqSession(headless=False) as s:
        ok = s.login(user, passwd, manual_ok=True)
        if ok:
            print("\n✅ 登录成功，登录态已保存")
            # 顺便探测发布页
            try:
                s.page.goto("https://xueqiu.com/statuses/create",
                            wait_until="domcontentloaded", timeout=90000)
                time.sleep(6)
                body = s.page.inner_text("body")
                print(f"   发帖页 URL: {s.page.url}")
                for kw in ["发布", "标题", "正文", "添加标签", "文章", "发表"]:
                    if kw in body:
                        print(f"   发帖页含 '{kw}'")
                print(f"   frame 数: {len(s.page.frames)}")
            except Exception as e:
                print(f"   发帖页探测失败: {str(e)[:80]}")
            return 0
        print("\n❌ 登录未完成")
        return 1


if __name__ == "__main__":
    sys.exit(main())
