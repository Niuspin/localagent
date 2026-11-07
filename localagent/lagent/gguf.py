"""GGUF 头部解析（只读元数据，不加载张量）+ models/ 目录扫描 + mmproj 自动配对。"""
from __future__ import annotations

import re
import struct
from pathlib import Path
from typing import Any

_FMT = {0: ("<B", 1), 1: ("<b", 1), 2: ("<H", 2), 3: ("<h", 2), 4: ("<I", 4), 5: ("<i", 4),
        6: ("<f", 4), 7: ("<?", 1), 10: ("<Q", 8), 11: ("<q", 8), 12: ("<d", 8)}

_FTYPE = {0: "F32", 1: "F16", 2: "Q4_0", 3: "Q4_1", 7: "Q8_0", 8: "Q5_0", 9: "Q5_1", 10: "Q2_K", 11: "Q3_K_S",
          12: "Q3_K_M", 13: "Q3_K_L", 14: "Q4_K_S", 15: "Q4_K_M", 16: "Q5_K_S", 17: "Q5_K_M", 18: "Q6_K",
          19: "IQ2_XXS", 20: "IQ2_XS", 21: "Q2_K_S", 22: "IQ3_XS", 23: "IQ3_XXS", 24: "IQ1_S", 25: "IQ4_NL",
          26: "IQ3_S", 27: "IQ3_M", 28: "IQ2_S", 29: "IQ2_M", 30: "IQ4_XS", 31: "IQ1_M", 32: "BF16",
          36: "TQ1_0", 37: "TQ2_0", 38: "MXFP4"}

_QUANT_RE = re.compile(r"(?<![A-Za-z0-9])((?:UD-)?(?:IQ\d(?:_[A-Z0-9]+)*|Q\d(?:_K)?(?:_[A-Z0-9]+)?|BF16|F16|F32|MXFP4))(?![A-Za-z0-9])", re.I)
_SHARD_RE = re.compile(r"^(.*)-(\d{5})-of-(\d{5})\.gguf$", re.I)


def read_meta(path: str | Path, max_array: int = 1024) -> dict[str, Any]:
    """读取 GGUF KV 元数据。大数组（分词表等）直接跳过。"""
    with open(path, "rb", buffering=1 << 20) as f:
        if f.read(4) != b"GGUF":
            raise ValueError("不是 GGUF 文件")
        ver = struct.unpack("<I", f.read(4))[0]
        if ver < 2:
            raise ValueError(f"不支持的 GGUF 版本 {ver}")
        n_tensors, n_kv = struct.unpack("<QQ", f.read(16))
        if n_kv > 100000:
            raise ValueError("GGUF 元数据异常")
        meta: dict[str, Any] = {"_version": ver, "_tensors": n_tensors}

        def rstr() -> str:
            n = struct.unpack("<Q", f.read(8))[0]
            if n > 64 << 20:
                raise ValueError("字符串过长")
            return f.read(n).decode("utf-8", "replace")

        def rval(t: int) -> Any:
            if t in _FMT:
                fmt, sz = _FMT[t]
                return struct.unpack(fmt, f.read(sz))[0]
            if t == 8:
                return rstr()
            if t == 9:
                et = struct.unpack("<I", f.read(4))[0]
                cnt = struct.unpack("<Q", f.read(8))[0]
                if et in _FMT:
                    fmt, sz = _FMT[et]
                    if cnt <= max_array:
                        return list(struct.unpack(f"<{cnt}{fmt[1]}", f.read(sz * cnt))) if cnt else []
                    f.seek(cnt * sz, 1)
                    return None
                if et == 8:
                    for _ in range(cnt):
                        n = struct.unpack("<Q", f.read(8))[0]
                        f.seek(n, 1)
                    return None
                raise ValueError("不支持嵌套数组")
            raise ValueError(f"未知值类型 {t}")

        for _ in range(n_kv):
            key = rstr()
            t = struct.unpack("<I", f.read(4))[0]
            meta[key] = rval(t)
    return meta


def kv_bytes_per_token(meta: dict, bytes_per_elem: float = 2.0) -> int:
    """估算每 token 的 KV 缓存字节数（f16 为 2 字节/元素）。混合注意力/MLA 架构做了近似处理。"""
    arch = meta.get("general.architecture", "")
    g = lambda k: meta.get(f"{arch}.{k}")  # noqa: E731
    n_layer = g("block_count") or 0
    if not n_layer:
        return 0
    if g("attention.kv_lora_rank"):  # MLA（DeepSeek 系）
        return int(n_layer * (g("attention.kv_lora_rank") + (g("rope.dimension_count") or 0)) * bytes_per_elem)
    hkv, hh, emb = g("attention.head_count_kv"), g("attention.head_count"), g("embedding_length")
    if isinstance(hkv, list):
        heads_sum = sum(h for h in hkv if h)
    else:
        h = hkv or (hh if not isinstance(hh, list) else max(hh or [0])) or 0
        interval = g("full_attention_interval")
        n_attn = n_layer // interval if interval else n_layer
        heads_sum = h * n_attn
    hh0 = hh if not isinstance(hh, list) else (max(hh) if hh else 0)
    head_dim = g("attention.key_length") or ((emb // hh0) if emb and hh0 else 128)
    vdim = g("attention.value_length") or head_dim
    return int(heads_sum * (head_dim + vdim) * bytes_per_elem)


def tensor_layout(path: str | Path) -> dict | None:
    """读取张量表，返回 {"expert_per_layer": {层号: 字节}, "dense": 非专家字节, "total": 字节}。
    仅对单文件 GGUF 精确；分片/解析失败返回 None（规划器退回经验比例）。"""
    path = Path(path)
    if _SHARD_RE.match(path.name):
        return None
    try:
        fsize = path.stat().st_size
        with open(path, "rb", buffering=1 << 20) as f:
            f.read(4)
            struct.unpack("<I", f.read(4))
            n_t, n_kv = struct.unpack("<QQ", f.read(16))
            align = 32

            def rstr() -> str:
                n = struct.unpack("<Q", f.read(8))[0]
                return f.read(n).decode("utf-8", "replace")

            def skip(t: int) -> Any:
                if t in _FMT:
                    return struct.unpack(_FMT[t][0], f.read(_FMT[t][1]))[0]
                if t == 8:
                    return rstr()
                if t == 9:
                    et = struct.unpack("<I", f.read(4))[0]
                    cnt = struct.unpack("<Q", f.read(8))[0]
                    if et in _FMT:
                        f.seek(cnt * _FMT[et][1], 1)
                    elif et == 8:
                        for _ in range(cnt):
                            f.seek(struct.unpack("<Q", f.read(8))[0], 1)
                    else:
                        raise ValueError
                    return None
                raise ValueError

            for _ in range(n_kv):
                key = rstr()
                t = struct.unpack("<I", f.read(4))[0]
                v = skip(t)
                if key == "general.alignment" and isinstance(v, int) and v > 0:
                    align = v
            ts = []
            for _ in range(n_t):
                name = rstr()
                nd = struct.unpack("<I", f.read(4))[0]
                f.seek(8 * nd, 1)
                f.seek(4, 1)
                off = struct.unpack("<Q", f.read(8))[0]
                ts.append((off, name))
            data_start = (f.tell() + align - 1) // align * align
        ts.sort()
        per_layer: dict[int, int] = {}
        dense = 0
        data_len = fsize - data_start
        for i, (off, name) in enumerate(ts):
            end = ts[i + 1][0] if i + 1 < len(ts) else data_len
            size = max(0, end - off)
            m = re.match(r"blk\.(\d+)\.", name)
            if m and "_exps" in name:
                per_layer[int(m.group(1))] = per_layer.get(int(m.group(1)), 0) + size
            else:
                dense += size
        return {"expert_per_layer": per_layer, "dense": dense, "total": data_len}
    except Exception:  # noqa: BLE001
        return None


def guess_quant(name: str, meta: dict) -> str:
    m = _QUANT_RE.findall(name)
    if m:
        return m[-1].upper().replace("UD-", "UD-")
    ft = meta.get("general.file_type")
    return _FTYPE.get(ft, "") if isinstance(ft, int) else ""


def is_mmproj_name(name: str) -> bool:
    return "mmproj" in name.lower()


_cache: dict[tuple, dict] = {}


def model_info(path: Path, models_dir: Path) -> dict:
    st = path.stat()
    key = (str(path), st.st_mtime_ns, st.st_size)
    if key in _cache:
        return _cache[key]
    info: dict[str, Any] = {
        "file": path.relative_to(models_dir).as_posix(), "path": str(path), "arch": "", "name": path.stem,
        "moe": False, "experts": None, "experts_used": None, "ctx_train": None, "quant": guess_quant(path.name, {}),
        "n_layers": None, "kv_per_tok": 0, "size_bytes": st.st_size, "error": None, "embd": None,
    }
    m = _SHARD_RE.match(path.name)
    if m:  # 分片：累计所有分片大小
        total = 0
        for p in path.parent.glob(f"{m.group(1)}-*-of-{m.group(3)}.gguf"):
            try:
                total += p.stat().st_size
            except OSError:
                pass
        info["size_bytes"] = max(total, st.st_size)
    try:
        meta = read_meta(path)
        arch = meta.get("general.architecture", "")
        info["arch"] = arch
        info["name"] = meta.get("general.name") or path.stem
        info["ctx_train"] = meta.get(f"{arch}.context_length")
        info["n_layers"] = meta.get(f"{arch}.block_count")
        info["embd"] = meta.get(f"{arch}.embedding_length")
        ec = meta.get(f"{arch}.expert_count")
        if isinstance(ec, int) and ec > 1:
            info["moe"], info["experts"] = True, ec
            info["experts_used"] = meta.get(f"{arch}.expert_used_count")
        info["quant"] = guess_quant(path.name, meta)
        info["kv_per_tok"] = kv_bytes_per_token(meta)
        info["has_chat_template"] = bool(meta.get("tokenizer.chat_template"))
    except Exception as e:  # noqa: BLE001
        info["error"] = f"{type(e).__name__}: {e}"
    _cache[key] = info
    return info


def _stem_key(name: str) -> str:
    s = re.sub(r"\.gguf$", "", name, flags=re.I)
    s = re.sub(r"-\d{5}-of-\d{5}$", "", s)
    s = _QUANT_RE.sub("", s)
    s = re.sub(r"[-_.]*(gguf|mmproj)[-_.]*", "-", s, flags=re.I)
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def scan(models_dir: Path) -> tuple[list[dict], list[dict]]:
    """返回 (模型列表, mmproj 列表)。深度≤2；分片只列第一片。"""
    models: list[Path] = []
    mmprojs: list[Path] = []
    if not models_dir.is_dir():
        return [], []
    for p in sorted(models_dir.rglob("*.gguf")):
        try:
            depth = len(p.relative_to(models_dir).parts)
        except ValueError:
            continue
        if depth > 3 or p.is_symlink() and not p.exists():
            continue
        if is_mmproj_name(p.name):
            mmprojs.append(p)
            continue
        m = _SHARD_RE.match(p.name)
        if m and m.group(2) != "00001":
            continue
        models.append(p)
    out = []
    for p in models:
        info = model_info(p, models_dir)
        info = dict(info)
        info["mmproj"] = None
        cands = [q for q in mmprojs if q.parent == p.parent]
        if cands:
            k = _stem_key(p.name)
            scored = []
            for q in cands:
                qk = _stem_key(q.name)
                score = 2 if (qk and (qk in k or k in qk)) else (1 if len(cands) == 1 else 0)
                pref = {"F16": 3, "BF16": 2, "Q8_0": 1}.get(guess_quant(q.name, {}), 0)
                scored.append((score, pref, q))
            scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
            if scored[0][0] > 0:
                info["mmproj"] = scored[0][2].relative_to(models_dir).as_posix()
        info["vision"] = bool(info["mmproj"])
        info["size_gb"] = round(info["size_bytes"] / 1e9, 2)
        out.append(info)
    mm = [{"file": q.relative_to(models_dir).as_posix(), "path": str(q), "size_gb": round(q.stat().st_size / 1e9, 2),
           "size_bytes": q.stat().st_size} for q in mmprojs]
    return out, mm
