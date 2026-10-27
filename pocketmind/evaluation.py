"""记忆召回评测：用固定探针检查「该记得的有没有记住」。

这不是人格评测（那需要人工盲评，见 docs/12）；只是 M1 的可判定回归测试。
建议先跑 `simulate` 再跑 `evaluate`。
"""

from __future__ import annotations

from typing import Any

from .pipeline import ChatEngine

PROBES = [
    ("我叫什么名字？", ["小明"]),
    ("我住在哪里？", ["杭州"]),
    ("我喜欢吃什么？", ["草莓"]),
    ("还记得那只大金毛吗？", ["金毛", "大金毛"]),
    ("我答应过你什么？", ["考试", "结果"]),
]


def run_evaluation(engine: ChatEngine) -> dict[str, Any]:
    cid = engine.new_conversation("评测探针")
    results = []
    passed = 0
    for question, keywords in PROBES:
        result = engine.send(question, cid)
        ok = any(k in result.reply for k in keywords)
        passed += 1 if ok else 0
        results.append(
            {
                "question": question,
                "reply": result.reply,
                "expected": keywords,
                "ok": ok,
                "used_memories": [m["memory_id"] for m in result.used_memories],
            }
        )
    return {
        "score": round(passed / len(PROBES), 3),
        "passed": passed,
        "total": len(PROBES),
        "results": results,
        "stats": engine.stats(),
    }
