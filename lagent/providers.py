"""统一模型/Provider 适配层：本地 llama-server、OpenAI 兼容接口与 Anthropic Messages 接口共用统一流式与完成契约。"""
from __future__ import annotations

import inspect
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator

from . import inference, net

logger = logging.getLogger(__name__)

_LLAMA_ONLY_KEYS = ("id_slot", "cache_prompt", "top_k", "min_p", "reasoning_budget", "chat_template_kwargs", "grammar")
_THINK_STRIP_RE = re.compile(r"<(?:think|thought|thinking)>.*?</(?:think|thought|thinking)>", re.S | re.I)


@dataclass(frozen=True)
class ProviderTarget:
    kind: str       # local | openai | anthropic
    url: str = ""
    model: str = "local"
    api_key: str = ""


def strip_think_tags(text: str) -> str:
    """去除模型输出中残留的 <think>/<thought>/<thinking> 思维块，便于 JSON 与正文解析。"""
    s = _THINK_STRIP_RE.sub("", text or "").strip()
    if s.startswith("```"):
        lines = s.splitlines()
        if len(lines) >= 2 and lines[0].startswith("```") and lines[-1].strip() == "```":
            s = "\n".join(lines[1:-1]).strip()
    return s


def _normalize_endpoint(ep: str, prov: str) -> tuple[str, str]:
    clean = str(ep or "").strip().rstrip("/")
    if not clean:
        return "", "local"
    p = str(prov or "auto").lower()
    if p == "auto":
        p = "anthropic" if ("anthropic.com" in clean.lower() or clean.endswith("/messages")) else "openai"
    if p == "anthropic":
        if not clean.endswith("/messages"):
            clean = clean + ("/messages" if clean.endswith("/v1") else "/v1/messages")
        return clean, "anthropic"
    if not clean.endswith("/chat/completions"):
        clean = clean + ("/chat/completions" if clean.endswith("/v1") else "/v1/chat/completions")
    return clean, "openai"


def _openai_to_anthropic_body(model: str, body: dict[str, Any], *, stream: bool) -> dict[str, Any]:
    sys_parts: list[str] = []
    msgs: list[dict[str, Any]] = []
    for m in body.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content", "")
        if role == "system":
            txt = content if isinstance(content, str) else " ".join(
                p.get("text", "") for p in content if isinstance(p, dict) and p.get("type") == "text"
            )
            if txt:
                sys_parts.append(txt)
            continue
        if role == "tool":
            block = {
                "type": "tool_result",
                "tool_use_id": str(m.get("tool_call_id") or ""),
                "content": content if isinstance(content, str) else json.dumps(content, ensure_ascii=False),
            }
            if msgs and msgs[-1]["role"] == "user" and isinstance(msgs[-1]["content"], list):
                msgs[-1]["content"].append(block)
            else:
                msgs.append({"role": "user", "content": [block]})
            continue
        if role == "assistant":
            blocks: list[dict[str, Any]] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text" and part.get("text"):
                        blocks.append({"type": "text", "text": part["text"]})
            for tc in m.get("tool_calls") or []:
                fn = tc.get("function") or {}
                raw_args = fn.get("arguments", "{}")
                try:
                    parsed_args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
                except ValueError:
                    parsed_args = {}
                if not isinstance(parsed_args, dict):
                    parsed_args = {"raw": parsed_args}
                blocks.append({
                    "type": "tool_use",
                    "id": str(tc.get("id") or ""),
                    "name": str(fn.get("name") or ""),
                    "input": parsed_args,
                })
            msgs.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
            continue
        # user message (text or multimodal list)
        if isinstance(content, list):
            u_blocks: list[dict[str, Any]] = []
            for p in content:
                if not isinstance(p, dict):
                    continue
                if p.get("type") == "text":
                    u_blocks.append({"type": "text", "text": str(p.get("text") or "")})
                elif p.get("type") == "image_url":
                    url = str((p.get("image_url") or {}).get("url") or "")
                    if url.startswith("data:") and ";base64," in url:
                        hdr, b64 = url.split(";base64,", 1)
                        mime = hdr[5:] or "image/jpeg"
                        u_blocks.append({"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}})
            msgs.append({"role": "user", "content": u_blocks or [{"type": "text", "text": ""}]})
        else:
            msgs.append({"role": "user", "content": str(content or "")})

    out: dict[str, Any] = {
        "model": model,
        "messages": msgs or [{"role": "user", "content": ""}],
        "max_tokens": max(64, int(body.get("max_tokens") or 4096)),
        "stream": stream,
    }
    if "temperature" in body and body["temperature"] is not None:
        out["temperature"] = body["temperature"]
    if "top_p" in body and body["top_p"] is not None:
        out["top_p"] = body["top_p"]
    if sys_parts:
        out["system"] = "\n\n".join(sys_parts)
    tools_in = body.get("tools") or []
    if tools_in:
        ant_tools = []
        for t in tools_in:
            fn = (t.get("function") or {}) if t.get("type") == "function" else t
            ant_tools.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        out["tools"] = ant_tools
        if body.get("tool_choice") == "required":
            out["tool_choice"] = {"type": "any"}
        elif body.get("tool_choice") == "auto":
            out["tool_choice"] = {"type": "auto"}
    return out


def _anthropic_resp_to_openai(data: dict[str, Any]) -> dict[str, Any]:
    blocks = data.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for idx, b in enumerate(blocks):
        if not isinstance(b, dict):
            continue
        if b.get("type") == "text":
            text_parts.append(str(b.get("text") or ""))
        elif b.get("type") == "tool_use":
            tool_calls.append({
                "index": idx,
                "id": b.get("id") or f"call_{idx}",
                "type": "function",
                "function": {
                    "name": b.get("name", ""),
                    "arguments": json.dumps(b.get("input") or {}, ensure_ascii=False),
                },
            })
    usage_raw = data.get("usage") or {}
    pt = int(usage_raw.get("input_tokens") or 0)
    ct = int(usage_raw.get("output_tokens") or 0)
    stop_reason = data.get("stop_reason")
    finish = "tool_calls" if stop_reason == "tool_use" else ("length" if stop_reason == "max_tokens" else "stop")
    msg: dict[str, Any] = {"role": "assistant", "content": "".join(text_parts)}
    if tool_calls:
        msg["tool_calls"] = tool_calls
    return {
        "choices": [{"index": 0, "message": msg, "finish_reason": finish}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct, "total_tokens": pt + ct},
    }


async def _stream_engine_compat(engine: Any, body: dict[str, Any], *, priority: int = 0) -> AsyncIterator[dict]:
    """兼容真实 Engine.chat_stream(body, priority=...) 与测试里的单参数 fake_chat_stream(body)。"""
    fn = engine.chat_stream
    accepts_prio = False
    try:
        sig = inspect.signature(fn)
        accepts_prio = "priority" in sig.parameters or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
    except (TypeError, ValueError):
        accepts_prio = False
    agen = fn(body, priority=priority) if accepts_prio else fn(body)
    async for ch in agen:
        yield ch


class UnifiedProvider:
    """统一的多后端模型调用器，供 Agent 循环与 ModelRouter 共用。"""

    def __init__(self, cfg: Any, engine: Any):
        self.cfg = cfg
        self.engine = engine

    def resolve(self, role: str = "main") -> ProviderTarget:
        if role == "compact":
            ep = str(self.cfg.get("agent", "compact_endpoint", default="") or "")
            prov = str(self.cfg.get("agent", "compact_provider", default="auto") or "auto")
            model = str(self.cfg.get("agent", "compact_model", default="") or "")
            key = self.cfg.secret("compact_api_key") or "" if hasattr(self.cfg, "secret") else ""
            url, kind = _normalize_endpoint(ep, prov)
            if url:
                default_m = "claude-3-5-haiku-latest" if kind == "anthropic" else "gpt-4o-mini"
                return ProviderTarget(kind=kind, url=url, model=model or default_m, api_key=key)
            return ProviderTarget(kind="local")

        if role in ("worker", "coder", "reviewer", "verifier", "researcher", "summarizer", "reader", "planner"):
            ep = str(self.cfg.get("swarm", "worker_endpoint", default="") or "")
            if ep.strip():
                model = str(self.cfg.get("swarm", "worker_model", default="") or "")
                key = self.cfg.secret("swarm_worker_api_key") or "" if hasattr(self.cfg, "secret") else ""
                url, kind = _normalize_endpoint(ep, "auto")
                if url:
                    return ProviderTarget(kind=kind, url=url, model=model or "local-worker", api_key=key)

        main_prov = str(self.cfg.get("agent", "main_provider", default="auto") or "auto").lower()
        main_ep = str(self.cfg.get("agent", "main_endpoint", default="") or "")
        if main_prov != "local" and main_ep.strip():
            model = str(self.cfg.get("agent", "main_model", default="") or "")
            key = self.cfg.secret("main_api_key") or "" if hasattr(self.cfg, "secret") else ""
            url, kind = _normalize_endpoint(main_ep, main_prov)
            if url:
                default_m = "claude-3-7-sonnet-latest" if kind == "anthropic" else "gpt-4o"
                return ProviderTarget(kind=kind, url=url, model=model or default_m, api_key=key)

        return ProviderTarget(kind="local")

    @staticmethod
    def _prepare_body(
        target: ProviderTarget,
        body: dict[str, Any],
        *,
        stream: bool,
        schema: dict | None = None,
        id_slot: int | None = None,
    ) -> dict[str, Any]:
        req = dict(body)
        req["stream"] = stream
        if target.kind == "local":
            if id_slot is not None:
                req["id_slot"] = int(id_slot)
            if schema is not None and not req.get("grammar"):
                req["grammar"] = inference.schema_to_gbnf(schema)
            return req

        for k in _LLAMA_ONLY_KEYS:
            req.pop(k, None)
        req["model"] = target.model
        if target.kind == "openai" and schema is not None and "response_format" not in req:
            req["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_output", "schema": schema},
            }
        return req

    async def stream_chat(
        self,
        body: dict[str, Any],
        *,
        role: str = "main",
        priority: int = 0,
        id_slot: int | None = None,
        schema: dict | None = None,
    ) -> AsyncIterator[dict]:
        target = self.resolve(role)
        if target.kind == "local":
            eff_slot = id_slot
            if hasattr(self.engine, "slots_count") and self.engine.slots_count() <= 1:
                eff_slot = None
            req = self._prepare_body(target, body, stream=True, schema=schema, id_slot=eff_slot)
            async for ch in _stream_engine_compat(self.engine, req, priority=priority):
                yield ch
            return

        hdrs = {"Content-Type": "application/json", "Accept": "text/event-stream"}
        if target.kind == "openai":
            if target.api_key:
                hdrs["Authorization"] = f"Bearer {target.api_key}"
            req = self._prepare_body(target, body, stream=True, schema=schema)
            req.setdefault("stream_options", {"include_usage": True})
            async for ch in net.sse_post_url(target.url, req, headers=hdrs, allow_private=True):
                yield ch
            return

        # Anthropic SSE -> OpenAI Delta Stream 转换
        if target.api_key:
            hdrs["x-api-key"] = target.api_key
        hdrs["anthropic-version"] = "2023-06-01"
        ant_body = _openai_to_anthropic_body(target.model, body, stream=True)
        in_tokens = 0
        out_tokens = 0
        tool_idx_map: dict[int, int] = {}
        next_tc_idx = 0
        async for ev in net.sse_post_url(target.url, ant_body, headers=hdrs, allow_private=True):
            etype = ev.get("type") or ev.get("_sse_event") or ""
            if etype == "message_start":
                usage = (ev.get("message") or {}).get("usage") or {}
                in_tokens = int(usage.get("input_tokens") or 0)
            elif etype == "content_block_start":
                bidx = int(ev.get("index") or 0)
                blk = ev.get("content_block") or {}
                if blk.get("type") == "tool_use":
                    tc_i = next_tc_idx
                    next_tc_idx += 1
                    tool_idx_map[bidx] = tc_i
                    yield {
                        "choices": [{
                            "index": 0,
                            "delta": {
                                "tool_calls": [{
                                    "index": tc_i,
                                    "id": blk.get("id") or f"call_{tc_i}",
                                    "type": "function",
                                    "function": {"name": blk.get("name") or "", "arguments": ""},
                                }]
                            },
                            "finish_reason": None,
                        }]
                    }
            elif etype == "content_block_delta":
                bidx = int(ev.get("index") or 0)
                delta = ev.get("delta") or {}
                dtype = delta.get("type")
                if dtype == "text_delta" and delta.get("text"):
                    yield {"choices": [{"index": 0, "delta": {"content": delta["text"]}, "finish_reason": None}]}
                elif dtype == "thinking_delta" and delta.get("thinking"):
                    yield {"choices": [{"index": 0, "delta": {"reasoning_content": delta["thinking"]}, "finish_reason": None}]}
                elif dtype == "input_json_delta" and "partial_json" in delta:
                    tc_i = tool_idx_map.get(bidx, 0)
                    yield {
                        "choices": [{
                            "index": 0,
                            "delta": {"tool_calls": [{"index": tc_i, "function": {"arguments": delta["partial_json"]}}]},
                            "finish_reason": None,
                        }]
                    }
            elif etype == "message_delta":
                usage = ev.get("usage") or {}
                out_tokens = int(usage.get("output_tokens") or out_tokens)
                sr = (ev.get("delta") or {}).get("stop_reason")
                fin = "tool_calls" if sr == "tool_use" else ("length" if sr == "max_tokens" else "stop")
                yield {
                    "choices": [{"index": 0, "delta": {}, "finish_reason": fin}],
                    "usage": {"prompt_tokens": in_tokens, "completion_tokens": out_tokens, "total_tokens": in_tokens + out_tokens},
                }

    async def complete(
        self,
        body: dict[str, Any],
        *,
        role: str = "compact",
        timeout: float = 120.0,
        priority: int = 1,
        schema: dict | None = None,
        allow_fallback: bool = True,
    ) -> dict[str, Any]:
        target = self.resolve(role)
        if target.kind != "local":
            try:
                hdrs = {"Content-Type": "application/json", "Accept": "application/json"}
                if target.kind == "anthropic":
                    if target.api_key:
                        hdrs["x-api-key"] = target.api_key
                    hdrs["anthropic-version"] = "2023-06-01"
                    payload = _openai_to_anthropic_body(target.model, body, stream=False)
                else:
                    if target.api_key:
                        hdrs["Authorization"] = f"Bearer {target.api_key}"
                    payload = self._prepare_body(target, body, stream=False, schema=schema)
                resp = await net.fetch(
                    target.url,
                    method="POST",
                    headers=hdrs,
                    data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    timeout=min(float(timeout), 120.0),
                    max_bytes=2_000_000,
                    allow_private=True,
                )
                if resp.status == 200:
                    data = json.loads(resp.body.decode("utf-8", "replace"))
                    return _anthropic_resp_to_openai(data) if target.kind == "anthropic" else data
                logger.warning("外部模型端点 %s (%s) 返回 HTTP %s", role, target.url, resp.status)
            except Exception as e:  # noqa: BLE001
                logger.warning("外部模型端点 %s (%s) 调用失败: %s", role, target.url, e)
            if not allow_fallback:
                raise net.NetError(f"外部模型端点 {role} 不可用")

        local_body = self._prepare_body(ProviderTarget("local"), body, stream=False, schema=schema)
        try:
            return await self.engine.chat(local_body, timeout=timeout, priority=priority)
        except TypeError:
            return await self.engine.chat(local_body, timeout=timeout)
