#!/usr/bin/env node
"use strict";

try {
  require.resolve("axios");
  require.resolve("dotenv");
  require.resolve("https-proxy-agent");
  require.resolve("@tavily/core");
} catch (error) {
  const payload = {
    jsonrpc: "2.0",
    id: null,
    error: {
      code: -32000,
      message: "v-search requires npm dependencies that are not bundled. Install them in marketplace/plugins/v-search with `npm install` before enabling this plugin.",
    },
  };
  process.stdout.write(`${JSON.stringify(payload)}\n`);
  process.exit(0);
}

const fs = require("fs").promises;
const path = require("path");
const axios = require("axios");
const dotenv = require("dotenv");
const { HttpsProxyAgent } = require("https-proxy-agent");
const { tavily } = require("@tavily/core");

const configPath = path.resolve(__dirname, "./config.env");
const rootConfigPath = path.resolve(__dirname, "../../config.env");
const manifestPath = path.resolve(__dirname, "./plugin-manifest.toml");

dotenv.config({ path: configPath });
dotenv.config({ path: rootConfigPath, override: false });

const {
  VSearchKey: API_KEY,
  VSearchUrl: API_URL,
  VSearchModel: MODEL,
  GrokModel: GROK_MODEL,
  TavilyModel: TAVILY_MODEL,
  SummaryKey: SUMMARY_KEY,
  SummaryUrl: SUMMARY_URL,
  SummaryModel: SUMMARY_MODEL,
  VSearchMaxToken: MAX_TOKENS,
  MaxConcurrent: MAX_CONCURRENT,
  HTTP_PROXY: PROXY,
  KimiSearchUrl: KIMI_SEARCH_URL,
  KimiSearchKey: KIMI_SEARCH_KEY,
  KimiSearchMaxResults: KIMI_SEARCH_MAX_RESULTS,
  KimiSearchIncludeContent: KIMI_SEARCH_INCLUDE_CONTENT,
  SearchMode: DEFAULT_SEARCH_MODE,
} = process.env;

const CONCURRENCY = Number.parseInt(MAX_CONCURRENT, 10) || 5;
const TOKENS = Number.parseInt(MAX_TOKENS, 10) || 50000;
const KIMI_MAX_RESULTS = Math.min(Math.max(Number.parseInt(KIMI_SEARCH_MAX_RESULTS, 10) || 5, 1), 20);
const KIMI_INCLUDE_CONTENT = KIMI_SEARCH_INCLUDE_CONTENT === "true";
const DEFAULT_PLUGIN_TIMEOUT_MS = 300000;
const MIN_SAFE_REPLY_MARGIN_MS = 5000;
const MAX_SAFE_REPLY_MARGIN_MS = 15000;
const GROK_MAX_RETRIES = 3;
const GROK_BASE_RETRY_DELAY_MS = 1200;

const log = (message) => {
  console.error(`[v-search] ${new Date().toISOString()}: ${message}`);
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
const getRemainingMs = (deadline) => Math.max(0, deadline - Date.now());
const getSafeReplyMarginMs = (timeoutMs) => {
  return Math.min(MAX_SAFE_REPLY_MARGIN_MS, Math.max(MIN_SAFE_REPLY_MARGIN_MS, Math.floor(timeoutMs * 0.05)));
};

async function loadPluginTimeoutMs() {
  try {
    const manifestContent = await fs.readFile(manifestPath, "utf8");
    const match = manifestContent.match(/timeout_ms\s*=\s*(\d+)/);
    if (match) return Number.parseInt(match[1], 10);
  } catch (error) {
    log(`failed to read plugin timeout: ${error.message}`);
  }
  return DEFAULT_PLUGIN_TIMEOUT_MS;
}

async function createDeadlineContext() {
  const timeoutMs = await loadPluginTimeoutMs();
  const safeMarginMs = getSafeReplyMarginMs(timeoutMs);
  const deadline = Date.now() + Math.max(1000, timeoutMs - safeMarginMs);
  return { timeoutMs, deadline };
}

function isGrokRetryableError(error) {
  const status = error?.response?.status;
  const message = (error?.message || "").toLowerCase();
  return status === 503 || message.includes("503") || message.includes("empty") || message.includes("空响应");
}

function cleanGrokContent(content) {
  return content.replace(/<think>[\s\S]*?<\/think>/g, "").trim();
}

async function resolveRedirect(url, signal) {
  if (!url || !url.includes("vertexaisearch.cloud.google.com/grounding-api-redirect")) return url;
  let targetUrl = url.trim();
  if (!targetUrl.startsWith("http")) targetUrl = `https://${targetUrl}`;
  try {
    const axiosConfig = {
      maxRedirects: 5,
      timeout: 15000,
      headers: { "User-Agent": "Mozilla/5.0" },
      responseType: "text",
      signal,
    };
    if (PROXY) {
      axiosConfig.httpsAgent = new HttpsProxyAgent(PROXY);
      axiosConfig.proxy = false;
    }
    const response = await axios.get(targetUrl, axiosConfig);
    const finalUrl = response.request?.res?.responseUrl || targetUrl;
    if (finalUrl !== targetUrl && !finalUrl.includes("grounding-api-redirect")) return finalUrl;
  } catch (error) {
    const fallbackUrl = error.request?.res?.responseUrl;
    if (fallbackUrl && fallbackUrl !== targetUrl && !fallbackUrl.includes("grounding-api-redirect")) return fallbackUrl;
  }
  return targetUrl;
}

async function callGroundingMode(topic, keyword, showURL, deadline, signal) {
  const currentTime = new Date().toLocaleString("zh-CN", { timeZone: "Asia/Shanghai" });
  const systemPrompt = `你是一个专业的语义搜索助手。当前系统时间: ${currentTime}。\n你的任务是根据用户提供的【检索目标主题】和具体的【检索关键词】，从互联网获取最相关、最准确的信息。`;
  const fullSystemPrompt = `${systemPrompt}\n请围绕检索目标给出结构化总结。${showURL ? "保留完整来源 URL。" : "尽量节省 URL 输出。"}`;
  const payload = {
    model: MODEL,
    messages: [
      { role: "system", content: fullSystemPrompt },
      { role: "user", content: `【检索目标主题】：${topic}\n【当前检索关键词】：${keyword}` },
    ],
    stream: false,
    max_tokens: TOKENS,
    tool_choice: "auto",
    tools: [{
      type: "function",
      function: {
        name: "googleSearch",
        description: "从谷歌搜索引擎获取实时信息。",
        parameters: { type: "object", properties: { query: { type: "string" } } },
      },
    }],
  };
  const remaining = deadline ? getRemainingMs(deadline) : 180000;
  if (remaining <= 0) return `[搜索超时] ${keyword}`;
  const response = await axios.post(API_URL, payload, {
    headers: { Authorization: `Bearer ${API_KEY}`, "Content-Type": "application/json" },
    timeout: Math.min(180000, remaining),
    signal,
    proxy: false,
  });
  let content = response.data.choices[0].message.content;
  try {
    const metadata = response.data.choices[0].message?.grounding_metadata || response.data.choices[0]?.grounding_metadata;
    const vertexUrlRegex = /(?:https?:\/\/)?vertexaisearch\.cloud\.google\.com\/grounding-api-redirect\/[\w\-=]+/g;
    const foundUrls = content.match(vertexUrlRegex) || [];
    const metadataUrls = (metadata && metadata.grounding_chunks)
      ? metadata.grounding_chunks.filter((chunk) => chunk.web).map((chunk) => chunk.web.uri)
      : [];
    const allVertexUrls = [...new Set([...foundUrls, ...metadataUrls])];
    const urlMap = new Map();
    if (allVertexUrls.length > 0) {
      await Promise.all(allVertexUrls.map(async (vertexUrl) => {
        const realUrl = await resolveRedirect(vertexUrl, signal);
        if (realUrl !== vertexUrl) urlMap.set(vertexUrl, realUrl);
      }));
      for (const [original, resolved] of urlMap.entries()) {
        content = content.split(original).join(resolved);
      }
    }
  } catch (error) {
    log(`grounding redirect normalization failed: ${error.message}`);
  }
  return content;
}

async function callGrokMode(topic, keyword, showURL, deadline, signal) {
  const payload = {
    model: GROK_MODEL || MODEL,
    messages: [
      {
        role: "system",
        content: `你是一个专业搜索助手。围绕主题“${topic}”提炼关键词“${keyword}”的最重要信息。${showURL ? "保留来源 URL。" : ""}`,
      },
      { role: "user", content: keyword },
    ],
    stream: false,
    max_tokens: TOKENS,
  };
  let lastError;
  for (let attempt = 1; attempt <= GROK_MAX_RETRIES; attempt += 1) {
    try {
      const remaining = getRemainingMs(deadline);
      if (remaining <= 0) return `[搜索超时] ${keyword}`;
      const response = await axios.post(API_URL, payload, {
        headers: { Authorization: `Bearer ${API_KEY}`, "Content-Type": "application/json" },
        timeout: Math.min(180000, remaining),
        signal,
      });
      return cleanGrokContent(response.data.choices[0].message.content || "");
    } catch (error) {
      lastError = error;
      if (!isGrokRetryableError(error) || attempt === GROK_MAX_RETRIES) break;
      await sleep(GROK_BASE_RETRY_DELAY_MS * attempt);
    }
  }
  throw lastError;
}

async function callTavilyMode(topic, keyword, showURL, deadline) {
  if (!process.env.TavilyKey) throw new Error("Set TavilyKey first.");
  const client = tavily({ apiKey: process.env.TavilyKey });
  const remaining = getRemainingMs(deadline);
  if (remaining <= 0) return `[搜索超时] ${keyword}`;
  const searchResult = await client.search(keyword, {
    maxResults: 5,
    includeAnswer: false,
    includeRawContent: false,
    includeImages: false,
    searchDepth: "advanced",
    topic: "general",
  });
  return summarizeExternalResults(topic, keyword, searchResult.results || [], showURL, deadline);
}

async function callKimiSearchMode(topic, keyword, showURL, deadline) {
  if (!KIMI_SEARCH_URL || !KIMI_SEARCH_KEY) throw new Error("Set KimiSearchUrl and KimiSearchKey first.");
  const remaining = getRemainingMs(deadline);
  if (remaining <= 0) return `[搜索超时] ${keyword}`;
  const response = await axios.post(
    `${KIMI_SEARCH_URL.replace(/\/$/, "")}/search`,
    {
      query: keyword,
      max_results: KIMI_MAX_RESULTS,
      include_content: KIMI_INCLUDE_CONTENT,
    },
    {
      headers: { Authorization: `Bearer ${KIMI_SEARCH_KEY}`, "Content-Type": "application/json" },
      timeout: Math.min(120000, remaining),
    },
  );
  return summarizeExternalResults(topic, keyword, response.data.results || [], showURL, deadline);
}

async function summarizeExternalResults(topic, keyword, results, showURL, deadline) {
  const apiKey = SUMMARY_KEY || API_KEY;
  const apiUrl = SUMMARY_URL || API_URL;
  const model = SUMMARY_MODEL || TAVILY_MODEL || MODEL;
  if (!apiKey || !apiUrl || !model) {
    return JSON.stringify({ topic, keyword, results }, null, 2);
  }
  const remaining = getRemainingMs(deadline);
  if (remaining <= 0) return `[搜索超时] ${keyword}`;
  const compactResults = results.slice(0, 8).map((item, index) => ({
    rank: index + 1,
    title: item.title || "",
    url: item.url || item.link || "",
    snippet: item.content || item.snippet || item.summary || "",
  }));
  const payload = {
    model,
    messages: [
      {
        role: "system",
        content: `你是研究助理。请围绕主题“${topic}”总结关键词“${keyword}”的搜索结果。${showURL ? "保留关键来源 URL。" : "尽量减少 URL。"}`
      },
      {
        role: "user",
        content: JSON.stringify(compactResults, null, 2),
      },
    ],
    stream: false,
    max_tokens: TOKENS,
  };
  const response = await axios.post(apiUrl, payload, {
    headers: { Authorization: `Bearer ${apiKey}`, "Content-Type": "application/json" },
    timeout: Math.min(120000, remaining),
  });
  return response.data.choices?.[0]?.message?.content || JSON.stringify(compactResults, null, 2);
}

function splitKeywords(keywordsRaw) {
  return String(keywordsRaw)
    .split(/\n|,/)
    .map((item) => item.trim())
    .filter(Boolean);
}

async function runResearch(params) {
  const topic = String(params.SearchTopic || params.search_topic || "").trim();
  const keywordsRaw = String(params.Keywords || params.keywords || "").trim();
  const mode = String(params.SearchMode || params.search_mode || DEFAULT_SEARCH_MODE || "kimisearch").trim().toLowerCase();
  const showURL = params.ShowURL === true || params.show_url === true || params.ShowURL === "true";

  if (!topic) throw new Error("Missing required SearchTopic.");
  if (!keywordsRaw) throw new Error("Missing required Keywords.");

  const keywords = splitKeywords(keywordsRaw);
  if (keywords.length === 0) throw new Error("No valid keywords found.");

  const { deadline } = await createDeadlineContext();
  const controller = new AbortController();
  const signal = controller.signal;

  const worker = async (keyword) => {
    switch (mode) {
      case "grounding":
        return callGroundingMode(topic, keyword, showURL, deadline, signal);
      case "grok":
        return callGrokMode(topic, keyword, showURL, deadline, signal);
      case "tavily":
        return callTavilyMode(topic, keyword, showURL, deadline);
      case "kimisearch":
        return callKimiSearchMode(topic, keyword, showURL, deadline);
      default:
        throw new Error(`Unsupported SearchMode: ${mode}`);
    }
  };

  const queue = [...keywords];
  const outputs = [];
  const concurrency = Math.max(1, Math.min(CONCURRENCY, queue.length));
  const workers = Array.from({ length: concurrency }, async () => {
    while (queue.length > 0) {
      const keyword = queue.shift();
      if (!keyword) return;
      try {
        const content = await worker(keyword);
        outputs.push({ keyword, content });
      } catch (error) {
        outputs.push({ keyword, content: `[搜索失败] ${keyword}: ${error.message || String(error)}` });
      }
    }
  });

  await Promise.all(workers);
  outputs.sort((a, b) => keywords.indexOf(a.keyword) - keywords.indexOf(b.keyword));

  return {
    topic,
    search_mode: mode,
    keyword_count: keywords.length,
    results: outputs,
    message: outputs.map((item, index) => `## ${index + 1}. ${item.keyword}\n\n${item.content}`).join("\n\n"),
  };
}

async function main() {
  const raw = await new Promise((resolve) => {
    let data = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { data += chunk; });
    process.stdin.on("end", () => resolve(data));
  });

  let request;
  try {
    request = JSON.parse(raw);
  } catch (_) {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id: null, error: { code: -32000, message: "Invalid JSON-RPC request." } })}\n`);
    return;
  }

  const id = Object.prototype.hasOwnProperty.call(request, "id") ? request.id : null;
  try {
    const method = request.method;
    if (method !== "v_search_research") throw new Error(`Unknown tool: ${method}`);
    const params = request.params && typeof request.params === "object" ? request.params : {};
    const result = await runResearch(params);
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({ jsonrpc: "2.0", id, error: { code: -32000, message: error.message || String(error) } })}\n`);
  }
}

main();
