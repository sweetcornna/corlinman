#!/usr/bin/env node
"use strict";

const https = require("https");

const DEFAULT_ENDPOINT = "https://api.anysearch.com/mcp";
const AVAILABLE_DOMAINS = new Set([
  "code", "tech", "fashion", "travel", "home", "ecommerce",
  "gaming", "film", "music", "finance", "academic", "legal",
  "business", "ip", "security", "education", "health", "religion",
  "geo", "environment", "energy", "ugc",
]);
const CONTENT_TYPES = new Set([
  "web", "news", "code", "doc", "academic", "data", "image", "video", "audio",
]);
const FRESHNESS_VALUES = new Set(["day", "week", "month", "year"]);
const ZONES = new Set(["cn", "intl"]);

function writeJson(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function success(id, result) {
  writeJson({ jsonrpc: "2.0", id, result });
}

function failure(id, message, code = -32000) {
  writeJson({ jsonrpc: "2.0", id, error: { code, message } });
}

function firstString(payload, keys) {
  for (const key of keys) {
    const value = payload[key];
    if (typeof value === "string" && value.trim()) return value.trim();
  }
  return "";
}

function parseStringList(value, fieldName) {
  if (value === undefined || value === null || value === "") return undefined;
  if (Array.isArray(value)) {
    return value.map((item) => String(item).trim()).filter(Boolean);
  }
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!trimmed) return undefined;
    try {
      const parsed = JSON.parse(trimmed);
      if (Array.isArray(parsed)) return parsed.map((item) => String(item).trim()).filter(Boolean);
      return [String(parsed).trim()].filter(Boolean);
    } catch (_) {
      return trimmed.split(",").map((item) => item.trim()).filter(Boolean);
    }
  }
  throw new Error(`${fieldName} must be a string or array.`);
}

function parseInteger(value, fieldName, min, max) {
  if (value === undefined || value === null || value === "") return undefined;
  const parsed = Number.parseInt(value, 10);
  if (Number.isNaN(parsed)) throw new Error(`${fieldName} must be an integer.`);
  return Math.max(min, Math.min(max, parsed));
}

function parseJsonObject(value, fieldName) {
  if (value === undefined || value === null || value === "") return undefined;
  if (typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value === "string") {
    try {
      const parsed = JSON.parse(value);
      if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) return parsed;
    } catch (_) {
      // fall through
    }
  }
  throw new Error(`${fieldName} must be a JSON object.`);
}

function validateAllowed(value, allowed, fieldName) {
  if (value !== undefined && !allowed.has(value)) {
    throw new Error(`Invalid ${fieldName}: ${value}.`);
  }
}

function parseApiKeys() {
  const raw = (process.env.ANYSEARCH_API_KEY || "").trim();
  if (!raw) return [];
  return raw.split(",").map((item) => item.trim()).filter(Boolean);
}

function pickApiKey() {
  const apiKeys = parseApiKeys();
  if (apiKeys.length === 0) return "";
  return apiKeys[Math.floor(Math.random() * apiKeys.length)];
}

function getTimeoutMs() {
  const parsed = Number.parseInt(process.env.ANYSEARCH_TIMEOUT_MS || "", 10);
  if (Number.isNaN(parsed)) return 30000;
  return Math.max(1000, Math.min(120000, parsed));
}

function getEndpoint() {
  const endpoint = (process.env.ANYSEARCH_ENDPOINT || DEFAULT_ENDPOINT).trim();
  return endpoint || DEFAULT_ENDPOINT;
}

function buildSearchArgs(argumentsObj) {
  const query = firstString(argumentsObj, ["query", "q", "text", "Query"]);
  if (!query) throw new Error("Missing required argument: query.");

  const args = { query };
  const domain = firstString(argumentsObj, ["domain", "Domain"]);
  const subDomain = firstString(argumentsObj, ["sub_domain", "subDomain", "subdomain"]);
  const zone = firstString(argumentsObj, ["zone"]);
  const freshness = firstString(argumentsObj, ["freshness"]);
  const contentTypes = parseStringList(argumentsObj.content_types ?? argumentsObj.contentTypes, "content_types");
  const maxResults = parseInteger(argumentsObj.max_results ?? argumentsObj.maxResults, "max_results", 1, 100);
  const subDomainParams = parseJsonObject(argumentsObj.sub_domain_params ?? argumentsObj.subDomainParams, "sub_domain_params");

  if (domain) {
    validateAllowed(domain, AVAILABLE_DOMAINS, "domain");
    args.domain = domain;
  }
  if (subDomain) args.sub_domain = subDomain;
  if (subDomainParams) args.sub_domain_params = subDomainParams;
  if (contentTypes) {
    for (const type of contentTypes) validateAllowed(type, CONTENT_TYPES, "content_types");
    args.content_types = contentTypes;
  }
  if (zone) {
    validateAllowed(zone, ZONES, "zone");
    args.zone = zone;
  }
  if (freshness) {
    validateAllowed(freshness, FRESHNESS_VALUES, "freshness");
    args.freshness = freshness;
  }
  if (maxResults !== undefined) args.max_results = maxResults;

  return args;
}

function buildListDomainsArgs(argumentsObj) {
  const domains = parseStringList(argumentsObj.domains, "domains");
  const domain = firstString(argumentsObj, ["domain", "Domain"]);

  if (domains && domains.length > 0) {
    if (domains.length > 5) throw new Error("domains supports a maximum of 5 domains.");
    for (const item of domains) validateAllowed(item, AVAILABLE_DOMAINS, "domains");
    return { domains };
  }

  if (!domain) return {};
  validateAllowed(domain, AVAILABLE_DOMAINS, "domain");
  return { domain };
}

function normalizeBatchQueries(value) {
  if (value === undefined || value === null || value === "") {
    throw new Error("Missing required argument: queries.");
  }

  let queries = value;
  if (typeof value === "string") {
    try {
      queries = JSON.parse(value);
    } catch (_) {
      queries = value.split("|").map((item) => ({ query: item.trim() })).filter((item) => item.query);
    }
  }

  if (!Array.isArray(queries)) queries = [queries];
  if (queries.length < 1 || queries.length > 5) throw new Error("batch search requires 1-5 queries.");

  return queries.map((item) => {
    if (typeof item === "string") return { query: item };
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      throw new Error("Each batch query must be a string or object.");
    }
    if (!item.query && !item.q && !item.text) {
      throw new Error("Each batch query requires query.");
    }
    return { ...item, query: item.query || item.q || item.text };
  });
}

function buildExtractArgs(argumentsObj) {
  const url = firstString(argumentsObj, ["url", "URL", "link"]);
  if (!url) throw new Error("Missing required argument: url.");
  if (!/^https?:\/\//i.test(url)) throw new Error("url must start with http:// or https://.");
  return { url };
}

function mapToolToAnySearch(toolName, argumentsObj) {
  switch (toolName) {
    case "any_search_search":
      return { method: "search", args: buildSearchArgs(argumentsObj) };
    case "any_search_list_domains":
      return { method: "list_domains", args: buildListDomainsArgs(argumentsObj) };
    case "any_search_batch_search":
      return { method: "batch_search", args: { queries: normalizeBatchQueries(argumentsObj.queries ?? argumentsObj.query_items) } };
    case "any_search_extract":
      return { method: "extract", args: buildExtractArgs(argumentsObj) };
    default:
      throw new Error(`Unknown tool: ${toolName}`);
  }
}

function callAnySearch(method, args) {
  const payload = JSON.stringify({
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: { name: method, arguments: args },
  });

  const url = new URL(getEndpoint());
  const headers = {
    "Content-Type": "application/json",
    "Content-Length": Buffer.byteLength(payload),
  };
  const apiKey = pickApiKey();
  if (apiKey) headers.Authorization = `Bearer ${apiKey}`;

  const options = {
    hostname: url.hostname,
    port: url.port || 443,
    path: `${url.pathname}${url.search}`,
    method: "POST",
    headers,
  };

  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (chunk) => { body += chunk; });
      res.on("end", () => {
        let data;
        try {
          data = JSON.parse(body);
        } catch (_) {
          reject(new Error(`Non-JSON response from API: ${body.slice(0, 500)}`));
          return;
        }

        if (res.statusCode >= 400) {
          reject(new Error(`HTTP ${res.statusCode}: ${JSON.stringify(data)}`));
          return;
        }
        if (data.error) {
          reject(new Error(data.error.message || JSON.stringify(data.error)));
          return;
        }

        resolve(data.result || {});
      });
    });

    req.setTimeout(getTimeoutMs(), () => {
      req.destroy(new Error("API request timed out."));
    });
    req.on("error", reject);
    req.write(payload);
    req.end();
  });
}

async function main() {
  const line = await new Promise((resolve) => {
    let raw = "";
    process.stdin.setEncoding("utf8");
    process.stdin.on("data", (chunk) => { raw += chunk; });
    process.stdin.on("end", () => resolve(raw.replace(/^\uFEFF/, "")));
  });

  let request;
  try {
    request = JSON.parse(line);
  } catch (_) {
    failure(null, "Invalid JSON-RPC request.");
    return;
  }

  const id = Object.prototype.hasOwnProperty.call(request, "id") ? request.id : null;
  try {
    const toolName = request.method;
    if (typeof toolName !== "string" || !toolName.trim()) {
      throw new Error("Missing JSON-RPC method.");
    }
    const argumentsObj = request.params && typeof request.params === "object" ? request.params : {};
    const mapped = mapToolToAnySearch(toolName, argumentsObj);
    const result = await callAnySearch(mapped.method, mapped.args);
    success(id, result);
  } catch (error) {
    failure(id, error.message || String(error));
  }
}

main();
