"use client";

import { Database, Network, Scale } from "lucide-react";

import { ChatInput } from "./ChatInput";
import type { LlmProviderOption, SendOverrides } from "@/lib/types";

type Suggestion = {
  title: string;
  tag: string;
  /** 这条问题要查 Neo4j 知识图谱，而不是知识库检索 */
  graph?: boolean;
  /** 这条问题要查 PostgreSQL 采购记录库 */
  db?: boolean;
};

// 10 条一块平铺，刻意不做分组 —— 三行组标题各自占掉一行高度，与「一眼看全」直接冲突。
// 数据源由标签上的 ⬡ / ▤ 图标区分（标签文字因此不必再写「图谱 ·」前缀）。
// ⚠ 10 条正好是偶数，双列排下来 5 行整、不留空位。**增删卡片时盯住这个奇偶**：
//   加成奇数的话末行会剩一格，那时别顺手给末张加 `sm:col-span-2` 补满 ——
//   一张横跨两格的超宽卡与其余各张的节奏对不上，反而比空着更显眼（2026-09-22 试过又撤了）。
const SUGGESTIONS: Suggestion[] = [
  { title: "供应商针对单一来源异议被驳回，可以再次异议吗？", tag: "单一来源" },
  { title: "中标公告发出后发现第一名为无效投标时，招标人应如何处理？", tag: "中标处理" },
  { title: "招标文件要求中标人提交履约保证金的最高限额是多少？", tag: "保证金" },
  { title: "投标人或者其他利害关系可以向哪些行政监督部门进行投诉？", tag: "质疑投诉" },
  // 图谱示例：这几条都实测过模型会选对工具、且图谱里有真数据（2026-09-17）。
  // 这类问题在知识库的 74 条问答对里必然检索不到，答案只存在图谱里。
  { title: "激光打印机的中标供应商有哪些？金额最高的是谁？", tag: "供应商", graph: true },
  { title: "打印机这个品类的中标情况怎么样？哪个地区采购得最多？", tag: "地区", graph: true },
  { title: "有哪些关于空调的采购？", tag: "采购记录", graph: true },
  { title: "采购最多的标的是什么？", tag: "排行", graph: true },
  { title: "空调的供应商和采购人有哪些？", tag: "交易双方", graph: true },
  // 数据库示例：这条实测过模型会选对工具、且库里确有数据（2026-09-18）。
  // ⚠ 原先还有一条「哪个城市的采购项目最多？」（分组聚合），2026-09-22 按用户要求删掉了
  //   —— 于是数据库这块**只剩明细（detail）一种模式**。两种模式的渲染形状完全不同
  //   （`PgResultBlock` 的表格列是按 `mode` 分流的，不是按「哪些字段非空」），
  //   要演示分组那个形态得先把这条加回来。
  { title: "中国科学技术大学采购过哪些项目？", tag: "明细", db: true },
];

export function WelcomeScreen({
  onSend,
  onStop,
  streaming,
  disabled,
  graphAvailable,
  dbAvailable,
  hybrid,
  onToggleHybrid,
  rerank,
  onToggleRerank,
  webSearch,
  onToggleWebSearch,
  webConfigured,
  llmProvider,
  llmDefaultProvider,
  llmProviders,
  onSelectLlmProvider,
}: {
  onSend: (text: string, overrides?: SendOverrides) => void;
  onStop: () => void;
  streaming: boolean;
  disabled: boolean;
  /** 图谱连不上时这些卡片点了也没用，如实置灰 */
  graphAvailable: boolean;
  /** 采购记录库连不上时同理 */
  dbAvailable: boolean;
  hybrid: boolean;
  onToggleHybrid: (value: boolean) => void;
  rerank: boolean;
  onToggleRerank: (value: boolean) => void;
  webSearch: boolean;
  onToggleWebSearch: (value: boolean) => void;
  /**
   * 服务端配没配 TAVILY_API_KEY，**原样转给 ChatInput**（首屏与多轮用的是同一个输入框
   * 组件，两处都得喂，否则开关的提示在一个界面上有一处在另一处没有）。
   * 三态语义见 ChatInput 的 prop 注释。
   */
  webConfigured?: boolean | null;
  /**
   * 文本大模型那四个 prop（选中的 / 服务端默认 / 可选项 / 选择回调），
   * **原样转给 `ChatInput`** —— 首屏与多轮是同一个输入框组件，
   * 漏掉这里的表现正是「首屏没有那个胶囊，多轮界面上有」。
   */
  llmProvider: string;
  llmDefaultProvider: string;
  llmProviders: LlmProviderOption[];
  onSelectLlmProvider: (name: string) => void;
}) {
  /**
   * 图谱/数据库卡片不需要再做任何「切模式」的动作了——整条链路固定走 `/api/agent`，
   * 这两个工具天生就在场。
   *
   * ⚠ 原先这里要 `onToggleAgent(true)` + 给这次请求带 `{ agent: true }` override
   *   （拨开关是给用户看的，override 是绕开 setState 的闭包竞态——不带的话请求会打到
   *   /api/chat，那边没注册工具，点了卡片必然答不出来）。2026-09-20 去掉 Agent 开关后
   *   两者都不需要了。**要恢复切换按钮的话，这里也得一起恢复。**
   */
  const pick = (item: Suggestion) => onSend(item.title);

  /** 某个工具不可用时只置灰用它的那几张卡，四条法律问题照常能点 */
  const blockedBy = (item: Suggestion) =>
    (item.graph === true && !graphAvailable) || (item.db === true && !dbAvailable);

  const blockReason = (item: Suggestion) =>
    item.db ? "采购记录库未连接，这条问题需要数据库数据" : "知识图谱未连接，这条问题需要图谱数据";

  return (
    <div className="flex min-h-full flex-col items-center justify-center px-4 py-10">
      <div className="w-full max-w-chat">
        <div className="mb-8 flex flex-col items-center text-center">
          <div className="mb-4 flex h-12 w-12 items-center justify-center rounded-2xl bg-brand text-white shadow-lg shadow-brand/20">
            <Scale className="h-6 w-6" />
          </div>
          <h1 className="text-[26px] font-semibold tracking-tight text-ink">
            有什么可以帮你的吗？
          </h1>
          <p className="mt-2 text-[13.5px] text-muted">
            基于招投标知识库的混合检索问答，回答中的引用序号可点击查看原文出处
          </p>
          {/*
            ⚠ 这段讲的是**屏幕上看不出来的那件事**（调了什么会列在回答上方），别删。
            ⚠ 原先还有前半截「标有 ⬡ 图谱、▤ 数据库的问题会分别查询 Neo4j 成交记录与
            采购记录库…」—— 中间有过一段「按数据源分组」的版本，那时每段自带小标题，
            这半截成了重复就被删了；**现在分组又去掉了（换成一块平铺网格）**，
            那半截其实重新变得有用。留着不补是因为它有一行半长，而卡片上的标签
            （供应商 / 地区 / 明细…）本身已经直说了问的是什么，图标只是个辅助记号。
            ⚠ 更早还写过「（自动切到 Agent 模式）」，那是「点卡片才切」的旧行为，早已删除；
            现在每条提问都走 Agent。
          */}
          <p className="mt-1 text-[12.5px] text-faint">
            模型会自主调用检索与图谱/数据库工具，调了什么会列在回答上方的「工具调用」里
          </p>
        </div>

        <ChatInput
          onSend={onSend}
          onStop={onStop}
          streaming={streaming}
          disabled={disabled}
          hybrid={hybrid}
          onToggleHybrid={onToggleHybrid}
          rerank={rerank}
          onToggleRerank={onToggleRerank}
          webSearch={webSearch}
          onToggleWebSearch={onToggleWebSearch}
          webConfigured={webConfigured}
          llmProvider={llmProvider}
          llmDefaultProvider={llmDefaultProvider}
          llmProviders={llmProviders}
          onSelectLlmProvider={onSelectLlmProvider}
        />

        {/*
          10 条一块平铺的双列网格（`sm` 以上两列、窄屏一列），正好 5 行整。
          ⚠ 刻意不做分组：三行组标题各自占掉一行高度，与「一眼看全」直接冲突。
          ⚠ 也别给这里加 `ring-*`（同下面卡片那条注释）。
        */}
        <div className="mt-4 grid grid-cols-1 gap-2 sm:grid-cols-2">
          {SUGGESTIONS.map((item) => (
            <button
              key={item.title}
              type="button"
              disabled={disabled || blockedBy(item)}
              title={!disabled && blockedBy(item) ? blockReason(item) : undefined}
              onClick={() => pick(item)}
              /*
                ⚠ 紧凑的关键是「标签与问题**同一行**」：早先标签独占一行，每张卡多出约
                  20px 高，11 张下来就是 200px 以上，首屏就看不全了。
                ⚠ hover 一律限定在 `enabled:` 上 —— 否则置灰的卡片鼠标移上去边框和底色
                  照样变，看着像能点。
                ⚠ 别给这里加 `ring-*`：Tailwind v3 的 ring 走 `--tw-ring-shadow` 参与
                  box-shadow 合成，与 `shadow-[…]` 那类任意值互斥，写了会静默不生效。
                  `shadow-sm` 是预设值，不受这条影响。
              */
              className="group flex items-center gap-2 rounded-xl border border-line bg-surface px-2.5 py-2 text-left transition duration-150 enabled:hover:border-brand/40 enabled:hover:bg-brand-soft/50 enabled:hover:shadow-sm disabled:cursor-not-allowed disabled:opacity-60"
            >
              <span className="inline-flex shrink-0 items-center gap-1 rounded-md bg-hover px-1.5 py-0.5 text-[10.5px] text-muted group-hover:bg-brand-soft group-hover:text-brand">
                {item.graph && <Network className="h-3 w-3" />}
                {item.db && <Database className="h-3 w-3" />}
                {item.tag}
              </span>
              {/*
                用 `<span>` 而不是 `<p>`：它现在是这一行 flex 里的第二格
                （`<button>` 里也只该放 phrasing content），`min-w-0` 让长句能正常折行。
              */}
              <span className="min-w-0 flex-1 text-[12.5px] leading-snug text-ink">
                {item.title}
              </span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
}
