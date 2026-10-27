"""3 天脚本模拟：验证「记得住 / 会忘 / 承诺 / 日记」的闭环。

不依赖真实 API；mock 模式下也能量化检查。
"""

from __future__ import annotations

from typing import Any

from .pipeline import ChatEngine

SCRIPT = [
    {
        "day": 1,
        "messages": [
            "你好呀，我叫小明，我住在杭州，我喜欢吃草莓。",
            "今天下午我在公园看到一只大金毛，特别开心。",
        ],
    },
    {
        "day": 2,
        "messages": [
            "你还记得我喜欢吃什么吗？",
            "我有点难过，作业没写完。",
        ],
    },
    {
        "day": 3,
        "messages": [
            "还记得那只大金毛吗？",
            "我答应你下周告诉你考试结果。",
        ],
    },
]

PROBES = [
    ("我叫什么名字？", ["小明"]),
    ("我住在哪里？", ["杭州"]),
    ("我喜欢吃什么？", ["草莓"]),
    ("还记得那只大金毛吗？", ["金毛", "大金毛"]),
]


def run_simulation(engine: ChatEngine, days: int = 3) -> dict[str, Any]:
    cid = engine.new_conversation("模拟对话")
    turns: list[dict[str, Any]] = []
    for block in SCRIPT[: max(1, min(days, len(SCRIPT)))]:
        for text in block["messages"]:
            result = engine.send(text, cid)
            turns.append(
                {
                    "day": block["day"],
                    "user": text,
                    "reply": result.reply,
                    "saved": result.saved_memories,
                    "used": [m["content"] for m in result.used_memories],
                }
            )
    probes = []
    passed = 0
    for question, keywords in PROBES:
        result = engine.send(question, cid)
        ok = any(k in result.reply for k in keywords)
        passed += 1 if ok else 0
        probes.append(
            {
                "question": question,
                "reply": result.reply,
                "expected": keywords,
                "ok": ok,
            }
        )
    return {
        "conversation_id": cid,
        "turns": turns,
        "probes": probes,
        "recall_score": round(passed / len(PROBES), 3),
        "stats": engine.stats(),
        "diary": engine.diary_today(),
    }
