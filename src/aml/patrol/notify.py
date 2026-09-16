"""通知队列：巡检写、agent 读，一次不超过 `notify_max_chars` 字。

为什么要队列：巡检是后台定时跑的，用户不在场；agent 在场但不知道后台干了什么。
于是后台把"新增了什么"写成条目，agent 在回答结尾取一句话念出来（念完 ack）。

设计取向：
  · **没有变化就什么都不说**（空 brief 不输出），否则每次回答都挂一句"无更新"就是刷屏；
  · brief 一定 ≤ 上限字数（超长截断加省略号），因为它要贴进对话结尾；
  · ack 是显式的：只有真的念给用户了才标记，避免"通知被静默吞掉"。
"""
from __future__ import annotations

import datetime as dt
import json


class NoticeQueue:
    def __init__(self, cfg):
        self.cfg = cfg
        self.path = cfg.state_dir / "notices.json"
        self.max_chars = int(cfg.section("patrol").get("notify_max_chars", 100) or 100)
        self.data = {"version": 1, "pending": [], "history": []}
        if self.path.is_file():
            try:
                with open(self.path, encoding="utf-8") as f:
                    loaded = json.load(f)
                self.data["pending"] = loaded.get("pending") or []
                self.data["history"] = loaded.get("history") or []
            except (OSError, ValueError):
                pass

    # ---- 持久化 ----
    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(self.path) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(self.data, f, ensure_ascii=False, indent=2)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        import os
        os.replace(tmp, self.path)

    # ---- 写 ----
    def add(self, kind: str, text: str, detail: str = "") -> str:
        # id 带微秒：同一秒内连加两条不会撞 id（否则 --ack --id 会把两条一起标记掉）
        now = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
        notice_id = f"{now}-{kind}"
        self.data["pending"].append({"id": notice_id, "kind": kind, "ts": now, "text": text.strip(),
                                     "detail": detail, "notified": False, "notified_at": None})
        self.save()
        return notice_id

    # ---- 读 ----
    def pending(self, include_notified: bool = False) -> list:
        if include_notified:
            return list(self.data["pending"])
        return [x for x in self.data["pending"] if not x.get("notified")]

    def brief(self, limit: int | None = None) -> str:
        """≤ 上限字数的一句话；没有未播报内容时返回空串。"""
        limit = int(limit or self.max_chars)
        parts = []
        for item in self.pending():
            text = (item.get("text") or "").strip()
            if text and text not in parts:
                parts.append(text)
        if not parts:
            return ""
        body = "；".join(parts)
        prefix = "📌 "
        room = max(1, limit - len(prefix))
        if len(body) > room:
            body = body[: room - 1] + "…"
        return prefix + body

    # ---- 确认 ----
    def ack(self, notice_id: str | None = None, all_: bool = False) -> int:
        moved, keep, count = [], [], 0
        for item in self.data["pending"]:
            hit = (not item.get("notified")) and (all_ or (notice_id and item["id"] == notice_id))
            if hit:
                item["notified"] = True
                item["notified_at"] = dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                count += 1
            (moved if item.get("notified") else keep).append(item)
        self.data["pending"] = keep
        self.data["history"] = (self.data["history"] + moved)[-50:]
        if count:
            self.save()
        return count

    def render(self) -> str:
        if not self.data["pending"] and not self.data["history"]:
            return "（队列为空）"
        lines = []
        for item in self.data["pending"]:
            flag = "已播报" if item.get("notified") else "未播报"
            lines.append(f"[{flag}] {item['ts']} {item['kind']} {item['id']}\n    {item['text']}")
            for line in (item.get("detail") or "").splitlines():
                lines.append(f"    | {line}")
        if self.data["history"]:
            lines.append(f"\n历史 {len(self.data['history'])} 条（最近 3 条）：")
            for item in self.data["history"][-3:]:
                lines.append(f"    {item['ts']} {item['kind']} {item['text']}")
        return "\n".join(lines)
