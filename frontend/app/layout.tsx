import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "招投标智能问答助手",
  description:
    "基于 Qdrant 向量检索 + BM25 混合召回、DeepSeek 生成的招投标与政府采购智能问答机器人",
};

/** 在首屏渲染前恢复主题，避免闪白 */
const themeScript = `
(function () {
  try {
    var saved = localStorage.getItem('bidding-qa-theme');
    var prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
    if (saved === 'dark' || (!saved && prefersDark)) {
      document.documentElement.classList.add('dark');
    }
  } catch (e) {}
})();
`;

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: themeScript }} />
      </head>
      <body className="h-full antialiased">{children}</body>
    </html>
  );
}
