/**
 * 修复「整张表挤成一行」的模型输出。
 *
 * GFM 要求表头、分隔行、数据行**各占一行**：分隔行不独占一行时，remark-gfm 解析不出
 * 表格，整行退化成 paragraph —— 用户看到的就是一串裸管道符。模型偶尔会这样输出，
 * 同一张表多行版能渲染、单行版不能。
 *
 * 纯字符串逻辑，不依赖 React，从这里单独放一份是为了能直接跑（见 Markdown.tsx 的引用）。
 */

/** GFM 分隔行里的单个单元格：`:---` / `---:` / `:---:` / `---` */
const TABLE_SEP_CELL = /^:?-{2,}:?$/;

/**
 * 把一行「被挤成一坨的表格」拆回多行。
 *
 * **已经是正常多行表格时一律返回 null**（调用方沿用原行）：那种行里根本没有分隔单元格，
 * 第一道判断就过不去。只有同时满足下面四条才动手——
 *
 *   1. 行首是 `|`
 *   2. 存在连续的「分隔单元格」
 *   3. 表头单元格数 == 分隔行单元格数
 *   4. 其余单元格数能被列数整除
 *
 * 任何一条不满足就放弃。**宁可不修，也不能把正文改坏。**
 *
 * @returns 拆好的多行数组；判定不了返回 null。
 */
export function splitCrammedTable(line: string): string[] | null {
  const trimmed = line.trim();
  // 只认以管道符开头的行；带前导文字的交给 markdown 自己处理
  if (!trimmed.startsWith("|")) return null;

  // 空单元格要丢掉：换行被空格替掉后，**上一行收尾的 `|` 和下一行起始的 `|` 中间会留下
  // 一个空单元格**（`… | 采购方向 | |:---|…` 里那个），它只是行边界的残迹。不丢的话
  // 表头会被它多算一列，与分隔行对不上，整条判定直接失败。
  // 代价：真正留空的单元格也会被丢掉，可能让那一行错位——但那种表本来就少见，
  // 且下面「列数必须整除」那条会把大多数错位挡掉。
  const cells = trimmed
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((c) => c.trim())
    .filter((c) => c !== "");
  // 一列表最少也要 表头+分隔+1 数据行 = 3 个单元格
  if (cells.length < 3) return null;

  const sepStart = cells.findIndex((c) => TABLE_SEP_CELL.test(c));
  if (sepStart <= 0) return null; // 没有分隔行，或它落在第一个单元格（那就没有表头）
  let sepEnd = sepStart;
  while (sepEnd < cells.length && TABLE_SEP_CELL.test(cells[sepEnd])) sepEnd += 1;

  const cols = sepEnd - sepStart;
  if (sepStart !== cols) return null; // 表头单元格数必须等于分隔行单元格数

  const rest = cells.slice(sepEnd);
  if (rest.length === 0 || rest.length % cols !== 0) return null; // 数据行列数对不齐

  const row = (xs: string[]) => `| ${xs.join(" | ")} |`;
  const out = [row(cells.slice(0, cols)), row(cells.slice(sepStart, sepEnd))];
  for (let i = 0; i < rest.length; i += cols) out.push(row(rest.slice(i, i + cols)));
  return out;
}

/**
 * 逐行修表格。正常表格里的每一行都不含分隔单元格，因此原样通过。
 */
export function repairCrammedTables(content: string): string {
  return content
    .split("\n")
    .flatMap((line) => splitCrammedTable(line) ?? [line])
    .join("\n");
}
