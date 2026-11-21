"""
雪球会话层 —— 含滑动验证处理。

【雪球登录的三种方式】
  验证码登录 / 账号密码登录 / 二维码登录（微信）
  账号密码登录表单：
    input[name='username']  placeholder="请输入手机号或者邮箱"
    input[name='password']  placeholder="请输入登录密码"

【滑动验证 —— 雪球的关键风控环节】
  用户明确提示：登录后有滑动验证。
  雪球的滑块是第三方验证码服务（易盾/极验/腾讯防水墙），
  特征：
    · 独立 **iframe**（不是主文档元素！query_selector 查不到）
    · iframe 内有滑块按钮 + 拼图缺口图
    · 需要在 iframe 内部做鼠标拖动

【三级处理策略】
  1) **首选：人工过一次** —— 打开可见窗口，人工拖动滑块，
     登录态存 .xq_profile 长期复用。调度时完全不需要再过验证。
     这是最稳的，也避免反复触发风控。
  2) **兜底：程序化拖动** —— 在 iframe 内用贝塞尔轨迹拖动滑块，
     带随机化停顿与中途微小抖动，模拟真人。
  3) **最后：二次验证** —— 部分场景滑块在**发帖时**才弹，
     publish 时同样需要处理（publish_post 里复用同一套滑块逻辑）。

【登录态判定 —— 不要用页面文字】
  页面文字判断会误判（游客页也有「登录」按钮）。
  权威判据是 **cookie**：
    u             = 用户 ID
    xq_a_token    = 访问令牌
    xqat          = 同上
  有 u + xq_a_token → 已登录
"""

import math
import random
import re
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).parent.parent.parent
USER_DATA_DIR = ROOT / ".xq_profile"

HOME = "https://xueqiu.com/"
PUBLISH_PAGE = "https://xueqiu.com/statuses/create"

# 登录态 cookie（雪球核心标志）
AUTH_COOKIES = ("u", "xq_a_token", "xqat")

# 滑块候选选择器（在不同 iframe 内尝试）
SLIDER_BTN_SELECTORS = [
    "[class*=btnSlide]", "[class*=slideBlock]", "[class*=slider-btn]",
    "[class*=dragBtn]", "[class*=slideBtn]", "[class*=sliderBlock]",
    "[class*=captcha-btn]", "[class*=dragBlock]", "[class*=moveBtn]",
    "[class*=sliderIcon]", "[class*=slideDrag]", "[class*=verify-slider]",
]
SLIDER_TRACK_SELECTORS = [
    "[class*=slideTrack]", "[class*=sliderTrack]", "[class*=captcha-track]",
    "[class*=slideWrapper]", "[class*=sliderContainer]", "[class*=dragTrack]",
]
SLIDER_ROOT_SELECTORS = [
    "[class*=captcha]", "[id*=captcha]", "[class*=verify]", "[class*=yidun]",
    "[class*=geetest]", "[class*=tcaptcha]", "[class*=nc_wrapper]",
]
# ⚠️ 排除项：首页的 slick-slider 是**股票指数轮播图**，
#    会被 [class*=slider] 命中 → 曾被误判成"滑块已出现"，浪费 10 分钟等待。
SLIDER_EXCLUDE = ("slick-slider", "slick-slide", "StockSlider", "stock-index",
                  "index_nav", "Header_", "widget__", "Widget")


def bezier_curve(t: float, p0: tuple, p1: tuple, p2: tuple, p3: tuple) -> tuple:
    """三次贝塞尔曲线 —— 滑块拖动轨迹用。"""
    u = 1 - t
    x = (u ** 3 * p0[0] + 3 * u ** 2 * t * p1[0]
         + 3 * u * t ** 2 * p2[0] + t ** 3 * p3[0])
    y = (u ** 3 * p0[1] + 3 * u ** 2 * t * p1[1]
         + 3 * u * t ** 2 * p2[1] + t ** 3 * p3[1])
    return x, y


class XqSession:
    """雪球会话。"""

    def __init__(self, headless: bool = False):
        self.headless = headless
        self._p = None
        self.ctx = None
        self.page = None
        self.last_requests = []

    def __enter__(self):
        self._p = sync_playwright().start()
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.ctx = self._p.chromium.launch_persistent_context(
            user_data_dir=str(USER_DATA_DIR),
            channel="chrome",
            headless=self.headless,
            args=["--window-size=1600,1000", "--disable-blink-features=AutomationControlled"],
        )
        self.ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});"
            "Object.defineProperty(navigator,'languages',{get:()=>['zh-CN','zh']});"
            "window.chrome = window.chrome || {runtime: {}};"
        )
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.page.set_viewport_size({"width": 1600, "height": 1000})

        # 监听所有请求（不限 xhr/fetch —— 文档级请求也要看）
        def on_req(r):
            try:
                if r.resource_type in ("xhr", "fetch", "document"):
                    self.last_requests.append((r.method, r.url))
            except Exception:
                pass
        self.page.on("request", on_req)
        return self

    def __exit__(self, *exc):
        try:
            self.ctx.close()
        except Exception:
            pass
        try:
            self._p.stop()
        except Exception:
            pass

    # ---------- 登录态 ----------
    def is_logged_in(self) -> bool:
        """
        判定登录态。

        ⚠️⚠️ **必须用有头模式（headless=False）调用**：
        实测雪球在 headless 下**不认登录态** ——
        headless=False 登录成功，但 headless=True 打开首页仍显示「立即登录/注册」。
        与"热门话题在 headless 下抓不到"是同一套反爬策略。

        【判定顺序：先看页面，再看 cookie】
        ⚠️ cookie 只是辅助 —— 实测登录成功后
        `ctx.cookies()` 里**未必有** xq_a_token（可能存在别的域或别的机制），
        而页面上的「立即登录/注册」消失才是**权威信号**。
        """
        try:
            body = self.page.inner_text("body")
        except Exception:
            body = ""

        # 1) 权威判据：页面不再显示登录入口
        guest = ("立即登录" in body) or ("登录/注册" in body)
        if not guest and body:
            return True

        # 2) 辅助判据：cookie
        try:
            names = {c["name"] for c in self.ctx.cookies()}
        except Exception:
            names = set()
        has_cookie = ("u" in names) and ("xq_a_token" in names or "xqat" in names)
        if has_cookie and not guest:
            return True

        # 3) 兜底：页面已登录时会有用户菜单
        try:
            has_user_menu = bool(self.page.evaluate("""() => {
                const t = document.body.innerText || '';
                return !/立即登录/.test(t);
            }"""))
            if has_user_menu and not guest:
                return True
        except Exception:
            pass

        return False

    def login_diagnostics(self) -> dict:
        """登录后诊断：cookie / localStorage / 页面状态。"""
        try:
            names = sorted(c["name"] for c in self.ctx.cookies())
        except Exception:
            names = []
        try:
            body = self.page.inner_text("body")
        except Exception:
            body = ""
        try:
            ls = self.page.evaluate("""() => {
                const out = {};
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    out[k] = String(localStorage.getItem(k)).slice(0, 60);
                }
                return out;
            }""")
        except Exception:
            ls = {}
        try:
            ss = self.page.evaluate("""() => {
                const out = {};
                for (let i = 0; i < sessionStorage.length; i++) {
                    const k = sessionStorage.key(i);
                    out[k] = String(sessionStorage.getItem(k)).slice(0, 60);
                }
                return out;
            }""")
        except Exception:
            ss = {}
        return {
            "url": self.page.url,
            "cookie_names": names,
            "localStorage": ls,
            "sessionStorage": ss,
            "guest_banner": "立即登录" in body,
            "body_head": body[:200],
        }

    def clear_auth_cookies(self) -> None:
        """清除失效的登录 cookie，强制重新登录。"""
        try:
            self.ctx.clear_cookies(name=re.compile(r"^(u|xq_a_token|xqat|xq_id_token|xq_r_token|cookiesu)$"))
            print("   已清除旧登录 cookie", flush=True)
        except Exception:
            pass

    def open(self) -> None:
        try:
            self.page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            time.sleep(3)
            self.page.goto(HOME, wait_until="domcontentloaded", timeout=90000)
        time.sleep(5)

    # ---------- 滑块 ----------
    def solve_geetest(self, attempts: int = 3, verbose: bool = True) -> bool:
        """
        自动求解**极验 geetest v3 slide3 拼图滑块**。

        ⚠️⚠️ 2026-11-15 新增。雪球账号密码登录后弹极验拼图，
        旧版 _find_slider 用的选择器（captcha/verify/yidun/...）**完全匹配不到**
        极验的类名（geetest_*），导致永远判「无滑块」→ 登录直接失败。

        极验 slide3 的结构（实测）：
          · `canvas.geetest_canvas_bg`    背景画布（含挖好的缺口）
          · `canvas.geetest_canvas_slice` 拼图块画布（随拖动重绘）
          · `div.geetest_slider_track`    滑轨（宽 ~176px）
          · `div.geetest_slider_tip`     提示文字「拖动滑块完成拼图」
          · 真实资源：`static.geetest.com/pictures/gt/<id>/<id>.webp`（完整原图）
                       `.../bg/<x>.webp`（带洞背景）、`.../slice/<y>.png`（拼图块）

        坐标换算（**实测标定**，勿改）：
          · 画布自然宽 260px，显示宽 258.14px
          · 滑块拖动位移 **=** 画布内拼图块位移，比例 **1:1**（实测 60px→60px）
          · 滑块最大行程 ~188px（超过后不再移动）

        缺口定位：把 slice.png 的 alpha 掩膜在 bg 画布上滑动，
        取「平均亮度最低」的偏移（缺口处被挖空并加了阴影 → 最暗）。

        返回 True 表示滑块已通过。
        """
        import random as _rng

        detect_js = r"""() => {
            const bgc = document.querySelector('canvas.geetest_canvas_bg');
            const scEl = document.querySelector('canvas.geetest_canvas_slice');
            const track = document.querySelector('.geetest_slider_track');
            if (!bgc || !scEl || !track) return {ok: 0, err: 'no-geetest'};

            const rs = performance.getEntriesByType('resource').map(e => e.name);
            const sliceUrl = rs.filter(u => /\/slice\//i.test(u)).pop();
            if (!sliceUrl) return {ok: 0, err: 'no-slice-url'};

            // 当前拼图块在画布中的左边缘（拖动起点）
            const cd = scEl.getContext('2d', {willReadFrequently: true})
                          .getImageData(0, 0, scEl.width, scEl.height).data;
            let curMin = -1;
            for (let y = 0; y < scEl.height; y++)
                for (let x = 0; x < scEl.width; x++) {
                    const i = (y * scEl.width + x) * 4;
                    if (cd[i + 3] > 30) { if (curMin < 0 || x < curMin) curMin = x; }
                }

            const tr = track.getBoundingClientRect();
            return {ok: 1, sliceUrl: sliceUrl, curMin: curMin,
                    canvasW: bgc.width, canvasH: bgc.height,
                    track: {x: tr.x, y: tr.y, w: tr.width, h: tr.height}};
        }"""

        # ⚠️ 核心：**归一化互相关（NCC）**定位缺口
        #   极验的缺口 = 原图像素被"挖空 + 加暗色阴影"，
        #   所以「最暗像素」会落在缺口**内部偏右**（阴影最深处）→ 拖过头。
        #   NCC 只看纹理结构、**对亮度/对比度线性变化免疫**，
        #   能把 slice 图案与缺口底下的原图纹理对齐 → 得到缺口**左边缘**。
        #   （实测 NCC 与人工看图一致；"最暗法"会偏右 ~40px。）
        match_js = r"""async (args) => {
            const {sliceUrl} = args;
            const bgc = document.querySelector('canvas.geatest_canvas_bg')
                     || document.querySelector('canvas.geetest_canvas_bg');
            const im = new Image(); im.crossOrigin = 'anonymous';
            await new Promise((res, rej) => { im.onload = res; im.onerror = rej; im.src = sliceUrl; });
            const c = document.createElement('canvas');
            c.width = im.naturalWidth; c.height = im.naturalHeight;
            const cx = c.getContext('2d', {willReadFrequently: true});
            cx.drawImage(im, 0, 0);
            const sd = cx.getImageData(0, 0, c.width, c.height).data;
            const px = [], py = [], sg = [];
            for (let y = 0; y < c.height; y++)
                for (let x = 0; x < c.width; x++) {
                    const i = (y * c.width + x) * 4;
                    if (sd[i + 3] > 200) {
                        px.push(x); py.push(y);
                        sg.push(0.299 * sd[i] + 0.587 * sd[i + 1] + 0.114 * sd[i + 2]);
                    }
                }
            const n = sg.length;
            if (!n) return {err: 'empty-mask'};
            const smean = sg.reduce((a, b) => a + b, 0) / n;
            let maskMinX = 1e9;
            for (const v of px) if (v < maskMinX) maskMinX = v;

            const W = bgc.width, H = bgc.height;
            const bd = bgc.getContext('2d', {willReadFrequently: true})
                          .getImageData(0, 0, W, H).data;
            const bl = (x, y) => 0.299 * bd[(y * W + x) * 4]
                              + 0.587 * bd[(y * W + x) * 4 + 1]
                              + 0.114 * bd[(y * W + x) * 4 + 2];

            let best = null;
            for (let dy = -10; dy <= 10; dy++) {
                for (let dx = -c.width + 2; dx < W - 2; dx++) {
                    let num = 0, den = 0, bm = 0, bn = 0, cnt = 0;
                    for (let k = 0; k < n; k++) {
                        const X = px[k] + dx, Y = py[k] + dy;
                        if (X < 0 || X >= W || Y < 0 || Y >= H) continue;
                        const v = bl(X, Y);
                        num += (sg[k] - smean) * v; den += (sg[k] - smean) * (sg[k] - smean);
                        bm += v; bn++; cnt++;
                    }
                    if (cnt < n * 0.92) continue;
                    const m2 = bm / bn;
                    let nu = 0, de = 0;
                    for (let k = 0; k < n; k++) {
                        const X = px[k] + dx, Y = py[k] + dy;
                        if (X < 0 || X >= W || Y < 0 || Y >= H) continue;
                        const v = bl(X, Y) - m2;
                        nu += (sg[k] - smean) * v; de += v * v;
                    }
                    const den2 = den * de;
                    if (den2 <= 0) continue;
                    const ncc = nu / Math.sqrt(den2);
                    if (!best || ncc > best[0]) best = [ncc, dx, dy];
                }
            }
            if (!best) return {err: 'no-match'};
            return {ncc: Math.round(best[0] * 10000) / 10000, dx: best[1], dy: best[2],
                    maskMinX: maskMinX, holeLeft: maskMinX + best[1]};
        }"""

        for attempt in range(1, attempts + 1):
            try:
                info = self.page.evaluate(detect_js)
            except Exception as e:
                if verbose:
                    print(f"   极验检测失败: {str(e)[:70]}", flush=True)
                return False
            if not info.get("ok"):
                if verbose:
                    print(f"   未检测到极验滑块 ({info.get('err')})", flush=True)
                return False

            try:
                m = self.page.evaluate(match_js, {"sliceUrl": info["sliceUrl"]})
            except Exception as e:
                if verbose:
                    print(f"   极验缺口匹配失败: {str(e)[:70]}", flush=True)
                return False
            if "holeLeft" not in m:
                if verbose:
                    print(f"   极验缺口匹配失败 ({m.get('err')})", flush=True)
                return False

            # 缺口左边缘（画布坐标）− 拼图块当前左边缘 = 需要的位移
            drag = m["holeLeft"] - info["curMin"]
            tr = info["track"]
            if verbose:
                print(f"   极验第{attempt}次: NCC={m['ncc']} 缺口左缘x={m['holeLeft']} "
                      f"块起点x={info['curMin']} → 拖动 {drag}px", flush=True)

            if drag <= 2:
                # 已在正确位置，直接松手
                pass
            else:
                # 贝塞尔 + 随机抖动的真实拖动轨迹
                rng = _rng.Random()
                sx = tr["x"] + 20
                sy = tr["y"] + tr["h"] / 2
                ex = sx + drag
                c1 = (sx + drag * rng.uniform(0.25, 0.4), sy + rng.uniform(-12, 12))
                c2 = (sx + drag * rng.uniform(0.6, 0.8), sy + rng.uniform(-12, 12))

                def bez(t, p0, p1, p2, p3):
                    u = 1 - t
                    return (u ** 3 * p0[0] + 3 * u * u * t * p1[0]
                            + 3 * u * t * t * p2[0] + t ** 3 * p3[0],
                            u ** 3 * p0[1] + 3 * u * u * t * p1[1]
                            + 3 * u * t * t * p2[1] + t ** 3 * p3[1])

                self.page.mouse.move(sx, sy)
                time.sleep(0.2)
                self.page.mouse.down()
                time.sleep(0.15)
                for i in range(1, 33):
                    t = i / 32
                    x, y = bez(t, (sx, sy), c1, c2, (ex, sy))
                    self.page.mouse.move(x, y)
                    time.sleep(0.010 + rng.uniform(0, 0.010))
                time.sleep(0.25)
                self.page.mouse.up()

            time.sleep(4)

            # 验证：面板消失 = 通过
            try:
                still = self.page.evaluate(
                    "() => !!document.querySelector('canvas.geetest_canvas_bg')")
            except Exception:
                still = True
            if not still:
                if verbose:
                    print("   ✅ 极验滑块已通过", flush=True)
                return True
            if verbose:
                print("   ❌ 滑块未通过（缺口定位偏差），重试", flush=True)
                time.sleep(2)
        return False

    def _find_slider(self):
        """
        在所有 frame 里找滑块。

        ⚠️ 滑块通常在 **iframe** 内，主文档 query_selector 找不到。
        返回 (frame, btn_el, track_el) 或 None。
        """
        for fr in self.page.frames:
            # 先找根容器（限定在可视区域内的小尺寸元素）
            root = None
            for sel in SLIDER_ROOT_SELECTORS:
                try:
                    for el in fr.query_selector_all(sel):
                        try:
                            if not el.is_visible():
                                continue
                            cls = (el.get_attribute("class") or "")
                            if any(x in cls for x in SLIDER_EXCLUDE):
                                continue
                            bb = el.bounding_box()
                            if not bb:
                                continue
                            # 滑块弹窗通常不大，且在视口内
                            if bb["width"] > 900 or bb["height"] > 600:
                                continue
                            if bb["width"] < 80:
                                continue
                            root = el
                            break
                        except Exception:
                            continue
                    if root:
                        break
                except Exception:
                    continue

            if not root:
                continue

            # 在根容器里找滑块按钮与轨道
            btn = None
            for sel in SLIDER_BTN_SELECTORS:
                try:
                    for el in root.query_selector_all(sel):
                        try:
                            if el.is_visible():
                                btn = el
                                break
                        except Exception:
                            continue
                    if btn:
                        break
                except Exception:
                    continue

            track = None
            for sel in SLIDER_TRACK_SELECTORS:
                try:
                    for el in root.query_selector_all(sel):
                        try:
                            if el.is_visible():
                                track = el
                                break
                        except Exception:
                            continue
                    if track:
                        break
                except Exception:
                    continue

            if btn:
                return fr, btn, track
        return None

    def solve_slider(self, attempts: int = 3, verbose: bool = True) -> bool:
        """
        处理滑动验证。

        策略：贝塞尔轨迹拖动 + 随机化停顿 + 中途微抖。
        失败则返回 False，交由人工处理（人工过一次的登录态会被 profile 复用）。
        """
        rng = random.Random()

        for attempt in range(1, attempts + 1):
            found = self._find_slider()
            if not found:
                if verbose:
                    print("   未发现滑块（可能已通过或未触发）", flush=True)
                return True

            fr, btn, track = found
            try:
                bb = btn.bounding_box()
                if not bb:
                    return False

                # 目标：拖到轨道右端（若没有轨道，按 260px 估）
                if track:
                    tb = track.bounding_box()
                    if tb:
                        start = (bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                        end = (tb["x"] + tb["width"] - bb["width"] / 2 - 2,
                               tb["y"] + tb["height"] / 2)
                    else:
                        start = (bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                        end = (start[0] + 260, start[1])
                else:
                    start = (bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2)
                    end = (start[0] + 260, start[1])

                dist = end[0] - start[0]
                if dist < 30:
                    dist = 200

                # 贝塞尔控制点：模拟人手的不规则轨迹
                c1 = (start[0] + dist * rng.uniform(0.25, 0.4),
                      start[1] + rng.uniform(-30, 30))
                c2 = (start[0] + dist * rng.uniform(0.6, 0.8),
                      start[1] + rng.uniform(-30, 30))

                if verbose:
                    print(f"   滑块拖动 第{attempt}次: {int(dist)}px", flush=True)

                # 真实鼠标事件（iframe 内坐标 = frame 内相对坐标 + frame 偏移）
                frame_off = fr.evaluate("() => {const r=document.body.getBoundingClientRect();"
                                        "return {x:r.x,y:r.y};}")
                sx = start[0] + frame_off["x"]
                sy = start[1] + frame_off["y"]

                self.page.mouse.move(sx, sy)
                time.sleep(rng.uniform(0.15, 0.35))
                self.page.mouse.down()
                time.sleep(rng.uniform(0.1, 0.25))

                steps = 28
                for i in range(1, steps + 1):
                    t = i / steps
                    x, y = bezier_curve(t, (0, 0),
                                       (c1[0] - start[0], c1[1] - start[1]),
                                       (c2[0] - start[0], c2[1] - start[1]),
                                       (end[0] - start[0], end[1] - start[1]))
                    # 中途微抖
                    jitter = rng.uniform(-1.6, 1.6)
                    self.page.mouse.move(sx + x, sy + y + jitter)
                    time.sleep(rng.uniform(0.008, 0.028))

                time.sleep(rng.uniform(0.1, 0.25))
                self.page.mouse.up()
                time.sleep(2.5)

                # 校验滑块是否消失
                if not self._find_slider():
                    if verbose:
                        print("   ✅ 滑块已通过", flush=True)
                    return True
                if verbose:
                    print("   ⚠️ 滑块仍在，尝试下一轮", flush=True)
            except Exception as e:
                if verbose:
                    print(f"   滑块处理异常: {str(e)[:90]}", flush=True)
        return False

    # ---------- 登录 ----------
    def _accept_terms(self) -> bool:
        """
        勾选「阅读并同意 服务协议、隐私政策」。

        ⚠️⚠️ **这是登录的前置条件**：不勾 → 点「登 录」前端校验直接 return
        → 一个请求都不发（本次排查耗时最长的坑）。

        【协议控件的两种可能结构，都要覆盖】
        1. `<label>` 内包裹自定义圆圈 `<i class="iconfont_iconfont_9UW">`
        2. 「阅读并同意」span 的**前一个兄弟节点** `<i class="... newLogin_nochecked">`

        【可靠的校验方式：图标 class 变化】
        未勾 = `newLogin_nochecked`，已勾 = `newLogin_checked`（实测字形  → ）。
        ⚠️ **不能用"登录按钮是否 active"来校验** ——
           按钮 class 含 `newLogin_btn-active` 是**表单填完、有值之后**才有的，
           在填表前检查必然误判为"没勾上"。
        """
        # 1) 先记下当前图标 class（用于判断是否已勾）
        def _icon_state():
            """
            读协议图标状态。

            ⚠️⚠️ **实测：勾选后 class 变成 `iconfont_iconfont_9UW`（既不是
            nochecked 也不是 checked）** —— 它只是**去掉了 nochecked 后缀**：
                未勾: "iconfont_iconfont_9UW newLogin_nochecked_3WZ "  字形 
                已勾: "iconfont_iconfont_9UW  "                      字形 
            → 所以判据必须是「**含 newLogin_nochecked = 未勾**」，
              不能去找 newLogin_checked（永远找不到）。
            ⚠️ 也不要用正则（Python→JS 的反斜杠转义会破坏），用 includes。
            """
            try:
                return self.page.evaluate(r"""() => {
                    const lab = document.querySelector('label[for="terms-law"]')
                        || [...document.querySelectorAll('label')]
                             .find(e => (e.textContent||'').includes('阅读并同意'));
                    if (!lab) return 'none';
                    const icons = [...lab.querySelectorAll('i')];
                    if (!icons.length) return 'none';
                    const c = icons[0].className || '';
                    return c.includes('newLogin_nochecked') ? 'unchecked' : 'checked';
                }""")
            except Exception:
                return "err"

        before = _icon_state()
        if before == "checked":
            print("   协议已是勾选状态", flush=True)
            return True

        # 2) 三种定位方式依次尝试
        clicked = False
        for attempt in range(3):
            try:
                clicked = self.page.evaluate(r"""
                    () => {
                        // 方式1：label 内的 <i> 图标
                        const labels = [...document.querySelectorAll('label')].filter(e => {
                            const t = (e.textContent || '');
                            return t.includes('阅读并同意') || t.includes('服务协议');
                        });
                        for (const l of labels) {
                            const icon = [...l.children].find(c =>
                                c.tagName === 'I' || /iconfont/i.test(c.className || ''));
                            if (icon) { icon.click(); return 'icon'; }
                        }
                        // 方式2：「阅读并同意」文本的前一个兄弟 <i>
                        const el = [...document.querySelectorAll('span,div,i,p,label')]
                            .find(e => (e.textContent||'').trim().startsWith('阅读并同意')
                                       && (e.textContent||'').trim().length < 20);
                        if (el) {
                            let prev = el.previousElementSibling;
                            for (let i = 0; i < 4 && prev; i++) {
                                const r = prev.getBoundingClientRect();
                                if (r.width > 0 && r.width < 30 && r.height > 0 && r.height < 30) {
                                    prev.click(); return 'sibling';
                                }
                                prev = prev.previousElementSibling;
                            }
                        }
                        // 方式3：label 内的隐藏 input
                        for (const l of labels) {
                            const cb = l.querySelector('input[type=checkbox]');
                            if (cb) { if (!cb.checked) cb.click(); return 'input'; }
                        }
                        return 'none';
                    }
                """)
            except Exception:
                clicked = False

            time.sleep(0.8)
            if _icon_state() == "checked":
                print("   已勾选服务协议/隐私政策", flush=True)
                return True

            # 兜底：JS click() 对自定义图标可能无效（React 事件委托），
            #       改用**真实鼠标坐标点击**图标中心
            try:
                pos = self.page.evaluate(r"""() => {
                    const lab = document.querySelector('label[for="terms-law"]')
                        || [...document.querySelectorAll('label')]
                             .find(e => (e.textContent||'').includes('阅读并同意'));
                    if (!lab) return null;
                    const icons = [...lab.querySelectorAll('i')];
                    if (!icons.length) return null;
                    const r = icons[0].getBoundingClientRect();
                    if (r.width === 0 || r.height === 0) return null;
                    return {x: r.x + r.width / 2, y: r.y + r.height / 2,
                            cls: icons[0].className};
                }""")
                if pos:
                    print(f"   用鼠标点击协议图标 {pos['cls'][:40]}", flush=True)
                    self.page.mouse.click(pos["x"], pos["y"])
                    time.sleep(0.9)
                    if _icon_state() == "checked":
                        print("   已勾选服务协议/隐私政策（鼠标点击）", flush=True)
                        return True
            except Exception as e:
                print(f"   鼠标点击协议图标异常: {str(e)[:60]}", flush=True)

            time.sleep(0.4)

        if not clicked or clicked == "none":
            print("   ⚠️ 未找到协议勾选控件", flush=True)
        else:
            print(f"   ⚠️ 协议已点但状态未变（{_icon_state()}）", flush=True)
        return False

    def _wait_login_modal(self, timeout: int = 25) -> bool:
        """等登录弹窗渲染出来（页面刚 goto 时弹窗还没挂载）。"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                ok = self.page.evaluate("""() => {
                    const m = document.querySelector('[class*=newLogin_modal]');
                    if (!m) return false;
                    const r = m.getBoundingClientRect();
                    return r.width > 100 && r.height > 100;
                }""")
                if ok:
                    return True
            except Exception:
                pass
            time.sleep(0.5)
        return False


    def _click_login_button(self) -> bool:
        """
        点击「登 录」按钮。

        ⚠️⚠️ 实测踩坑（两个都失败过）：
          · get_by_text("登 录", exact=True) → 匹配不到
            实际按钮文本是 **'登    录'（4个空格）**：
              codes=[30331, 32, 32, 32, 32, 24405]  ('登',' ',' ',' ',' ','录')
          · xpath=//*[normalize-space(text())='登录'] → count=0
            因为 normalize-space 把连续空格压成**一个**空格，
            得到 '登 录'，仍不等于 '登录'。
        ✅ 正解：按 **class** 定位 `newLogin_modal__login__btn_*`
           （snowflake 哈希后缀，用前缀匹配）。
        """
        # 1) class 前缀匹配（最稳）
        try:
            loc = self.page.locator("div[class*='newLogin_modal__login__btn']")
            if loc.count():
                el = loc.first
                el.scroll_into_view_if_needed(timeout=4000)
                time.sleep(0.4)
                bb = el.bounding_box()
                if bb:
                    self.page.mouse.click(bb["x"] + bb["width"] / 2,
                                         bb["y"] + bb["height"] / 2)
                else:
                    el.click(timeout=6000, force=True)
                return True
        except Exception:
            pass

        # 2) 文本兜底：去掉所有空白后匹配
        try:
            found = self.page.evaluate("""() => {
                const out = [];
                document.querySelectorAll('div,span,button,a').forEach(el => {
                    const t = (el.textContent||'');
                    if (t.replace(/\\s+/g,'') !== '登录') return;
                    const r = el.getBoundingClientRect();
                    if (r.width < 100 || r.height < 25) return;
                    out.push({x:r.x+r.width/2, y:r.y+r.height/2});
                });
                return out;
            }""")
            if found:
                self.page.mouse.click(found[0]["x"], found[0]["y"])
                return True
        except Exception:
            pass

        # 3) Playwright locator 宽松匹配
        try:
            self.page.get_by_text("登 录").first.click(timeout=6000)
            return True
        except Exception:
            pass
        try:
            self.page.get_by_text("登录", exact=False).first.click(timeout=6000)
            return True
        except Exception:
            return False

    def _fill_login_form(self, user: str, passwd: str) -> bool:
        """
        填写「账号密码登录」表单。

        ⚠️ 2026-11-15 实测：**必须用真实键盘事件**（page.type），
        不能只用原生 setter + dispatchEvent。
        原因：React 的受控组件依赖「输入事件序列」判断用户是否真的输入过，
        只 dispatch 一次 'input' 有时不会被状态机采纳（表单停留在空值，
        提交时前端校验直接 return → 点登录零请求）。

        → 真实 click 聚焦 + 逐字符 type（delay 让 React 有机会处理）。
        """
        ok_all = True
        for name, val in (("username", user), ("password", passwd)):
            try:
                el = self.page.query_selector(f'input[name="{name}"]')
                if not el:
                    print(f"   填写 {name}: 找不到输入框", flush=True)
                    ok_all = False
                    continue
                el.click(timeout=6000)
                time.sleep(0.25)
                el.type(val, delay=45, timeout=30000)
                print(f"   填写 {name}: OK", flush=True)
            except Exception as e:
                print(f"   填写 {name} 异常: {str(e)[:70]}", flush=True)
                ok_all = False
        return ok_all

    def login(self, user: str, passwd: str, manual_ok: bool = True) -> bool:
        """
        账号密码登录 + **滑块处理**。

        ⚠️⚠️ **两个硬性前提**（实测踩坑）：
        1. 必须 **headless=False** —— headless 下雪球不认登录态
        2. 必须**有头模式下完成登录**且**不关闭浏览器**
           —— Chrome 的 session cookie（xq_a_token 等）在进程退出时不持久化，
              登录态会丢。所以**登录与发帖必须在同一个 session 内完成**。

        滑块真相（2026-11-17 实测，纠正了之前的错误结论）：
        · 点「登 录」**是有效的** —— 会触发
          GET /snb/provider/gee_test/get?type=login_pwd&telephone=...
        · 滑块是**极验（GeeTest）拼图**，且在**主文档**里（不是 iframe）
        · 检测依据：页面出现「拖动滑块完成拼图」
        """
        from publish.xq.slider import _has_slider_text, solve_slider

        self.open()

        if self.is_logged_in():
            print("✅ 已有有效登录态", flush=True)
            return True

        # cookie 残留但服务端不认 → 清掉重来
        try:
            names = {c["name"] for c in self.ctx.cookies()}
            if "u" in names or "xq_a_token" in names:
                print("   检测到失效登录态（cookie 残留但服务端未登录）", flush=True)
                self.clear_auth_cookies()
                self.open()
        except Exception:
            pass

        # 等登录弹窗渲染（否则找不到协议控件、也切不了标签）
        if self._wait_login_modal():
            print("   登录弹窗已加载", flush=True)
        else:
            print("   ⚠️ 登录弹窗未出现，尝试重新加载", flush=True)
            self.open()
            self._wait_login_modal()

        # 切到「账号密码登录」（必须点精确的 <a>）
        switched = False
        try:
            self.page.locator("xpath=//a[normalize-space(text())='账号密码登录']").first.click(
                timeout=5000, force=True)
            switched = True
        except Exception:
            for el in self.page.query_selector_all("a,span,div"):
                try:
                    if el.is_visible() and (el.inner_text() or "").strip() == "账号密码登录":
                        el.click(timeout=5000, force=True)
                        switched = True
                        break
                except Exception:
                    continue
        # 等标签切换完成（判据：出现「忘记密码」= 账号密码标签；
        #                 出现「邮箱验证码登录」= 还在验证码标签）
        for _ in range(12):
            try:
                t = self.page.inner_text("body")
                if "忘记密码" in t:
                    break
            except Exception:
                pass
            time.sleep(0.5)
        time.sleep(1.0)
        if switched:
            print("   已切到「账号密码登录」标签", flush=True)

        # 勾协议（前置条件，不勾则点登录无请求）
        self._accept_terms()

        # 填表（真实键盘输入，isTrusted 事件）
        for name, val in (("username", user), ("password", passwd)):
            try:
                el = self.page.query_selector(f"input[name='{name}']")
                if el and el.is_visible():
                    el.click()
                    time.sleep(0.2)
                    el.press("Control+A")
                    time.sleep(0.1)
                    el.press("Delete")
                    time.sleep(0.15)
                    el.type(val, delay=60)
                    time.sleep(0.4)
                    el.press("Tab")
                    time.sleep(0.4)
            except Exception as e:
                print(f"   填 {name} 失败: {str(e)[:60]}", flush=True)
        print("   账号密码已填", flush=True)

        # 点登录
        if not self._click_login_button():
            print("   ❌ 未能点击登录按钮", flush=True)
            return False
        print("   已点击登录", flush=True)
        time.sleep(4)

        # ★ 滑块处理
        if _has_slider_text(self.page):
            print("   检测到滑块验证（极验拼图），程序化处理中...", flush=True)
            ok = solve_slider(self.page, attempts=4)
            print(f"   滑块处理: {'✅ 已通过' if ok else '❌ 未通过'}", flush=True)

            if ok:
                # ⚠️⚠️ **极验是前置校验**：滑块过了之后**必须再点一次登录**
                #    才会真正提交表单（否则滑块白过，登录请求不发）。
                time.sleep(1.5)
                print("   滑块已过，重新点击登录以提交表单", flush=True)
                self._click_login_button()
                time.sleep(4)
                # 可能又出现新滑块
                if _has_slider_text(self.page):
                    ok2 = solve_slider(self.page, attempts=3)
                    print(f"   二次滑块: {'✅ 已通过' if ok2 else '❌ 未通过'}", flush=True)
                    if ok2:
                        time.sleep(1.5)
                        self._click_login_button()
                        time.sleep(4)

            if not ok:
                if not manual_ok:
                    return False
                self.show_login_hint("请手动拖动滑块完成拼图验证")
                deadline = time.time() + 600
                while time.time() < deadline:
                    time.sleep(3)
                    if self.is_logged_in():
                        print("✅ 登录成功", flush=True)
                        return True
                    if _has_slider_text(self.page):
                        if solve_slider(self.page, attempts=2):
                            time.sleep(1.5)
                            self._click_login_button()
                            time.sleep(3)
        else:
            print("   未出现滑块", flush=True)

        # 等登录完成（最多 90 秒）
        deadline = time.time() + 90
        last = 0
        while time.time() < deadline:
            if self.is_logged_in():
                print("✅ 登录成功", flush=True)
                try:
                    diag = self.login_diagnostics()
                    print(f"   诊断: cookie={diag['cookie_names']}", flush=True)
                    print(f"   诊断: localStorage keys={list(diag['localStorage'].keys())}",
                          flush=True)
                except Exception as e:
                    print(f"   诊断失败: {str(e)[:60]}", flush=True)
                return True
            if time.time() - last > 10:
                last = time.time()
                try:
                    body = self.page.inner_text("body")
                    print(f"   等待登录中…（'立即登录/注册'在页面: "
                          f"{'立即登录/注册' in body}）", flush=True)
                except Exception:
                    pass
            time.sleep(2)
        return self.is_logged_in()

    def show_login_hint(self, msg: str = "请拖动滑块完成拼图验证") -> None:
        try:
            self.page.evaluate(r"""
                () => {
                    const m = document.createElement('div');
                    m.id = 'ax-xq-hint';
                    m.style.cssText = [
                        'position:fixed','top:10px','left:10px','z-index:2147483647',
                        'background:#e94b4b','color:#fff','font-size:15px','padding:12px 20px',
                        'border-radius:8px','line-height:1.7','box-shadow:0 4px 16px rgba(0,0,0,.25)',
                        'max-width:360px'
                    ].join(';');
                    m.innerHTML = '<b>还差最后一步</b><br>' + msg;
                    document.body.appendChild(m);
                }
            """, msg)
        except Exception:
            pass
