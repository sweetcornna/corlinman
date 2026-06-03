#!/usr/bin/env node
"use strict";

const https = require("https");

const API_KEY = (process.env.TINYFISH_API_KEY || "").trim();
const SEARCH_API_HOST = "api.search.tinyfish.ai";
const FETCH_API_HOST = "api.fetch.tinyfish.ai";
const DEBUG_MODE = (process.env.DebugMode || "false").toLowerCase() === "true";

function debugLog(msg, ...args) {
  if (DEBUG_MODE) console.error(`[TinyFish][Debug] ${msg}`, ...args);
}

function writeJson(obj) {
  process.stdout.write(`${JSON.stringify(obj)}\n`);
}

function success(id, result) {
  writeJson({ jsonrpc: "2.0", id, result });
}

function failure(id, message, code = -32000) {
  writeJson({ jsonrpc: "2.0", id, error: { code, message } });
}

function httpsRequest(options, postData = null) {
  return new Promise((resolve, reject) => {
    const req = https.request(options, (res) => {
      let data = "";
      res.on("data", (chunk) => { data += chunk; });
      res.on("end", () => {
        try {
          const parsed = data ? JSON.parse(data) : {};
          resolve({ statusCode: res.statusCode, headers: res.headers, body: parsed });
        } catch (error) {
          reject(new Error(`Response parse failed: ${error.message}. Raw: ${data.substring(0, 200)}`));
        }
      });
    });
    req.on("error", (error) => reject(new Error(`Network request failed: ${error.message}`)));
    req.setTimeout(60000, () => {
      req.destroy();
      reject(new Error("Request timed out (60s)"));
    });
    if (postData) req.write(postData);
    req.end();
  });
}

async function handleSearch(args) {
  const query = args.query || args.q || args.text || args.Query;
  if (!query) throw new Error("Missing required query.");

  const location = args.location || "";
  const language = args.language || "";
  const page = Number.parseInt(args.page ?? "0", 10);
  const thumbnails = args.thumbnails === true || args.thumbnails === "true";

  const params = new URLSearchParams();
  params.set("query", query);
  if (location) params.set("location", location);
  if (language) params.set("language", language);
  if (!Number.isNaN(page) && page >= 0 && page <= 10) params.set("page", String(page));
  if (thumbnails) params.set("thumbnails", "true");

  const path = `/?${params.toString()}`;
  debugLog(`Search request: ${SEARCH_API_HOST}${path}`);

  const res = await httpsRequest({
    hostname: SEARCH_API_HOST,
    port: 443,
    path,
    method: "GET",
    headers: {
      "X-API-Key": API_KEY,
      "User-Agent": "corlinman-tinyfish-browser/1.0",
    },
  });

  if (res.statusCode !== 200) {
    const errMsg = res.body?.error?.message || `API error: ${res.statusCode}`;
    throw new Error(`Search failed: ${errMsg}`);
  }

  const data = res.body;
  const results = data.results || [];
  return {
    query,
    total_results: data.total_results || results.length,
    page: data.page || 0,
    results: results.map((item) => ({
      position: item.position,
      title: item.title,
      snippet: item.snippet,
      url: item.url,
      site_name: item.site_name,
      thumbnail_url: item.thumbnail_url,
    })),
  };
}

async function handleFetch(args) {
  let urls = args.urls || args.url || args.Url;
  if (!urls) throw new Error("Missing required urls.");

  if (typeof urls === "string") {
    try {
      urls = JSON.parse(urls);
    } catch (_) {
      urls = [urls];
    }
  }
  if (!Array.isArray(urls)) urls = [urls];
  if (urls.length === 0) throw new Error("URL list cannot be empty.");
  if (urls.length > 10) urls = urls.slice(0, 10);

  const requestBody = {
    urls,
    format: args.format || "markdown",
    links: args.links === true || args.links === "true",
    image_links: args.image_links === true || args.image_links === "true" || args.imageLinks === true,
    include_html_head: args.include_html_head === true || args.include_html_head === "true" || args.includeHtmlHead === true,
  };
  const postData = JSON.stringify(requestBody);
  debugLog(`Fetch request: ${FETCH_API_HOST}/ body=${postData.substring(0, 300)}`);

  const res = await httpsRequest({
    hostname: FETCH_API_HOST,
    port: 443,
    path: "/",
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-API-Key": API_KEY,
      "User-Agent": "corlinman-tinyfish-browser/1.0",
      "Content-Length": Buffer.byteLength(postData),
    },
  }, postData);

  if (res.statusCode !== 200) {
    const errMsg = res.body?.error?.message || `API error: ${res.statusCode}`;
    throw new Error(`Fetch failed: ${errMsg}`);
  }

  return {
    results: res.body.results || [],
    errors: res.body.errors || [],
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
    failure(null, "Invalid JSON-RPC request.");
    return;
  }

  const id = Object.prototype.hasOwnProperty.call(request, "id") ? request.id : null;
  try {
    if (!API_KEY) throw new Error("Set TINYFISH_API_KEY first.");
    const method = request.method;
    const params = request.params && typeof request.params === "object" ? request.params : {};
    if (method === "tinyfish_search") {
      success(id, await handleSearch(params));
      return;
    }
    if (method === "tinyfish_fetch") {
      success(id, await handleFetch(params));
      return;
    }
    throw new Error(`Unknown tool: ${method}`);
  } catch (error) {
    failure(id, error.message || String(error));
  }
}

main();
