"""
雪球滑块验证处理（极验 GeeTest 拼图）。

【实测结构 —— 2026-11-17，重要修正】
雪球用的是**极验（GEETest）拼图验证**，触发接口：
    GET https://www.xueqiu.com/snb/provider/gee_test/get?type=login_pwd&telephone=...
    GET https://www.xueqiu.com/snb/provider/gee_test/get?type=uc_login_web_vcode_action&...

⚠️⚠️ **三个关键事实（推翻了我之前的判断）**：
1. **滑块不在 iframe 里** —— 实测点击「登 录」后 `iframe 数 = 0`，
   滑块是**主文档里的元素**（雪球自己实现的极验变体）。
   → 我之前用 `page.frames` 找滑块，注定找不到。
2. **弹窗提示文案**：「拖动下方滑块 / 落雨点拼合处就可以放手啦」
   「拖动滑块完成拼图」「关闭验证」「刷新验证」
3. **点击「登 录」其实是有效的** —— 它确实触发了 gee_test 请求，
   我之前误判为"点击无效"，是因为请求过滤关键词里没有 `gee_test`。
   → 之前的错误结论：以为按钮点不动；真相是点动了，弹出的是滑块。

【滑块视觉结构（截图实测）】
    ┌─────────────────────────┐
    │  拖动下方滑块             │
    │  ▣拼图块      ▣缺口      │   ← 左边可移动的拼图块，右边深色缺口
    │  拖动滑块完成拼图  │     │   ← 底部轨道
    │   ⠿          ↻           │   ← 圆形手柄（带三条竖线）+ 刷新
    └─────────────────────────┘
→ 要做的是：把手柄从左边拖到缺口 x 位置。

【定位策略（不靠硬编码像素）】
1. DOM 找滑块容器与手柄（按 class 关键词 + 提示文案双重定位）
2. **图像分析找缺口**：截图拼图区，找那块"深色方块"（缺口）
3. 计算位移 = 缺口中心 x - 手柄中心 x
4. 贝塞尔轨迹拖动（模拟真人，带中途微抖与随机停顿）
5. 校验：滑块消失 或 出现「验证通过」

【为什么需要图像分析】
极验拼图的缺口位置是**随机的**，硬编码比例必然失败。
"""

import random
import time
from pathlib import Path

ROOT = Path(__file__).parent.parent.parent

# 滑块容器的候选（按雪球实测 + 极验通用 class）
CONTAINER_SELECTORS = [
    "[class*='geetest']", "[class*='gee_test']", "[class*='captcha']",
    "[class*='verify']", "[class*='slide']", "[class*='drag']",
]
HANDLE_SELECTORS = [
    "[class*='slider_button']", "[class*='sliderBtn']", "[class*='btnSlide']",
    "[class*='slideButton']", "[class*='slideBtn']", "[class*='dragBtn']",
    "[class*='dragBlock']", "[class*='icon']", "[class*='btn']",
]


def _has_slider_text(page) -> bool:
    """用提示文案判断滑块是否出现（比 class 更可靠）。"""
    try:
        body = page.inner_text("body")
    except Exception:
        return False
    return any(k in body for k in ("拖动滑块完成拼图", "拖动下方滑块",
                                   "拼合处", "滑动验证", "请完成安全验证"))


def find_slider(page):
    """
    定位滑块容器与手柄。

    返回 (container_el, handle_el, bbox_dict) 或 None。
    """
    if not _has_slider_text(page):
        return None

    # 1) 找容器：含提示文案的元素里取**面积最小**的
    #    ⚠️ 之前取"第一个" → 选中了最外层（整个视口 1585x1000），
    #       导致缺口分析的坐标全错、位移算了 951px。
    container = None
    best_area = None
    for el in page.query_selector_all("div,section"):
        try:
            if not el.is_visible():
                continue
            t = (el.inner_text() or "")
            if "拖动滑块完成拼图" not in t and "拖动下方滑块" not in t:
                continue
            bb = el.bounding_box()
            if not bb or bb["width"] < 150 or bb["height"] < 80:
                continue
            area = bb["width"] * bb["height"]
            if best_area is None or area < best_area:
                best_area = area
                container = el
        except Exception:
            continue

    if container is None:
        # 兜底：按 class 找
        for sel in CONTAINER_SELECTORS:
            try:
                for el in page.query_selector_all(sel):
                    if not el.is_visible():
                        continue
                    bb = el.bounding_box()
                    if bb and 150 < bb["width"] < 700 and 80 < bb["height"] < 500:
                        container = el
                        break
                if container:
                    break
            except Exception:
                continue

    if container is None:
        return None

    # 2) 找手柄：容器内最小的可拖动元素（优先 class 匹配）
    handle = None
    for sel in HANDLE_SELECTORS:
        try:
            cands = []
            for el in container.query_selector_all(sel):
                try:
                    if not el.is_visible():
                        continue
                    bb = el.bounding_box()
                    if bb and 15 <= bb["width"] <= 70 and 15 <= bb["height"] <= 70:
                        cands.append((bb["width"] * bb["height"], el))
                except Exception:
                    continue
            if cands:
                cands.sort(key=lambda x: x[0])
                handle = cands[0][1]
                break
        except Exception:
            continue

    if handle is None:
        # 兜底：容器内最小面积的元素（手柄通常是最小的方块）
        try:
            cands = []
            for el in container.query_selector_all("div,span,i,img"):
                bb = el.bounding_box()
                if bb and 12 <= bb["width"] <= 70 and 12 <= bb["height"] <= 70:
                    cands.append((bb["width"] * bb["height"], el))
            if cands:
                cands.sort(key=lambda x: x[0])
                handle = cands[0][1]
        except Exception:
            pass

    if handle is None:
        return None

    try:
        return container, handle, handle.bounding_box()
    except Exception:
        return None


def find_gap_x(page, container_el, shots_dir: str | None = None,
                verbose_gap: bool = False):
    """
    图像分析找**缺口左边缘**与**拼图块左边缘**（相对容器左边）。

    【实测像素数据 —— 2026-11-17，参数寻优 48 组得出】
    容器截图 276x220，各处 RGB 合计：
        缺口内部      RGB(41,32,19)   合计 **92**   ← 最暗
        地图背景      RGB(78,70,65)   合计 213       ← 也偏暗，会干扰普通阈值
        左侧拼图块    RGB(139,120,87) 合计 346
        顶部/底部文字 RGB(254,252,249) 合计 755

    ⚠️ 暗阈 150/200 **一个都命中不了**（缺口有纹理，不是纯色）
    ✅ 暗阈 340-460 + 占比 0.30-0.40 → 15 组命中，命中段都是 (171,215)/(175,215)

    【⚠️⚠️ 关键：目标点必须是缺口的「左边缘」，不是中心】
    拼图块宽约 40px，初始位置在拼图区最左（相对容器 x≈15）。
    若按「缺口中心 − 手柄中心」算，会多算约半个拼图块（≈30px），
    而缺口本身只有 40px 宽 → 误差足以失败。
    → 位移 = 缺口左边缘 − 拼图块左边缘（两者都相对容器左边）

    返回 (gap_left, piece_left)；找不到返回 None。
    """
    try:
        from PIL import Image
    except Exception:
        return None

    try:
        bb = container_el.bounding_box()
        if not bb:
            return None
        shots_dir = shots_dir or str(ROOT / "assets")
        Path(shots_dir).mkdir(parents=True, exist_ok=True)
        img_path = f"{shots_dir}/_slider_gap.png"
        page.screenshot(path=img_path, clip=bb)

        im = Image.open(img_path).convert("RGB")
        w, h = im.size
        px = im.load()

        DARK = 420          # 参数寻优最优
        RATIO = 0.30
        y0, y1 = int(h * 0.20), int(h * 0.62)   # 拼图区
        band = max(1, y1 - y0)

        col_ratio = []
        for x in range(w):
            dark = 0
            for y in range(y0, y1):
                r, g, b = px[x, y]
                if r + g + b < DARK:
                    dark += 1
            col_ratio.append(dark / band)

        def _segments(min_ratio):
            segs = []
            start = None
            for x, ratio in enumerate(col_ratio):
                if ratio >= min_ratio:
                    if start is None:
                        start = x
                else:
                    if start is not None:
                        segs.append((start, x - 1))
                        start = None
            if start is not None:
                segs.append((start, w - 1))
            return segs

        def _mean_dark(sg):
            tot = 0
            n = 0
            for x in range(sg[0], sg[1] + 1):
                for y in range(y0, y1, 2):
                    tot += sum(px[x, y])
                    n += 1
            return tot / max(1, n)

        # ---- 缺口：右侧、较窄、较暗的段 ----
        cand = [sg for sg in _segments(RATIO)
                if sg[1] - sg[0] >= 10]
        if not cand:
            cand = [sg for sg in _segments(RATIO * 0.7)
                    if sg[1] - sg[0] >= 8]
        if not cand:
            return None

        # ⚠️⚠️ **缺口 = 宽度最接近 40px 的暗段**（极验拼图块宽度固定≈40px）
        #   实测数据：
        #     [(8,168), (222,266)] → (222,266) 宽 45 ✓ 缺口；(8,168) 是拼图块与背景连成一片
        #     [(134,144), (162,173), (211,237)] → 宽 11/11/27，都不像缺口（可能没检出）
        #     [(121,131)] → 宽 11，也不是
        #   不能用「最右」（第1次会选到背景右边缘暗区）
        #   也不能用「最暗」（背景暗区会带偏 60px）
        if verbose_gap:
            print(f"      暗段集合={cand}", flush=True)

        # 按宽度筛：30~60px 优先（缺口宽度≈40）
        width_ok = [sg for sg in cand if 28 <= (sg[1] - sg[0]) <= 62]
        if width_ok:
            gap = max(width_ok, key=lambda sg: sg[1] - sg[0])
        else:
            # 退化：取最右的段
            gap = max(cand, key=lambda sg: sg[0])

        # 拼图块初始位置：极验版式固定，在拼图区最左
        # （实测相对容器左边 ≈ 15px；手柄左边缘只有 3px，不能用手柄）
        piece_left = int(w * 0.055)
        return gap[0], piece_left
    except Exception:
        return None


def drag(page, handle_el, distance: int, rng: random.Random,
         steps: int = 30) -> None:
    """贝塞尔轨迹拖动（模拟真人）。"""
    try:
        bb = handle_el.bounding_box()
        if not bb:
            return
        sx = bb["x"] + bb["width"] / 2
        sy = bb["y"] + bb["height"] / 2
    except Exception:
        return

    c1 = (sx + distance * rng.uniform(0.25, 0.4), sy + rng.uniform(-14, 14))
    c2 = (sx + distance * rng.uniform(0.6, 0.8), sy + rng.uniform(-14, 14))
    ex = sx + distance
    ey = sy + rng.uniform(-3, 3)

    def bez(t):
        u = 1 - t
        return (u ** 3 * sx + 3 * u ** 2 * t * c1[0] + 3 * u * t ** 2 * c2[0] + t ** 3 * ex,
                u ** 3 * sy + 3 * u ** 2 * t * c1[1] + 3 * u * t ** 2 * c2[1] + t ** 3 * ey)

    page.mouse.move(sx, sy)
    time.sleep(rng.uniform(0.15, 0.35))
    page.mouse.down()
    time.sleep(rng.uniform(0.1, 0.25))
    for i in range(1, steps + 1):
        x, y = bez(i / steps)
        page.mouse.move(x, y + rng.uniform(-1.2, 1.2))
        time.sleep(rng.uniform(0.008, 0.026))
    time.sleep(rng.uniform(0.08, 0.2))
    page.mouse.up()


def solve_slider(page, attempts: int = 10, verbose: bool = True) -> bool:
    """
    处理雪球滑块（极验拼图）。

    返回 True 表示滑块已通过或消失。
    """
    rng = random.Random()

    for attempt in range(1, attempts + 1):
        found = find_slider(page)
        if not found:
            if verbose:
                print("   未发现滑块（可能已通过）", flush=True)
            return True

        container, handle, bb = found
        if verbose:
            try:
                cbb = container.bounding_box()
            except Exception:
                cbb = None
            print(f"   滑块已定位（{attempt}/{attempts}）"
                  f" 容器bbox={cbb} 手柄bbox={bb}", flush=True)

        # 找缺口（返回 缺口左边缘, 拼图块左边缘）
        found_gap = find_gap_x(page, container, verbose_gap=verbose)
        distance = None
        if found_gap:
            gap_left, piece_left = found_gap
            try:
                cbb = container.bounding_box()
                # 位移 = 缺口左边缘 − 拼图块左边缘（都相对容器左边）
                distance = int(gap_left - piece_left)
            except Exception:
                distance = None

        if distance is None:
            # 无图像分析 → 按轨道比例拖（极验缺口通常在 55%-80%）
            try:
                cbb = container.bounding_box()
                distance = int(cbb["width"] * 0.6)
            except Exception:
                distance = 120
            if verbose:
                print("   ⚠️ 无法定位缺口，按比例拖动", flush=True)

        # 位移不能为负，也不能超出轨道
        try:
            cbb = container.bounding_box()
            max_d = int(cbb["width"] * 0.85)
            distance = max(10, min(distance, max_d))
        except Exception:
            distance = max(10, min(distance, 300))

        if verbose and found_gap:
            print(f"   缺口左边缘={found_gap[0]} 拼图块左边缘={found_gap[1]} "
                  f"→ 拖动位移 {distance}px", flush=True)
        # ⚠️ 检测有误差 → 前后微调几个候选值，提高命中率
        if distance:
            distance = distance + rng.choice([-10, -5, 0, 0, 0, 5, 10])
        elif verbose:
            print(f"   拖动位移 {distance}px（按比例）", flush=True)
        drag(page, handle, distance, rng)
        time.sleep(2.5)

        # 校验
        if not _has_slider_text(page):
            if verbose:
                print("   ✅ 滑块已通过", flush=True)
            return True
        # 失败 → 点刷新重试
        if verbose:
            print("   ⚠️ 未通过，尝试刷新", flush=True)
        try:
            for el in page.query_selector_all("span,div,a,i"):
                if el.is_visible() and (el.inner_text() or "").strip() == "刷新验证":
                    el.click(timeout=3000, force=True)
                    break
        except Exception:
            pass
        time.sleep(2.0)

    return not _has_slider_text(page)


if __name__ == "__main__":
    print("雪球滑块处理模块（供 publisher.py 调用）")
    print("关键修正：滑块在主文档，不在 iframe；提示文案是可靠的检测依据")
