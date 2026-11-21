"""
定位 Geetest「网络不给力」(113) 的根因：抓取**所有**请求（含失败/被拒/子框架）
与页面 console error，看验证回执到底发往哪里、为何失败。
"""
import base64
import io
import json
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from config import env_loader  # noqa: E402
from source.solve_xq_geetest import analyze, human_drag  # noqa: E402

ROOT = Path(__file__).parent.parent
UD = str(ROOT / ".xq_profile")
ASSETS = ROOT / "assets"

DUMP_JS = """() => {
    const bg=document.querySelector('canvas.geetest_canvas_bg');
    const full=document.querySelector('canvas.geetest_canvas_fullbg');
    const slice=document.querySelector('canvas.geetest_canvas_slice');
    const btn=document.querySelector('.geetest_slider_button');
    const bb=bg?bg.getBoundingClientRect():null;
    const tb=btn?btn.getBoundingClientRect():null;
    return {
        bg: bg?bg.toDataURL('image/png'):null,
        full: full?full.toDataURL('image/png'):null,
        slice: slice?slice.toDataURL('image/png'):null,
        bgBox: bb?{x:bb.x,y:bb.y,w:bb.width,h:bb.height}:null,
        btnBox: tb?{x:tb.x,y:tb.y,w:tb.width,h:tb.height}:null,
    };
}"""


def main():
    env = env_loader.load_env()
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")

    events = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=UD, channel="chrome", headless=False,
            args=["--window-size=1600,1000"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        T0 = time.time()

        def on_req(r):
            try:
                events.append({"t": round(time.time() - T0, 2), "k": "REQ",
                               "u": r.url[:200], "rt": r.resource_type,
                               "m": r.method})
            except Exception:
                pass

        def on_failed(r):
            try:
                events.append({"t": round(time.time() - T0, 2), "k": "FAILED",
                               "u": r.url[:200], "err": (r.failure or "")[:120]})
            except Exception:
                pass

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                try:
                    body = r.text()[:300]
                except Exception:
                    body = "<no-body>"
                events.append({"t": round(time.time() - T0, 2), "k": "RESP",
                               "u": r.url[:200], "st": r.status, "body": body})
            except Exception:
                pass

        def on_console(m):
            try:
                if m.type in ("error", "warning"):
                    events.append({"t": round(time.time() - T0, 2), "k": "CONSOLE",
                                   "u": m.type, "body": (m.text or "")[:220]})
            except Exception:
                pass

        page.on("request", on_req)
        page.on("requestfailed", on_failed)
        page.on("response", on_resp)
        page.on("console", on_console)

        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)

        page.evaluate("""() => {
            const els=[...document.querySelectorAll('a,span,div')].filter(e=>
                (e.innerText||'').trim()==='账号密码登录' && e.getBoundingClientRect().width>0);
            if(els.length) els[0].click();
        }""")
        time.sleep(2.5)
        page.get_by_placeholder("请输入手机号或者邮箱").first.fill(user)
        page.get_by_placeholder("请输入登录密码").first.fill(passwd)
        time.sleep(0.5)
        page.evaluate("""() => { const r=document.querySelector('[class*=newLogin_nochecked]'); if(r) r.click(); }""")
        time.sleep(1.2)
        events.clear()
        page.evaluate("""() => { const b=document.querySelector('div[class*=newLogin_modal__login__btn]'); if(b) b.click(); }""")
        time.sleep(7)

        def dec(u):
            return base64.b64decode(u.split(",", 1)[1]) if u else None

        data = page.evaluate(DUMP_JS)
        cands, meta = analyze(dec(data["bg"]), dec(data["full"]), dec(data["slice"]))
        D = cands[0][0]
        scale = data["bgBox"]["w"] / float(meta["logical"][0])
        btn = data["btnBox"]
        start = (btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
        end = (start[0] + D * scale, start[1])
        print("拖动 dx=%.1f (D=%d)" % (D * scale, D), flush=True)

        DRAG = len(events)
        human_drag(page, start, end)
        time.sleep(10)
        page.screenshot(path=str(ASSETS / "_net_after_drag.png"))

        (ASSETS / "_login_events.json").write_text(
            json.dumps(events, ensure_ascii=False, indent=2), encoding="utf-8")

        print("\n=== 拖动后事件（全部） ===", flush=True)
        for e in events[DRAG:]:
            if e["k"] == "REQ":
                print("  REQ    [%s] %s" % (e["rt"], e["u"][:150]), flush=True)
            elif e["k"] == "RESP":
                print("  RESP   %s %s" % (e.get("st"), e["u"][:150]), flush=True)
                b = e.get("body") or ""
                if b and b != "<no-body>":
                    print("          %s" % b[:220], flush=True)
            elif e["k"] == "FAILED":
                print("  FAILED %s  err=%s" % (e["u"][:150], e.get("err")), flush=True)
            else:
                print("  CONSOLE[%s] %s" % (e.get("u"), (e.get("body") or "")[:200]), flush=True)

        time.sleep(1)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
