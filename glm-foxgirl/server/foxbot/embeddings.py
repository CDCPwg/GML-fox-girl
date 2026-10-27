"""Embedding 客户端与向量工具（OpenAI 兼容 /embeddings，未配置时返回 None 优雅降级）。"""
import json
import urllib.request

from . import config


def embed_text(text):
    cfg = config.load_config().get("memory", {})
    base = (cfg.get("embed_base_url") or "").strip()
    if not base:
        return None
    headers = {"Content-Type": "application/json"}
    if cfg.get("embed_api_key"):
        headers["Authorization"] = "Bearer " + cfg["embed_api_key"]
    body = {"model": cfg.get("embed_model", "embedding-3"), "input": text}
    req = urllib.request.Request(base.rstrip("/") + "/embeddings",
                                 data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return [float(x) for x in data["data"][0]["embedding"]]
    except Exception:
        return None


def _cosine(a, b):
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
