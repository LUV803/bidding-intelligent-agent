"""A/B 单次探针 —— 打一发 /api/agent，把六格表需要的证据打出来。

用法（**必须在仓库根目录跑**，它按相对路径读用例）:

    uv run python .claude/skills/ab-compare/ab_one.py vllm   ag-05
    uv run python .claude/skills/ab-compare/ab_one.py ollama ag-05

⚠ 中文不能内联进 `curl -d`（Git Bash 按 GBK 发出去 → 服务端 400），
  所以问题从 eval/agent_cases.jsonl 读，不经命令行 argv。
⚠ 它打的是 127.0.0.1:8000 —— 本机跑时要先开 SSH 隧道（见 start-services）。
"""

import json
import pathlib
import sys
import urllib.error
import urllib.request

sys.stdout.reconfigure(encoding="utf-8")  # 否则 Git Bash 下中文乱码

CASES = pathlib.Path("eval/agent_cases.jsonl")
URL = "http://127.0.0.1:8000/api/agent"


def main() -> int:
    if len(sys.argv) != 3:
        print(__doc__)
        return 2
    provider, case_id = sys.argv[1], sys.argv[2]

    if not CASES.exists():
        print(f"✗ 找不到 {CASES.resolve()} —— 请在仓库根目录跑")
        return 2
    cases = {c["id"]: c for c in map(json.loads, CASES.read_text(encoding="utf-8").splitlines())}
    case = cases.get(case_id)
    if case is None:
        print(f"✗ 没有用例 {case_id}；可选：{', '.join(cases)}")
        return 2
    question = case["question"]

    body = json.dumps({
        "question": question,
        "llm_provider": provider,   # 空串 = 跟服务端默认，A/B 必须显式给
        "rerank": False,            # A/B 前置：重排关
        "web_search": False,        # A/B 前置：联网关
    }, ensure_ascii=False).encode("utf-8")

    req = urllib.request.Request(URL, data=body, headers={"Content-Type": "application/json"})

    calls, results, text, total, errs = [], [], [], None, []
    try:
        with urllib.request.urlopen(req, timeout=900) as resp:   # Ollama 在 CPU 上很慢，给足
            for raw in resp:                    # SSE 帧 = `data: {json}\n\n`，没有 event: 行
                line = raw.decode("utf-8").strip()
                if not line.startswith("data:"):
                    continue
                ev = json.loads(line[5:].strip())
                t = ev.get("type")
                # 进度打 stderr —— Ollama 在 CPU 上一发可能十几分钟，没有这个分不出
                # 「在跑」和「卡住」。delta/reasoning 是逐字的，不记。
                if t in ("start", "sources", "tool_call", "tool_result", "answer_reset", "done"):
                    extra = f' {ev.get("name", "")}' if t == "tool_call" else ""
                    print(f"  · {t}{extra}", file=sys.stderr, flush=True)
                if t == "tool_call":
                    calls.append(f'R{ev["round"]} {ev["name"]} «{ev["query"]}»')
                elif t == "tool_result":
                    results.append(f'ok={ev["ok"]} 新增={len(ev["new_indices"])} '
                                   f'已知={len(ev["known_indices"])} err={ev["error"]!r} '
                                   f'图={ev["graph"] is not None} 库={ev["pg"] is not None}')
                elif t == "delta":
                    text.append(ev["content"])
                elif t == "error":
                    errs.append(ev.get("message", ""))          # ⚠ 报错也要收下来
                elif t == "done":
                    total = ev["total_ms"]
    except urllib.error.URLError as exc:
        print(f"✗ 连不上 {URL}：{exc}（隧道开了吗？后端起了吗？）")
        return 1

    print(f'provider = {provider}   用例 = {case_id}   问题 = {question}')
    print(f'调了哪些 = {calls or "（一个都没调）"}')
    print(f'几轮     = {len(calls)}')
    print(f'结果     = {results or "（无）"}')
    print(f'总耗时   = {total} ms')
    print(f'正文     = {"".join(text)}')
    if errs:
        print(f'⚠ error 事件 = {errs}')
    return 0


if __name__ == "__main__":
    sys.exit(main())
