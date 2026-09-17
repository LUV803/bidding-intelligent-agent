"use client";

import {
  ArrowUp,
  Cpu,
  FileImage,
  Globe,
  Loader2,
  Paperclip,
  Sparkles,
  Square,
  X,
  Zap,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";

import { analyzeImage } from "@/lib/api";
import type { LlmProviderOption, SendOverrides, VisionPayload } from "@/lib/types";

/**
 * 单张图片的字节上限，**必须与后端 `VISION_MAX_IMAGE_BYTES` 一致**。
 *
 * ⚠ 这里的预检**只是体验**（早点告诉用户，不用等一个来回），真正的防线在服务端
 * ——前端校验永远可以被绕过。
 */
const MAX_IMAGE_BYTES = 10 * 1024 * 1024;

function readAsDataURL(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ""));
    reader.onerror = () => reject(new Error("读取图片失败"));
    reader.readAsDataURL(file);
  });
}

export function ChatInput({
  onSend,
  onStop,
  streaming,
  disabled,
  placeholder = "给招投标智能问答助手发送消息…",
  hybrid,
  onToggleHybrid,
  rerank,
  onToggleRerank,
  webSearch,
  onToggleWebSearch,
  webConfigured = null,
  llmProvider,
  llmDefaultProvider,
  llmProviders,
  onSelectLlmProvider,
}: {
  /**
   * ⚠ 第二个参数是**本次请求的开关覆盖**，不要收窄成 `(text: string) => void`：
   * 附件要以 `overrides.image` 的形式跟着这次发送走（绕开 setState 的闭包竞态），
   * 收窄了类型对不上，WelcomeScreen 那边也传不进来。
   */
  onSend: (text: string, overrides?: SendOverrides) => void;
  onStop: () => void;
  streaming?: boolean;
  disabled?: boolean;
  placeholder?: string;
  hybrid: boolean;
  onToggleHybrid: (value: boolean) => void;
  rerank: boolean;
  onToggleRerank: (value: boolean) => void;
  webSearch: boolean;
  onToggleWebSearch: (value: boolean) => void;
  /**
   * 服务端配没配 TAVILY_API_KEY（`health.web_configured`）。**三态，别收成 boolean**：
   * `null` = 健康检查还没回来、还不知道，此时一个字都不该多说。
   *
   * 只有确认为 `false` 时才会在下面那个按钮的 tooltip 里追加一句提示。这句提示是
   * **那条路径上唯一的反馈通道**：开关打开而服务端没配 Key 时，不发 error 事件、
   * 按钮也不置灰，用户只会拿到一次**没有网页条目**的普通回答 —— 而「没搜到」与
   * 「压根没搜」在正文里完全分不出来。原先它挂在 Sidebar 底部的状态块里，
   * 2026-09-20 随状态块一起挪到这里（那块整体删掉了，见 Sidebar.tsx 的注释）。
   */
  webConfigured?: boolean | null;
  /**
   * 这次提问用哪家文本大模型。**空串 = 跟服务端默认**（`llmDefaultProvider`）。
   *
   * 与 `llmDefaultProvider` / `llmProviders` / `onSelectLlmProvider` 是**一组**，
   * 四个要一起传（与 `hybrid` 那三个开关同一条纪律：漏一处就是静默故障）。
   * ⚠ 「服务端默认」这条路径必须能走通：切换只影响**之后**的提问，历史照旧，
   * 所以 `llmProvider` 为空串时后端行为与加这个功能之前逐字节相同。
   */
  llmProvider: string;
  /** 服务端默认那家（`health.llm_provider`，已归一化）。`llmProvider` 为空时显示的就是它 */
  llmDefaultProvider: string;
  /** 可选项，**只含服务端真配了 Key 的**。少于 2 个时整个胶囊不渲染 */
  llmProviders: LlmProviderOption[];
  onSelectLlmProvider: (name: string) => void;
}) {
  const [value, setValue] = useState("");
  // 已解析好的图片（解析失败时为 null，原因放在 imageError 里）
  const [image, setImage] = useState<VisionPayload | null>(null);
  const [analyzing, setAnalyzing] = useState(false);
  const [imageError, setImageError] = useState<string | null>(null);
  const [dragging, setDragging] = useState(false);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  // 自适应高度：最多 200px
  useEffect(() => {
    const el = textareaRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  }, [value]);

  useEffect(() => {
    if (!streaming) textareaRef.current?.focus();
  }, [streaming]);

  // ---------- 选图 / 拖图 ----------
  const handleFile = async (file: File) => {
    setImageError(null);
    if (!file.type.startsWith("image/")) {
      setImageError("只能上传图片文件（png / jpg / webp 等）");
      return;
    }
    if (file.size > MAX_IMAGE_BYTES) {
      setImageError(
        `图片不能超过 ${MAX_IMAGE_BYTES / 1024 / 1024}MB（这张 ${(file.size / 1024 / 1024).toFixed(1)}MB）`,
      );
      return;
    }

    setAnalyzing(true);
    setImage(null);
    try {
      const dataUrl = await readAsDataURL(file);
      // 切掉 `data:image/png;base64,` 前缀：前后端约定只传**裸 base64**
      // （后端对带前缀的形态也兼容，但它会记一条警告——约定就是约定）
      const base64 = dataUrl.slice(dataUrl.indexOf(",") + 1);
      const result = await analyzeImage(base64, file.type || "image/png");
      // ⚠ 后端解析失败返回的是 **HTTP 200 + ok:false**（降级而非报错），所以要判 ok
      if (!result.ok) {
        setImageError(result.note ?? "图片解析失败，请重试或换一张图");
        return;
      }
      setImage(result);
    } catch (error) {
      setImageError(error instanceof Error ? error.message : "图片解析失败，请检查后端服务");
    } finally {
      setAnalyzing(false);
    }
  };

  const submit = () => {
    const text = value.trim();
    // 仍然只认文字：图片是「这次提问的上下文」，不是独立消息。纯图无文字发不出去。
    if (!text || streaming || disabled) return;
    onSend(text, image ? { image } : undefined);
    setValue("");
    // 附件随这次提问一起消费掉。图片上下文已存进那条用户消息里（history 补前缀靠它），
    // 追问时不必再挂一遍。
    setImage(null);
    setImageError(null);
  };

  const handleKeyDown = (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
    // Enter 发送，Shift+Enter 换行；输入法组合期间不拦截
    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
      event.preventDefault();
      submit();
    }
  };

  const hasChip = analyzing || Boolean(imageError) || Boolean(image);

  // ---------- 文本大模型胶囊 ----------
  // 当前**生效**那家：选过就用选的那家，没选过就跟服务端默认。
  const effectiveLlm = llmProvider || llmDefaultProvider;
  const currentLlm = llmProviders.find((p) => p.name === effectiveLlm) ?? null;

  /**
   * 在可选供应商里**循环**切换。
   *
   * ⚠ `currentLlm` 为 null 时（选过的那家 Key 后来被摘了，或服务端默认那家压根没配 Key）
   *   `findIndex` 回 `-1`，而 `(-1 + 1) % len === 0` **正好落到第一家** —— 这是有意的：
   *   点一下就能离开一个失效的选项，而不是卡在死状态里。
   *
   * ⚠ 刻意**不做「回到服务端默认」那一档**：默认那家必然就在列表里（`llm_provider` 只
   *   可能是 `_PROVIDERS` 里的名字），循环到它时发出的 `llm_provider` 虽然是个实名，
   *   行为却与空串**完全一样**（`resolve_provider` 只认名字）。多一档只是多一次点击。
   */
  const cycleLlm = () => {
    const next = llmProviders[(llmProviders.findIndex((p) => p.name === effectiveLlm) + 1) % llmProviders.length];
    if (next) onSelectLlmProvider(next.name);
  };

  return (
    <div className="w-full">
      <div
        onDragOver={(event) => {
          // 不 preventDefault 的话浏览器会直接把图片当页面打开
          event.preventDefault();
          if (!disabled) setDragging(true);
        }}
        onDragLeave={(event) => {
          // 只认真正离开容器：鼠标移过内部子元素时也会触发 dragleave，
          // 不判 relatedTarget 就会狂闪
          if (event.currentTarget.contains(event.relatedTarget as Node | null)) return;
          setDragging(false);
        }}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          if (disabled) return;
          const file = event.dataTransfer.files?.[0];
          if (file) void handleFile(file);
        }}
        className={clsx(
          // ⚠ 这里的 pb 与下面工具条的定位方式是**一对**，别单独动其中一个。
          // 工具条原先是 `absolute inset-x-3.5 bottom-2.5`，靠 pb-11 在文档流里
          // 预留出它的高度。加了第 4 个开关后窄屏（375px）放不下会换行，而绝对定位
          // 是从底部**向上**长的——换行后会盖住 textarea 的最后一行。
          // 改成正常流的 flex 行后，总高与原来一致（pt-3.5 + textarea + mt-0.5 +
          // 工具条 32px + pb-2.5），窄屏换行时容器自然变高，不会压到文字。
          // ⚠ 2026-09-20 改 OpenAI 风格时**刻意不动这一串间距**（px-4 pb-2.5 pt-3.5，
          // 以及工具条自己的 mt-0.5 / gap-1.5 / gap-y-1）：只换子元素样式，把上面那个
          // 窄屏坑的复现条件原样保持住。
          // ⚠ 阴影是**任意值**，所以别再加 `focus-within:ring-*`：Tailwind v3 的 ring 靠
          // --tw-ring-shadow 参与 box-shadow 合成，而任意值 shadow-[…] 不走合成变量，
          // 两者同时写 ring 会被整条盖掉、**静默不显示**。焦距态只用下面那个 border 变色。
          "rounded-[28px] border bg-surface px-4 pb-2.5 pt-3.5 shadow-[0_2px_14px_rgba(0,0,0,0.04)] transition-colors dark:shadow-[0_2px_18px_rgba(0,0,0,0.4)]",
          dragging ? "border-brand/60 bg-brand-soft/40" : "border-line",
          "focus-within:border-brand/60",
        )}
      >
        {/* 附件 chip。在**正常文档流**里（不是绝对定位），所以不需要为它调 pb-11
            —— 那个 padding 是给底部工具条留的位，与这里互不影响。 */}
        {hasChip && (
          <div className="mb-2.5 flex items-start gap-2 rounded-xl border border-line bg-canvas px-2.5 py-1.5">
            {analyzing ? (
              <>
                <Loader2 className="mt-[3px] h-3.5 w-3.5 shrink-0 animate-spin text-brand" />
                <span className="text-[12.5px] leading-relaxed text-muted">
                  正在识别图片内容…
                </span>
              </>
            ) : image ? (
              <>
                <FileImage className="mt-[3px] h-3.5 w-3.5 shrink-0 text-brand" />
                <span className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-muted">
                  <span className="font-medium text-ink">{image.doc_type}</span>
                  {image.summary && <>　{image.summary}</>}
                  <span className="text-faint">
                    （已提取 {Object.keys(image.fields).length} 个字段，将作为本次提问的上下文）
                  </span>
                </span>
              </>
            ) : (
              <>
                <X className="mt-[3px] h-3.5 w-3.5 shrink-0 text-amber-500" />
                <span className="min-w-0 flex-1 text-[12.5px] leading-relaxed text-amber-600 dark:text-amber-400">
                  {imageError}
                </span>
              </>
            )}
            {!analyzing && (
              <button
                type="button"
                title="移除附件"
                onClick={() => {
                  setImage(null);
                  setImageError(null);
                }}
                className="shrink-0 rounded p-0.5 text-faint transition-colors hover:text-ink"
              >
                <X className="h-3.5 w-3.5" />
              </button>
            )}
          </div>
        )}

        <textarea
          ref={textareaRef}
          rows={1}
          value={value}
          disabled={disabled}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={disabled ? "服务初始化中…" : placeholder}
          className="block max-h-[200px] w-full resize-none bg-transparent text-[15px] leading-relaxed text-ink outline-none placeholder:text-faint disabled:cursor-not-allowed"
        />

        {/* 底部工具条。**在正常文档流里**（不是绝对定位）——理由见上面容器的 pb 注释。
            gap-y-1 给换行后的两行之间留出间距。 */}
        <div className="mt-0.5 flex flex-wrap items-center gap-1.5 gap-y-1">
          {/* 用 hidden input + ref 而不是 <label> 包裹：按钮在 absolute 工具条里，
              label 的隐式触发容易和外层容器的事件打架 */}
          <input
            ref={fileRef}
            type="file"
            accept="image/*"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              // 先清空 value，否则「选同一张图第二次」不会触发 onChange
              event.target.value = "";
              if (file) void handleFile(file);
            }}
          />
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={disabled || analyzing}
            title="上传图片（招标公告、中标公告等），解析结果会作为本次提问的上下文"
            className={clsx(
              // h-8 与三个胶囊、发送/停止按钮**同高**（2026-09-20 统一；原先这里是 h-7，
              // 胶囊靠 py-1 撑到 ~26px，一行里三个尺寸参差不齐）。
              "flex h-8 w-8 items-center justify-center rounded-full transition-colors",
              image
                ? "bg-brand-soft text-brand"
                : "text-muted hover:bg-hover hover:text-ink",
              (disabled || analyzing) && "cursor-not-allowed opacity-50",
            )}
          >
            <Paperclip className="h-4 w-4" />
          </button>

          {/*
            ⚠ 下面四个胶囊（混合检索 / 重排序 / 联网搜索 / 文本大模型）**共用同一串类名**，
            改一处要四处一起改。2026-09-20 改 OpenAI 风格时统一定成：
              基础  flex h-8 items-center gap-1.5 rounded-full px-3 text-[12.5px] transition-colors
              关    bg-hover text-muted hover:text-ink        ← **无描边**，靠浅填充浮在 bg-surface 上
              开    bg-brand-soft text-brand
              告警  bg-amber-500/15 text-amber-600 dark:text-amber-500（仅联网那个用）
            去边框是这次改版的主要一笔：ChatGPT 的 Search / Create image 胶囊就是没有描边的，
            靠一层极浅的灰底区分。它同时给窄屏腾出了 2×px（每颗省 2px 边框），抵消
            px-2.5 → px-3 的增量。
            刻意**不抽成组件**：四者的点击处理、title、禁用条件各不相同，抽出来的 props
            比省下的字节还多；这里只保证类名字符串一致。
            ⚠ 第 4 个（大模型）与前三个**形状不同**：它不是一个布尔开关，而是「点一下换
            下一家」，且多一个「没得选就不渲染」的第三态 —— 见下面那块的注释。
          */}
          <button
            type="button"
            onClick={() => onToggleHybrid(!hybrid)}
            title="开启后同时使用向量语义检索与 BM25 关键词检索（RRF 融合）"
            className={clsx(
              "flex h-8 items-center gap-1.5 rounded-full px-3 text-[12.5px] transition-colors",
              hybrid ? "bg-brand-soft text-brand" : "bg-hover text-muted hover:text-ink",
            )}
          >
            <Zap className="h-3 w-3" />
            混合检索
          </button>

          <button
            type="button"
            onClick={() => onToggleRerank(!rerank)}
            title="开启后用 CrossEncoder 重排模型对初筛结果二次精排，顶部来源更相关，代价是每次查询多几百毫秒"
            className={clsx(
              "flex h-8 items-center gap-1.5 rounded-full px-3 text-[12.5px] transition-colors",
              rerank ? "bg-brand-soft text-brand" : "bg-hover text-muted hover:text-ink",
            )}
          >
            <Sparkles className="h-3 w-3" />
            重排序
          </button>

          {/*
            ⚠ 这里原先是「Agent 模式」切换按钮（2026-09-20 删）。Agent 模式现在是**唯一**
            路径、不是开关，所以按钮连同 `agent` / `onToggleAgent` 两个 prop 一起没了。
            端点选择硬编码在 `lib/api.ts` 的 `streamChat()` 里，别再往这个工具条上加回来
            ——除非同时把 `useChat` 的状态与 `SendOverrides.agent` 一起恢复。
          */}

          <button
            type="button"
            onClick={() => onToggleWebSearch(!webSearch)}
            title={
              "开着时每次提问都会联网（Tavily）搜一次最新公告与法规，与知识库结果合并作答，网页条目并入「参考来源」面板。注意：网页内容来自公开网络，未经知识库校验，请核对原文；每次提问消耗一次 Tavily 额度" +
              // 只有**确认**服务端没配 Key 时才说这句（`null` = 健康检查还没回来，
              // 那时说「未配」是错的）。这是「开关打开但压根不会联网」的唯一提示，
              // 见 webConfigured 的 prop 注释。
              (webConfigured === false
                ? "。⚠ 服务端未配置 TAVILY_API_KEY，开关开着也不会联网（问答链路不受影响）"
                : "")
            }
            className={clsx(
              "flex h-8 items-center gap-1.5 rounded-full px-3 text-[12.5px] transition-colors",
              webSearch
                ? webConfigured === false
                  ? // 开着但服务端没配 Key —— 这时候「已开启」的 brand 色是在骗人：
                    // 点了不会有任何联网，正文里也看不出区别。用琥珀色把它标出来，
                    // 好让用户去 hover 看上面那句原因。
                    // （底色 0.15 是「去边框」后的补偿：原先靠 border-amber-500/50 加厚，
                    //   现在没有描边了，填充太淡在这排浅灰胶囊里会看不出是告警。）
                    "bg-amber-500/15 text-amber-600 dark:text-amber-500"
                  : "bg-brand-soft text-brand"
                : "bg-hover text-muted hover:text-ink",
            )}
          >
            <Globe className="h-3 w-3" />
            联网搜索
          </button>

          {/*
            文本大模型选择（2026-09-21 加）。**与前三个开关形状不同**：它不是一个布尔开关，
            点一下在可选供应商里循环，所以用一个非开关态的图标（Cpu）。

            ⚠ `llmProviders.length >= 2` 才渲染，**两个理由合成一个分支**：
              ① 服务端只配了一家 Key 时「选」这件事不存在，摆一个点不动的死按钮只是噪声；
              ② 健康检查还没回来时 `llmProviders` 是空数组，那时也**不该**猜 ——
                 与 `webConfigured` 的 `null` 三态是同一条纪律（还不知道就一个字都不说）。
            所以这里不需要额外的 null 判断，空数组天然落进「不渲染」。

            ⚠ 显示的是 `currentLlm?.label ?? effectiveLlm` —— 找不到时显示**原始名字**
              （如 `zai`），不回落成第一家：那会显示成一个当时压根不会生效的模型。
          */}
          {llmProviders.length >= 2 && (
            <button
              type="button"
              onClick={cycleLlm}
              title={
                `本次提问用哪家文本大模型，点击切换。当前：${currentLlm?.label ?? effectiveLlm}` +
                (currentLlm ? `（${currentLlm.model}）` : "（服务端未提供它的模型信息，可能已不可用）") +
                `。可选：${llmProviders.map((p) => `${p.label}（${p.model}）`).join("、")}` +
                // 这个选择是**存本机**的（与 hybrid / rerank 同构），得说出来：
                // 否则用户换台机器会以为「我明明选过」。
                (llmProvider
                  ? "。⚠ 这个选择存在本机浏览器里，会一直生效到你再切换"
                  : "。当前跟的是服务端默认") +
                "。切换只影响之后的提问，已有的回答仍是原来那个模型产出的"
              }
              className={clsx(
                "flex h-8 items-center gap-1.5 rounded-full px-3 text-[12.5px] transition-colors",
                // 选过（= 偏离了服务端默认）才上品牌色。没选时用「关」的配色：
                // 那时显示的就是服务端默认，把它画成高亮会让人以为「这是我开的东西」。
                llmProvider ? "bg-brand-soft text-brand" : "bg-hover text-muted hover:text-ink",
              )}
            >
              <Cpu className="h-3 w-3" />
              {currentLlm?.label ?? effectiveLlm}
            </button>
          )}

          <div className="ml-auto">
            {streaming ? (
              // 与发送按钮**同占一格、同尺寸（h-8 w-8）**，切换时不会跳动。
              // 2026-09-20 去掉了 border-line（这一行里的控件现在一律无描边），
              // 改成 hover 时底色加深一档：hover(240) → line(229) 亮色成立，
              // hover(48) → line(58) 暗色也成立，两态都不用加 dark: 变体。
              <button
                type="button"
                onClick={onStop}
                title="停止生成"
                className="flex h-8 w-8 items-center justify-center rounded-full bg-hover text-ink transition-colors hover:bg-line"
              >
                <Square className="h-3.5 w-3.5 fill-current" />
              </button>
            ) : (
              // ⚠ 这个按钮 2026-09-20 那次改版**一个字没动**：它本来就是「圆形实心 +
              // ArrowUp + 空输入时浅灰」，正是 OpenAI 那个形状，配色按项目约定保持品牌色
              // （CLAUDE.md：加颜色请沿用语义变量，不要硬编码 hex）。
              <button
                type="button"
                onClick={submit}
                disabled={!value.trim() || disabled}
                title="发送"
                className={clsx(
                  "flex h-8 w-8 items-center justify-center rounded-full transition-all",
                  value.trim() && !disabled
                    ? "bg-brand text-white hover:brightness-110"
                    : "cursor-not-allowed bg-hover text-faint",
                )}
              >
                <ArrowUp className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>
      </div>

      <p className="mt-2 text-center text-[11.5px] text-faint">
        内容由 AI 基于知识库生成，引用序号可点击查看来源，请结合原文核实
      </p>
    </div>
  );
}
