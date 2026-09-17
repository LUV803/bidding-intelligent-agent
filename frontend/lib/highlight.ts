/**
 * 把关键词标黄 —— 用 **CSS Custom Highlight API**（`CSS.highlights` + `Range`）。
 *
 * ⚠⚠ 为什么不用「把命中词包成 `<mark class="kw-mark">`」那条路（`SourcePanel` 用的就是它）：
 *   消息正文是 **Markdown 渲染出来的**。要在里面插 `<mark>`，就得在**解析之前**改源串，
 *   而那会连带把代码块、链接 URL、表格分隔行一起改坏 —— 一个只在特定正文里才炸的 bug。
 *   事后包 DOM 则要跟 React 抢同一棵子树。这条 API 两头都不碰：它只画一层**与 DOM 无关的
 *   高亮**，正文怎么渲染的完全不用管。
 *
 * ⚠ 兼容性走**特性检测 + 静默降级**（同 `reranker` / `graph_store` 的「降级而非报错」）：
 *   浏览器不认就返回 0，**不抛异常** —— 定位（滚动）照常发生，只是词不标黄。
 *   为一个纯视觉增强把整条「点搜索结果」链路搞崩是不划算的。
 */

/**
 * 高亮名。⚠ 必须与 `globals.css` 里 `::highlight(...)` 那个名字**逐字一致** ——
 * 对不上的表现是「滚动到了、但什么都没标黄」，不报错。
 */
const HIGHLIGHT_NAME = "conv-hit";

/** `CSS.highlights` 的形状。TS 的标准 lib 里未必有，自己声明一份（别用 `any`） */
interface HighlightRegistry {
  set(name: string, highlight: unknown): void;
  delete(name: string): boolean;
}

function registry(): HighlightRegistry | null {
  if (typeof CSS === "undefined") return null;
  return (CSS as unknown as { highlights?: HighlightRegistry }).highlights ?? null;
}

type HighlightCtor = new (...ranges: Range[]) => unknown;

function highlightCtor(): HighlightCtor | null {
  const ctor = (globalThis as unknown as { Highlight?: HighlightCtor }).Highlight;
  return typeof ctor === "function" ? ctor : null;
}

/** 抹掉当前的高亮。**先清理再设置**，所以同一个名字永远只有一批区间 */
export function clearKeywordHighlight(): void {
  registry()?.delete(HIGHLIGHT_NAME);
}

/**
 * 在 `root` 子树里把所有 `keyword` 标黄，返回标了几处。
 *
 * `root` 是**一条消息**的容器（`msg-<id>`），所以词出现在思考过程、工具结果还是
 * 来源片段里都能标到 —— 侧栏的片段只告诉你「命中的是问还是答」，标黄补的是「在这一段的哪里」。
 *
 * ⚠ 只认**渲染后的文本**：关键词若被 Markdown 拆开（`履约**保证**金`），这里匹配不到。
 *   这是这条 API 的固有代价，换来的是正文渲染一个字都不用动。
 */
export function highlightKeyword(root: HTMLElement, keyword: string): number {
  clearKeywordHighlight();
  const reg = registry();
  const Ctor = highlightCtor();
  // ⚠ 空关键词必须在这里挡掉：下面那个 `indexOf` 循环以 `needle.length` 步进，
  //   空串会让它原地打转（死循环）。`keyword.trim()` 之后为空 = 用户只敲了空格。
  const needle = keyword.trim().toLowerCase();
  if (!reg || !Ctor || !needle) return 0;

  const ranges: Range[] = [];
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  for (let node = walker.nextNode(); node; node = walker.nextNode()) {
    const text = node.nodeValue ?? "";
    const haystack = text.toLowerCase();
    for (
      let at = haystack.indexOf(needle);
      at >= 0;
      at = haystack.indexOf(needle, at + needle.length)
    ) {
      const range = document.createRange();
      range.setStart(node, at);
      range.setEnd(node, at + needle.length);
      ranges.push(range);
    }
  }

  if (ranges.length === 0) return 0;
  reg.set(HIGHLIGHT_NAME, new Ctor(...ranges));
  return ranges.length;
}
