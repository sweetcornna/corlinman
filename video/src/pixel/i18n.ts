import { createContext, useContext } from "react";

export type Locale = "en" | "zh";

export const COPY = {
  en: {
    s1_title: "GENESIS", s1_sub: "00:00",
    s2_title: "ASSEMBLE", s2_sub: "ONE BINARY",
    s3_title: "AGENT", s3_sub: "v0.1 · MIT",
    s3_above_title: "SELF-HOST · OPEN SOURCE",
    s4_title: "STREAM", s4_sub: "SSE · OPENAI-COMPATIBLE",
    s5_title: "PROVIDERS", s5_sub: "6 · HOT-SWAP RUNTIME",
    s6_title: "PLUGINS", s6_sub: "JSON-RPC 2.0 · SANDBOXED",
    s7_title: "TOOLS", s7_sub: "HOT-SWAP · RUNTIME",
    s8_title: "SWARM", s8_sub: "16 AGENTS · 47 SKILLS",
    s9_title: "HUMAN IN THE LOOP", s9_sub: "APPROVAL-GATED",
    s10_title: "TIDEPOOL", s10_sub: "DAY · NIGHT · ONE CONSOLE",
    s11_title: "OBSERVE", s11_sub: "LOCK-ON · OBSERVABLE",
    s12_title: "MIT · v0.1",
    tagline: "self-host the agent · own the loop",
    mouth_stream: "> stream(token)",
    mouth_tool: "> tool.call",
    mouth_approve: "> approved",
  },
  zh: {
    s1_title: "启 动", s1_sub: "第 零 秒",
    s2_title: "装 配", s2_sub: "单 文 件 部 署",
    s3_title: "智 能 体", s3_sub: "v0.1 · MIT",
    s3_above_title: "自 部 署 · 开 源",
    s4_title: "流 式 响 应", s4_sub: "SSE · OpenAI 兼 容",
    s5_title: "模 型 供 应 商", s5_sub: "六 家 · 热 切 换",
    s6_title: "插 件", s6_sub: "JSON-RPC · 沙 箱 化",
    s7_title: "工 具", s7_sub: "热 切 换 · 运 行 时",
    s8_title: "集 群", s8_sub: "16 个 体 · 47 技 能",
    s9_title: "人 在 回 路", s9_sub: "操 作 前 必 审 批",
    s10_title: "控 制 台", s10_sub: "日 夜 · 同 一 界 面",
    s11_title: "可 观 测", s11_sub: "锁 定 · 全 程 可 追 溯",
    s12_title: "MIT · v0.1",
    tagline: "自 己 部 署 · 完 全 掌 控",
    mouth_stream: "> 数 据 流",
    mouth_tool: "> 调 用 工 具",
    mouth_approve: "> 已 批 准",
  },
} as const;

export type CopyKey = keyof (typeof COPY)["en"];

export const LocaleContext = createContext<Locale>("en");

export const useLocale = (): Locale => useContext(LocaleContext);

export const useCopy = () => {
  const locale = useLocale();
  return COPY[locale];
};
