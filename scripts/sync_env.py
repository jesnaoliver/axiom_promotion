#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
从项目资产库同步凭证到本地。

## 为什么需要这个脚本

项目资产库里的 `AXIOM多平台发布项目/凭证/env-credentials.txt` 是团队共享的
凭证源。新同事 clone 仓库后，本地没有 `.env`，所有依赖凭证的脚本都会失败。

本脚本负责把资产库上的凭证拉到本地缓存 `.env.asset`，
供 `config/env_loader.py` 读取。

## 凭证来源与优先级

`config/env_loader.py` 的优先级为：
**进程环境变量 > 本地 .env > 资产库缓存(.env.asset)**

所以你也可以完全不用本脚本，直接放自己的 `.env`；本脚本只是给
「不想自己申请账号、想直接用团队凭证」的人用的便捷通道。

## 两种用法

### 用法一：直接粘贴下载链接（推荐，60 分钟内有效）

1. 在 WorkBuddy 里执行：
   「把资产库里的 AXIOM多平台发布项目/凭证/env-credentials.txt 下载链接给我」
   或让助手调用 netdrive 的 `file_download` 拿到签名 URL
2. 执行：

```bash
python scripts/sync_env.py --url "<上一步拿到的完整URL>"
```

### 用法二：从环境变量读取

```bash
export AXIOM_ENV_URL="<下载链接>"
python scripts/sync_env.py
```

### 用法三：已有本地 .env 时，仅校验

```bash
python scripts/sync_env.py --check
```

## 注意

- 下载链接**约 60 分钟失效**，过期后重新签发即可。
- 落地文件权限设为 600（POSIX）；Windows 下 ACL 继承所在目录。
- 落地后**不要**把内容贴进聊天工具或提交进 git。
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.env_loader import ASSET_CACHE, LOCAL_ENV, print_report  # noqa: E402

#: 凭证文件应有的键位（用于校验下载内容是否完整）
EXPECTED_KEYS = (
    "axiom_user",
    "axiom_passwd",
    "xq_user",
    "xq_passwd",
    "xgb_user",
    "xgb_passwd",
)


def _harden(path: Path) -> None:
    """尽量收紧文件权限（POSIX 有效；Windows 忽略）。"""
    try:
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def _validate(text: str) -> tuple[bool, list, str]:
    """校验下载内容是否像一份完整凭证。

    Returns:
        (是否有效, 缺失的键, 第一行错误信息)
    """
    lines = [ln.strip() for ln in text.splitlines()]
    if any(ln.startswith("<?xml") or "<Error>" in ln for ln in lines[:5]):
        return False, [], "下载到的不是凭证文件，而是错误响应（链接可能已过期）"

    found = {}
    for ln in lines:
        if not ln or ln.startswith("#") or "=" not in ln:
            continue
        key, _, value = ln.partition("=")
        found[key.strip()] = value.strip().strip('"').strip("'")

    missing = [k for k in EXPECTED_KEYS if not found.get(k)]
    if missing:
        return False, missing, f"缺少键位：{', '.join(missing)}"
    return True, [], ""


def download(url: str, dest: Path) -> tuple[bool, str]:
    """下载并落地凭证文件。"""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=60) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        return False, f"HTTP {exc.code} —— 链接可能已过期（签名约 60 分钟有效）"
    except Exception as exc:  # noqa: BLE001
        return False, f"下载失败：{exc}"

    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return False, "下载内容不是 UTF-8 文本，可能不是凭证文件"

    ok, missing, err = _validate(text)
    if not ok:
        return False, err

    dest.write_text(text, encoding="utf-8")
    _harden(dest)
    return True, f"已写入 {dest}（{len(raw)} 字节）"


def main() -> int:
    parser = argparse.ArgumentParser(description="从项目资产库同步凭证到本地")
    parser.add_argument("--url", help="资产库凭证文件的签名下载链接")
    parser.add_argument(
        "--check",
        action="store_true",
        help="不下载，仅检查本地凭证状态",
    )
    parser.add_argument(
        "--dest",
        default=str(ASSET_CACHE),
        help="落地路径，默认 .env.asset",
    )
    args = parser.parse_args()

    if args.check:
        print_report()
        return 0

    url = args.url or os.environ.get("AXIOM_ENV_URL", "")
    if not url:
        print("❌ 未提供下载链接")
        print()
        print("用法一：")
        print('  python scripts/sync_env.py --url "<资产库下载链接>"')
        print()
        print("用法二：")
        print('  export AXIOM_ENV_URL="<资产库下载链接>"')
        print("  python scripts/sync_env.py")
        print()
        print("提示：链接需在 WorkBuddy 中通过 netdrive 的 file_download 签发，约 60 分钟有效。")
        print()
        print_report()
        return 1

    dest = Path(args.dest)
    ok, msg = download(url, dest)
    if not ok:
        print(f"❌ {msg}")
        print()
        print_report()
        return 1

    print(f"✅ {msg}")
    print()
    print_report()
    if LOCAL_ENV.exists():
        print()
        print(f"ℹ️  本地 {LOCAL_ENV.name} 已存在，它优先级高于资产库缓存。")
        print("   如需强制使用资产库凭证，删除本地 .env 或调整 config/env_loader.py 的优先级。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
