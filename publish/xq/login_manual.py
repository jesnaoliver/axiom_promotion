"""
雪球登录（人工点击模式）。

⚠️ 为什么需要人工点一下「登 录」：
  程序化点击（mouse.click / down+up / locator.click）**全部无效**——
  请求只发出去一个 web_behavior 埋点，真正的登录接口从不触发。
  但协议勾选（.newLogin_nochecked 图标）用程序点击是**有效的**，
  说明事件本身能送达，问题出在提交链路的内部校验或环境检测。
  → 让真人点一次，既能验证账号密码，也能拿到真实接口路径与滑块结构。

流程：
  1. 打开窗口，自动切「账号密码登录」、填账号密码、勾协议
  2. 提示用户点「登 录」→ 拖动滑块
  3. 脚本监听所有请求 + 检测登录态，成功后自动保存 profile
"""

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from config import env_loader  # noqa: E402

ROOT = Path(__file__).parent.parent.parent

REQS = []


def load_env() -> dict:
    """读取凭证（统一走 config.env_loader，支持本地 .env 与资产库缓存）。"""
    return env_loader.load_env()


def main():
    from playwright.sync_api import sync_playwright

    env = load_env()
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")
    if not user or not passwd:
        print("❌ .env 缺少 xq_user / xq_passwd")
        return 1

    profile = ROOT / ".xq_profile"
    profile.mkdir(parents=True, exist_ok=True)

    print("=" * 62)
    print("雪球登录（人工点击模式）")
    print("=" * 62)
    print(f"账号: {user}\n")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(profile),
            channel="chrome",
            headless=False,
            args=["--window-size=1600,1000", "--start-maximized",
                  "--disable-blink-features=AutomationControlled"],
        )
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome = window.chrome || {runtime: {}};"
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_viewport_size({"width": 1600, "height": 1000})

        def on_req(r):
            try:
                REQS.append((r.method, r.resource_type, r.url[:130]))
            except Exception:
                pass
        page.on("request", on_req)

        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(7)

        # 1) 切「账号密码登录」
        try:
            page.locator("xpath=//a[normalize-space(text())='账号密码登录']").first.click(
                timeout=5000, force=True)
            time.sleep(3)
        except Exception as e:
            print("⚠️ 切标签失败:", str(e)[:80])

        # 2) 勾协议（自定义图标 .newLogin_nochecked）
        agree = page.evaluate(r"""() => {
          const el = [...document.querySelectorAll('span,div,i,p,label')]
            .find(e => (e.textContent||'').trim().startsWith('阅读并同意')
                       && (e.textContent||'').trim().length < 20);
          if (!el) return false;
          let prev = el.previousElementSibling;
          for (let i=0;i<4 && prev;i++) {
            const r = prev.getBoundingClientRect();
            if (r.width>0 && r.width<30 && r.height>0 && r.height<30) {
              prev.click();
              return true;
            }
            prev = prev.previousElementSibling;
          }
          return false;
        }""")
        print("协议已勾选:", agree, flush=True)
        time.sleep(1)

        # 3) 填表（真实键盘输入）
        for name, val in (("username", user), ("password", passwd)):
            try:
                el = page.query_selector(f"input[name='{name}']")
                if not el:
                    continue
                el.click()
                time.sleep(0.3)
                el.press("Control+A")
                time.sleep(0.1)
                el.press("Delete")
                time.sleep(0.2)
                el.type(val, delay=70)
                time.sleep(0.4)
                el.press("Tab")
                time.sleep(0.4)
            except Exception as e:
                print(f"  填 {name} 异常: {str(e)[:60]}")
        print("账号密码已自动填好", flush=True)

        # 4) 提示 + 角标
        try:
            page.evaluate(r"""
                () => {
                    const m = document.createElement('div');
                    m.id = 'ax-xq-hint';
                    m.style.cssText = [
                        'position:fixed','top:10px','left:10px','z-index:2147483647',
                        'background:#e94b4b','color:#fff','font-size:15px','padding:14px 20px',
                        'border-radius:8px','line-height:1.8','box-shadow:0 4px 16px rgba(0,0,0,.3)',
                        'max-width:400px'
                    ].join(';');
                    m.innerHTML =
                      '<div style="font-size:19px;margin-bottom:8px">还差最后一步</div>'
                      + '<div style="margin:4px 0">① 点右上角<big>蓝色「登 录」</big>按钮</div>'
                      + '<div style="margin:4px 0">② 如弹出滑块，<big>拖动滑块</big>完成验证</div>'
                      + '<div style="margin:8px 0 0;opacity:.85;font-size:13px">'
                      + '账号密码已自动填好，只需点一下</div>';
                    document.body.appendChild(m);
                }
            """)
        except Exception:
            pass

        print("\n" + "=" * 62, flush=True)
        print("  👉 请在浏览器窗口里点「登 录」，并完成滑块验证", flush=True)
        print("     账号密码已填好，只需点一下", flush=True)
        print("=" * 62 + "\n", flush=True)

        REQS.clear()
        deadline = time.time() + 900
        last = 0
        logged = False
        try:
            page.bring_to_front()
        except Exception:
            pass
        while time.time() < deadline:
            time.sleep(3)
            # 持续抢焦点，确保窗口在用户视线内
            try:
                page.bring_to_front()
            except Exception:
                pass

            # 检测登录成功
            try:
                names = {c["name"] for c in ctx.cookies()}
                body = page.inner_text("body")
                if ("u" in names and "xq_a_token" in names
                        and "立即登录/注册" not in body):
                    logged = True
                    break
            except Exception:
                pass

            if time.time() - last > 15:
                last = time.time()
                # 打印新增的关键请求
                interesting = [r for r in REQS if r[1] in ("xhr", "fetch", "document")
                               and "web_behavior" not in r[2]]
                if interesting:
                    print("   新请求:", flush=True)
                    for m, t, u in interesting[-6:]:
                        print(f"     {m} {u}", flush=True)
                print("   等待中...", flush=True)

        if logged:
            print("\n✅ 登录成功！", flush=True)
            print("   登录态已保存到 .xq_profile", flush=True)
            # 探测发帖页
            try:
                page.goto("https://xueqiu.com/statuses/create",
                          wait_until="domcontentloaded", timeout=90000)
                time.sleep(7)
                print(f"   发帖页 URL: {page.url}", flush=True)
                t = page.inner_text("body")[:300]
                print(f"   发帖页文本: {t[:200]}", flush=True)
                print(f"   frame 数: {len(page.frames)}", flush=True)
                for fr in page.frames:
                    print(f"     frame: {fr.url[:100]}", flush=True)
                page.screenshot(path=str(ROOT / "assets" / "_xq_editor.png"))
                print("   截图: assets/_xq_editor.png", flush=True)
            except Exception as e:
                print(f"   发帖页探测失败: {str(e)[:80]}", flush=True)
            ctx.close()
            return 0

        print("\n❌ 未检测到登录成功", flush=True)
        print("   捕获到的请求:", flush=True)
        for m, t, u in REQS[-25:]:
            print(f"     {m} [{t}] {u}", flush=True)
        ctx.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
