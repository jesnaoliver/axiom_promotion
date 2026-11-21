"""
AXIOM 数据源 —— 从 efficientinvest.cn 拉取四个模块的原始数据。

站点结构（2026-11-02 实测）：
  API 基址 : https://www.efficientinvest.cn/api_group/api
  登录     : POST /v1/auth/email/login  {email, password}  → token
  仪表盘   : GET  /v1/daily-summary/latest?frequency=daily   (每日概览)
             GET  /v1/daily-summary/latest?frequency=weekly
  智能体   : GET  /v1/ai-agent/sessions?agentType=vibe-trading
             POST /v1/ai-agent/chat  (问答)
  情报精选 : GET  /v1/intelligence?page=1&pageSize=10&sortBy=postDate&sortOrder=DESC
  机构研报 : GET  /v1/research-analysis?page=1&pageSize=10&sortBy=createdAt&sortOrder=DESC
  历史日报 : GET  /v1/daily-summary?page=0&pageSize=10

凭证从 .env 读取（axiom_user / axiom_passwd）。

【合规提醒】本模块只负责"取原始数据"。
脱敏与合规改写由 content/sanitize.py 负责 —— 因为这两件事必须分离，
否则很容易在取数环节就把不该出现的内容带进正文。
"""

import json
import os
import sys
import time
from pathlib import Path

import urllib.error
import urllib.request

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from config.env_loader import load_env  # noqa: E402

BASE = "https://www.efficientinvest.cn/api_group/api"


def _load_env() -> dict:
    """读取凭证（统一走 config.env_loader，支持本地 .env 与资产库缓存）。"""
    return load_env()


class AxiomClient:
    """AXIOM API 客户端。"""

    def __init__(self, email: str | None = None, password: str | None = None):
        env = _load_env()
        self.email = email or env.get("axiom_user", "")
        self.password = password or env.get("axiom_passwd", "")
        self.token: str | None = None
        self._refresh_token: str | None = None
        self._token_expires = 0
        self.user: dict = {}

    # ---------- 底层 ----------
    def _headers(self, extra: dict | None = None) -> dict:
        h = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://www.efficientinvest.cn",
            "Referer": "https://www.efficientinvest.cn/",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/156.0.0.0 Safari/537.36",
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        if extra:
            h.update(extra)
        return h

    def _request(self, method: str, path: str, payload: dict | None = None,
                 retries: int = 2) -> dict | list:
        url = BASE + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last_err = None
        for attempt in range(retries + 1):
            try:
                req = urllib.request.Request(url, data=data, headers=self._headers(),
                                             method=method)
                with urllib.request.urlopen(req, timeout=30) as r:
                    body = r.read().decode("utf-8", errors="replace")
                    if not body.strip():
                        return {}
                    return json.loads(body)
            except urllib.error.HTTPError as e:
                last_err = f"HTTP {e.code}"
                if e.code == 401 and attempt < retries:
                    self.login()
                    continue
                try:
                    return json.loads(e.read().decode("utf-8", errors="replace"))
                except Exception:
                    return {"error": last_err}
            except Exception as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(1.5 * (attempt + 1))
        return {"error": last_err}

    # ---------- 认证 ----------
    def login(self) -> bool:
        if not self.email or not self.password:
            return False
        res = self._request("POST", "/v1/auth/email/login",
                             {"email": self.email, "password": self.password},
                             retries=0)
        data = (res or {}).get("data") if isinstance(res, dict) else None
        if not data:
            return False
        self.token = data.get("token")
        self._refresh_token = data.get("refreshToken")
        self._token_expires = (data.get("tokenExpires") or 0) / 1000
        self.user = data.get("user", {})
        return True

    def ensure_login(self) -> bool:
        if self.token and time.time() < self._token_expires - 60:
            return True
        return self.login()

    def whoami(self) -> dict:
        self.ensure_login()
        res = self._request("GET", "/v1/auth/me")
        return res.get("data", res) if isinstance(res, dict) else {}

    # ---------- 仪表盘 ----------
    def daily_dashboard(self) -> dict:
        """每日概览（含宏观速览、行业聚焦、重点公司观点、风险信号）。"""
        self.ensure_login()
        res = self._request("GET", "/v1/daily-summary/latest?frequency=daily")
        return res.get("data", res) if isinstance(res, dict) else {}

    def weekly_dashboard(self) -> dict:
        self.ensure_login()
        res = self._request("GET", "/v1/daily-summary/latest?frequency=weekly")
        return res.get("data", res) if isinstance(res, dict) else {}

    def daily_history(self, page: int = 0, page_size: int = 10) -> list:
        self.ensure_login()
        res = self._request("GET", f"/v1/daily-summary?page={page}&pageSize={page_size}")
        if isinstance(res, dict):
            rows = res.get("data") or res.get("items") or res.get("list") or []
            return rows if isinstance(rows, list) else []
        return res if isinstance(res, list) else []

    # ---------- 情报精选（知识星球）----------
    def intelligence(self, page: int = 1, page_size: int = 10) -> dict:
        """情报精选：知识星球高价值内容。"""
        self.ensure_login()
        res = self._request(
            "GET",
            f"/v1/intelligence?page={page}&pageSize={page_size}"
            f"&sortBy=postDate&sortOrder=DESC")
        return res.get("data", res) if isinstance(res, dict) else {}

    # ---------- 机构研报 ----------
    def research_analysis(self, page: int = 1, page_size: int = 10) -> dict:
        """机构研报：研究报告列表。"""
        self.ensure_login()
        res = self._request(
            "GET",
            f"/v1/research-analysis?page={page}&pageSize={page_size}"
            f"&sortBy=createdAt&sortOrder=DESC")
        return res.get("data", res) if isinstance(res, dict) else {}

    # ---------- 智能体 ----------
    def agent_sessions(self, page: int = 1, page_size: int = 20) -> list:
        self.ensure_login()
        res = self._request(
            "GET",
            f"/v1/ai-agent/sessions?agentType=vibe-trading&page={page}&pageSize={page_size}")
        if isinstance(res, dict):
            rows = res.get("data") or res.get("items") or res.get("list") or []
            return rows if isinstance(rows, list) else []
        return res if isinstance(res, list) else []

    def agent_chat(self, question: str, session_id: str | None = None) -> dict:
        """
        向智能体提问。

        注意：智能体的回答可能包含具体个股买卖建议 ——
        小红书端绝不可直接引用，只能提取其"方法论/分析框架"部分。
        """
        self.ensure_login()
        payload = {"question": question, "agentType": "vibe-trading"}
        if session_id:
            payload["sessionId"] = session_id
        res = self._request("POST", "/v1/ai-agent/chat", payload)
        return res.get("data", res) if isinstance(res, dict) else {}

    # ---------- 一次性抓取 ----------
    def collect_all(self) -> dict:
        """抓取四个模块的原始数据，供内容生成使用。"""
        self.ensure_login()
        return {
            "fetched_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "user": self.whoami(),
            "dashboard_daily": self.daily_dashboard(),
            "dashboard_weekly": self.weekly_dashboard(),
            "intelligence": self.intelligence(),
            "research": self.research_analysis(),
            "daily_history": self.daily_history(),
        }


if __name__ == "__main__":
    c = AxiomClient()
    if not c.ensure_login():
        print("登录失败，检查 .env 凭证")
        raise SystemExit(1)
    print("登录成功 ✅")
    u = c.whoami()
    print(f"  用户: {u.get('email')} | 角色: {(u.get('role') or {}).get('name')}"
          f" | 等级: {u.get('tier')}")
    print("\n=== 每日概览 ===")
    d = c.daily_dashboard()
    for k, v in list(d.items())[:6]:
        txt = str(v)[:150]
        print(f"  [{k}] {txt}")
    print("\n=== 情报精选 ===")
    intel = c.intelligence()
    rows = intel.get("items") or intel.get("list") or intel.get("data") or []
    if isinstance(rows, dict):
        rows = rows.get("items", [])
    print(f"  共 {len(rows)} 条")
    for r in rows[:4]:
        print(f"  - {str(r.get('title'))[:60]}")
    print("\n=== 机构研报 ===")
    ra = c.research_analysis()
    rows2 = ra.get("items") or ra.get("list") or ra.get("data") or []
    if isinstance(rows2, dict):
        rows2 = rows2.get("items", [])
    print(f"  共 {len(rows2)} 条")
    for r in rows2[:4]:
        print(f"  - {str(r.get('documentName') or r.get('title'))[:60]}")
