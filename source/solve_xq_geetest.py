"""
雪球 Geetest 拼图滑块求解器（自研，纯本地图像分析，无第三方打码服务）。

原理（2026-11-11 实测校准）：
  Geetest 拼图渲染三张 canvas（均 260x160 逻辑尺寸）：
    · geetest_canvas_bg     → 背景图，**缺口被挖空**（含阴影）
    · geetest_canvas_fullbg → 完整原图（视觉隐藏，像素完整）
    · geetest_canvas_slice  → 拼图块（RGBA，alpha>60 即为块体）
  ① diff(bg, fullbg) → 缺口区域（**可能多块**，必须做连通域，不能直接用
     整体 bbox —— 这是第一版失败的原因：两个连通域的并集 bbox 算出了 ±128px 的偏差）
  ② 拼图块 bbox 与各连通域 bbox **尺寸最接近者** = 真缺口
     （实测：块 49x42，域0 49x41 → 匹配；域1 44x36 → 不匹配）
  ③ 目标位移 D = 缺口左边界 - 拼图块左边界
  ④ 屏幕位移 = D * (画布显示宽 / 逻辑宽)

拖动轨迹用三次贝塞尔 + 末端减速 + 微抖动，模拟真人。
失败会刷新新拼图 → 重新求解重试（最多 N 次）。
"""
import base64
import io
import json
import random
import time
from pathlib import Path

from PIL import Image, ImageChops
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent
UD = str(ROOT / ".xq_profile")
ASSETS = ROOT / "assets"
MAX_TRY = 4


# ---------------- 图像分析 ----------------
def _mask_from(img, thr=60):
    w, h = img.size
    a = img.getchannel("A")
    px = a.load()
    return [[1 if px[x, y] > thr else 0 for x in range(w)] for y in range(h)]


def _components(mask, w, h, min_area=40):
    """4-邻域连通域。"""
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
                    comps.append({
                        "x0": min(xs), "x1": max(xs), "y0": min(ys), "y1": max(ys),
                        "area": len(pts),
                    })
    return comps


def analyze(bg_b, full_b, slice_b):
    """
    返回候选位移列表（按可信度降序）：
    [(D, 说明), ...]，D = 拼图块需要向右移动的逻辑像素数
    """
    bg = Image.open(io.BytesIO(bg_b)).convert("RGB")
    full = Image.open(io.BytesIO(full_b)).convert("RGB")
    sl = Image.open(io.BytesIO(slice_b)).convert("RGBA")
    w, h = bg.size

    pmask = _mask_from(sl, 60)
    pxs = [x for y in range(h) for x in range(w) if pmask[y][x]]
    pys = [y for y in range(h) for x in range(w) if pmask[y][x]]
    if not pxs:
        return [], None
    pw = max(pxs) - min(pxs)
    ph = max(pys) - min(pys)
    piece_x0 = min(pxs)
    piece_y0 = min(pys)

    diff = ImageChops.difference(bg, full).convert("L")
    dpx = diff.load()
    dmask = [[1 if dpx[x, y] > 28 else 0 for x in range(w)] for y in range(h)]
    comps = _components(dmask, w, h)

    # 按「尺寸接近拼图块」排序
    def score(c):
        dw = abs((c["x1"] - c["x0"]) - pw)
        dh = abs((c["y1"] - c["y0"]) - ph)
        return dw + dh

    comps.sort(key=score)
    out = []
    for c in comps:
        D = c["x0"] - piece_x0
        out.append((D, "域 x%d..%d 尺寸%dx%d 匹配分%d" % (
            c["x0"], c["x1"], c["x1"] - c["x0"], c["y1"] - c["y0"], score(c))))
    meta = {"piece_bbox": (piece_x0, piece_y0, max(pxs), max(pys)),
            "piece_size": (pw, ph), "logical": (w, h),
            "comps": len(comps)}
    return out, meta


# ---------------- 拖动 ----------------
def _bezier(t, p0, p1, p2, p3):
    u = 1 - t
    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
            u ** 3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * p3[1])


def human_drag(page, start, end, steps=40):
    rng = random.Random()
    dx = end[0] - start[0]
    p1 = (start[0] + dx * rng.uniform(0.25, 0.4), start[1] + rng.uniform(-5, 5))
    p2 = (start[0] + dx * rng.uniform(0.62, 0.82), start[1] + rng.uniform(-4, 4))
    p3 = (end[0], end[1])
    page.mouse.move(start[0], start[1])
    time.sleep(0.28)
    page.mouse.down()
    time.sleep(0.16)
    for i in range(1, steps + 1):
        t = i / steps
        x, y = _bezier(t, start, p1, p2, p3)
        if i > steps - 7:
            x += rng.uniform(-1.1, 1.1)
            y += rng.uniform(-0.9, 0.9)
        page.mouse.move(x, y)
        time.sleep(rng.uniform(0.008, 0.028))
    page.mouse.up()
    time.sleep(0.5)


# ---------------- 页面交互 ----------------
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

LOGIN_API_JS = """async () => {
    const r = await fetch('/v4/statuses/user_timeline.json?page=1&size=3',{credentials:'include'});
    const t = await r.text();
    let d=null,parsed=false; try{d=JSON.parse(t);parsed=true;}catch(e){}
    return {status:r.status, parsed:parsed, hasUser:!!(d&&d.user&&d.user.id),
            user:d&&d.user?{id:d.user.id,sn:d.user.screen_name}:null, raw:t.slice(0,140)};
}"""


def open_login_modal(page):
    """首页直接渲染登录弹窗 → 切到账号密码登录。"""
    page.evaluate("""() => {
        const els=[...document.querySelectorAll('a,span,div')].filter(e=>
            (e.innerText||'').trim()==='账号密码登录' && e.getBoundingClientRect().width>0);
        if(els.length) els[0].click();
    }""")
    time.sleep(2.5)


def submit_credentials(page, user, passwd):
    """填账号密码 → 勾协议 → 点登录 → 触发验证器。"""
    open_login_modal(page)
    page.get_by_placeholder("请输入手机号或者邮箱").first.fill(user)
    page.get_by_placeholder("请输入登录密码").first.fill(passwd)
    time.sleep(0.5)
    # 勾「阅读并同意 服务协议、隐私政策」（class 带 hash 后缀，必须用属性选择器）
    page.evaluate("""() => {
        const r=document.querySelector('[class*=newLogin_nochecked]');
        if(r) r.click();
    }""")
    time.sleep(1.2)
    page.evaluate("""() => {
        const b=document.querySelector('div[class*=newLogin_modal__login__btn]');
        if(b) b.click();
    }""")
    time.sleep(7)


def main():
    env = {}
    for line in open(ROOT / ".env", encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    user = env.get("xq_user", "")
    passwd = env.get("xq_passwd", "")

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=UD, channel="chrome", headless=False,
            args=["--window-size=1600,1000"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)

        submit_credentials(page, user, passwd)

        def dec(u):
            return base64.b64decode(u.split(",", 1)[1]) if u else None

        done = False
        for attempt in range(1, MAX_TRY + 1):
            data = page.evaluate(DUMP_JS)
            if not (data.get("bg") and data.get("full") and data.get("slice")):
                print("[%d] 未取到 canvas" % attempt, flush=True)
                time.sleep(3)
                continue

            bg_b = dec(data["bg"])
            full_b = dec(data["full"])
            sl_b = dec(data["slice"])
            cands, meta = analyze(bg_b, full_b, sl_b)
            if not cands:
                print("[%d] 未解析出候选" % attempt, flush=True)
                time.sleep(3)
                continue
            print("[%d] 块bbox=%s 尺寸=%s 连通域=%d" % (
                attempt, meta["piece_bbox"], meta["piece_size"], meta["comps"]), flush=True)
            for D, desc in cands:
                print("    候选 D=%d  (%s)" % (D, desc), flush=True)

            # 偶数次尝试用次优候选，覆盖「首次判错」的情况
            pick = cands[0] if attempt % 2 == 1 else cands[1 if len(cands) > 1 else 0]
            D, desc = pick
            print("    → 采用 D=%d (%s)" % (D, desc), flush=True)

            logical_w = meta["logical"][0]
            scale = data["bgBox"]["w"] / float(logical_w)
            dx_screen = D * scale
            btn = data["btnBox"]
            start = (btn["x"] + btn["w"] / 2, btn["y"] + btn["h"] / 2)
            end = (start[0] + dx_screen, start[1])
            print("    拖动 %s → %s (dx=%.1f)" % (start, end, dx_screen), flush=True)

            human_drag(page, start, end)
            time.sleep(5)

            body = page.inner_text("body")
            slider_gone = "拖动滑块完成拼图" not in body and "拖动下方滑块" not in body
            print("    滑块层消失=%s" % slider_gone, flush=True)
            page.screenshot(path=str(ASSETS / ("_geetest_try%d.png" % attempt)))

            api = page.evaluate(LOGIN_API_JS)
            print("    登录API: %s" % json.dumps(api, ensure_ascii=False), flush=True)
            if api.get("hasUser"):
                print("✅ 登录成功 (screen_name=%s)" % (api.get("user") or {}).get("sn"), flush=True)
                done = True
                break
            if slider_gone:
                # 验证层消失但仍未登录 → 可能是登录提交失败，等一下再看
                time.sleep(5)
                api2 = page.evaluate(LOGIN_API_JS)
                if api2.get("hasUser"):
                    print("✅ 登录成功（延迟生效）", flush=True)
                    done = True
                    break
                print("    验证层消失但未登录 → 重试", flush=True)
            time.sleep(3)

        if not done:
            page.screenshot(path=str(ASSETS / "_geetest_final_fail.png"))
            print("❌ 全部尝试失败", flush=True)
        time.sleep(2)
        ctx.close()
        return 0 if done else 1


if __name__ == "__main__":
    raise SystemExit(main())
