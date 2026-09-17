"""按 custom_id 升序重排 Batch 结果 jsonl，保证输出的 custom_id 严格递增。

输入：batch/batch_output.jsonl（run_batch.py 下载的原始结果）
输出：batch/batch_output_sorted.jsonl（可用 -o 改，或用 --in-place 原地覆盖）

为什么要排：服务端返回的顺序**不保证与输入一致**。实测这次的结果大体有序，但整体旋转过
（第一行是 custom_id=5500）并夹杂 8 处逆序——所以绝不能按行位置对回原表，
run_batch.py 的 parse_results() 正是因此才用 dict 按 custom_id 取值。
本脚本不参与那条链路，纯粹是为了让文件能人工翻查、能 diff。

⚠ 排序只改**行的先后**，不改行内容：读进来是原始文本行，写出去还是同一行，
   不重新序列化——否则 JSON 的键序、空格、转义都可能被顺手改动，diff 出来一片红。

运行：
    uv run python batch/按custom_id排序.py
    uv run python batch/按custom_id排序.py -i 别的.jsonl -o 别的_sorted.jsonl
    uv run python batch/按custom_id排序.py --in-place
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# 本文件在 <项目根>/batch/ 下，parents[1] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "batch"

# 默认输入。项目文档里有时把它称作 batch_results_raw.jsonl，仓库里没有这个名字——
# 指的就是 run_batch.py 下载后原样存下来的这份。
DEFAULT_INPUT = BATCH_DIR / "batch_output.jsonl"


def make_sort_key(custom_id) -> tuple[int, int, str]:
    """数字 id 按**数值**排，非数字的排在最后。

    不能直接按字符串排：零填充只在行数不超过 10^6 时成立。到 1000000 行时
    "1000000" < "999999"（字典序）而数值上它更大，顺序就错了。
    """
    text = str(custom_id).strip()
    if text.isdigit():
        return (0, int(text), "")
    return (1, 0, text)


def sort_key(entry: tuple) -> tuple:
    """(能否解析, id 排序键, 原始行号)。

    第三个元素是兜底：custom_id 相同、或都解析不出来时，保持它们在原文件里的先后，
    排序结果才可复现。sorted() 本身稳定，显式写出来是为了让这个约定看得见。
    """
    custom_id, _text, line_no = entry
    if custom_id is None or str(custom_id).strip() == "":
        return (1, (1, 0, ""), line_no)
    return (0, make_sort_key(custom_id), line_no)


def load_lines(path: Path) -> tuple[list[tuple], int]:
    """读回原始文本行，顺带解析出 custom_id。

    返回 [(custom_id, 原始行文本, 原始行号)] 与跳过的空白行数。
    解析失败的行 custom_id 记为 None —— 它们不会被丢掉，只是排到末尾。
    """
    entries: list[tuple] = []
    blank = 0
    with path.open(encoding="utf-8") as fh:
        for line_no, raw in enumerate(fh, start=1):
            # 只去掉行尾换行，行内空白原样保留，避免「排序」顺手改了内容
            text = raw.rstrip("\r\n")
            if not text.strip():
                blank += 1
                continue
            try:
                custom_id = json.loads(text).get("custom_id")
            except json.JSONDecodeError:
                custom_id = None
            entries.append((custom_id, text, line_no))
    return entries, blank


def numeric_ids(entries: list[tuple]) -> list[int]:
    """取出其中是纯数字的 custom_id（非数字的按设计排在末尾，不参与递增性判断）。"""
    return [int(c) for c, _, _ in entries if c is not None and str(c).strip().isdigit()]


def is_increasing(entries: list[tuple]) -> bool:
    nums = numeric_ids(entries)
    return all(a < b for a, b in zip(nums, nums[1:]))


def main() -> int:
    parser = argparse.ArgumentParser(description="按 custom_id 升序重排 Batch 结果 jsonl")
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="输入的 jsonl")
    parser.add_argument(
        "-o", "--output", type=Path, default=None, help="输出的 jsonl（默认 <输入名>_sorted.jsonl）"
    )
    parser.add_argument("--in-place", action="store_true", help="原地覆盖输入文件")
    args = parser.parse_args()

    if not args.input.exists():
        print(f"[错误] 找不到输入文件：{args.input}", file=sys.stderr)
        return 1

    output = args.input if args.in_place else (args.output or args.input.with_name(f"{args.input.stem}_sorted.jsonl"))

    # 先把整个文件读进内存再打开输出：--in-place 时输入输出是同一个文件，
    # 边读边写会把还没读到的行覆盖掉。
    entries, blank = load_lines(args.input)
    ordered = sorted(entries, key=sort_key)

    output.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：Windows 文本模式会把 "\n" 翻译成 "\r\n"，产出的就不是 LF 了
    with output.open("w", encoding="utf-8", newline="\n") as fh:
        for _, text, _ in ordered:
            fh.write(text + "\n")

    print(f"输入：{args.input}")
    print(f"输出：{output}")
    print(f"  读取行数：{len(entries)}" + (f"（另有空白行 {blank} 条已跳过）" if blank else ""))

    nums = numeric_ids(ordered)
    unparsed = len(ordered) - len(nums)
    if unparsed:
        print(f"  [警告] {unparsed} 条没有可用的 custom_id，已附在末尾（未丢弃）")

    if nums:
        unique = sorted(set(nums))
        print(f"  custom_id：{unique[0]} … {unique[-1]}（{len(unique)} 个唯一值）")
        dup = len(nums) - len(unique)
        if dup:
            print(f"  [警告] 有 {dup} 个重复的 custom_id")
        missing = [n for n in range(unique[0], unique[-1] + 1) if n not in set(unique)]
        if missing:
            head = missing[:10]
            print(f"  [提示] 中间缺 {len(missing)} 个号：{head}{' …' if len(missing) > 10 else ''}")

    print(f"  排序前已递增：{is_increasing(entries)}")
    print(f"  排序后严格递增：{is_increasing(ordered)}")
    print(f"  文件大小：{output.stat().st_size} 字节")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
