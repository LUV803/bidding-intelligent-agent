"""选择 CSV 文件，把知识图谱注入 Neo4j（英文标签版）。

CSV 1（标的物_交易频次.csv）
    (:SubjectMatter {name, tradeFrequency})

CSV 2（招标采购标的物信息提取训练数据_2_知识图谱.csv）
    (:SubjectMatter {name})          (:Transaction {id, amount})
    (:Purchaser {name})              (:Agency {name})
    (:Location {name})               (:Supplier {name})
    (:SubjectMatter)-[:HAS_TRANSACTION]->(:Transaction)
    (:SubjectMatter)-[:PURCHASED_BY]->(:Purchaser)
    (:SubjectMatter)-[:AGENT_BY]->(:Agency)
    (:SubjectMatter)-[:LOCATED_AT]->(:Location)
    (:SubjectMatter)-[:SUPPLIED_BY]->(:Supplier)

用法：
    uv run python batch/to_neo4j.py                    # 弹窗选文件，注入本地实例
    uv run python batch/to_neo4j.py a.csv b.csv        # 直接给路径，不弹窗
    uv run python batch/to_neo4j.py --dry-run          # 只解析并报告，不入库
    uv run python batch/to_neo4j.py --database kg      # 换库

⚠ 这个模型回答不了「这家供应商中了多少钱」——**实测能错 24 倍**。
   所有当事方都挂在**标的物**上，不挂交易。「打印/复印纸」这一个节点同时连着
   202 家供应商和 236 笔交易，但边里没有「哪笔交易属于谁」的信息，那个关联在这个
   模型里**存不下来**。于是：

       MATCH (i:SubjectMatter)-[:SUPPLIED_BY]->(s), (i)-[:HAS_TRANSACTION]->(t)
       RETURN s.name, sum(t.amount)      // 错

   这是笛卡尔积：每个标的物下「所有供应商 × 所有交易」两两相乘。

   注意**总量看着还行、单体却错得离谱**，别被总量骗了（2026-09-17 实测）：
     · 全局合计只虚高 1.6 倍——因为大多数标的物只出现过一次，把倍数稀释掉了
     · 单个实体：某信息科技有限公司真实中标 1 笔 51,750 元，
       而上面那个查询会给它算出 1,234,147.84 元（«打印/复印纸» 全部 236 笔）—— **24 倍**
     · 标的物越热门、该实体的真实份额越小，错得越狠

   要按供应商/采购商算钱，必须让当事方挂在 :Transaction 上（见
   batch/知识图谱_导入Neo4j.py 的「以交易为中心」模型）。

⚠ 别和 batch/知识图谱_导入Neo4j.py 建的那套混在同一个库里
   那个脚本建的是**中文标签**（:标的物 / :交易 / :采购商 …）且以交易为中心，
   和这里的英文标签是两套词汇、两个形状。同一个库里跑两遍，同名实体会有
   :标的物 和 :SubjectMatter 两份，节点数看着像翻倍，查起来两套都得记得。
   本机现状（2026-09-17 起）：英文那套**已从库 neo4j 里清掉**（19161 节点 + 6 个
   en_* 约束），且该库已用 dbms.recreateDatabase 重建存储，连属性键表里那 4 个
   英文孤儿键（name/amount/id/tradeFrequency）都不剩。所以再跑本脚本会把英文那套
   重新建出来——而且是**连键带节点一起**，要清理就得再重建一次存储。
   除非是有意为之，否则用 --database 指到别的库，别让它又进 neo4j。

⚠ 凭据从 .env 的 NEO4J_* 读，不要写进命令行——会留在 shell 历史里。
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

# ---------- 目标实例 ----------
# 只连本地（本机跑的是 Neo4j Kernel 2026.08.1）：bolt 明文，没有云端的
# 免费档暂停/删除机制，也不用维护第二套凭据。
LOCAL_URI = "bolt://localhost:7687"
LOCAL_DATABASE = "neo4j"

# 选文件时的默认目录。用户习惯把成品放 data/processed，但这两个 CSV 实际在 batch/结果，
# 两个地方都找一遍，找不到再退到项目根目录
CANDIDATE_DIRS = [
    RESULT_DIR,
    PROJECT_ROOT / "data" / "processed",
    PROJECT_ROOT / "data" / "batch",
    PROJECT_ROOT,
]

# ---------- CSV 列名 ----------
CSV_SUBJECT = "标的物"
CSV_FREQ = "交易频次"
CSV_PURCHASER = "采购商"
CSV_AGENCY = "代理机构"
CSV_SUPPLIER = "供应商"
CSV_AMOUNT = "交易额"
CSV_LOCATION = "所在地"

# 按列名认文件，**不按文件名**——选文件是人工操作，顺序和名字都靠不住
KG_REQUIRED = [CSV_SUBJECT, CSV_PURCHASER, CSV_SUPPLIER, CSV_AMOUNT]
FREQ_REQUIRED = [CSV_SUBJECT, CSV_FREQ]

# 每批 UNWIND 多少行
BATCH_SIZE = 500

# (约束名, 标签, 属性)
#
# ⚠ 约束名在**整个数据库**里全局唯一，不是按标签分区的。
#   这里原本叫 purchaser_name / supplier_name / agency_name，和
#   batch/知识图谱_导入Neo4j.py（中文标签那套）的**约束名重名**。那个脚本先跑过，
#   于是 :Purchaser / :Supplier / :Agency 既没有唯一约束、也没有配套索引，
#   MERGE 每行退化成全标签扫描（图小看不出来，图大了是灾难）。
#
# ⚠ IF NOT EXISTS 的判据有**两条**，不是一条：
#     ① 同名约束已存在          → 跳过
#     ② 已存在**等价**约束（同标签 + 同属性 + 同类型）→ 也跳过
#   第②条更阴：改名重建时，旧名还没删，新名就会被判成「已有等价的」而跳过；
#   等旧名一删，这个标签就**裸奔**了。改约束名务必「先建后删」，且删前确认
#   新约束真的建出来了（SHOW CONSTRAINTS 数一遍），别信它没报错就没事。
#
#   加 en_ 前缀是为了①；② 靠上面那个操作顺序兜住。
CONSTRAINTS: list[tuple[str, str, str]] = [
    ("en_subject_matter_name", "SubjectMatter", "name"),
    ("en_transaction_id", "Transaction", "id"),
    ("en_purchaser_name", "Purchaser", "name"),
    ("en_agency_name", "Agency", "name"),
    ("en_location_name", "Location", "name"),
    ("en_supplier_name", "Supplier", "name"),
]

# ---------- 写入用 Cypher ----------
# 标的物和采购商在这份数据里没有空值，所以主查询不判空
Q_DEALS = """
UNWIND $rows AS row
MERGE (i:SubjectMatter {name: row.name})
MERGE (t:Transaction {id: row.id})
  SET t.amount = row.amount
MERGE (i)-[:HAS_TRANSACTION]->(t)
MERGE (p:Purchaser {name: row.purchaser})
MERGE (i)-[:PURCHASED_BY]->(p)
"""

# 供应商也没有空值
Q_SUPPLIERS = """
UNWIND $rows AS row
MATCH (i:SubjectMatter {name: row.name})
MERGE (s:Supplier {name: row.supplier})
MERGE (i)-[:SUPPLIED_BY]->(s)
"""

# 代理机构和所在地有空值，调用方只把非空的传进来
Q_AGENCIES = """
UNWIND $rows AS row
MATCH (i:SubjectMatter {name: row.name})
MERGE (a:Agency {name: row.agency})
MERGE (i)-[:AGENT_BY]->(a)
"""

Q_LOCATIONS = """
UNWIND $rows AS row
MATCH (i:SubjectMatter {name: row.name})
MERGE (l:Location {name: row.location})
MERGE (i)-[:LOCATED_AT]->(l)
"""

# 用 MERGE 而不是 MATCH：只选频次 CSV 一个文件时标的物节点还不存在，
# 这样也能建出来，两个文件的先后顺序就不重要了
Q_FREQ = """
UNWIND $rows AS row
MERGE (i:SubjectMatter {name: row.name})
  SET i.tradeFrequency = row.tradeFrequency
"""


def load_env() -> dict[str, str]:
    """读 .env。手写解析，和 run_batch.load_api_key() 一个路子。

    环境变量优先，方便临时换实例而不改文件。
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

    for key in (
        "NEO4J_URI", "NEO4J_USERNAME", "NEO4J_PASSWORD", "NEO4J_DATABASE",
        "NEO4J_CONNECTION_TIMEOUT", "NEO4J_MAX_CONNECTION_LIFETIME",
        "NEO4J_MAX_CONNECTION_POOL_SIZE",
    ):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def resolve_credentials(args) -> dict[str, str]:
    """命令行 > .env > 内置默认。"""
    env = load_env()
    return {
        "uri": args.uri or env.get("NEO4J_URI") or LOCAL_URI,
        "database": args.database or env.get("NEO4J_DATABASE") or LOCAL_DATABASE,
        "username": args.username or env.get("NEO4J_USERNAME", "neo4j"),
        "password": args.password or env.get("NEO4J_PASSWORD", ""),
    }


def pick_files() -> list[Path]:
    """弹出文件选择框。"""
    try:
        from tkinter import Tk, filedialog
    except ImportError:
        print("[错误] 这个 Python 没有 tkinter，无法弹窗选文件。\n"
              "       请改成把 CSV 路径直接写在命令行上。", file=sys.stderr)
        raise SystemExit(1)

    initial = next((d for d in CANDIDATE_DIRS if d.exists()), PROJECT_ROOT)

    root = Tk()
    root.withdraw()                 # 只留对话框，不要那个空白主窗口
    root.attributes("-topmost", True)  # 否则对话框容易藏在终端后面
    try:
        chosen = filedialog.askopenfilenames(
            title="选择要注入的 CSV（可多选）",
            initialdir=str(initial),
            filetypes=[("CSV 文件", "*.csv"), ("全部文件", "*.*")],
        )
    finally:
        root.destroy()
    return [Path(p) for p in chosen]


def classify(path: Path) -> str | None:
    """按列名判断这是哪个 CSV。返回 'kg' / 'freq' / None。"""
    try:
        columns = list(pd.read_csv(path, nrows=0).columns)
    except Exception:
        return None
    if all(c in columns for c in FREQ_REQUIRED):
        return "freq"
    if all(c in columns for c in KG_REQUIRED):
        return "kg"
    return None


def read_rows(path: Path, columns: list[str]) -> list[dict]:
    """读 CSV，空单元格一律转成 None。

    必须转干净：MERGE (a:Agency {name: ''}) 会建出一个**名字是空串**的节点，
    把 7698 行没有代理机构的记录全挂上去。
    而且不能只判 `is None`：pandas 读空单元格给的是 NaN 或空串，
    而 **float('nan') 在 Python 里是真值**，下游判空会全部放行。

    keep_default_na=False：别把 "NA"、"null" 这类字面量也当成空值——
    企业名里真出现这种字符串的话，那是个正经名字。
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


def as_float(value):
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


def build_deal_rows(path: Path) -> list[dict]:
    """CSV 2 → 每行一条交易。属性名转成英文，Cypher 那边好读。"""
    raw = read_rows(path, [CSV_SUBJECT, CSV_PURCHASER, CSV_AGENCY,
                           CSV_SUPPLIER, CSV_AMOUNT, CSV_LOCATION])
    rows = []
    for i, r in enumerate(raw):
        rows.append({
            # id 用行序：它是 :Transaction 的唯一键，重跑时同样的行落到同一个节点上（幂等）
            "id": i,
            "name": r[CSV_SUBJECT],
            "amount": as_float(r[CSV_AMOUNT]),
            "purchaser": r[CSV_PURCHASER],
            "agency": r[CSV_AGENCY],
            "supplier": r[CSV_SUPPLIER],
            "location": r[CSV_LOCATION],
        })
    return rows


def build_freq_rows(path: Path) -> list[dict]:
    """CSV 1 → 标的物 + 交易频次。"""
    raw = read_rows(path, [CSV_SUBJECT, CSV_FREQ])
    return [{"name": r[CSV_SUBJECT], "tradeFrequency": as_int(r[CSV_FREQ])} for r in raw]


def batched(rows: list[dict], size: int):
    for start in range(0, len(rows), size):
        yield start, rows[start:start + size]


def report(session) -> None:
    print("\n  节点：")
    for row in session.run(
        "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC"
    ).data():
        print(f"    {row['label']:15} {row['c']:>6}")
    print("  关系：")
    for row in session.run(
        "MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c ORDER BY c DESC"
    ).data():
        print(f"    {row['t']:18} {row['c']:>6}")


def main() -> int:
    parser = argparse.ArgumentParser(description="选择 CSV 并注入 Neo4j")
    parser.add_argument("files", nargs="*", type=Path, help="CSV 路径（不给就弹窗选）")
    parser.add_argument("--uri", default=None, help="覆盖连接 URI")
    parser.add_argument("--database", default=None, help="覆盖数据库名")
    parser.add_argument("--username", default=None, help="覆盖用户名")
    parser.add_argument("--password", default=None,
                        help="覆盖密码。⚠ 会留在 shell 历史里，优先用 .env")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"每批行数（默认 {BATCH_SIZE}）")
    parser.add_argument("--limit", type=int, default=0, help="只导前 N 行交易（0 = 全量）")
    parser.add_argument("--dry-run", action="store_true", help="只解析和报告，不连库、不写入")
    parser.add_argument("--clear", action="store_true", help="先删掉库里所有节点和关系")
    args = parser.parse_args()

    # ---------- 选文件 ----------
    paths = [p for p in args.files] or pick_files()
    if not paths:
        print("没有选文件，退出。")
        return 1

    deals_path, freq_path = None, None
    for path in paths:
        if not path.exists():
            print(f"[错误] 找不到：{path}", file=sys.stderr)
            return 1
        kind = classify(path)
        if kind == "freq":
            if freq_path:
                print(f"[警告] 已有一个频次 CSV（{freq_path.name}），忽略 {path.name}", file=sys.stderr)
                continue
            freq_path = path
        elif kind == "kg":
            if deals_path:
                print(f"[警告] 已有一个交易 CSV（{deals_path.name}），忽略 {path.name}", file=sys.stderr)
                continue
            deals_path = path
        else:
            print(f"[错误] 认不出 {path.name} 是哪种 CSV。\n"
                  f"       交易表要有 {KG_REQUIRED}，频次表要有 {FREQ_REQUIRED}。\n"
                  f"       实际列：{list(pd.read_csv(path, nrows=0).columns)}", file=sys.stderr)
            return 1

    deals = build_deal_rows(deals_path) if deals_path else []
    freqs = build_freq_rows(freq_path) if freq_path else []
    if args.limit:
        deals = deals[: args.limit]

    # ---------- 解析报告 ----------
    print("选择文件：")
    if deals_path:
        print(f"  [交易] {deals_path}")
        print(f"          {len(deals)} 行")
        for col, key in ((CSV_AGENCY, "agency"), (CSV_LOCATION, "location"),
                         (CSV_AMOUNT, "amount")):
            blank = sum(1 for d in deals if d[key] is None)
            if blank:
                print(f"          {col}：{blank} 行为空"
                      + ("（跳过，不建节点）" if key in ("agency", "location") else ""))
    if freq_path:
        print(f"  [频次] {freq_path}")
        print(f"          {len(freqs)} 行")

    def n_distinct(key: str) -> int:
        return len({d[key] for d in deals if d[key]})

    if args.dry_run:
        print("\n[--dry-run] 未连接数据库、未写入。")
        if deals:
            print("  预计写入：")
            print(f"    :SubjectMatter   {n_distinct('name')}")
            print(f"    :Transaction     {len(deals)}")
            print(f"    :Purchaser       {n_distinct('purchaser')}")
            print(f"    :Supplier        {n_distinct('supplier')}")
            print(f"    :Agency          {n_distinct('agency')}")
            print(f"    :Location        {n_distinct('location')}")
            print(f"    :HAS_TRANSACTION {len(deals)}（每条交易一条边）")
            print(f"    :PURCHASED_BY    {len({(d['name'], d['purchaser']) for d in deals})}（去重后）")
            print(f"    :SUPPLIED_BY     {len({(d['name'], d['supplier']) for d in deals})}（去重后）")
            print(f"    :AGENT_BY        {len({(d['name'], d['agency']) for d in deals if d['agency']})}（去重后）")
            print(f"    :LOCATED_AT      {len({(d['name'], d['location']) for d in deals if d['location']})}（去重后）")
        if freqs:
            print(f"    :SubjectMatter.tradeFrequency  {len(freqs)} 行的频次属性")
        return 0

    # ---------- 连接 ----------
    from neo4j import GraphDatabase

    cred = resolve_credentials(args)
    if not cred["password"]:
        print(f"[错误] 没有密码。请在 .env 里补一行：\n"
              f"         NEO4J_PASSWORD=...\n"
              f"       （文件在 {PROJECT_ROOT / '.env'}，已被 .gitignore 覆盖）",
              file=sys.stderr)
        return 1

    env = load_env()
    driver = GraphDatabase.driver(
        cred["uri"],
        auth=(cred["username"], cred["password"]),
        connection_timeout=int(env.get("NEO4J_CONNECTION_TIMEOUT") or 60),
        max_connection_lifetime=int(env.get("NEO4J_MAX_CONNECTION_LIFETIME") or 3600),
        max_connection_pool_size=int(env.get("NEO4J_MAX_CONNECTION_POOL_SIZE") or 50),
    )

    try:
        driver.verify_connectivity()
    except Exception as exc:
        name = type(exc).__name__
        print(f"[错误] 连不上 {cred['uri']}（用户 {cred['username']}）", file=sys.stderr)
        print(f"       {name}: {str(exc)[:300]}", file=sys.stderr)
        if "Auth" in name or "Unauthorized" in str(exc):
            print(f"\n       认证失败。核对 .env 里的 NEO4J_USERNAME / NEO4J_PASSWORD\n"
                  f"         （本地实例装完第一次登录用的是默认的 neo4j / neo4j，之后会强制改密）",
                  file=sys.stderr)
        return 1

    print(f"\n已连接：{cred['uri']}  库={cred['database']}")

    with driver.session(database=cred["database"]) as session:
        existing = session.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        if args.clear:
            print(f"  --clear：删除现有 {existing} 个节点及其关系")
            session.run("MATCH (n) DETACH DELETE n")
        elif existing:
            # 不拦：下面的 MERGE 幂等，重跑只补齐不翻倍。但要让人知道库里本来就有东西
            print(f"  [提示] 库里已有 {existing} 个节点，本次是 MERGE 叠加（不会重复计数）")

        # 唯一约束要在写入前建：它自带索引，MERGE 找「这个名称的节点在不在」
        # 才有索引可走；否则每行都全表扫
        for name, label, prop in CONSTRAINTS:
            session.run(f"CREATE CONSTRAINT {name} IF NOT EXISTS "
                        f"FOR (n:{label}) REQUIRE n.{prop} IS UNIQUE")
        print(f"  约束：已确保 {len(CONSTRAINTS)} 个")

        # ---------- 写入 ----------
        if deals:
            written = 0
            total = (len(deals) + args.batch_size - 1) // args.batch_size
            for no, (start, chunk) in enumerate(batched(deals, args.batch_size), start=1):
                session.run(Q_DEALS, rows=chunk)
                written += len(chunk)

                part = [r for r in chunk if r["supplier"]]
                if part:
                    session.run(Q_SUPPLIERS, rows=part)
                part = [r for r in chunk if r["agency"]]
                if part:
                    session.run(Q_AGENCIES, rows=part)
                part = [r for r in chunk if r["location"]]
                if part:
                    session.run(Q_LOCATIONS, rows=part)

                print(f"  交易 {no}/{total}（{written}/{len(deals)} 行）", end="\r")
            print(f"  交易 {total}/{total}（{len(deals)}/{len(deals)} 行）")

        if freqs:
            for _, chunk in batched(freqs, args.batch_size):
                session.run(Q_FREQ, rows=chunk)
            print(f"  频次：写入 {len(freqs)} 行")

        report(session)

        if freqs:
            orphans = session.run(
                "MATCH (i:SubjectMatter) WHERE i.tradeFrequency IS NULL RETURN count(i) AS c"
            ).single()["c"]
            if orphans:
                print(f"  [警告] {orphans} 个标的物没有交易频次（两份 CSV 对不上）")
            else:
                print("  [通过] 所有标的物都带上了交易频次")

    driver.close()
    print("\n完成。在 Neo4j Browser 里试试：")
    print("  MATCH (i:SubjectMatter)-[:SUPPLIED_BY]->(s:Supplier) "
          "RETURN i.name, collect(s.name)[..5] LIMIT 10")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
