"""记忆质量反馈：让"被召回"和"有用"分开计分。

外部 review 里最深的一条缺口：现在只知道"这条被召回了"，不知道"召回之后有没有帮上忙"。
于是所有记忆永远按 embedding 相似度排 —— 一条屡次导致返工的经验，和一条屡次救场的经验，
排序上没有任何区别。

本模块做三件事：
  1. **记录**（`record`）：agent 用完一条记忆后打点 —— worked / failed / used
  2. **算分**（`reliability`）：拉普拉斯平滑的成功率，没有数据时返回 None（不是 0，也不是 1）
  3. **参与排序**（配合 `retrieval.py`）：**只在同档位内重排**，不改变档位门槛 ——
     否则一条高分新记忆会因为"还没被用过"被压到阈值以下，反而搜不到

字段口径（写在 metadata 里，不动后端 schema）：
    usage_count / success_count / failure_count
    last_used_at / last_failed_at / last_verified_at
"""
from __future__ import annotations

import datetime as dt

from .http import MemoryClient

OUTCOMES = ("worked", "failed", "used")


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def reliability(meta: dict, prior: float = 1.0, strength: float = 2.0):
    """拉普拉斯平滑后的可靠度 ∈ (0,1)；没有使用数据时返回 None。

    prior/strength 的含义：`(success + prior) / (success + failure + strength)` ——
    默认先验 1:1，等价于"没见过它时按五五开算"，所以 1 次成功不会让它变成满分。
    返回 None 是刻意的：调用方必须显式决定"没有数据"怎么处理（我们选择不惩罚新记忆）。
    """
    success = int(meta.get("success_count") or 0)
    failure = int(meta.get("failure_count") or 0)
    if success + failure == 0:
        return None
    return round((success + prior) / (success + failure + strength), 3)


def rank_factor(meta: dict, floor: float = 0.85, ceiling: float = 1.15) -> float:
    """把可靠度变成乘在分数上的系数：**没有数据 → 1.0（中性，不惩罚新记忆）**。

    区间是 [floor, ceiling]，可靠度 0.5（五五开）恰好落在 1.0（中性），
    于是"被证实有效"能真正排到前面，"屡次失败"也只是降权、不会消失。
    第一版写成上限 1.0（只能降不能升），结果"有用的排前面"永远不成立 —— 测试抓到了。
    """
    value = reliability(meta)
    if value is None:
        return 1.0
    return round(floor + (ceiling - floor) * value, 4)


def _find(client: MemoryClient, content_hash: str):
    """按 hash 找一条记忆（先试直接取，后端不支持就退化成扫描）。"""
    for method in ("get",):
        getter = getattr(client, method, None)
        if getter is None:
            continue
        try:
            memory = getter(content_hash)
            if memory and memory.get("content_hash") == content_hash:
                return memory
        except Exception:  # noqa: BLE001 - 后端可能没这个端点
            pass
    for memory in client.iter_memories():
        if memory.get("content_hash") == content_hash:
            return memory
    return None


def record(cfg, content_hash: str, outcome: str, note: str = "", client: MemoryClient | None = None,
           log=print) -> dict:
    """给一条记忆打点。outcome ∈ worked / failed / used。

    计数器是"读-改-写"：先取回当前 metadata，再整体写回（后端没有原子自增，够用）。
    """
    if outcome not in OUTCOMES:
        raise ValueError(f"outcome 必须是 {OUTCOMES} 之一，收到 {outcome!r}")
    client = client or MemoryClient(cfg.api)
    memory = _find(client, content_hash)
    if not memory:
        return {"ok": False, "error": f"找不到这条记忆：{content_hash}"}

    meta = dict(memory.get("metadata") or {})
    meta["usage_count"] = int(meta.get("usage_count") or 0) + 1
    if outcome == "worked":
        meta["success_count"] = int(meta.get("success_count") or 0) + 1
    elif outcome == "failed":
        meta["failure_count"] = int(meta.get("failure_count") or 0) + 1
        meta["last_failed_at"] = _now()
    meta["last_used_at"] = _now()
    if note:
        meta["last_note"] = note[:200]

    response = client.update(content_hash, {"metadata": meta})
    ok = bool(response.get("success", True))
    # 同时打服务的**原生质量评分**（Dashboard 的 Analytics 也看得到）：
    # metadata 计数用于排序（检索结果里直接读到），原生 rating 用于服务的质量视图。
    native = None
    rating = {"worked": 1, "used": 0, "failed": -1}[outcome]
    try:
        native = client.rate(content_hash, rating, feedback=note or outcome)
    except Exception as e:  # noqa: BLE001 - 后端版本不同可能没这个端点，不该让反馈整体失败
        native = {"success": False, "error": f"{type(e).__name__}: {str(e)[:60]}"}
    log(f"  反馈已记录：{outcome}（usage {meta['usage_count']}，"
        f"成功 {meta.get('success_count', 0)} / 失败 {meta.get('failure_count', 0)}，"
        f"可靠度 {reliability(meta)}）")
    return {"ok": ok, "outcome": outcome, "meta": meta, "response": response, "native": native}


def verify(cfg, content_hash: str, days: int = 180, client: MemoryClient | None = None,
           log=print) -> dict:
    """人工复核"仍然成立"：写 `last_verified_at` 并把复核期顺延。

    与 `review --postpone` 的区别：那个只动 `review_after`（到期日），
    这个额外留下"什么时候被人看过"的痕迹 —— 排查错误记忆时要的就是这个。
    """
    client = client or MemoryClient(cfg.api)
    memory = _find(client, content_hash)
    if not memory:
        return {"ok": False, "error": f"找不到这条记忆：{content_hash}"}
    meta = dict(memory.get("metadata") or {})
    meta["last_verified_at"] = _now()
    meta["review_after"] = (dt.date.today() + dt.timedelta(days=days)).isoformat()
    response = client.update(content_hash, {"metadata": meta})
    log(f"  已标记复核：{content_hash}（下次复核 {meta['review_after']}）")
    return {"ok": bool(response.get("success", True)), "review_after": meta["review_after"],
            "verified_at": meta["last_verified_at"]}
