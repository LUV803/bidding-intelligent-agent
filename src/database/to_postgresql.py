"""选择 XLSX 文件，把整表注入 PostgreSQL。

默认目标：batch/结果/招标采购标的物信息提取训练数据_2_标的物.xlsx（8751 行 × 19 列）
          → 库 chatbot 的 bid_records 表（**一张平表**，列名照搬 Excel）

用法：
    uv run python src/database/to_postgresql.py                # 弹窗选文件
    uv run python src/database/to_postgresql.py a.xlsx         # 直接给路径，不弹窗
    uv run python src/database/to_postgresql.py --dry-run      # 只解析并报告，不连库
    uv run python src/database/to_postgresql.py --limit 100    # 只灌前 100 行
    uv run python src/database/to_postgresql.py --keep         # 追加而不是先清空
    uv run python src/database/to_postgresql.py -d otherdb     # 换库（不存在则自动建）

⚠ **默认是整表替换**（TRUNCATE + COPY），不是追加。重跑一次的结果与第一次完全相同，
   不会翻倍——这是刻意的：这份 xlsx 是快照，且没有可靠的业务主键
   （「项目编号」有 116 行为空，做不了主键），没法做 upsert。
   要追加就显式加 --keep。

⚠ 换列就要重建表。`CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，**不会**补列；
   下面 check_schema() 会在列对不上时拒绝执行（COPY 按列名错位是静默的，必须挡住）。

⚠ 「核心标的物」与「标的物」是**两列不同的东西**：前者是模型抽取的原始值，
   后者是 batch/提数据_插标的物.py 之后插进去的。两列都入库，不做取舍。

⚠ 凭据从 .env 的 POSTGRES_* 读，不要写进命令行——会留在 shell 历史里。

跑完自己会复核一遍（行数 / 每列空值数 / 金额合计），但那只是自证；要独立确认就用 psql：

    psql -h localhost -U postgres -d chatbot -c 'select count(*) from bid_records;'
"""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

import pandas as pd
import psycopg
from psycopg import sql

# Windows 控制台默认 GBK，中文 print 会直接抛 UnicodeEncodeError
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ⚠ 本文件在 <项目根>/src/database/ 下，parents[2] 才是项目根。
#   batch/ 下的脚本（to_neo4j.py 等）在 <项目根>/batch/，那边是 parents[1]。
#   写错的表现是 .env 和默认 xlsx 全都找不到，而且不报错、只是路径全空。
PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULT_DIR = PROJECT_ROOT / "batch" / "结果"

DEFAULT_XLSX = RESULT_DIR / "招标采购标的物信息提取训练数据_2_标的物.xlsx"

# 弹窗选文件时的默认目录。目标文件实际在 batch/结果（用户可能记成 data/processed），
# 几处都找一遍，都不在再退到项目根
CANDIDATE_DIRS = [
    RESULT_DIR,
    PROJECT_ROOT / "data" / "processed",
    PROJECT_ROOT / "data" / "raw",
    PROJECT_ROOT,
]

# ---------- 目标 ----------
TABLE = "bid_records"
# 只在 .env 里没有 POSTGRES_DATABASE 时兜底。正常走 .env（见 resolve_config）
DEFAULT_DATABASE = "chatbot"

# ---------- 列定义 ----------
# 类型常量：既用于生成 DDL，也用于挑选转换函数（CONVERTERS），一处定义两处用
TEXT, NUM, DATE, TS = "text", "numeric(18,2)", "date", "timestamp"

# (列名, 类型)。**顺序即写入顺序**，DDL 与 COPY 的列清单都从这里生成，不手抄两遍
COLUMN_TYPES: list[tuple[str, str]] = [
    ("标题", TEXT),
    ("类别", TEXT),
    ("来源", TEXT),
    ("发布时间", DATE),
    ("省份", TEXT),
    ("市区", TEXT),
    ("县城", TEXT),
    ("项目编号", TEXT),
    ("项目名称", TEXT),
    ("采购人", TEXT),
    ("代理机构", TEXT),
    ("预算", NUM),
    ("项目地址", TEXT),
    ("周期", TEXT),
    ("中标人", TEXT),
    ("中标金额", NUM),
    ("中标时间", TS),
    ("核心标的物", TEXT),
    ("标的物", TEXT),
]

COLUMNS = [name for name, _ in COLUMN_TYPES]

# 非空约束只加在这四列上：它们既是每行都必须有的标识性字段，实测空值也确实为 0。
# 其余列哪怕当前为空值 0（如省份、中标金额），也不加——写死了以后会挡住合法数据。
NOT_NULL = {"标题", "项目名称", "采购人", "中标人"}

# COPY 每批多少行
BATCH_SIZE = 1000


# ---------------------------------------------------------------- 值转换
def _na(value) -> bool:
    """空值判定：None / NaN / NaT 都算。

    ⚠ 不能只判 `is None`。pandas 的空单元格给的是 NaN（数值列）或 NaT（日期列），
      而 **float('nan') 在 Python 里是真值** —— 漏判的表现是库里出现字符串 'nan'，
      不报错，只是查询结果莫名其妙。
    """
    if value is None:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):  # 数组之类的 pd.isna 会返回数组，不是标量
        return False


def as_text(value) -> str | None:
    if _na(value):
        return None
    text = str(value).strip()
    return text or None


def as_numeric(value) -> Decimal | None:
    """转 Decimal 而不是 float。

    numeric 列用 Decimal 是精确的；float 会有二进制表示误差，金额列上不该有。
    str() 这一步同时解决了 numpy 标量的问题——psycopg 不认 numpy.float64。
    """
    if _na(value):
        return None
    return Decimal(str(value)).quantize(Decimal("0.01"))


def as_date(value) -> date | None:
    """只要日期。pandas 的 Timestamp 是 datetime 的子类，这里取 .date()。"""
    if _na(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.to_datetime(value).date()


def as_timestamp(value) -> datetime | None:
    """带时间的完整值（实测中标时间带毫秒，如 2023-10-13 18:18:30.213）。"""
    if _na(value):
        return None
    if isinstance(value, datetime):
        # to_pydatetime() 拿到的是纯 Python datetime，不带 pandas/numpy 的类型
        return value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
    return pd.to_datetime(value).to_pydatetime()


CONVERTERS = {TEXT: as_text, NUM: as_numeric, DATE: as_date, TS: as_timestamp}


# ---------------------------------------------------------------- 配置
def load_env() -> dict[str, str]:
    """读 .env。手写解析，和 batch/to_neo4j.py:168 一个路子。

    刻意不走 bidding_qa.config 的 Settings：那个类 extra="ignore"，
    .env 里写了、config.py 没声明的键会被**静默丢弃**（NEO4J_* 那 7 个键就这么
    白写了很久）。独立脚本用不到服务端配置，自己解析更省事也更不容易踩坑。

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

    for key in ("POSTGRES_HOST", "POSTGRES_PORT", "POSTGRES_USER",
                "POSTGRES_PASSWORD", "POSTGRES_DATABASE"):
        if os.environ.get(key):
            env[key] = os.environ[key]
    return env


def resolve_config(args) -> dict:
    """命令行 > .env > 内置默认。"""
    env = load_env()
    return {
        "host": args.host or env.get("POSTGRES_HOST") or "localhost",
        "port": int(args.port or env.get("POSTGRES_PORT") or 5432),
        "user": args.user or env.get("POSTGRES_USER") or "postgres",
        "password": args.password or env.get("POSTGRES_PASSWORD") or "",
        "database": args.database or env.get("POSTGRES_DATABASE") or DEFAULT_DATABASE,
    }


def conninfo(cfg: dict, database: str) -> dict:
    """psycopg.connect 的参数。密码走这里而不是命令行，免得留在 shell 历史里。"""
    return {
        "host": cfg["host"],
        "port": cfg["port"],
        "user": cfg["user"],
        "password": cfg["password"],
        "dbname": database,
        "connect_timeout": 10,
    }


# ---------------------------------------------------------------- 选文件
def pick_file() -> Path | None:
    """弹出文件选择框。取消返回 None。"""
    try:
        from tkinter import Tk, filedialog
    except ImportError:
        print("[错误] 这个 Python 没有 tkinter，无法弹窗选文件。\n"
              "       请改成把 xlsx 路径直接写在命令行上。", file=sys.stderr)
        raise SystemExit(1)

    initial = next((d for d in CANDIDATE_DIRS if d.exists()), PROJECT_ROOT)

    root = Tk()
    root.withdraw()                    # 只留对话框，不要那个空白主窗口
    root.attributes("-topmost", True)  # 否则对话框容易藏在终端后面
    try:
        chosen = filedialog.askopenfilename(
            title="选择要注入的 Excel",
            initialdir=str(initial),
            filetypes=[("Excel 文件", "*.xlsx"), ("全部文件", "*.*")],
        )
    finally:
        root.destroy()
    return Path(chosen) if chosen else None


# ---------------------------------------------------------------- 读表
def read_rows(path: Path) -> tuple[list[tuple], list[str]]:
    """读 xlsx，逐格转成 Python 原生类型。

    返回 (行元组列表, 问题列表)。行元组的顺序与 COLUMNS 一致。

    这里必须**逐格显式转换**，不能让 pandas 的对象直接流进 psycopg：
      · df.to_dict("records") 给的是 numpy 标量，psycopg 不认，会抛
        `cannot adapt type 'numpy.float64'`
      · 空值有 None / NaN / NaT 三副面孔，pd.isna 才认得全
    """
    df = pd.read_excel(path)

    missing = [c for c in COLUMNS if c not in df.columns]
    if missing:
        extra = [c for c in df.columns if c not in COLUMNS]
        print(f"[错误] {path.name} 缺少本脚本需要的列：{missing}", file=sys.stderr)
        if extra:
            print(f"       多余（本脚本不认）的列：{extra}", file=sys.stderr)
        print(f"       实际列：{list(df.columns)}", file=sys.stderr)
        raise SystemExit(1)

    problems: list[str] = []
    rows: list[tuple] = []
    # start=1 是「数据行序号」；对应到 Excel 里是第 2 行起（第 1 行是表头）
    for i, record in enumerate(df.to_dict("records"), start=1):
        values = []
        for name, kind in COLUMN_TYPES:
            raw = record[name]
            try:
                values.append(CONVERTERS[kind](raw))
            except (InvalidOperation, ValueError, TypeError) as exc:
                problems.append(f"第 {i} 行「{name}」={raw!r} 转换失败（{type(exc).__name__}），已置空")
                values.append(None)
        rows.append(tuple(values))
    return rows, problems


def summarize(rows: list[tuple]) -> dict:
    """按列统计空值数与数值合计，供 --dry-run 报告和写入后复核共用。"""
    nulls = {name: 0 for name in COLUMNS}
    for row in rows:
        for name, value in zip(COLUMNS, row):
            if value is None:
                nulls[name] += 1
    amount = sum(r[COLUMNS.index("中标金额")] or Decimal(0) for r in rows)
    return {"rows": len(rows), "nulls": nulls, "amount": amount}


# ---------------------------------------------------------------- 建库建表
def build_ddl() -> str:
    """生成建表语句。中文标识符一律加双引号。

    PostgreSQL 允许非 ASCII 标识符，不加引号时它会做大小写折叠；中文没有大小写
    所以实际能过，但列名里一旦混进大写拉丁字母或空格就会**静默**错位。
    统一加引号，把这条规则变成不需要思考的事。
    """
    lines = []
    for name, kind in COLUMN_TYPES:
        null = " NOT NULL" if name in NOT_NULL else ""
        lines.append(f'    "{name}" {kind}{null}')
    body = ",\n".join(lines)
    return (f'CREATE TABLE IF NOT EXISTS "{TABLE}" (\n'
            f'    id bigserial PRIMARY KEY,\n'
            f'{body}\n'
            f')')


def ensure_database(cfg: dict) -> str | None:
    """目标库不存在就建。返回 None 表示成功，否则是错误说明。

    ⚠ CREATE DATABASE 既没有 IF NOT EXISTS，也**不能在事务块里执行**。
      所以先查 pg_database，再用 autocommit 连接去执行。
    """
    try:
        with psycopg.connect(**{**conninfo(cfg, "postgres"), "autocommit": True}) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (cfg["database"],))
                if cur.fetchone():
                    return None
                print(f"  库 {cfg['database']!r} 不存在，正在创建…")
                # 库名是标识符，不能参数化，用 Identifier 转义
                cur.execute(
                    sql.SQL("CREATE DATABASE {} ENCODING 'UTF8' TEMPLATE template0").format(
                        sql.Identifier(cfg["database"])
                    )
                )
    except psycopg.Error as exc:
        return f"{type(exc).__name__}: {str(exc).strip()[:300]}"
    return None


def check_schema(conn) -> str | None:
    """表已存在时核对列。对不上就拒绝——COPY 的错位是静默的。

    `CREATE TABLE IF NOT EXISTS` 对已存在的表是空操作，不会补列也不会改列。
    如果改了 COLUMN_TYPES 却没重建表，接下来 COPY 会按列名去对，对不上的直接报错、
    对得上的悄悄写进旧列 —— 后者才可怕。
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = %s ORDER BY ordinal_position",
            (TABLE,),
        )
        actual = [r[0] for r in cur.fetchall()]
    expected = ["id"] + COLUMNS
    if actual and actual != expected:
        only_db = [c for c in actual if c not in expected]
        only_script = [c for c in expected if c not in actual]
        return (f"现有表 {TABLE} 的列与脚本不一致，拒绝写入。\n"
                f"       库里多出：{only_db or '（无）'}\n"
                f"       脚本多出：{only_script or '（无）'}\n"
                f"       改了列定义就得重建表：DROP TABLE {TABLE}; 再跑一次本脚本。")
    return None


# ---------------------------------------------------------------- 写入
def batched(rows: list[tuple], size: int):
    for start in range(0, len(rows), size):
        yield start, rows[start:start + size]


def load(conn, rows: list[tuple], *, replace: bool, batch_size: int) -> int:
    """单事务：TRUNCATE（可选）+ COPY。

    整段在一个事务里 —— 中途失败会整体回滚，不会留下「清空了但没灌进去」的半截状态。
    """
    with conn.cursor() as cur:
        if replace:
            cur.execute(sql.SQL("TRUNCATE TABLE {} RESTART IDENTITY").format(sql.Identifier(TABLE)))

        cols = sql.SQL(", ").join(sql.Identifier(c) for c in COLUMNS)
        stmt = sql.SQL("COPY {} ({}) FROM STDIN").format(sql.Identifier(TABLE), cols)

        written = 0
        total = (len(rows) + batch_size - 1) // batch_size
        for no, (_, chunk) in enumerate(batched(rows, batch_size), start=1):
            with cur.copy(stmt) as copy:
                for row in chunk:
                    copy.write_row(row)
            written += len(chunk)
            print(f"  写入 {no}/{total} 批（{written}/{len(rows)} 行）", end="\r")
    if rows:
        print(f"  写入 {total}/{total} 批（{len(rows)}/{len(rows)} 行）")
    conn.commit()
    return written


def verify(conn, expected: dict) -> bool:
    """拿库里实际的形状和「打算写进去的形状」对一遍。

    这是自证，不是独立验证——但它能抓住编码、类型、截断这类整批性的错误，
    而那些恰恰是 COPY 最可能出的问题。
    """
    print("\n复核（库里实际 vs 本次解析）：")
    ok = True

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(TABLE)))
        n = cur.fetchone()[0]
    mark = "OK  " if n == expected["rows"] else "FAIL"
    print(f"  [{mark}] 行数            {n} / {expected['rows']}")
    ok = ok and n == expected["rows"]

    with conn.cursor() as cur:
        for name in COLUMNS:
            cur.execute(
                sql.SQL("SELECT count(*) FROM {} WHERE {} IS NULL").format(
                    sql.Identifier(TABLE), sql.Identifier(name)
                )
            )
            got = cur.fetchone()[0]
            want = expected["nulls"][name]
            if got != want:
                print(f"  [FAIL] 空值「{name}」    库里 {got} / 解析 {want}")
                ok = False
        print(f"  [OK  ] 19 列空值数    全部一致")

    with conn.cursor() as cur:
        cur.execute(sql.SQL("SELECT sum({}) FROM {}").format(
            sql.Identifier("中标金额"), sql.Identifier(TABLE)
        ))
        got = cur.fetchone()[0] or Decimal(0)
    mark = "OK  " if got == expected["amount"] else "FAIL"
    print(f"  [{mark}] 中标金额合计    {got} / {expected['amount']}")
    ok = ok and got == expected["amount"]

    return ok


# ---------------------------------------------------------------- 主流程
def main() -> int:
    parser = argparse.ArgumentParser(description="选择 XLSX 并注入 PostgreSQL")
    parser.add_argument("xlsx", nargs="?", type=Path, default=None, help="xlsx 路径（不给就弹窗选）")
    parser.add_argument("-d", "--database", default=None, help=f"目标库（默认 {DEFAULT_DATABASE}）")
    parser.add_argument("--host", default=None, help="覆盖主机")
    parser.add_argument("--port", default=None, help="覆盖端口")
    parser.add_argument("--user", default=None, help="覆盖用户名")
    parser.add_argument("--password", default=None,
                        help="覆盖密码。⚠ 会留在 shell 历史里，优先用 .env")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE, help=f"每批行数（默认 {BATCH_SIZE}）")
    parser.add_argument("--limit", type=int, default=0, help="只灌前 N 行（0 = 全量）")
    parser.add_argument("--keep", action="store_true",
                        help="追加而不是先清空（默认是整表替换）")
    parser.add_argument("--dry-run", action="store_true", help="只解析和报告，不连库、不写入")
    args = parser.parse_args()

    # ---------- 选文件 ----------
    path = args.xlsx or pick_file()
    if path is None:
        print("没有选文件，退出。")
        return 1
    if not path.exists():
        print(f"[错误] 找不到：{path}", file=sys.stderr)
        return 1

    # ---------- 解析 ----------
    rows, problems = read_rows(path)
    if args.limit:
        rows = rows[: args.limit]
    if not rows:
        print(f"[错误] {path.name} 里一行数据都没有", file=sys.stderr)
        return 1

    stats = summarize(rows)

    print(f"选择文件：{path}")
    print(f"  行数：{stats['rows']}    列数：{len(COLUMNS)}")
    print(f"  列空值分布：")
    for name in COLUMNS:
        blank = stats["nulls"][name]
        if blank:
            print(f"    {name:<12} {blank:>5} 行为空")
    for message in problems[:10]:
        print(f"  [警告] {message}", file=sys.stderr)
    if len(problems) > 10:
        print(f"  [警告] …另有 {len(problems) - 10} 条", file=sys.stderr)

    if args.dry_run:
        print("\n[--dry-run] 未连接数据库、未写入。")
        print(f"\n将写入 {args.database or DEFAULT_DATABASE} 库的 {TABLE} 表，建表语句：\n")
        for line in build_ddl().splitlines():
            print(f"    {line}")
        print(f"\n写入方式：{'追加（--keep）' if args.keep else '整表替换（TRUNCATE + COPY）'}")
        print(f"预计写入：{stats['rows']} 行")
        return 0

    # ---------- 连接 ----------
    cfg = resolve_config(args)
    if not cfg["password"]:
        print(f"[错误] 没有密码。请在 .env 里补一行：\n"
              f"         POSTGRES_PASSWORD=...\n"
              f"       （文件在 {PROJECT_ROOT / '.env'}，已被 .gitignore 覆盖）", file=sys.stderr)
        return 1

    err = ensure_database(cfg)
    if err:
        print(f"[错误] 连不上 {cfg['host']}:{cfg['port']}（用户 {cfg['user']}）", file=sys.stderr)
        print(f"       {err}", file=sys.stderr)
        if "password" in err.lower() or "authentication" in err.lower() or "认证" in err:
            print(f"\n       认证失败。核对 .env 里的 POSTGRES_USER / POSTGRES_PASSWORD。", file=sys.stderr)
        elif "refused" in err.lower() or "timeout" in err.lower():
            print(f"\n       连不上服务。确认 PostgreSQL 已启动：\n"
                  f"         服务 postgresql-x64-17 应在「正在运行」状态。", file=sys.stderr)
        return 1

    print(f"\n已连接：{cfg['host']}:{cfg['port']}  库={cfg['database']}  用户={cfg['user']}")

    try:
        with psycopg.connect(**conninfo(cfg, cfg["database"])) as conn:
            schema_err = check_schema(conn)
            if schema_err:
                print(f"[错误] {schema_err}", file=sys.stderr)
                return 1

            with conn.cursor() as cur:
                cur.execute(build_ddl())
            conn.commit()

            existing = 0
            with conn.cursor() as cur:
                cur.execute(sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(TABLE)))
                existing = cur.fetchone()[0]
            if args.keep:
                if existing:
                    print(f"  [提示] --keep：库里已有 {existing} 行，本次追加，跑完共 {existing + len(rows)} 行")
            elif existing:
                print(f"  整表替换：先清掉现有 {existing} 行")

            print("写入中…")
            load(conn, rows, replace=not args.keep, batch_size=args.batch_size)

            ok = verify(conn, stats)
    except psycopg.Error as exc:
        print(f"\n[错误] {type(exc).__name__}: {str(exc).strip()[:400]}", file=sys.stderr)
        return 1

    if not ok:
        print("\n[错误] 复核未通过，见上面 FAIL 行。", file=sys.stderr)
        return 1

    print(f"\n完成。在 psql 里试试：")
    print(f"  psql -h {cfg['host']} -U {cfg['user']} -d {cfg['database']} \\")
    print(f"       -c 'select \"标的物\", \"采购人\", \"中标金额\" from {TABLE} limit 5;'")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
