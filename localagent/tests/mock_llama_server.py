#!/usr/bin/env python3
"""测试用的假 llama-server：实现 --help/--version、/health、/props、流式 /v1/chat/completions（含工具调用/思考/用量）。
行为由用户消息里的关键词决定，便于在没有真实模型的环境里端到端验证 LocalAgent。"""
import json
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HELP = """----- common params -----
-c,    --ctx-size N
-ngl,  --gpu-layers, --n-gpu-layers N
-fa,   --flash-attn [on|off|auto]
-ctk,  --cache-type-k TYPE
-ctv,  --cache-type-v TYPE
-t,    --threads N
-b,    --batch-size N
-ub,   --ubatch-size N
-cmoe, --cpu-moe
-ncmoe, --n-cpu-moe N
-ot,   --override-tensor <tensor name pattern>=<buffer type>,...
-fit,  --fit [on|off]
-fitt, --fit-target MiB0,MiB1,...
-mm,   --mmproj FILE
--no-mmproj-offload
--image-max-tokens N
--jinja
--reasoning-format FORMAT
--no-webui
--no-context-shift
-np,   --parallel N
--api-key KEY
-cram, --cache-ram N
"""


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


if "--help" in sys.argv or "-h" in sys.argv:
    print(HELP)
    sys.exit(0)
if "--version" in sys.argv:
    print("version: 6123 (deadbee)\nbuilt with mock", file=sys.stderr)
    sys.exit(0)

PORT = int(arg("--port", "8080"))
KEY = arg("--api-key", "")
NCTX = int(arg("-c", "4096"))
MMPROJ = arg("--mmproj")
LOG = os.environ.get("MOCK_LOG")
LOAD = float(os.environ.get("MOCK_LOAD_SECONDS", "0.8"))
ready = False


def log_req(obj):
    if LOG:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def text_of(m):
    c = m.get("content")
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if p.get("type") == "text")
    return c or ""


def count_tokens(messages):
    n = 0
    for m in messages:
        t = text_of(m)
        cjk = sum(1 for ch in t if ord(ch) > 255)
        n += cjk * 0.85 + (len(t) - cjk) / 3.4 + 6
        for tc in m.get("tool_calls") or []:
            n += len(tc["function"].get("arguments", "")) / 3 + 8
    return int(n)


def script(messages):
    """返回 ('text', 内容, 思考) 或 ('tool', 名称, 参数)。"""
    last = messages[-1]
    users = [m for m in messages if m["role"] == "user"]
    u = text_of(users[-1]) if users else ""
    # 只看最近一条用户消息之后的 assistant/tool 数量
    idx = max(i for i, m in enumerate(messages) if m["role"] == "user")
    after = messages[idx + 1:]
    ntool = sum(1 for m in after if m["role"] == "tool")
    if last["role"] == "tool":
        tc = text_of(last)
        if "multi" in u and ntool == 1:
            return ("tool", "read_file", {"path": "notes/hello.txt"})
        return ("text", f"完成。工具返回: {tc[:80]}", None)
    if "创建文件" in u:
        return ("tool", "write_file", {"path": "notes/hello.txt", "content": "你好，世界\n第二行\n"})
    if "编辑文件" in u:
        return ("tool", "edit_file", {"path": "notes/hello.txt", "old_string": "第二行", "new_string": "改过的第二行"})
    if "并行读取" in u:      # 一步里同时发出两个只读调用
        return ("tools", [("read_file", {"path": "notes/hello.txt"}), ("list_dir", {"path": "."})], None)
    if "读取上传" in u:
        return ("tool", "read_file", {"path": "uploads/doc.txt"})
    if "读取" in u:
        return ("tool", "read_file", {"path": "notes/hello.txt"})
    if "慢速" in u:
        return ("slow", "慢" * 600, None)
    if "删除" in u:
        return ("tool", "delete_path", {"path": "notes/hello.txt"})
    if "命令" in u:
        return ("tool", "run_command", {"command": "echo mock-ok > mock_out.txt && cat mock_out.txt"})
    if "危险" in u:
        return ("tool", "run_command", {"command": "rm -rf /"})
    if "越权" in u:
        return ("tool", "write_file", {"path": "../escape.txt", "content": "x"})
    if "搜索" in u:
        return ("tool", "web_search", {"query": "llama.cpp moe"})
    if "记住" in u:
        return ("tool", "memory_save", {"text": "用户喜欢简洁的回答", "kind": "preference"})
    if "多步" in u:
        return ("tool", "todo_write", {"items": [{"text": "a", "status": "in_progress"}, {"text": "b", "status": "pending"}]})
    if "思考" in u:
        return ("text", "这是思考之后的回答。", "让我想想……先分析问题，再给出答案。")
    if "长文本" in u:
        return ("text", "段落。" * 400, None)
    if "文本调用" in u:
        return ("text", '<tool_call>{"name": "list_dir", "arguments": {"path": "."}}</tool_call>', None)
    if "摘要" in os.environ.get("MOCK_NONE", "") or "交接备忘" in text_of(messages[0]):
        return ("text", "- 目标：测试\n- 已完成：若干步骤\n- 下一步：继续", None)
    return ("text", "收到: " + u[:60], None)


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _auth(self):
        return not KEY or self.headers.get("Authorization") == f"Bearer {KEY}"

    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        if self.path.startswith("/health"):
            return self._json(200, {"status": "ok"}) if ready else self._json(503, {"error": {"message": "Loading model"}})
        if not self._auth():
            return self._json(401, {"error": {"message": "bad key"}})
        if self.path.startswith("/props"):
            return self._json(200, {"default_generation_settings": {"n_ctx": NCTX}, "modalities": {"vision": bool(MMPROJ), "audio": False},
                                    "model_path": arg("-m")})
        self._json(404, {})

    def do_POST(self):
        if not self._auth():
            return self._json(401, {"error": {"message": "bad key"}})
        n = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self.path.startswith("/v1/chat/completions"):
            return self._json(404, {})
        msgs = body["messages"]
        ptoks = count_tokens(msgs) + len(json.dumps(body.get("tools", []))) // 4
        log_req({"n_msgs": len(msgs), "ptoks": ptoks, "has_tools": bool(body.get("tools")), "kwargs": body.get("chat_template_kwargs"),
                 "stream": body.get("stream"), "roles": [m["role"] for m in msgs], "system": text_of(msgs[0])[:4000],
                 "last": text_of(msgs[-1])[:300], "tool_names": [t["function"]["name"] for t in body.get("tools", [])],
                 "images": sum(1 for m in msgs if isinstance(m.get("content"), list) for p in m["content"] if p.get("type") == "image_url")})
        if ptoks + 16 > NCTX:
            return self._json(400, {"error": {"message": f"the request exceeds the available context size ({ptoks} > {NCTX})", "type": "exceed_context_size_error", "n_prompt_tokens": ptoks, "n_ctx": NCTX}})
        kind, a, b = script(msgs)
        if not body.get("stream"):
            return self._json(200, {"choices": [{"message": {"role": "assistant", "content": a if kind == "text" else ""}, "finish_reason": "stop"}],
                                    "usage": {"prompt_tokens": ptoks, "completion_tokens": 20}})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        def send(obj):
            data = ("data: " + json.dumps(obj, ensure_ascii=False) + "\n\n").encode()
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
            self.wfile.flush()

        def chunk(delta, fin=None, **extra):
            send({"choices": [{"index": 0, "delta": delta, "finish_reason": fin}], **extra})

        try:
            chunk({"role": "assistant", "content": ""})
            comp = 0
            if kind == "slow":
                for i in range(0, len(a), 5):
                    chunk({"content": a[i:i + 5]}); time.sleep(0.05)
                fin = "stop"
            elif kind == "text":
                if b:
                    for i in range(0, len(b), 6):
                        chunk({"reasoning_content": b[i:i + 6]}); time.sleep(0.005)
                for i in range(0, len(a), 5):
                    chunk({"content": a[i:i + 5]}); comp += 2; time.sleep(0.002)
                fin = "stop"
            elif kind == "tools":
                for i, (nm, ar) in enumerate(a):
                    chunk({"tool_calls": [{"index": i, "id": f"call_mock{i + 1}", "type": "function",
                                           "function": {"name": nm, "arguments": json.dumps(ar, ensure_ascii=False)}}]})
                comp, fin = 40, "tool_calls"
            else:
                args = json.dumps(b, ensure_ascii=False)
                half = len(args) // 2
                chunk({"tool_calls": [{"index": 0, "id": "call_mock1", "type": "function", "function": {"name": a, "arguments": args[:half]}}]})
                chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[half:]}}]})
                comp, fin = 30, "tool_calls"
            chunk({}, fin, usage={"prompt_tokens": ptoks, "completion_tokens": comp}, timings={"predicted_per_second": 42.5, "prompt_per_second": 900.0})
            done = b"data: [DONE]\n\n"
            self.wfile.write(f"{len(done):x}\r\n".encode() + done + b"\r\n")
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True


def main():
    global ready
    for line in ("llama_model_loader: loaded meta data (mock)", f"load_tensors: offloaded 0/0 layers to GPU (ctx={NCTX})"):
        print(line, file=sys.stderr, flush=True)
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    time.sleep(LOAD)
    ready = True
    print("main: server is listening on http://127.0.0.1:%d" % PORT, file=sys.stderr, flush=True)
    while True:
        time.sleep(1)


if __name__ == "__main__":
    main()
