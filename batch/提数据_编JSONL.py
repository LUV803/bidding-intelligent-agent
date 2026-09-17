"""提取 Excel 里的「项目名称」，编成智谱 Batch API 的 JSONL 请求文件。

输入：batch/结果/招标采购标的物信息提取训练数据_2.xlsx（「项目名称」在第 9 列）
模板：batch/batch_input.jsonl（请求文件，提供 method / url / model / 提示词）
输出：batch/batch_requests_10.jsonl（可用 -o 改）

⚠ 模板必须用 batch_input.jsonl，不能用 batch_output.jsonl。
   后者是**结果**文件，顶层只有 response / custom_id / id，没有 method、url、messages，
   提示词也不在里面，照着它拼不出请求。（脚本对喂错的模板会直接报错，不会产出废文件。）

⚠ 对齐警告：这份 jsonl 的 custom_id 是 **_2.xlsx 里的行号**（去重后 8751 行），
   而 run_batch.py 的 DEFAULT_SOURCE_XLSX 指向 data/raw 下的原始文件（8789 行）。
   去重删掉了 38 行，两边从第一处被删的重复行起就错开，越往后偏得越多——
   直接跑 run_batch.py 回填会把抽取结果**静默写错行**（不报错，只是全错）。
   真要回填必须显式指定源文件：
       uv run python batch/run_batch.py --source "batch/结果/招标采购标的物信息提取训练数据_2.xlsx"

运行：
    uv run python batch/提数据_编JSONL.py
    uv run python batch/提数据_编JSONL.py -i 别的文件.xlsx -o 别的输出.jsonl
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from openpyxl import load_workbook

# 本文件在 <项目根>/batch/ 下，parents[1] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "batch"
RESULT_DIR = BATCH_DIR / "结果"

DEFAULT_INPUT = RESULT_DIR / "招标采购标的物信息提取训练数据_2.xlsx"
DEFAULT_OUTPUT = BATCH_DIR / "batch_requests_10.jsonl"

# 请求结构的来源，必须是请求文件（见文件头 ⚠）
TEMPLATE_JSONL = BATCH_DIR / "batch_input.jsonl"

# 「项目名称」所在列（从 1 数，即 Excel 的第 9 列；第 10 列是「采购人」）。
# 位置写死 + 表头校验：提错列不会报错，只会安安静静产出一批「采购人」当项目名称，
# 等跑完 8751 条才发现就晚了。所以读到表头先确认它真的叫「项目名称」，不对立刻退出。
NAME_COLUMN = 9

# 提示词里项目名称前面的标签。模板切分与拼接共用同一个常量，避免两处各写一份而对不上
NAME_LABEL = "项目名称："

# custom_id 最短长度。官方文档没写这条，是实测撞出来的：
# "0"、"1" … 上传时整个文件被拒 ——
#   400 {"error":{"code":"1210","message":"custom id 长度不足, 最短: 6"}}
MIN_CUSTOM_ID_LEN = 6


def load_template(path: Path) -> dict:
    """从模板 jsonl 的第一条请求里拆出「请求结构」和「提示词前缀」。

    提示词是**逐字取回来的**，不是在本文件里重写一遍：一旦提示词在两处各存一份，
    改了一处忘了另一处，整批数据就白跑（8789 条要排两小时队）。
    """
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            request = json.loads(line)

            # 拿结果文件当模板是最容易犯的错，这里点名说出来，别让人对着 KeyError 猜
            if "method" not in request or "body" not in request:
                raise ValueError(
                    f"{path} 看起来是**结果**文件（顶层键：{list(request.keys())}），"
                    "不是请求文件。模板请用 batch/batch_input.jsonl。"
                )

            content = request["body"]["messages"][0]["content"]
            if NAME_LABEL not in content:
                raise ValueError(f"模板提示词里找不到 {NAME_LABEL!r}：{content[:80]!r}")

            return {
                "method": request["method"],
                "url": request["url"],
                "model": request["body"]["model"],
                # rsplit 而不是 split：提示词正文里也可能出现「项目名称：」，只切最后那个
                "prefix": content.rsplit(NAME_LABEL, 1)[0],
                "temperature": request["body"].get("temperature", 0),
            }

    raise ValueError(f"{path} 里没有任何请求行")


def extract_names(path: Path, column: int) -> list[str]:
    """按行读出所有非空的「项目名称」。"""
    # 只读模式：这份表 8751 行 × 18 列，普通模式会把整本簿子连同样式全载进内存，
    # 而我们只要一列。data_only=True 取单元格的值而不是公式本身。
    workbook = load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = sheet.iter_rows(values_only=True)

        header = next(rows, None)
        if header is None:
            raise ValueError(f"{path} 是空表")
        actual = header[column - 1] if len(header) >= column else None
        if str(actual).strip() != "项目名称":
            raise ValueError(
                f"第 {column} 列的表头是 {actual!r}，不是「项目名称」——"
                "表结构可能变了，核对列位置后再跑"
            )

        names: list[str] = []
        for row in rows:
            value = row[column - 1] if len(row) >= column else None
            if value is None:
                continue
            # 空值行跳过：空的项目名称送进模型只会浪费一次请求、还占一个 custom_id
            text = str(value).strip()
            if text:
                names.append(text)
        return names
    finally:
        # 只读模式也要显式关闭：不关会一直占着文件句柄，Windows 下再想写这个文件就被拒
        workbook.close()


def build_request(custom_id: str, project_name: str, template: dict) -> dict:
    """组装单条请求：把项目名称接到提示词前缀后面，其余字段照抄模板。"""
    return {
        "custom_id": custom_id,
        "method": template["method"],
        "url": template["url"],
        "body": {
            "model": template["model"],
            "messages": [
                {
                    "role": "user",
                    "content": f"{template['prefix']}{NAME_LABEL}{project_name}",
                }
            ],
            "temperature": template["temperature"],
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="把「项目名称」编成智谱 Batch API 的 JSONL 请求文件")
    parser.add_argument("-i", "--input", type=Path, default=DEFAULT_INPUT, help="含「项目名称」的 Excel")
    parser.add_argument("-o", "--output", type=Path, default=DEFAULT_OUTPUT, help="输出的 jsonl 路径")
    parser.add_argument(
        "-t", "--template", type=Path, default=TEMPLATE_JSONL, help="请求结构模板（jsonl）"
    )
    args = parser.parse_args()

    for path, label in ((args.input, "输入"), (args.template, "模板")):
        if not path.exists():
            print(f"[错误] 找不到{label}文件：{path}", file=sys.stderr)
            return 1

    try:
        template = load_template(args.template)
        names = extract_names(args.input, NAME_COLUMN)
    except ValueError as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1

    args.output.parent.mkdir(parents=True, exist_ok=True)
    # newline="\n"：Windows 文本模式会把 "\n" 翻译成 "\r\n"，产出的就不是 LF 了。
    # 实测 CRLF 服务端也能正常解析，这里统一成 LF 只是为了和 batch_input.jsonl 一致。
    with args.output.open("w", encoding="utf-8", newline="\n") as fh:
        for position, name in enumerate(names):
            # custom_id 用行号（从 0 起）零填充：既满足 6 位下限，
            # 又能让 run_batch.py 的 int(custom_id) 直接还原成行位置。
            custom_id = str(position).zfill(MIN_CUSTOM_ID_LEN)
            # ensure_ascii=False 让中文按原样写入而不是转成 \uXXXX，便于人工核对
            fh.write(json.dumps(build_request(custom_id, name, template), ensure_ascii=False) + "\n")

    print(f"输入：{args.input}")
    print(f"模板：{args.template}")
    print(f"输出：{args.output}")
    print(f"  生成条数：{len(names)}")
    if names:
        print(
            f"  custom_id：{str(0).zfill(MIN_CUSTOM_ID_LEN)} … "
            f"{str(len(names) - 1).zfill(MIN_CUSTOM_ID_LEN)}（{MIN_CUSTOM_ID_LEN} 位补零，连续递增）"
        )
    print(f"  模型：{template['model']}  temperature={template['temperature']}")
    print(f"  文件大小：{args.output.stat().st_size / 1024 / 1024:.2f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
