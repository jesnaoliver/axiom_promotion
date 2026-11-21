"""
登录态同步工具 —— 把浏览器里的登录态同步到脚本 profile。

背景：
  小红书的 www 主站与 creator 创作服务平台是两套独立登录体系。
  你在主站登录 ≠ 创作平台已登录（判据是 creator 域下的 web_session cookie）。

用法：
    python publish/sync_login.py           # 从已开的 CDP Chrome 同步
    python publish/sync_login.py --check   # 只检查
"""

import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
from publish.browser import CDP_HOST, CDP_PORT, is_cdp_available  # noqa: E402

ROOT = Path(__file__).parent.parent
PROFILE = ROOT / ".browser_profile"

CDP_PROFILE = ROOT / ".chrome_cdp"
DEFAULT_CHROME = Path(r"C:\Users\Administrator\AppData\Local\Google\Chrome\User Data")


def cookie_db_path(profile: Path) -> Path | None:
    for cand in [profile / "Default" / "Network" / "Cookies",
                 profile / "Default" / "Cookies"]:
        if cand.exists():
            return cand
    return None


def has_web_session(profile: Path) -> tuple[bool, int]:
    """
    检查 profile 是否已登录创作服务平台。

    创作平台的登录 cookie 不是 web_session（那是主站的），
    实际判据是下列任一：
      galaxy_creator_session_id / galaxy.creator.beaker.session.id
      x-user-id-creator.xiaohongshu.com / customerClientId
    """
    db = cookie_db_path(profile)
    if not db:
        return False, 0
    names = [
        "galaxy_creator_session_id",
        "galaxy.creator.beaker.session.id",
        "x-user-id-creator.xiaohongshu.com",
        "customerClientId",
    ]
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?immutable=1", uri=True)
        placeholders = ",".join("?" * len(names))
        n = con.execute(
            f"SELECT COUNT(*) FROM cookies WHERE name IN ({placeholders}) "
            "AND host_key LIKE '%xiaohongshu%'",
            names,
        ).fetchone()[0]
        con.close()
        return n > 0, n
    except Exception:
        return False, 0


def sync(target: Path, source: Path) -> dict:
    """把 source profile 的登录相关数据复制到 target。"""
    result = {"copied": [], "failed": []}

    # Local State 含 cookie 解密密钥，必须同步
    ls_src, ls_dst = source / "Local State", target / "Local State"
    if ls_src.exists():
        shutil.copy2(ls_src, ls_dst)
        result["copied"].append("Local State")

    s_def, t_def = source / "Default", target / "Default"
    (t_def / "Network").mkdir(parents=True, exist_ok=True)

    for item in ["Network/Cookies", "Network/Cookies-journal",
                 "Preferences", "Secure Preferences", "Login Data", "Web Data",
                 "Local Storage", "Session Storage", "IndexedDB"]:
        s, d = s_def / item, t_def / item
        if not s.exists():
            continue
        try:
            if s.is_dir():
                shutil.copytree(s, d, dirs_exist_ok=True)
            else:
                d.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(s, d)
            result["copied"].append(item)
        except Exception as e:
            result["failed"].append(f"{item}: {type(e).__name__}")

    return result


def main():
    check_only = "--check" in sys.argv

    print("=" * 64)
    print("小红书登录态检查")
    print("=" * 64)

    # 1) 脚本 profile 自带登录态？
    ok, n = has_web_session(PROFILE)
    if ok:
        print(f"✅ 脚本 profile 已登录（web_session × {n}）")
        return 0
    print("❌ 脚本 profile 未登录（缺 web_session）")

    # 2) CDP Chrome 里是否已登录？
    if is_cdp_available():
        try:
            r = sync(PROFILE, CDP_PROFILE)
            ok2, n2 = has_web_session(PROFILE)
            print(f"✅ 已从 CDP Chrome 同步 {len(r['copied'])} 项")
            if r["failed"]:
                print(f"⚠️  {len(r['failed'])} 项失败: {r['failed']}")
            if ok2:
                print(f"✅ 同步成功，登录态有效（web_session × {n2}）")
                print("\n下一步：python scheduler.py --date <YYYY-MM-DD>")
                return 0
            print("❌ 同步后仍未检测到 web_session —— "
                  "请确认你登录的是 creator.xiaohongshu.com 创作服务平台")
            return 1
        except Exception as e:
            print(f"⚠️  同步失败: {type(e).__name__}: {e}")
    else:
        print(f"⚠️  未检测到 CDP Chrome（端口 {CDP_PORT}）")

    # 3) 默认 Chrome 里是否有？
    if DEFAULT_CHROME.exists():
        ok3, n3 = has_web_session(DEFAULT_CHROME)
        if ok3:
            print(f"✅ 默认 Chrome 里有创作平台登录态（web_session × {n3}），正在同步…")
            r = sync(PROFILE, DEFAULT_CHROME)
            ok4, n4 = has_web_session(PROFILE)
            if ok4:
                print(f"✅ 同步成功（web_session × {n4}）")
                print("\n下一步：python scheduler.py --date <YYYY-MM-DD>")
                return 0
            print("❌ 同步后仍未检测到 web_session")
            return 1

    print()
    print("需要你在创作服务平台登录一次：")
    print("  1) 打开 https://creator.xiaohongshu.com/publish/publish")
    print("  2) 用「手机号 + 短信验证码」登录（主站登录不算，必须是创作平台）")
    print("  3) 登录后运行本脚本同步，或运行 python publish/login.py --check")
    return 1


if __name__ == "__main__":
    sys.exit(main())
