"""生成体积很小的合成 GGUF（只有头部元数据 + 占位张量），用于测试解析器/规划器。"""
from __future__ import annotations

import struct
from pathlib import Path


def _s(s: str) -> bytes:
    b = s.encode()
    return struct.pack("<Q", len(b)) + b


def _kv(key: str, val) -> bytes:
    out = _s(key)
    if isinstance(val, str):
        return out + struct.pack("<I", 8) + _s(val)
    if isinstance(val, bool):
        return out + struct.pack("<I", 7) + struct.pack("<?", val)
    if isinstance(val, int):
        return out + struct.pack("<I", 4) + struct.pack("<I", val)
    if isinstance(val, float):
        return out + struct.pack("<I", 6) + struct.pack("<f", val)
    raise TypeError(val)


def make(path: Path, *, arch="qwen3moe", name="Mock-MoE-A1B", layers=8, experts=16, used=2, ctx=16384, embd=256, heads=8, kv_heads=2,
         expert_bytes=1 << 20, dense_bytes=64 << 10, file_type=15) -> Path:
    kvs = {"general.architecture": arch, "general.name": name, "general.file_type": file_type, f"{arch}.block_count": layers,
           f"{arch}.context_length": ctx, f"{arch}.embedding_length": embd, f"{arch}.attention.head_count": heads,
           f"{arch}.attention.head_count_kv": kv_heads, f"{arch}.attention.key_length": 64, f"{arch}.attention.value_length": 64,
           "tokenizer.chat_template": "{{ messages }}"}
    if experts:
        kvs[f"{arch}.expert_count"] = experts
        kvs[f"{arch}.expert_used_count"] = used
    tensors = [("token_embd.weight", dense_bytes * 2)]
    for i in range(layers):
        tensors += [(f"blk.{i}.attn_q.weight", dense_bytes), (f"blk.{i}.ffn_gate_exps.weight", expert_bytes if experts else dense_bytes)]
    tensors.append(("output.weight", dense_bytes))
    head = b"GGUF" + struct.pack("<I", 3) + struct.pack("<QQ", len(tensors), len(kvs))
    head += b"".join(_kv(k, v) for k, v in kvs.items())
    off, infos = 0, b""
    for n, sz in tensors:
        infos += _s(n) + struct.pack("<I", 1) + struct.pack("<Q", sz) + struct.pack("<I", 0) + struct.pack("<Q", off)
        off += (sz + 31) // 32 * 32
    hdr = head + infos
    pad = (-len(hdr)) % 32
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "wb") as f:
        f.write(hdr + b"\0" * pad)
        f.truncate(len(hdr) + pad + off)
    return path


if __name__ == "__main__":
    import sys
    make(Path(sys.argv[1]))
