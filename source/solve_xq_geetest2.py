"""
雪球登录（自研 Geetest 求解 + 网络抓包诊断）。

目标：
  1. 触发 Geetest 拼图 → 本地图像分析求缺口 → 拟人拖动 → 验证通过
  2. 抓取登录相关的所有 XHR，看服务端到底回什么
  3. 判定登录是否真的成功（cookie + 页面标志 + 服务端 API 三重判据）

同时修正：验证通过后若登录弹窗仍在，重新点一次「登 录」补发登录请求。
"""
import base64
import io
import json
import random
import time
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

import sys
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

STATE_JS = """async () => {
    const ck = document.cookie;
    const has = n => ck.includes(n+'=');
    const r = await fetch('/v4/statuses/user_timeline.json?page=1&size=3',{credentials:'include'});
    const t = await r.text();
    let d=null,parsed=false; try{d=JSON.parse(t);parsed=true;}catch(e){}
    let body='';
    try{ body=document.body.innerText.slice(0,400);}catch(e){}
    return {
        cookies: {u:has('u'), xq_a_token:has('xq_a_token'), xqat:has('xqat')},
        api: {status:r.status, parsed:parsed, hasUser:!!(d&&d.user&&d.user.id),
              user:d&&d.user?{id:d.user.id,sn:d.user.screen_name}:null, raw:t.slice(0,160)},
        pageFlags: {
            立即登录: body.includes('立即登录'), 登录注册: body.includes('登录/注册'),
            我的主页: body.includes('我的主页'), 退出: body.includes('退出'),
            写文章: body.includes('写文章'), 发文章: body.includes('发文章'),
            弹窗可见: !!document.querySelector('[class*=newLogin_modal]'),
        },
        url: location.href,
    };
}"""


def main():
    env = env_loader.load_env()
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=UD, channel="chrome", headless=False,
            args=["--window-size=1600,1000"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        traffic = []

        def on_req(r):
            if r.resource_type in ("xhr", "fetch"):
                traffic.append({"dir": "REQ", "m": r.method, "u": r.url,
                                "post": (r.post_data or "")[:200]})

        def on_resp(r):
            try:
                if r.request.resource_type not in ("xhr", "fetch"):
                    return
                try:
                    body = r.text()[:400]
                except Exception:
                    body = "<no-body>"
                traffic.append({"dir": "RESP", "m": r.request.method, "u": r.url,
                                "st": r.status, "body": body})
            except Exception:
                pass

        page.on("request", on_req)
        page.on("response", on_resp)

        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)

        # 登录流程
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
        traffic.clear()
        page.evaluate("""() => { const b=document.querySelector('div[class*=newLogin_modal__login__btn]'); if(b) b.click(); }""")
        time.sleep(7)

        def dec(u):
            return base64.b64decode(u.split(",", 1)[1]) if u else None

        data = page.evaluate(DUMP_JS)
        if not (data.get("bg") and data.get("slice")):
            print("无 canvas", flush=True)
            page.screenshot(path=str(ASSETS / "_login2_fail.png"))
            ctx.close()
            return 2

        cands, meta = analyze(dec(data["bg"]), dec(data["full"]), dec(data["slice"]))
        print("块bbox=%s 尺寸=%s 候选=%s" % (meta["piece_bbox"], meta["piece_size"],
                                            [(d, t) for d, t in cands]), flush=True)
        D = cands[0][0]
        scale = data["bgBox"]["w"] / float(meta["logical"][0])
        dx = D * scale
        btn = data["btnBox"]
        start = (btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
        end = (start[0] + dx, start[1])
        print("拖动 %s → %s (dx=%.1f, D=%d)" % (start, end, dx, D), flush=True)
        human_drag(page, start, end)
        time.sleep(6)

        body = page.inner_text("body")
        print("验证层消失: %s" % ("拖动滑块完成拼图" not in body), flush=True)
        for kw in ["验证通过", "验证失败", "请再试", "拖动", "滑块"]:
            if kw in body:
                print("  含 '%s'" % kw, flush=True)
        page.screenshot(path=str(ASSETS / "_login2_after_drag.png"))

        # 若弹窗还在，重新点登录补发请求
        st = page.evaluate(STATE_JS)
        if st["pageFlags"].get("弹窗可见") and not st["api"]["hasUser"]:
            print("弹窗仍在且未登录 → 补点一次「登 录」", flush=True)
            page.evaluate("""() => {
                const b=document.querySelector('div[class*=newLogin_modal__login__btn]');
                if(b) b.click();
            }""")
            time.sleep(8)

        st = page.evaluate(STATE_JS)
        print("\n=== 状态 ===", flush=True)
        print(json.dumps(st, ensure_ascii=False, indent=2), flush=True)

        # 刷新页面再判一次（Cookie 落盘需要真实导航）
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(7)
        st2 = page.evaluate(STATE_JS)
        print("\n=== 刷新后状态 ===", flush=True)
        print(json.dumps(st2, ensure_ascii=False, indent=2), flush=True)
        page.screenshot(path=str(ASSETS / "_login2_reload.png"))

        print("\n=== 登录相关网络流量 ===", flush=True)
        seen = set()
        for t in traffic:
            u = t["u"]
            if not any(k in u for k in ("login", "captcha", "verify", "geetest",
                                        "passport", "session", "checkin")):
                continue
            key = (t["dir"], u.split("?")[0], t.get("st"))
            if key in seen:
                continue
            seen.add(key)
            print("  [%s] %s %s" % (t["dir"], t.get("st", ""), u[:120]), flush=True)
            if t.get("post"):
                print("      POST: %s" % t["post"][:200], flush=True)
            if t.get("body"):
                print("      %s" % t["body"][:280], flush=True)

        ok = st2["api"]["hasUser"]
        print("\n%s" % ("✅ 登录成功" if ok else "❌ 仍未登录"), flush=True)
        time.sleep(2)
        ctx.close()
        return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
