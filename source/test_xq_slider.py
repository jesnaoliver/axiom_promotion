"""实测：程序化处理雪球滑块（极验拼图）。"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from playwright.sync_api import sync_playwright  # noqa: E402
from publish.xq.slider import (  # noqa: E402
    _has_slider_text, find_gap_x, find_slider, solve_slider,
)

UD = r"E:\WorkbuddyWork\2026-11-01-18-31-53\xhs\.xq_profile"
SHOTS = r"E:\WorkbuddyWork\2026-11-01-18-31-53\xhs\assets"

env = {}
for line in open(r"E:\WorkbuddyWork\2026-11-01-18-31-53\xhs\.env", encoding="utf-8"):
    line = line.strip()
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=UD, channel="chrome", headless=False,
        args=["--window-size=1600,1000"])
    ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    page.set_viewport_size({"width": 1600, "height": 1000})

    page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
    time.sleep(8)

    # 切账号密码 + 填表 + 勾协议 + 点登录
    page.locator("xpath=//a[normalize-space(text())='账号密码登录']").first.click(
        timeout=5000, force=True)
    time.sleep(3)
    for name, val in (("username", env["xq_user"]), ("password", env["xq_passwd"])):
        try:
            el = page.query_selector(f"input[name='{name}']")
            if el and el.is_visible():
                el.click(); time.sleep(0.2); el.fill(""); el.type(val, delay=60)
        except Exception:
            pass
    page.evaluate(r"""() => {
        const el=[...document.querySelectorAll('span,div,i,p,label')]
            .find(e=>(e.textContent||'').trim().startsWith('阅读并同意')
                     &&(e.textContent||'').trim().length<20);
        if(!el) return false;
        let prev=el.previousElementSibling;
        for(let i=0;i<4&&prev;i++){
            const r=prev.getBoundingClientRect();
            if(r.width>0&&r.width<30&&r.height>0&&r.height<30){prev.click();return true;}
            prev=prev.previousElementSibling;
        }
        return false;
    }""")
    time.sleep(1)
    btn = page.query_selector("div[class*='newLogin_modal__login__btn']")
    bb = btn.bounding_box()
    page.mouse.click(bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
    time.sleep(5)

    print("滑块出现:", _has_slider_text(page), flush=True)
    page.screenshot(path=f"{SHOTS}/_slider_before.png")

    found = find_slider(page)
    print("定位结果:", "✅" if found else "❌", flush=True)
    if found:
        container, handle, hbb = found
        print("  容器 bbox:", container.bounding_box(), flush=True)
        print("  手柄 bbox:", hbb, flush=True)
        gap = find_gap_x(page, container, SHOTS)
        print("  缺口 x:", gap, flush=True)
        page.screenshot(path=f"{SHOTS}/_slider_gap.png")

    print("\n=== 开始拖动 ===", flush=True)
    ok = solve_slider(page, attempts=4)
    print("结果:", "✅ 通过" if ok else "❌ 失败", flush=True)
    page.screenshot(path=f"{SHOTS}/_slider_after.png")

    # 是否已登录
    body = page.inner_text("body")
    print("\n'立即登录/注册'仍在:", "立即登录/注册" in body, flush=True)
    ck = {c["name"] for c in ctx.cookies()}
    print("cookie u:", "u" in ck, "| xq_a_token:", "xq_a_token" in ck, flush=True)
    ctx.close()
