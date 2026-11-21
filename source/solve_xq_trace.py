"""
雪球登录全链路抓包（定位「验证通过但没发登录请求」的断点）。

把**所有** xhr/fetch 请求与响应（含跨域 geetest 校验）落到 JSON，
以便逐条分析：geetest verify 是否发出 → 响应 validate 是否被接受 →
xueqiu 是否补发真正的登录 POST。
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
OUT = ASSETS / "_login_trace.json"

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

    log = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=UD, channel="chrome", headless=False,
            args=["--window-size=1600,1000"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_req(r):
            try:
                if r.resource_type in ("xhr", "fetch"):
                    log.append({"t": round(time.time() - T0, 2), "dir": "REQ",
                                "m": r.method, "u": r.url,
                                "post": (r.post_data or "")[:600]})
            except Exception:
                pass

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                try:
                    body = r.text()[:800]
                except Exception:
                    body = "<no-body>"
                log.append({"t": round(time.time() - T0, 2), "dir": "RESP",
                            "st": r.status, "u": r.url, "body": body})
            except Exception:
                pass

        page.on("request", on_req)
        page.on("response", on_resp)

        T0 = time.time()
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

        MARK = len(log)
        page.evaluate("""() => { const b=document.querySelector('div[class*=newLogin_modal__login__btn]'); if(b) b.click(); }""")
        time.sleep(7)

        def dec(u):
            return base64.b64decode(u.split(",", 1)[1]) if u else None

        data = page.evaluate(DUMP_JS)
        if not (data.get("bg") and data.get("slice")):
            print("无 canvas", flush=True)
            OUT.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
            ctx.close()
            return 2

        cands, meta = analyze(dec(data["bg"]), dec(data["full"]), dec(data["slice"]))
        D = cands[0][0]
        scale = data["bgBox"]["w"] / float(meta["logical"][0])
        btn = data["btnBox"]
        start = (btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
        end = (start[0] + D * scale, start[1])
        print("拖动 dx=%.1f (D=%d)" % (D * scale, D), flush=True)

        DRAG_MARK = len(log)
        human_drag(page, start, end)
        time.sleep(10)

        body = page.inner_text("body")
        print("验证层消失: %s" % ("拖动滑块完成拼图" not in body), flush=True)
        # 验证层是否还在 + 提示文案
        try:
            tips = page.evaluate("""() => {
                const out=[];
                document.querySelectorAll('[class*=geetest]').forEach(e=>{
                    const t=(e.innerText||'').trim();
                    if(t && t.length<120) out.push({cls:(e.className||'').toString().slice(0,50), t:t});
                });
                return out.slice(0,15);
            }""")
            print("geetest 节点:", json.dumps(tips, ensure_ascii=False)[:900], flush=True)
        except Exception as e:
            print("tips 失败:", str(e)[:60], flush=True)
        page.screenshot(path=str(ASSETS / "_trace_after_drag.png"))

        # 补点登录
        page.evaluate("""() => { const b=document.querySelector('div[class*=newLogin_modal__login__btn]'); if(b) b.click(); }""")
        time.sleep(8)

        OUT.write_text(json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")
        print("\n轨迹已存:", OUT, flush=True)

        # 只打关键条目
        print("\n=== 关键流量 ===", flush=True)
        for e in log[MARK:]:
            u = e["u"]
            if not any(k in u for k in ("gee_test", "geetest", "passport", "login",
                                        "verify", "ajax.php", "getchallenge")):
                continue
            if e["dir"] == "REQ":
                print("  REQ  %s" % u[:130], flush=True)
                if e.get("post"):
                    print("       POST %s" % e["post"][:400], flush=True)
            else:
                print("  RESP %s %s" % (e.get("st"), u[:130]), flush=True)
                print("       %s" % (e.get("body") or "")[:400], flush=True)

        time.sleep(1)
        ctx.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
