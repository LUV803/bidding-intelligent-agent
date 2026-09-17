"""对比开启 / 关闭重排序的检索结果，用于评估重排收益与延迟。

用法：
    python scripts/eval_rerank.py            # 默认对比预设问题
    python scripts/eval_rerank.py "你的问题"  # 自定义问题
"""

from __future__ import annotations

import sys

import httpx

API = "http://localhost:8000/api/search"

QUERIES = [
    "供应商针对单一来源异议被驳回，可以再次异议吗？",
    "中标公告发出后发现第一名为无效投标时，招标人应如何处理？",
    "招标文件要求中标人提交履约保证金的最高限额是多少？",
    "投标人或者其他利害关系可以向哪些行政监督部门进行投诉？",
    "评标委员会的专家人数和构成有什么要求？",
    "今天天气怎么样？",  # 无关问题，验证重排不会把噪声顶上来
]


def run(client: httpx.Client, query: str, rerank: bool) -> dict:
    response = client.post(API, json={"query": query, "mode": "hybrid", "top_k": 5, "rerank": rerank})
    response.raise_for_status()
    return response.json()


def main() -> None:
    queries = sys.argv[1:] or QUERIES
    with httpx.Client(timeout=120) as client:
        totals = {"no_rerank": 0.0, "rerank": 0.0, "rerank_only": 0.0}
        for query in queries:
            off = run(client, query, rerank=False)
            on = run(client, query, rerank=True)
            totals["no_rerank"] += off["latency_ms"]["total_ms"]
            totals["rerank"] += on["latency_ms"]["total_ms"]
            totals["rerank_only"] += on["latency_ms"].get("rerank_ms", 0.0)

            print(f"\n【{query}】")
            print(f"  不重排 {off['latency_ms']['total_ms']:>7.1f}ms  "
                  f"重排 {on['latency_ms']['total_ms']:>7.1f}ms  "
                  f"(其中重排模型 {on['latency_ms'].get('rerank_ms', 0):.1f}ms)")

            before = [s["doc_id"] for s in off["sources"]]
            print("  不重排顺序：" + " | ".join(f"[{i}] {s['question'][:22]}" for i, s in enumerate(off["sources"], 1)))
            for source in on["sources"]:
                moved = source["rank_fused"] - source["index"] if source["rank_fused"] else 0
                arrow = "  " if moved == 0 else (f"↑{moved}" if moved > 0 else f"↓{-moved}")
                print(
                    f"  [{source['index']}] {arrow:<4} 重排 {source['rerank_score']:.3f} "
                    f"| 向量 {source['vector_score'] if source['vector_score'] is None else round(source['vector_score'], 3)} "
                    f"| BM25 {source['bm25_score'] if source['bm25_score'] is None else round(source['bm25_score'], 1)} "
                    f"| {source['question'][:34]}"
                )

            after = [s["doc_id"] for s in on["sources"]]
            if before and after and before[0] != after[0]:
                print("  → 首位发生变化")
            elif not before and not after:
                print("  → 两路均判定为无匹配")
            elif before == after:
                print("  → 顺序完全一致")

        count = len(queries)
        print(
            f"\n平均耗时：不重排 {totals['no_rerank'] / count:.1f}ms / "
            f"重排 {totals['rerank'] / count:.1f}ms "
            f"（增量 {totals['rerank_only'] / count:.1f}ms）"
        )


if __name__ == "__main__":
    main()
