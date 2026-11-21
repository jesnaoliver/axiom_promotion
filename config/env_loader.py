# -*- coding: utf-8 -*-
"""
统一凭证加载器。

## 为什么需要这个模块

改造前，`.env` 的解析逻辑散落在 9 个文件里（`source/axiom_api.py`、
`publish/xq/login.py`、`publish/tgb/login.py` 等），每处各写一遍，
行为还不一致：有的会 `os.environ.setdefault`，有的不会；
有的剥引号，有的不剥。改一处漏一处，很容易出现
「雪球能读到密码、淘股吧读不到」这类诡异问题。

本模块把所有读取逻辑收敛到一处，并新增**资产库兜底**：
当本地没有 `.env` 时，自动从项目资产库拉取
`AXIOM多平台发布项目/凭证/env-credentials.txt`。

## 凭证优先级（从高到低）

1. **进程环境变量** —— 最高优先级，便于临时覆盖与 CI 注入
2. **本地 `.env`** —— 各人自配的文件
3. **资产库缓存** —— 从项目资产库下载的 `env-credentials.txt`
   （见 `scripts/sync_env.py`，由 WorkBuddy 通过 netdrive MCP 签发 URL 后同步）

三处都找不到时返回空 dict，**不抛异常**，保持与改造前一致的容错行为。

## 安全约束

- 本模块**永不打印**凭证值，只打印键名。
- `.env` 与资产库缓存文件均已在 `.gitignore` 中排除。
- 资产库文件仅在项目成员目录内可见，**严禁外传到仓库或聊天工具**。
"""

from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).parent.parent

#: 本地凭证文件（各人自配，已被 .gitignore 排除）
LOCAL_ENV = ROOT / ".env"

#: 从资产库同步下来的凭证缓存（已��� .gitignore 排除）
ASSET_CACHE = ROOT / ".env.asset"

#: 资产库中凭证文件的稳定标识（用于日志与文档说明）
ASSET_FILE_ID = "TXYPscTPAWKp"
ASSET_PATH_HINT = "AXIOM多平台发布项目/凭证/env-credentials.txt"

#: 所有已知凭证键名（仅键名，不含值）
KNOWN_KEYS = (
    "axiom_user",
    "axiom_passwd",
    "xq_user",
    "xq_passwd",
    "xgb_user",
    "xgb_passwd",
)


def _parse(text: str) -> dict:
    """解析 dotenv 文本为 dict。

    与改造前的行为保持一致：
    - 忽略空行与 `#` 开头的注释
    - 忽略不含 `=` 的行
    - 去掉值的首尾空白与成对引号
    """
    out: dict = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            out[key] = value
    return out


def _read_file(path: Path) -> dict:
    """读取单个 dotenv 文件，失败返回空 dict。"""
    try:
        if path.exists():
            return _parse(path.read_text(encoding="utf-8"))
    except OSError:
        pass
    return {}


def load_env(*, export: bool = True) -> dict:
    """加载凭证。

    Args:
        export: 是否把读取到的值写入 ``os.environ``（用 setdefault，
            **不覆盖**已存在的环境变量）。与改造前多数调用点行为一致。

    Returns:
        合并后的凭证字典。进程环境变量优先级最高。
    """
    merged: dict = {}

    # 1) 资产库缓存（最低优先级，先铺底）
    merged.update(_read_file(ASSET_CACHE))

    # 2) 本地 .env（覆盖资产库缓存）
    merged.update(_read_file(LOCAL_ENV))

    # 3) 进程环境变量（最高优先级）
    for key in KNOWN_KEYS:
        value = os.environ.get(key)
        if value:
            merged[key] = value

    if export:
        for key, value in merged.items():
            os.environ.setdefault(key, value)

    return merged


def get(key: str, default: str = "") -> str:
    """读取单个凭证值（便捷函数）。"""
    return load_env().get(key, default)


def describe() -> dict:
    """返回凭证**存在性**报告，不含任何明文值。

    用于排查「某个平台的凭证到底读到没有」，避免在日志里泄露密码。
    """
    env = load_env(export=False)
    return {
        "来源": {
            "本地.env": LOCAL_ENV.exists(),
            "资产库缓存": ASSET_CACHE.exists(),
            "环境变量": any(os.environ.get(k) for k in KNOWN_KEYS),
        },
        "键位": {k: bool(env.get(k)) for k in KNOWN_KEYS},
    }


def print_report() -> None:
    """打印凭证存在性报告（只显示有/无，不显示值）。"""
    info = describe()
    print("凭证来源：")
    for src, present in info["来源"].items():
        print(f"  {'✅' if present else '❌'} {src}")
    print("键位完整性：")
    for key, present in info["键位"].items():
        print(f"  {'✅' if present else '⚠️ '} {key}")


if __name__ == "__main__":
    print_report()
    print()
    print(f"资产库凭证位置：{ASSET_PATH_HINT}")
    print("缺凭证时运行：python scripts/sync_env.py")
