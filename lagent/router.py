"""多角色模型路由（ModelRouter）：支持主推理本地引擎，并将摘要压缩等辅助任务按配置分流到独立轻量端点，未配置或失败时自动回退本地引擎。"""
from __future__ import annotations

import json
import logging
from typing import Any

from . import net

logger = logging.getLogger(__name__)


class ModelRouter:
    """按任务角色（main / compact / embedding）选择推理后端，支持 OpenAI / Anthropic 兼容端点。"""

    def __init__(self, cfg: Any, engine: Any):
        self.cfg = cfg
        self.engine = engine

    def _compact_target(self) -> tuple[str, str, str, str]:
        ep = str(self.cfg.get("agent", "compact_endpoint", default="") or "").strip().rstrip("/")
        model = str(self.cfg.get("agent", "compact_model", default="") or "").strip()
        prov = str(self.cfg.get("agent", "compact_provider", default="auto") or "auto").lower()
        key = self.cfg.secret("compact_api_key") or "" if hasattr(self.cfg, "secret") else ""
        if not ep:
            return "", model, key, "local"
        if prov == "auto":
            prov = "anthropic" if ("anthropic.com" in ep.lower() or ep.endswith("/messages")) else "openai"
        if prov == "anthropic":
            if not ep.endswith("/messages"):
                ep = ep + ("/messages" if ep.endswith("/v1") else "/v1/messages")
            return ep, model or "claude-3-5-haiku-latest", key, "anthropic"
        if not ep.endswith("/chat/completions"):
            ep = ep + ("/chat/completions" if ep.endswith("/v1") else "/v1/chat/completions")
        return ep, model or "gpt-4o-mini", key, "openai"

    def _embed_target(self) -> tuple[str, str, str]:
        ep = str(self.cfg.get("agent", "embed_endpoint", default="") or "").strip().rstrip("/")
        model = str(self.cfg.get("agent", "embed_model", default="") or "").strip()
        key = self.cfg.secret("embed_api_key") or "" if hasattr(self.cfg, "secret") else ""
        if not ep:
            return "", model, key
        if not ep.endswith("/embeddings"):
            ep = ep + ("/embeddings" if ep.endswith("/v1") else "/v1/embeddings")
        return ep, model or "text-embedding-3-small", key

    @staticmethod
    def _to_anthropic_payload(model: str, messages: list[dict], temperature: float, max_tokens: int) -> dict:
        sys_parts: list[str] = []
        msgs: list[dict] = []
        for m in messages:
            role = m.get("role", "user")
            c = m.get("content", "")
            text = c if isinstance(c, str) else " ".join(p.get("text", "") for p in c if isinstance(p, dict))
            if role == "system":
                if text:
                    sys_parts.append(text)
            elif role in ("user", "assistant"):
                msgs.append({"role": role, "content": text})
        payload: dict[str, Any] = {
            "model": model,
            "messages": msgs or [{"role": "user", "content": ""}],
            "max_tokens": max(64, int(max_tokens)),
            "temperature": temperature,
        }
        if sys_parts:
            payload["system"] = "\n\n".join(sys_parts)
        return payload

    async def complete(
        self,
        *,
        role: str,
        messages: list[dict],
        temperature: float = 0.2,
        max_tokens: int = 1200,
        timeout: float = 900.0,
        disable_thinking: bool = True,
        response_format: dict | None = None,
    ) -> str:
        if role == "compact":
            url, model_name, api_key, prov = self._compact_target()
            if url:
                try:
                    hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
                    if prov == "anthropic":
                        if api_key:
                            hdrs["x-api-key"] = api_key
                        hdrs["anthropic-version"] = "2023-06-01"
                        payload = self._to_anthropic_payload(model_name, messages, temperature, max_tokens)
                    else:
                        if api_key:
                            hdrs["Authorization"] = f"Bearer {api_key}"
                        payload = {
                            "model": model_name,
                            "messages": messages,
                            "temperature": temperature,
                            "max_tokens": max_tokens,
                            "stream": False,
                        }
                        if response_format:
                            payload["response_format"] = response_format
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
                        if prov == "anthropic":
                            blocks = data.get("content") or []
                            txt = "".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("type") == "text").strip()
                        else:
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
        if response_format:
            body["response_format"] = response_format
        try:
            r = await self.engine.chat(body, timeout=timeout, priority=1 if role != "main" else 0)
        except TypeError:
            r = await self.engine.chat(body, timeout=timeout)
        return (r.get("choices") or [{}])[0].get("message", {}).get("content", "") or ""

    async def embed(self, texts: list[str], *, timeout: float = 15.0) -> list[list[float]] | None:
        """为混合记忆检索生成密集向量；未配置外部端点且本地引擎不支持时返回 None。"""
        clean = [str(t or "")[:8000] for t in texts if str(t or "").strip()]
        if not clean:
            return []
        url, model_name, api_key = self._embed_target()
        if url:
            try:
                hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
                if api_key:
                    hdrs["Authorization"] = f"Bearer {api_key}"
                payload = {"model": model_name, "input": clean}
                resp = await net.fetch(
                    url,
                    method="POST",
                    headers=hdrs,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=float(timeout),
                    max_bytes=4_000_000,
                    allow_private=True,
                )
                if resp.status == 200:
                    data = json.loads(resp.body.decode("utf-8", "replace"))
                    items = sorted(data.get("data") or [], key=lambda x: int(x.get("index", 0)))
                    vecs = [list(map(float, it["embedding"])) for it in items if isinstance(it.get("embedding"), list)]
                    if len(vecs) == len(clean):
                        return vecs
            except Exception as e:  # noqa: BLE001
                logger.debug("外部 Embedding 端点调用失败: %s", e)
        emb_fn = getattr(self.engine, "embeddings", None)
        if callable(emb_fn) and getattr(self.engine, "status", "") == "ready":
            try:
                return await emb_fn(clean, timeout=timeout)
            except Exception:  # noqa: BLE001
                return None
        return None
