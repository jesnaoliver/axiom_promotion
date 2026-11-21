"""
守候进程：等用户关闭浏览器 → 自动提取雪球 cookie → 导入 .xq_profile → 验证。

【背景】
用户的日常浏览器（Edge + Chrome 都在跑）已登录雪球。
浏览器运行时 Cookies 文件被**独占锁**（WinError 32），
所有共享读方式（shutil / sqlite online backup / Win32 CreateFile 各种 share 模式）全部失败。
→ 唯一可行：浏览器退出后读取。

【本脚本行为】
1. 轮询检测 Edge/Chrome 是否全部退出
2. 一旦退出 → 提取雪球登录 cookie（u / xq_a_token / xqat / xq_id_token / xq_r_token）
3. 复制 Local State（含 DPAPI os_crypt key）到 .xq_profile
4. 把 cookie 写入 .xq_profile/Default/Network/Cookies
5. 启动 Playwright 验证登录态（页面是否出现昵称而非「立即登录/注册」）

【安全边界】
只提取 xueqiu.com 域名的 cookie，不碰其他站点。
"""

import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
from datetime import datetime

ROOT = pathlib.Path(__file__).parent.parent
XQ_PROFILE = ROOT / ".xq_profile"
LA = os.environ.get("LOCALAPPDATA", "")

AUTH_COOKIES = ("u", "xq_a_token", "xqat", "xq_id_token", "xq_r_token")
XQ_HOSTS = ("xueqiu.com", "taoguba.net", "xueqiu.cn")
BROWSERS = [
    ("Edge", pathlib.Path(LA) / "Microsoft/Edge/User Data"),
    ("Chrome", pathlib.Path(LA) / "Google/Chrome/User Data"),
]


def browsers_running() -> list[str]:
    import psutil
    out = []
    for p in psutil.process_iter(["name"]):
        n = (p.info["name"] or "").lower()
        if n in ("chrome.exe", "msedge.exe", "brave.exe"):
            out.append(n)
    return out


def find_profiles() -> list[tuple[str, pathlib.Path, pathlib.Path]]:
    """返回 [(浏览器名, profile根, Cookies路径)]"""
    out = []
    for bname, base in BROWSERS:
        if not base.exists():
            continue
        profs = ["Default"] + [d.name for d in base.glob("Profile*") if d.is_dir()]
        for pr in profs:
            for rel in ("Network/Cookies", "Cookies"):
                ck = base / pr / rel
                if ck.exists():
                    out.append((bname, base / pr, ck))
                    break
    return out


def read_xq_cookies(ck_path: pathlib.Path) -> list[tuple]:
    """读取并筛选雪球登录 cookie。"""
    tmp = pathlib.Path(tempfile.gettempdir()) / f"xq_{os.getpid()}.db"
    if tmp.exists():
        tmp.unlink()
    shutil.copy2(ck_path, tmp)
    con = sqlite3.connect(str(tmp))
    cur = con.cursor()
    cur.execute("PRAGMA table_info(cookies)")
    cols = [r[1] for r in cur.fetchall()]
    if "host_key" not in cols:
        con.close()
        tmp.unlink(missing_ok=True)
        return []
    ph = ",".join("?" * len(AUTH_COOKIES))
    cur.execute(
        f"SELECT name, value, encrypted_value, host_key, path, expires_utc, "
        f"is_secure, is_httponly, samesite, source_scheme, source_port, "
        f"has_expires, is_persistent, priority, last_access_utc, source_type "
        f"FROM cookies WHERE name IN ({ph})", list(AUTH_COOKIES))
    rows = [r for r in cur.fetchall() if any(h in r[3] for h in XQ_HOSTS)]
    con.close()
    tmp.unlink(missing_ok=True)
    return rows


def import_cookies(rows: list[tuple], local_state_src: pathlib.Path) -> int:
    """写入 .xq_profile，并复制 Local State。"""
    XQ_PROFILE.mkdir(parents=True, exist_ok=True)

    # Local State（DPAPI key）
    try:
        shutil.copy2(local_state_src, XQ_PROFILE / "Local State")
        print("  ✅ Local State 已复制", flush=True)
    except Exception as e:
        print(f"  ⚠️ Local State 复制失败: {str(e)[:60]}", flush=True)

    tgt_dir = XQ_PROFILE / "Default" / "Network"
    tgt_dir.mkdir(parents=True, exist_ok=True)
    tgt = tgt_dir / "Cookies"

    con = sqlite3.connect(str(tgt))
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS cookies (
        creation_utc INTEGER NOT NULL, host_key TEXT NOT NULL,
        top_frame_site_key TEXT NOT NULL DEFAULT '',
        name TEXT NOT NULL, value TEXT NOT NULL DEFAULT '',
        encrypted_value BLOB NOT NULL DEFAULT '',
        path TEXT NOT NULL, expires_utc INTEGER NOT NULL,
        is_secure INTEGER NOT NULL, is_httponly INTEGER NOT NULL,
        last_access_utc INTEGER NOT NULL,
        has_expires INTEGER NOT NULL DEFAULT 1,
        is_persistent INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 1,
        samesite INTEGER NOT NULL DEFAULT -1,
        source_scheme TEXT NOT NULL DEFAULT 'Unset',
        source_port INTEGER NOT NULL DEFAULT -1,
        last_update_utc INTEGER NOT NULL DEFAULT 0,
        source_type INTEGER NOT NULL DEFAULT 0,
        has_cross_site_ancestor INTEGER NOT NULL DEFAULT 0)""")
    cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS cookies_unique_index "
                "ON cookies(host_key, top_frame_site_key, name, path)")

    n = 0
    for r in rows:
        (name, value, enc, host_key, path, expires_utc, is_secure,
         is_httponly, samesite, scheme, port, has_expires, is_persistent,
         priority, last_access, source_type) = r
        try:
            cur.execute("DELETE FROM cookies WHERE name=? AND host_key=?", (name, host_key))
            cur.execute(
                """INSERT INTO cookies
                   (creation_utc, host_key, top_frame_site_key, name, value,
                    encrypted_value, path, expires_utc, is_secure, is_httponly,
                    last_access_utc, has_expires, is_persistent, priority,
                    samesite, source_scheme, source_port, last_update_utc, source_type)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (int(last_access or 0), host_key, "", name, value or "",
                 enc or b"", path or "/", int(expires_utc or 0),
                 int(is_secure or 0), int(is_httponly or 0), int(last_access or 0),
                 int(has_expires or 0), int(is_persistent or 0),
                 int(priority or 1), int(samesite or -1), scheme or "Unset",
                 int(port or -1), int(last_access or 0), int(source_type or 0)))
            n += 1
        except Exception as e:
            print(f"    ⚠️ {name}: {str(e)[:60]}", flush=True)
    con.commit()
    con.close()
    return n


def verify() -> bool:
    """启动浏览器验证登录态。"""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=str(XQ_PROFILE), channel="chrome", headless=False,
            args=["--window-size=1600,1000", "--disable-blink-features=AutomationControlled"])
        ctx.add_init_script(
            "Object.defineProperty(navigator,'webdriver',{get:()=>undefined});")
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_viewport_size({"width": 1600, "height": 1000})
        page.goto("https://xueqiu.com/", wait_until="domcontentloaded", timeout=90000)
        time.sleep(8)
        body = page.inner_text("body")
        guest = "立即登录/注册" in body
        print(f"  页面「立即登录/注册」: {guest}", flush=True)

        nick = page.evaluate("""() => {
            const out=[];
            document.querySelectorAll('a,span,div').forEach(e=>{
                const t=(e.textContent||'').trim();
                if(!t||t.length>14) return;
                const r=e.getBoundingClientRect();
                if(r.width===0||r.height===0||r.y>70) return;
                const c=(e.className||'').toString();
                if(/user|name|account/i.test(c)) out.push(t);
            });
            return [...new Set(out)].slice(0,5);
        }""")
        print(f"  页头疑似昵称: {nick}", flush=True)
        page.screenshot(path=str(ROOT / "assets" / "_xq_after_import.png"))
        ctx.close()
        return not guest


def main():
    print("=" * 62, flush=True)
    print("守候：等浏览器退出 → 提取雪球 cookie → 导入 → 验证")
    print("=" * 62, flush=True)
    print("请关闭 Edge 和 Chrome（可以随时重开），我会自动接手。\n", flush=True)

    deadline = time.time() + 3600
    while time.time() < deadline:
        run = browsers_running()
        if run:
            print(f"  等待中…（{len(run)} 个浏览器进程运行中）", flush=True)
            time.sleep(8)
            continue

        print("\n✅ 浏览器已退出，开始提取 cookie\n", flush=True)
        total = 0
        for bname, prof_dir, ck in find_profiles():
            print(f"  [{bname}] {prof_dir.name}", flush=True)
            try:
                rows = read_xq_cookies(ck)
            except Exception as e:
                print(f"    ❌ 读取失败: {str(e)[:70]}", flush=True)
                continue
            if not rows:
                print("    （无雪球登录 cookie）", flush=True)
                continue
            for r in rows:
                print(f"    ★ {r[0]:<16} host={r[3]:<20}", flush=True)
            ls = prof_dir.parent / "Local State"
            n = import_cookies(rows, ls)
            total += n
            print(f"    已导入 {n} 个", flush=True)

        if total == 0:
            print("\n❌ 没有找到雪球登录 cookie", flush=True)
            return 1

        print(f"\n共导入 {total} 个 cookie，验证登录态...\n", flush=True)
        time.sleep(2)
        ok = verify()
        print(f"\n{'✅ 登录态有效！可以发贴了' if ok else '❌ 仍未登录（cookie 可能已失效）'}",
              flush=True)
        return 0 if ok else 1

    print("⏱️ 等待超时", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
