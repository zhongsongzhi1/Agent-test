from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9-]+|[\u4e00-\u9fff]", text.lower())


def cosine_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q = Counter(query_tokens)
    d = Counter(doc_tokens)
    dot = sum(q[token] * d[token] for token in q.keys() & d.keys())
    q_norm = math.sqrt(sum(v * v for v in q.values()))
    d_norm = math.sqrt(sum(v * v for v in d.values()))
    if not q_norm or not d_norm:
        return 0.0
    return dot / (q_norm * d_norm)


class KnowledgeIndex:
    """轻量级本地检索索引。

    TODO(candidate/P1): 完成权限感知检索、重排、答案生成、引用溯源
    和被过滤文档报告。文档正文必须视为不可信数据，不能让正文中的
    指令改变系统策略；完成实现后不得向 API 返回 debug/candidate_note。
    """

    def search(
        self,
        query: str,
        user_permissions: list[str],
        top_k: int = 3,
    ) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                """
                SELECT id, doc_id, source_path, title, permission, content
                FROM knowledge_chunks
                """
            ).fetchall()
        # 权限过滤：只有当用户拥有 chunk.permission 或 chunk.permission == 'knowledge:read' 时可见
        accessible = []
        filtered_doc_ids = set()
        q_tokens = tokenize(query)
        scored = []
        for row in rows:
            perm = row["permission"]
            if perm == "knowledge:read" or perm in user_permissions:
                content = f"{row['title']} {row['content'] or ''}"
                score = cosine_score(q_tokens, tokenize(content))
                scored.append((score, row))
            else:
                filtered_doc_ids.add(row["doc_id"])

        # 按相关性排序并选 top_k
        scored.sort(key=lambda x: x[0], reverse=True)
        top = [r for s, r in scored[:top_k] if s > 0]

        citations = [
            {
                "doc_id": row["doc_id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "chunk_id": row["id"],
            }
            for row in top
        ]

        
        if top:
            snippets = []
            for row in top:
                text = (row["content"] or "").strip()
                snippet = text[:200]
                snippets.append(f"{row['title']}: {snippet}")
            answer = "\n".join(snippets)
        else:
            answer = ""

        return {
            "answer": answer,
            "citations": citations,
            "filtered_doc_ids": sorted(filtered_doc_ids),
        }
