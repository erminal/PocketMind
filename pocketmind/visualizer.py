"""记忆向量可视化：把高维 embedding 投影到 2D/3D，并把标量映射成颜色/大小。

说明：
- 向量本身是 64/1024 维，不能直接“画在坐标图上”；
- 这里用 PCA 做线性投影（可解释、无额外依赖），只保留前 3 个主成分；
- 投影会损失距离/角度信息，图上远近是近似，不是严格语义距离；
- 标量（valence/importance/half_life）通过颜色/大小/筛选展示，不参与坐标；
- 只读：不改记忆、不改 schema。
"""

from __future__ import annotations

import json
from typing import Any

import numpy as np

from .db import Database


def _load_vectors(db: Database, entity_id: str, status: str, limit: int) -> list[dict[str, Any]]:
    rows = db.fetchall(
        """SELECT id, content, category, valence, importance, half_life_days,
                  embedding_json, created_at, status, pinned, gist
           FROM memories
           WHERE entity_id=? AND status=?
           ORDER BY created_at DESC LIMIT ?""",
        (entity_id, status, limit),
    )
    out: list[dict[str, Any]] = []
    for row in rows:
        try:
            vec = json.loads(row.get("embedding_json") or "[]")
        except Exception:
            vec = []
        if not isinstance(vec, list) or len(vec) < 2:
            continue
        out.append({**row, "vector": [float(x) for x in vec]})
    return out


def build_projection(
    db: Database,
    entity_id: str,
    status: str = "active",
    limit: int = 500,
    max_edges: int = 220,
) -> dict[str, Any]:
    items = _load_vectors(db, entity_id, status, max(20, min(limit, 1000)))
    if not items:
        return {
            "method": "pca",
            "count": 0,
            "variance": [],
            "points": [],
            "edges": [],
            "note": "没有带向量的记忆。先聊几句或跑 simulate。",
        }
    # 统一维度：以最短维度为准；真实 API 换模型后旧向量可能维度不同
    dim = min(len(it["vector"]) for it in items)
    if dim < 2:
        return {"method": "pca", "count": 0, "variance": [], "points": [], "edges": [], "note": "向量维度不足。"}
    matrix = np.asarray([it["vector"][:dim] for it in items], dtype=float)
    matrix = matrix - matrix.mean(axis=0, keepdims=True)
    if matrix.shape[0] == 1:
        coords = np.zeros((1, 3))
        var = np.zeros(3)
    else:
        # SVD 的右奇异向量乘奇异值 = 主成分得分
        u, s, _ = np.linalg.svd(matrix, full_matrices=False)
        coords = u[:, :3] * s[:3]
        if coords.shape[1] < 3:
            coords = np.pad(coords, ((0, 0), (0, 3 - coords.shape[1])))
        total = float(np.sum(s**2)) or 1.0
        var = (s[:3] ** 2 / total).tolist()
    # 归一化到 [-1, 1]，方便前端画图；保留相对结构
    span = np.max(np.abs(coords), axis=0)
    span[span == 0] = 1.0
    coords = coords / span

    # 余弦邻居边：只画最强的少数边，避免图太乱
    norm = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-9)
    sim = norm @ norm.T
    edges: list[dict[str, Any]] = []
    seen: set[tuple[int, int]] = set()
    for i in range(len(items)):
        order = np.argsort(sim[i])[::-1]
        for j in order[1:3]:
            a, b = sorted((i, int(j)))
            if a == b or (a, b) in seen:
                continue
            seen.add((a, b))
            if sim[a, b] >= 0.35:
                edges.append({"a": a, "b": b, "score": round(float(sim[a, b]), 4)})
            if len(edges) >= max_edges:
                break
        if len(edges) >= max_edges:
            break

    points = []
    for i, it in enumerate(items):
        points.append(
            {
                "i": i,
                "id": it["id"],
                "x": round(float(coords[i, 0]), 5),
                "y": round(float(coords[i, 1]), 5),
                "z": round(float(coords[i, 2]), 5),
                "category": it["category"],
                "content": (it["content"] or "")[:500],
                "gist": it.get("gist", ""),
                "valence": round(float(it["valence"]), 3),
                "importance": round(float(it["importance"]), 3),
                "half_life_days": round(float(it["half_life_days"]), 1),
                "pinned": bool(it["pinned"]),
                "created_at": it["created_at"],
            }
        )
    return {
        "method": "pca",
        "dims": 3,
        "count": len(points),
        "variance": [round(float(x), 4) for x in var],
        "points": points,
        "edges": edges,
        "note": "PCA 投影只保留前三个主成分；图上距离是近似，不代表完整高维距离。颜色/大小由标量决定。",
    }
