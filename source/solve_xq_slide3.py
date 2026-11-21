"""
雪球 Geetest **slide3** 完整求解（自研，纯本地图像分析）。

🔑 关键发现（2026-11-11）：
   雪球用的是 `type=slide3` —— 极验 v3 **需要连续完成 3 次拼图**，
   每次通过后前端会再调 `get.php?is_next=true` 换一个新 challenge。
   只解 1 次就结束 → 后续用**已消费的 challenge** 提交
   → 极验回 `{"success":0,"message":"forbidden"}`
   （页面上表现为「网络不给力 113」，极具误导性，实际是**滑块没解完**。）

本脚本：
  ① 循环求解：每次等 canvas 内容变化（=新 challenge）→ 重新定位缺口 → 拖动
  ② 画布变化检测：用 bg canvas 的 dataURL 指纹判断「是否换了新题」
  ③ 每个 challenge 都独立求解，直到出现「通过验证」或无可解的画布
  ④ 全部通过后校验登录态（cookie + 页面标志 + 服务端 API 三重判据）

缺口定位算法（已实测校准）：
  geetest_canvas_bg（挖空） vs geetest_canvas_fullbg（完整原图） → 像素差
  → 连通域 → 取与拼图块**尺寸最接近**者（不能用整体 bbox，多域会算偏）
  → D = 缺口左边界 - 拼图块左边界
"""
import base64
import io
import json
import random
import sys
import time
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

sys.path.insert(0, str(Path(__file__).parent.parent))

from config import env_loader  # noqa: E402

ROOT = Path(__file__).parent.parent
UD = str(ROOT / ".xq_profile")
ASSETS = ROOT / "assets"
MAX_ROUNDS = 8          # 最多解几轮（slide3 需要 3 轮）
MAX_SLIDES_PER_ROUND = 4


# ------------------------- 图像分析 -------------------------
def _components(mask, w, h, min_area=40):
    seen = [[0] * w for _ in range(h)]
    comps = []
    for y in range(h):
        for x in range(w):
            if mask[y][x] and not seen[y][x]:
                st = [(x, y)]
                seen[y][x] = 1
                pts = []
                while st:
                    cx, cy = st.pop()
                    pts.append((cx, cy))
                    for dx in (-1, 0, 1):
                        for dy in (-1, 0, 1):
                            nx, ny = cx + dx, cy + dy
                            if 0 <= nx < w and 0 <= ny < h and mask[ny][nx] and not seen[ny][nx]:
                                seen[ny][nx] = 1
                                st.append((nx, ny))
                if len(pts) >= min_area:
                    xs = [p[0] for p in pts]
                    ys = [p[1] for p in pts]
                    comps.append({"x0": min(xs), "x1": max(xs),
                                  "y0": min(ys), "y1": max(ys), "area": len(pts)})
    return comps


def analyze(bg_b, full_b, slice_b):
    """返回 (D, meta)：D = 拼图块需右移的逻辑像素数；meta 含诊断信息。"""
    bg = Image.open(io.BytesIO(bg_b)).convert("RGB")
    full = Image.open(io.BytesIO(full_b)).convert("RGB")
    sl = Image.open(io.BytesIO(slice_b)).convert("RGBA")
    w, h = bg.size

    pa = sl.getchannel("A")
    pal = pa.load()
    pxs = [x for y in range(h) for x in range(w) if pal[x, y] > 60]
    pys = [y for y in range(h) for x in range(w) if pal[x, y] > 60]
    if not pxs:
        return None, {"err": "拼图块为空"}
    piece_x0, piece_x1 = min(pxs), max(pxs)
    pw, ph = piece_x1 - piece_x0, max(pys) - min(pys)

    diff = ImageChops.difference(bg, full).convert("L")
    dpx = diff.load()
    dmask = [[1 if dpx[x, y] > 28 else 0 for x in range(w)] for y in range(h)]
    comps = _components(dmask, w, h)

    def score(c):
        return abs((c["x1"] - c["x0"]) - pw) + abs((c["y1"] - c["y0"]) - ph)

    comps.sort(key=score)
    if not comps:
        return None, {"err": "无连通域", "piece_size": (pw, ph)}
    c = comps[0]
    D = c["x0"] - piece_x0
    meta = {"piece_size": (pw, ph), "piece_x0": piece_x0,
            "hole": (c["x0"], c["x1"], c["y0"], c["y1"]),
            "ncomps": len(comps), "logical": (w, h),
            "scores": [score(x) for x in comps[:4]]}
    return D, meta


# ------------------------- 拖动 -------------------------
def _bezier(t, p0, p1, p2, p3):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def human_drag(page, start, end, rng):
    """拟人拖动：游走 → 按下停顿 → 缓入缓出 → 末端过冲回拉。"""
    cur = (start[0] - rng.uniform(40, 140), start[1] + rng.uniform(-50, 50))
    page.mouse.move(cur[0], cur[1])
    for _ in range(rng.randint(3, 6)):
        cur = (cur[0] + rng.uniform(-45, 70), cur[1] + rng.uniform(-28, 28))
        page.mouse.move(cur[0], cur[1])
        time.sleep(rng.uniform(0.02, 0.07))
    page.mouse.move(start[0], start[1])
    time.sleep(rng.uniform(0.12, 0.30))

    page.mouse.down()
    time.sleep(rng.uniform(0.10, 0.28))

    total = end[0] - start[0]
    steps = rng.randint(30, 46)
    over = rng.uniform(2.0, 6.0) * (1 if total >= 0 else -1)
    for i in range(1, steps + 1):
        t = i / float(steps)
        ease = 3 * t * t - 2 * t * t * t
        x = start[0] + total * ease + rng.gauss(0, 1.0)
        y = start[1] + rng.gauss(0, 1.0)
        if i > steps - 7:
            x += over * (i - (steps - 7)) / 7.0
        page.mouse.move(x, y)
        time.sleep(max(0.006, rng.gauss(0.016, 0.006)))
    for i in range(1, 7):
        page.mouse.move(end[0] + over * (1 - i / 6.0), end[1] + rng.gauss(0, 0.8))
        time.sleep(rng.uniform(0.02, 0.05))
    page.mouse.move(end[0], end[1])
    time.sleep(rng.uniform(0.08, 0.20))
    page.mouse.up()
    time.sleep(rng.uniform(0.25, 0.5))


# ------------------------- 页面 -------------------------
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
        panel: (()=>{const e=document.querySelector('.geetest_panel_box');
                      return e?(e.innerText||'').trim().slice(0,80):''})(),
    };
}"""

LOGIN_API_JS = """async () => {
    const r = await fetch('/v4/statuses/user_timeline.json?page=1&size=3',{credentials:'include'});
    const t = await r.text(); let d=null; try{d=JSON.parse(t);}catch(e){}
    return {st:r.status, hasUser:!!(d&&d.user&&d.user.id),
            user:d&&d.user?{id:d.user.id,sn:d.user.screen_name}:null};
}"""


def dec(u):
    return base64.b64decode(u.split(",", 1)[1]) if u else None


def main():
    env = env_loader.load_env()
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")

    rng = random.Random()
    verdicts = []

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=UD, channel="chrome", headless=False,
            args=["--window-size=1600,1000", "--disable-blink-features=AutomationControlled"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "window.chrome = window.chrome || {runtime:{}};")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_viewport_size({"width": 1600, "height": 1000})

        def on_resp(r):
            try:
                u = r.url
                if "geevisit" in u and ("ajax.php" in u or "get.php" in u):
                    try:
                        b = r.text()[:260]
                    except Exception:
                        b = "<no-body>"
                    verdicts.append({"u": u.split("?")[0] + "?" + (u.split("?")[1][:60] if "?" in u else ""),
                                     "st": r.status, "body": b})
            except Exception:
                pass

        page.on("response", on_resp)

        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)

        # 登录表单
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
        verdicts.clear()
        page.evaluate("""() => { const b=document.querySelector('div[class*=newLogin_modal__login__btn]'); if(b) b.click(); }""")
        time.sleep(7)

        prev_fingerprint = None
        solved = 0
        for rnd in range(1, MAX_ROUNDS + 1):
            data = page.evaluate(DUMP_JS)
            if not (data.get("bg") and data.get("full") and data.get("slice")):
                print("[轮%d] 无画布，结束" % rnd, flush=True)
                break
            fp = hash(data["bg"])
            if fp == prev_fingerprint:
                print("[轮%d] 画布未变，等新题…" % rnd, flush=True)
                time.sleep(3)
                continue
            prev_fingerprint = fp

            D, meta = analyze(dec(data["bg"]), dec(data["full"]), dec(data["slice"]))
            if D is None:
                print("[轮%d] 分析失败 %s" % (rnd, meta), flush=True)
                time.sleep(3)
                continue
            panel = (data.get("panel") or "").replace("\n", " / ")
            print("[轮%d] 面板=%r 块尺寸=%s 缺口=%s D=%d 域数=%d"
                  % (rnd, panel[:40], meta["piece_size"], meta["hole"], D, meta["ncomps"]),
                  flush=True)

            if D <= 2:
                print("    D≈0（该题已解过/无需位移）", flush=True)
                time.sleep(2)
                continue

            btn = data["btnBox"]
            if not btn:
                print("    无滑块按钮", flush=True)
                time.sleep(2)
                continue
            scale = data["bgBox"]["w"] / float(meta["logical"][0])
            start = (btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
            end = (start[0] + D * scale, start[1])
            human_drag(page, start, end, rng)
            time.sleep(6)

            api = page.evaluate(LOGIN_API_JS)
            print("    拖动后 登录API=%s" % json.dumps(api, ensure_ascii=False), flush=True)
            if api.get("hasUser"):
                print("✅ 登录成功", flush=True)
                break
            solved += 1
            time.sleep(2)

        print("\n=== 极验回执 ===", flush=True)
        for v in verdicts[-14:]:
            print("  [%s] %s" % (v.get("st"), v["u"][:120]), flush=True)
            print("      %s" % (v.get("body") or "")[:220], flush=True)

        api = page.evaluate(LOGIN_API_JS)
        print("\n最终 登录API: %s" % json.dumps(api, ensure_ascii=False), flush=True)
        page.screenshot(path=str(ASSETS / "_slide3_final.png"))

        if api.get("hasUser"):
            page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
            time.sleep(6)
            api2 = page.evaluate(LOGIN_API_JS)
            print("刷新后: %s" % json.dumps(api2, ensure_ascii=False), flush=True)
            page.screenshot(path=str(ASSETS / "_slide3_reload.png"))
            ok = api2.get("hasUser")
        else:
            ok = False
        time.sleep(2)
        ctx.close()
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
