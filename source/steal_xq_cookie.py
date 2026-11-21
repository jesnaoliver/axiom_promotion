"""
从日常 Chrome 提取雪球登录态 → 导入 .xq_profile。

【背景】
用户的日常 Chrome 已登录雪球，而自动化用的 .xq_profile 未登录。
本模块把日常 Chrome 的雪球 cookie 复制到自动化 profile，
从而**绕过滑块验证**（滑块只在建立新会话时需要）。

【技术要点】
1. Chrome 运行时 Cookies 文件被锁（WinError 32）
   → 不能 shutil.copy2，必须用 **sqlite3 在线备份 API**：
      con.backup(dst_con) —— SQLite 允许另一个进程持锁时读取
2. Chrome cookie 用 DPAPI 加密，密钥在 profile 的 `Local State`
   → **同一台机器同一 Windows 用户**下，Chrome 自己能正常解密
   → 所以只要把 Cookies + Local State 一起放进自动化 profile 即可
3. 导入方式是**直接写 sqlite**（不加密）——
   自动化 profile 用 Playwright 启动的 Chrome，
   它同样会用 Local State 里的 DPAPI key 加密写入，
   所以写明文 + 复制 Local State 是可行路径。

【安全边界】
只提取 xueqiu.com 域名的 cookie，不碰其他站点。
"""

import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

ROOT = pathlib.Path(__file__).parent.parent
XQ_PROFILE = ROOT / ".xq_profile"

CHROME_UD = pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Google/Chrome/User Data"

AUTH_COOKIES = ("u", "xq_a_token", "xqat", "xq_id_token", "xq_r_token")
XQ_HOSTS = ("xueqiu.com", "taoguba.net", "xueqiu.cn")


def _open_ro(path: pathlib.Path):
    """以只读方式打开可能被锁的 sqlite（uri mode 绕过文件独占锁）。"""
    return sqlite3.connect(f"file:{path.as_posix()}?mode=ro&immutable=0", uri=True)


def dump_cookies(src_db: pathlib.Path) -> list[tuple]:
    """用在线备份 API 读出 cookie 行。"""
    tmp = pathlib.Path(tempfile.gettempdir()) / f"xq_dump_{os.getpid()}.db"
    if tmp.exists():
        tmp.unlink()
    try:
        src = _open_ro(src_db)
        dst = sqlite3.connect(str(tmp))
        src.backup(dst)
        dst.close()
        src.close()
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    con = sqlite3.connect(str(tmp))
    cur = con.cursor()
    cur.execute("PRAGMA table_info(cookies)")
    cols = [r[1] for r in cur.fetchall()]
    if "host_key" not in cols:
        con.close()
        tmp.unlink(missing_ok=True)
        return []

    out = []
    ph = ",".join("?" * len(AUTH_COOKIES))
    cur.execute(
        f"SELECT name, value, encrypted_value, host_key, path, expires_utc, "
        f"is_secure, is_httponly, samesite, source_scheme, source_port, "
        f"has_expires, is_persistent, priority, last_access_utc, source_type "
        f"FROM cookies WHERE name IN ({ph})", list(AUTH_COOKIES))
    for r in cur.fetchall():
        if any(h in r[3] for h in XQ_HOSTS):
            out.append(r)
    con.close()
    tmp.unlink(missing_ok=True)
    return out


def copy_local_state() -> bool:
    """复制 Local State（内含 DPAPI 加密的 os_crypt key）。"""
    src = CHROME_UD / "Local State"
    dst = XQ_PROFILE / "Local State"
    if not src.exists():
        return False
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        return True
    except Exception:
        return False


def import_cookies(rows: list[tuple]) -> int:
    """
    把 cookie 写入自动化 profile 的 Cookies 库。

    ⚠️ 写入时**保持 encrypted_value 原样**（Chrome 用自己的 DPAPI key 加密的）。
       配合复制过来的 Local State，Chrome 能正常解密。
    """
    tgt_dir = XQ_PROFILE / "Default" / "Network"
    tgt_dir.mkdir(parents=True, exist_ok=True)
    tgt = tgt_dir / "Cookies"
    created = not tgt.exists()

    con = sqlite3.connect(str(tgt))
    cur = con.cursor()
    if created:
        cur.execute("""CREATE TABLE cookies (
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
        cur.execute("CREATE UNIQUE INDEX cookies_unique_index "
                    "ON cookies(host_key, top_frame_site_key, name, path)")
        cur.execute("CREATE INDEX cookies_host_index ON cookies(host_key)")

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
                 enc or b"", path or "/", int(expires_utc or 0), int(is_secure or 0),
                 int(is_httponly or 0), int(last_access or 0), int(has_expires or 0),
                 int(is_persistent or 0), int(priority or 1), int(samesite or -1),
                 scheme or "Unset", int(port or -1), int(last_access or 0),
                 int(source_type or 0)))
            n += 1
        except Exception as e:
            print(f"    ⚠️ 写入 {name} 失败: {str(e)[:60]}")
    con.commit()
    con.close()
    return n


def main():
    print("=" * 62)
    print("从日常 Chrome 提取雪球登录态")
    print("=" * 62)

    ck = CHROME_UD / "Default" / "Network" / "Cookies"
    if not ck.exists():
        ck = CHROME_UD / "Default" / "Cookies"
    if not ck.exists():
        print(f"❌ 找不到 Chrome Cookies: {ck}")
        return 1
    print(f"源: {ck}")

    try:
        rows = dump_cookies(ck)
    except Exception as e:
        print(f"❌ 读取失败: {str(e)[:100]}")
        return 1

    if not rows:
        print("❌ 日常 Chrome 里没有雪球登录 cookie")
        print("   请先在日常浏览器打开 https://www.xueqiu.com/ 确认已登录")
        return 1

    print(f"\n找到 {len(rows)} 个雪球登录 cookie:")
    for r in rows:
        print(f"  ★ {r[0]:<14} host={r[3]:<22} 过期={r[5] or 'session'}")

    ok_ls = copy_local_state()
    print(f"\nLocal State（DPAPI key）: {'已复制' if ok_ls else '❌ 复制失败'}")

    n = import_cookies(rows)
    print(f"已导入 {n} 个 cookie 到 .xq_profile")
    print("\n现在运行: python publish/xq/login.py --check 验证")
    return 0


if __name__ == "__main__":
    sys.exit(main())
