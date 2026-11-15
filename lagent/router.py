"""多角色模型路由（ModelRouter）：支持主推理本地引擎，并将摘要压缩等辅助任务按配置分流到独立轻量端点，未配置或失败时自动回退本地引擎。"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import net

logger = logging.getLogger(__name__)


class ModelRouter:
    """按任务角色（main / compact）选择推理后端。"""

    def __init__(self, cfg: Any, engine: Any):
        self.cfg = cfg
        self.engine = engine

    def _compact_target(self) -> tuple[str, str, str]:
        ep = str(self.cfg.get("agent", "compact_endpoint", default="") or "").strip().rstrip("/")
        model = str(self.cfg.get("agent", "compact_model", default="") or "").strip()
        key = ""
        if hasattr(self.cfg, "secret"):
            key = self.cfg.secret("compact_api_key") or ""
        if not ep:
            return "", model, key
        if not ep.endswith("/chat/completions"):
            ep = ep + ("/chat/completions" if ep.endswith("/v1") else "/v1/chat/completions")
        return ep, model or "gpt-4o-mini", key

    async def complete(
        self,
        *,
        role: str,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 1200,
        timeout: float = 900.0,
        disable_thinking: bool = True,
    ) -> str:
        if role == "compact":
            url, model_name, api_key = self._compact_target()
            if url:
                try:
                    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
                    if api_key:
                        hdrs["Authorization"] = f"Bearer {api_key}"
                    payload = {
                        "model": model_name,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "stream": False,
                    }
                    resp = await net.fetch(
                        url,
                        method="POST",
                        headers=hdrs,
                        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        timeout=min(float(timeout), 90.0),
                        max_bytes=2_000_000,
                        allow_private=True,
                    )
                    if resp.status == 200:
                        data = json.loads(resp.body.decode("utf-8", "replace"))
                        txt = ((data.get("choices") or [{}])[0].get("message", {}).get("content", "") or "").strip()
                        if txt:
                            return txt
                except Exception as e:  # noqa: BLE001
                    logger.warning("外部压缩模型端点调用失败，已回退本地引擎: %s", e)

        body: dict[str, Any] = {
            "model": "local",
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if disable_thinking:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        r = await self.engine.chat(body, timeout=timeout)
        return (r.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""
