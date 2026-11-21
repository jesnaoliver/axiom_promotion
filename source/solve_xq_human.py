"""
最后一搏：更「像人」的拖动轨迹 + 抓取 geetest 验证回执原文。

背景（2026-11-11 实测）：
  · 缺口定位已解决（bg vs fullbg 连通域 + 尺寸匹配）→ 拖动后页面出现「通过验证」
  · 但极验服务端回 `服务端forbidden` → 极验对**轨迹特征**做了风控拒绝
  · 验证请求发往 https://api.geevisit.com/ajax.php（雪球自建极验域）

本脚本：
  ① 打印 ajax.php 的**响应原文**（此前只抓了 xhr，漏掉 script 类型）
  ② 用更拟人的轨迹重试：
     - 落点前先随机游走（真人不会直线冲过去）
     - 按下后有 100~250ms 停顿（真人按住会犹豫）
     - 末端**过冲再回拉**（真人会冲过头）
     - 速度高斯抖动而非均匀
"""
import base64
import io
import json
import random
import sys
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))
from source.solve_xq_geetest import analyze  # noqa: E402

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


def human_drag_v2(page, start, end, rng):
    """更拟人的拖动：游走 → 按下停顿 → 过冲回拉 → 抬起停顿。"""
    # 1) 落点前游走
    cur = (start[0] - rng.uniform(30, 120), start[1] + rng.uniform(-40, 40))
    page.mouse.move(cur[0], cur[1])
    for _ in range(rng.randint(3, 6)):
        cur = (cur[0] + rng.uniform(-40, 60), cur[1] + rng.uniform(-25, 25))
        page.mouse.move(cur[0], cur[1])
        time.sleep(rng.uniform(0.02, 0.07))
    # 精确移到滑块上
    for i in range(1, 9):
        page.mouse.move(start[0] + (cur[0] - start[0]) * (1 - i / 9.0),
                        start[1] + (cur[1] - start[1]) * (1 - i / 9.0))
        time.sleep(0.015)
    page.mouse.move(start[0], start[1])
    time.sleep(rng.uniform(0.12, 0.28))       # 真人悬停

    # 2) 按下
    page.mouse.down()
    time.sleep(rng.uniform(0.10, 0.26))       # 真人按住有迟疑

    total = end[0] - start[0]
    # 3) 主行程（带高斯抖动的加速-减速）
    steps = rng.randint(32, 48)
    overshoot = rng.uniform(2.0, 6.0) * (1 if total > 0 else -1)
    for i in range(1, steps + 1):
        t = i / float(steps)
        # 缓入缓出
        ease = 3 * t * t - 2 * t * t * t
        x = start[0] + total * ease
        y = start[1] + rng.gauss(0, 1.1)
        if i > steps - 8:                       # 末端过冲
            x += overshoot * (i - (steps - 8)) / 8.0
        page.mouse.move(x, y)
        time.sleep(max(0.006, rng.gauss(0.016, 0.006)))
    # 4) 回拉到目标
    for i in range(1, 7):
        page.mouse.move(end[0] + overshoot * (1 - i / 6.0),
                        end[1] + rng.gauss(0, 0.8))
        time.sleep(rng.uniform(0.02, 0.05))
    page.mouse.move(end[0], end[1])
    time.sleep(rng.uniform(0.08, 0.20))
    page.mouse.up()
    time.sleep(rng.uniform(0.25, 0.5))


def main():
    env = {}
    for line in open(ROOT / ".env", encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")

    replies = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=UD, channel="chrome", headless=False,
            args=["--window-size=1600,1000"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()

        def on_resp(r):
            try:
                u = r.url
                if "geevisit" in u or "geetest" in u or "gee_test" in u:
                    try:
                        b = r.text()[:600]
                    except Exception:
                        b = "<no-body>"
                    replies.append({"u": u[:220], "st": r.status, "body": b})
            except Exception:
                pass

        def on_console(m):
            try:
                if m.type == "error":
                    replies.append({"u": "CONSOLE", "st": 0,
                                    "body": (m.text or "")[:300]})
            except Exception:
                pass

        page.on("response", on_resp)
        page.on("console", on_console)

        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)

        rng = random.Random()
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
        page.evaluate("""() => { const b=document.querySelector('div[class*=newLogin_modal__login__btn]'); if(b) b.click(); }""")
        time.sleep(7)

        def dec(u):
            return base64.b64decode(u.split(",", 1)[1]) if u else None

        done = False
        for attempt in range(1, 4):
            data = page.evaluate(DUMP_JS)
            if not (data.get("bg") and data.get("slice")):
                print("[%d] 无 canvas" % attempt, flush=True)
                break
            cands, meta = analyze(dec(data["bg"]), dec(data["full"]), dec(data["slice"]))
            if not cands:
                print("[%d] 无候选" % attempt, flush=True)
                time.sleep(4)
                continue
            D = cands[0][0]
            scale = data["bgBox"]["w"] / float(meta["logical"][0])
            btn = data["btnBox"]
            if not btn:
                print("[%d] 无滑块按钮" % attempt, flush=True)
                break
            start = (btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
            end = (start[0] + D * scale, start[1])
            print("[%d] D=%d dx=%.1f" % (attempt, D, D * scale), flush=True)
            replies.clear()
            human_drag_v2(page, start, end, rng)
            time.sleep(10)

            body = page.inner_text("body")
            state = "通过验证" if "通过验证" in body else ("网络不给力" if "网络不给力" in body else "无状态")
            print("    面板状态: %s" % state, flush=True)

            api = page.evaluate("""async () => {
                const r = await fetch('/v4/statuses/user_timeline.json?page=1&size=3',{credentials:'include'});
                const t = await r.text(); let d=null; try{d=JSON.parse(t);}catch(e){}
                return {st:r.status, hasUser:!!(d&&d.user&&d.user.id),
                        user:d&&d.user?{id:d.user.id,sn:d.user.screen_name}:null};
            }""")
            print("    登录API: %s" % json.dumps(api, ensure_ascii=False), flush=True)

            print("    回执:", flush=True)
            for rp in replies:
                print("      [%s] %s" % (rp.get("st"), rp["u"][:110]), flush=True)
                print("        %s" % (rp.get("body") or "")[:260], flush=True)

            page.screenshot(path=str(ASSETS / ("_human_try%d.png" % attempt)))
            if api.get("hasUser"):
                print("✅ 登录成功", flush=True)
                done = True
                break
            time.sleep(4)

        if done:
            time.sleep(3)
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
            time.sleep(6)
            api = page.evaluate("""async () => {
                const r = await fetch('/v4/statuses/user_timeline.json?page=1&size=3',{credentials:'include'});
                const t = await r.text(); let d=null; try{d=JSON.parse(t);}catch(e){}
                return {st:r.status, hasUser:!!(d&&d.user&&d.user.id),
                        user:d&&d.user?{id:d.user.id,sn:d.user.screen_name}:null};
            }""")
            print("刷新后:", json.dumps(api, ensure_ascii=False), flush=True)
            page.screenshot(path=str(ASSETS / "_human_reload.png"))
        time.sleep(2)
        ctx.close()
    return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
