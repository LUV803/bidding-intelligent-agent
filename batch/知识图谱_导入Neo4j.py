"""把知识图谱的两个 CSV 导入 Neo4j。

输入：batch/结果/招标采购标的物信息提取训练数据_2_知识图谱.csv（8751 行 × 6 列）
      batch/结果/标的物_交易频次.csv（2309 行 × 2 列）
目标：.env 里 NEO4J_* 指定的那个库

图模型（以「交易」为中心的星型）：

    (:采购商)-[:采购]->(:交易 {行号, 交易额})
    (:供应商)-[:中标]->(:交易)
    (:代理机构)-[:代理]->(:交易)
    (:交易)-[:标的]->(:标的物 {名称, 交易频次})
    (:交易)-[:发生地]->(:所在地)

为什么不让列直接连边（(:采购商)-[:采购]->(:标的物)）：那样「所在地」只能挂在标的物上，
而「打印纸」并不位于合肥市——所在地是**某一次采购**的属性，不是标的物的属性。
中间这层「交易」节点承载交易额和地点，语义才对，平行边也不会把聚合查询搞乱。

为什么用驱动推而不是 LOAD CSV：本地实例其实可以用 `LOAD CSV FROM 'file:///...'`，
但那要求文件落在 Neo4j 自己的 import 目录里；用驱动就不用管这件事，且能逐批报进度。

运行：
    uv run python batch/知识图谱_导入Neo4j.py --dry-run   # 只解析和报告，不碰数据库
    uv run python batch/知识图谱_导入Neo4j.py --limit 50  # 只写前 50 行，冒烟测试
    uv run python batch/知识图谱_导入Neo4j.py             # 全量
    uv run python batch/知识图谱_导入Neo4j.py --clear     # 先清空再导
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import pandas as pd

# 本文件在 <项目根>/batch/ 下，parents[1] 即项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[1]
BATCH_DIR = PROJECT_ROOT / "batch"
RESULT_DIR = BATCH_DIR / "结果"

DEFAULT_KG_CSV = RESULT_DIR / "招标采购标的物信息提取训练数据_2_知识图谱.csv"
DEFAULT_FREQ_CSV = RESULT_DIR / "标的物_交易频次.csv"

ITEM_COLUMN = "标的物"
FREQ_COLUMN = "交易频次"

# 图谱 CSV 里必须齐的列。少一列就建不出完整的图，直接拒绝而不是凑合导
KG_COLUMNS = ["标的物", "采购商", "代理机构", "供应商", "交易额", "所在地"]

# 每批 UNWIND 多少行。500 在「往返次数」和「单次事务大小」之间比较平衡：
# 8751 行 → 18 批；再大对本地实例没收益，再小纯属多跑几十个来回
BATCH_SIZE = 500

# 建立约束用。名字取 ASCII：中文约束名在 Neo4j Browser 里能建，
# 但报错信息和 SHOW CONSTRAINTS 输出会难读
CONSTRAINTS: list[tuple[str, str, str]] = [
    ("purchaser_name", "采购商", "名称"),
    ("supplier_name", "供应商", "名称"),
    ("agency_name", "代理机构", "名称"),
    ("item_name", "标的物", "名称"),
    ("place_name", "所在地", "名称"),
    ("deal_row", "交易", "行号"),
]

# ---------- 写入用的 Cypher ----------
# 装在同一批里的行会一次成型：交易节点 + 采购商 + 标的物 + 两条边。
# 采购商/标的物在这份数据里没有空值，所以不用判空。
QUERY_MAIN = """
UNWIND $rows AS row
MERGE (t:交易 {行号: row.行号})
  SET t.交易额 = row.交易额
MERGE (b:采购商 {名称: row.采购商})
MERGE (b)-[:采购]->(t)
MERGE (i:标的物 {名称: row.标的物})
MERGE (t)-[:标的]->(i)
"""

# 供应商、代理机构、所在地分开跑：代理机构 88% 是空的，混进主查询就得写
# FOREACH + CASE 那种绕圈子的判空，可读性差得多。
QUERY_SUPPLIER = """
UNWIND $rows AS row
MATCH (t:交易 {行号: row.行号})
MERGE (s:供应商 {名称: row.供应商})
MERGE (s)-[:中标]->(t)
"""

QUERY_PLACE = """
UNWIND $rows AS row
MATCH (t:交易 {行号: row.行号})
MERGE (p:所在地 {名称: row.所在地})
MERGE (t)-[:发生地]->(p)
"""

QUERY_AGENCY = """
UNWIND $rows AS row
MATCH (t:交易 {行号: row.行号})
MERGE (a:代理机构 {名称: row.代理机构})
MERGE (a)-[:代理]->(t)
"""

# 交易频次是标的物**节点**上的属性，不是边——它描述的是这个标的物整体被采购过多少次
QUERY_FREQ = """
UNWIND $rows AS row
MATCH (i:标的物 {名称: row.标的物})
  SET i.交易频次 = row.交易频次
"""


def load_env() -> dict[str, str]:
    """读 .env 里的 NEO4J_* 配置。

    手写解析，和 run_batch.load_api_key() 一个路子——为了几个键引 python-dotenv 不值当。
    环境变量优先，方便临时指向别的库而不改文件。
    """
    env: dict[str, str] = {}
    env_file = PROJECT_ROOT / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            env[name.strip()] = value.strip().strip('"').strip("'")

    for key in ("NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE",
                "NEO4J_CONNECTION_TIMEOUT", "NEO4J_MAX_CONNECTION_LIFETIME",
                "NEO4J_MAX_CONNECTION_POOL_SIZE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def require(env: dict[str, str], key: str) -> str:
    value = env.get(key, "").strip()
    if not value:
        print(
            f"[错误] 缺少 {key}。\n"
            f"       请在 {PROJECT_ROOT / '.env'} 里补上一行：{key}=...",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return value


def read_csv(path: Path, columns: list[str]) -> list[dict]:
    """读 CSV，空单元格一律转成 None。

    必须转干净：MERGE (a:代理机构 {名称: ''}) 会建出一个**名字是空串**的节点，
    把 7698 行没有代理机构的记录全挂到它上面——图上多出一个巨大的幽灵节点。

    而且不能只判 `is None` 就算完：pandas 读空单元格给的是 NaN 或空串，
    而 **float('nan') 在 Python 里是真值**，下游 `if row["代理机构"]` 会全部放行。
    所以这里统一成 None，让判空只剩一种写法。

    keep_default_na=False：让 pandas 别自作主张把 "NA"、"null" 这类字面量也当成空值——
    企业名里真出现这种字符串的话，那是个正经名字，不是缺失。
    """
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in columns if c not in df.columns]
    if missing:
        print(f"[错误] {path.name} 里缺少列 {missing}，实际列：{list(df.columns)}", file=sys.stderr)
        raise SystemExit(1)

    rows: list[dict] = []
    for record in df[columns].to_dict("records"):
        clean: dict = {}
        for col in columns:
            value = record[col]
            text = "" if value is None else str(value).strip()
            clean[col] = text or None
        rows.append(clean)
    return rows


def as_amount(value):
    """交易额转成 float，空值返回 None。"""
    if value is None or str(value).strip() == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def as_int(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def batched(rows: list[dict], size: int):
    for start in range(0, len(rows), size):
        yield start, rows[start : start + size]


def report(session) -> None:
    """打印各标签的节点数、各类型的关系数。"""
    print("\n  节点：")
    for row in session.run(
        "MATCH (n) RETURN labels(n)[0] AS 标签, count(*) AS 数量 ORDER BY 数量 DESC"
    ).data():
        print(f"    {row['标签']:10} {row['数量']:>6}")
    print("  关系：")
    for row in session.run(
        "MATCH ()-[r]->() RETURN type(r) AS 类型, count(*) AS 数量 ORDER BY 数量 DESC"
    ).data():
        print(f"    {row['类型']:10} {row['数量']:>6}")


def main() -> int:
    parser = argparse.ArgumentParser(description="把知识图谱 CSV 导入 Neo4j")
    parser.add_argument("-g", "--graph", type=Path, default=DEFAULT_KG_CSV, help="图谱 CSV")
    parser.add_argument("-f", "--freq", type=Path, default=DEFAULT_FREQ_CSV, help="交易频次 CSV")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"每批行数（默认 {BATCH_SIZE}）")
    parser.add_argument("--limit", type=int, default=0, help="只导前 N 行（0 = 全量），用于冒烟测试")
    parser.add_argument("--dry-run", action="store_true", help="只解析和报告，不连库、不写入")
    parser.add_argument("--clear", action="store_true", help="先删掉库里所有节点和关系")
    parser.add_argument("--database", default=None, help="覆盖 .env 里的 NEO4J_DATABASE")
    args = parser.parse_args()

    for path, label in ((args.graph, "图谱 CSV"), (args.freq, "频次 CSV")):
        if not path.exists():
            print(f"[错误] 找不到{label}：{path}", file=sys.stderr)
            return 1

    rows = read_csv(args.graph, KG_COLUMNS)
    freq_rows = read_csv(args.freq, [ITEM_COLUMN, FREQ_COLUMN])
    # 行号来自 CSV 里的行序。MERGE 拿它当交易节点的键，所以重跑时同样的行会落到
    # 同一个节点上（幂等）。副作用是：CSV 中间插/删一行，后面所有行的「交易」节点
    # 都会认到别的行上去——所以换数据源时要配 --clear，别指望 MERGE 能自己认出来
    for i, row in enumerate(rows):
        row["行号"] = i
        row["交易额"] = as_amount(row["交易额"])
    for row in freq_rows:
        row[FREQ_COLUMN] = as_int(row[FREQ_COLUMN])

    if args.limit:
        rows = rows[: args.limit]

    # ---------- 解析阶段的报告 ----------
    print(f"图谱 CSV：{args.graph}")
    print(f"  读取行数：{len(rows)}" + (f"（--limit {args.limit} 截断）" if args.limit else ""))
    for column in KG_COLUMNS:
        blank = sum(1 for r in rows if r[column] is None)
        if blank:
            print(f"    {column}：{blank} 行为空" + ("（跳过，不建节点）" if column in ("代理机构", "所在地") else ""))

    print(f"频次 CSV：{args.freq}")
    print(f"  读取行数：{len(freq_rows)}")

    if args.dry_run:
        print("\n[--dry-run] 未连接数据库、未写入。")
        print("  预计写入：")
        print(f"    交易节点      {len(rows)}")
        print(f"    采购商节点    {len({r['采购商'] for r in rows if r['采购商']})}")
        print(f"    供应商节点    {len({r['供应商'] for r in rows if r['供应商']})}")
        print(f"    代理机构节点  {len({r['代理机构'] for r in rows if r['代理机构']})}")
        print(f"    标的物节点    {len({r['标的物'] for r in rows if r['标的物']})}")
        print(f"    所在地节点    {len({r['所在地'] for r in rows if r['所在地']})}")
        print(f"    关系          采购 {len(rows)} / 中标 {sum(1 for r in rows if r['供应商'])} / "
              f"代理 {sum(1 for r in rows if r['代理机构'])} / 标的 {len(rows)} / "
              f"发生地 {sum(1 for r in rows if r['所在地'])}")
        return 0

    # ---------- 连接 ----------
    from neo4j import GraphDatabase

    env = load_env()
    uri = require(env, "NEO4J_URI")
    database = args.database or require(env, "NEO4J_DATABASE")
    driver = GraphDatabase.driver(
        uri,
        auth=(require(env, "NEO4J_USERNAME"), require(env, "NEO4J_PASSWORD")),
        connection_timeout=int(env.get("NEO4J_CONNECTION_TIMEOUT") or 60),
        max_connection_lifetime=int(env.get("NEO4J_MAX_CONNECTION_LIFETIME") or 3600),
        max_connection_pool_size=int(env.get("NEO4J_MAX_CONNECTION_POOL_SIZE") or 50),
    )

    try:
        driver.verify_connectivity()
    except Exception as exc:
        print(f"[错误] 连不上 {uri}：{type(exc).__name__}: {str(exc)[:200]}", file=sys.stderr)
        return 1

    print(f"\n已连接：{uri}  库={database}")

    with driver.session(database=database) as session:
        existing = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]

        if args.clear:
            print(f"  --clear：删除现有 {existing} 个节点及其关系")
            session.run("MATCH (n) DETACH DELETE n")
        elif existing:
            # 不拦：下面的 MERGE 是幂等的，重跑只会补齐、不会翻倍。
            # 但要让人知道库里本来就有东西，免得误以为是这次导入的结果
            print(f"  [提示] 库里已有 {existing} 个节点，本次是 MERGE 叠加（不会重复计数）")

        # ---------- 约束 ----------
        # 必须在写入前建：唯一约束自带索引，MERGE 找「这个名称的节点在不在」
        # 才有索引可走；否则 8751 行每行都在全表扫，慢得没边
        for name, label, prop in CONSTRAINTS:
            session.run(
                f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE"
            )
        print(f"  约束：已确保 {len(CONSTRAINTS)} 个")

        # ---------- 写入 ----------
        counters = {"主": 0, "供应商": 0, "所在地": 0, "代理机构": 0, "频次": 0}
        total_batches = (len(rows) + args.batch_size - 1) // args.batch_size

        for batch_no, (start, chunk) in enumerate(batched(rows, args.batch_size), start=1):
            session.run(QUERY_MAIN, rows=chunk)
            counters["主"] += len(chunk)

            suppliers = [r for r in chunk if r["供应商"]]
            if suppliers:
                session.run(QUERY_SUPPLIER, rows=suppliers)
                counters["供应商"] += len(suppliers)

            places = [r for r in chunk if r["所在地"]]
            if places:
                session.run(QUERY_PLACE, rows=places)
                counters["所在地"] += len(places)

            agencies = [r for r in chunk if r["代理机构"]]
            if agencies:
                session.run(QUERY_AGENCY, rows=agencies)
                counters["代理机构"] += len(agencies)

            print(f"  交易 {batch_no}/{total_batches}（{start + len(chunk)}/{len(rows)} 行）", end="\r")

        print(f"  交易 {total_batches}/{total_batches}（{len(rows)}/{len(rows)} 行）")

        # ---------- 交易频次 ----------
        for _, chunk in batched(freq_rows, args.batch_size):
            session.run(QUERY_FREQ, rows=chunk)
            counters["频次"] += len(chunk)

        print(f"  写入完成：主查询 {counters['主']} 行，供应商 {counters['供应商']} 行，"
              f"所在地 {counters['所在地']} 行，代理机构 {counters['代理机构']} 行，"
              f"频次 {counters['频次']} 行")

        report(session)

        # 频次没挂上的标的物 = 两个 CSV 对不齐，属于静默失败，必须报出来
        orphans = session.run(
            "MATCH (i:标的物) WHERE i.交易频次 IS NULL RETURN count(i) AS c"
        ).single()["c"]
        if orphans:
            print(f"  [警告] {orphans} 个标的物节点没有交易频次（两份 CSV 不一致）")
        else:
            print("  [通过] 所有标的物节点都带上了交易频次")

    driver.close()
    print("\n完成。在 Neo4j Browser 里试试：")
    print('  MATCH (b:采购商)-[:采购]->(t:交易)-[:标的]->(i:标的物) RETURN * LIMIT 25')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
