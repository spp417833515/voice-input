#!/usr/bin/env node
/* 译文页 → 宿主“已呈现”接缝的页面半边：在 vm 里加载真实 static/v3 业务脚本（translate-controller /
   platform / bootstrap / http / catalog / context），用假 fetch 与假 WebKit 桥，输出每个场景的逻辑时序
   与桥上真实发出的原始字符串。宿主半边（parse_presented_message + TranslateLifecycle）在 pytest 中消费。
   不需要 gi，不开网络，不碰剪贴板。 */
"use strict";
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const repo = path.resolve(__dirname, "../..");
const biz = path.join(repo, "static/v3/js/business");
const openapi = JSON.parse(fs.readFileSync(path.join(repo, "static/v3/openapi.json"), "utf8"));
const ORIGIN = "https://127.0.0.1:17945";

function jsonRes(status, body) {
  return {
    ok: status >= 200 && status < 300,
    status: status,
    text: function () { return Promise.resolve(JSON.stringify(body)); },
    json: function () { return Promise.resolve(body); }
  };
}

function fakeWindow(log, opts) {
  const listeners = {};
  const w = {
    addEventListener: function (n, fn) { (listeners[n] = listeners[n] || []).push(fn); },
    dispatch: function (n) { (listeners[n] || []).forEach(function (fn) { fn(); }); }
  };
  if (opts.generation) w.__V3_TRANSLATE_GENERATION__ = opts.generation;
  if (opts.bridge) {
    w.webkit = { messageHandlers: { oviTranslatePresented: { postMessage: function (raw) { log.push({ k: "bridge", raw: raw }); } } } };
  }
  return w;
}

function load(log, winOpts) {
  const win = fakeWindow(log, winOpts);
  const ctx = {
    window: win, console: console, FormData: FormData, Blob: Blob, URLSearchParams: URLSearchParams,
    setTimeout: setTimeout, clearTimeout: clearTimeout, Promise: Promise, JSON: JSON, Object: Object,
    Array: Array, Error: Error, Uint8Array: Uint8Array
  };
  ctx.globalThis = ctx;
  win.V3Business = {};
  vm.createContext(ctx);
  ["catalog.js", "context.js", "http.js", "clipboard.js", "platform.js", "translate-controller.js", "bootstrap.js"].forEach(function (n) {
    vm.runInContext(fs.readFileSync(path.join(biz, n), "utf8"), ctx, { filename: n });
  });
  return { B: win.V3Business, win: win, ctx: ctx };
}

function recordingView(log) {
  let state = {};
  return {
    setState: function (patch) {
      state = Object.assign({}, state, patch);
      log.push({ k: "dom", phase: state.phase, text: state.text || "", error: state.error || null });
    },
    getState: function () { return state; }
  };
}

function world(log, routes) {
  return function (url, init) {
    const method = (init && init.method) || "GET";
    const p = new URL(String(url)).pathname;
    log.push({ k: "fetch", method: method, path: p, body: p === "/api/v1/events/platform" ? JSON.parse(init.body) : undefined });
    const h = routes[method + " " + p];
    if (!h) return Promise.resolve(jsonRes(404, { ok: false, error: "not_found", message: p }));
    return Promise.resolve(h(init));
  };
}

function controller(log, B, routes, presented, extra) {
  const ctxObj = B.createContext({ protocol: "https:", hostname: "127.0.0.1", origin: ORIGIN, href: ORIGIN + "/overlay-translate", search: "" });
  const catalog = B.createCatalog(openapi);
  const client = B.createHttp({ catalog: catalog, context: ctxObj, fetch: world(log, routes), origin: ORIGIN });
  const ctl = B.createTranslateController(Object.assign({
    client: client, context: ctxObj, clipboard: { writeText: function () { return Promise.resolve(); } },
    presented: presented, initialImage: new Blob([new Uint8Array([137, 80, 78, 71])], { type: "image/png" })
  }, extra || {}));
  ctl.bind(recordingView(log));
  return ctl;
}

const never = function () { return new Promise(function () {}); };
const GEN_A = "A".repeat(32);
const GEN_B = "B".repeat(32);

async function scenario(name, fn) {
  const log = [];
  try { await fn(log); } catch (e) { log.push({ k: "exception", message: String(e && e.message || e) }); }
  return [name, log];
}

(async function main() {
  const out = {};
  const ok = function () { return jsonRes(202, { ok: true }); };
  const runs = [
    /* API 2xx 已返回，但 platform idle 上报一直挂起：DOM 仍立即呈现，桥在 DOM 之后发且不等 idle */
    scenario("idle_pending", async function (log) {
      const { B, win } = load(log, { bridge: true, generation: GEN_A });
      const ctl = controller(log, B, {
        "GET /api/v1/config": function () { return jsonRes(200, { translate_target_lang: "en" }); },
        "POST /api/v1/events/platform": function (init) { return JSON.parse(init.body).event === "busy" ? ok() : never(); },
        "POST /api/v1/ocr-translate": function () { return jsonRes(200, { ok: true, text: "Hello", ocr_text: "你好", target_lang: "en" }); }
      }, B.createPresentedNotifier({ window: win }));
      await ctl.start();
      await new Promise(function (r) { setTimeout(r, 20); });
      log.push({ k: "end" });
    }),
    /* 配置初始化失败且无回退语言：不发 ocr，页内错误后立即通知 error */
    scenario("config_fail", async function (log) {
      const { B, win } = load(log, { bridge: true, generation: GEN_A });
      const ctl = controller(log, B, {
        "GET /api/v1/config": function () { return jsonRes(500, { ok: false, error: "internal", message: "配置读取失败" }); }
      }, B.createPresentedNotifier({ window: win }));
      await ctl.start();
      log.push({ k: "end" });
    }),
    /* OCR/翻译接口错误：页内真实错误码，error 只通知一次，platform error 带同一错误码 */
    scenario("ocr_error", async function (log) {
      const { B, win } = load(log, { bridge: true, generation: GEN_A });
      const ctl = controller(log, B, {
        "GET /api/v1/config": function () { return jsonRes(200, { translate_target_lang: "en" }); },
        "POST /api/v1/events/platform": ok,
        "POST /api/v1/ocr-translate": function () { return jsonRes(502, { ok: false, error: "translate_failed", message: "模型不可用" }); }
      }, B.createPresentedNotifier({ window: win }));
      await ctl.start();
      await ctl.platformSettled();
      ctl.applyResponse({ text: "迟到", ocr_text: "", target_lang: "en" }, "en"); // 重复呈现不再通知
      log.push({ k: "end" });
    }),
    /* 网络层异常（非 HTTP）：仍显示可见错误并通知 */
    scenario("network_exception", async function (log) {
      const { B, win } = load(log, { bridge: true, generation: GEN_A });
      const routes = {
        "GET /api/v1/config": function () { return jsonRes(200, { translate_target_lang: "en" }); },
        "POST /api/v1/events/platform": ok
      };
      const ctl = controller(log, B, routes, B.createPresentedNotifier({ window: win }));
      routes["POST /api/v1/ocr-translate"] = function () { throw new TypeError("Failed to fetch"); };
      await ctl.start();
      log.push({ k: "end" });
    }),
    /* 接口说明加载失败（bootstrap 早期失败）：挂可见错误后通知 */
    scenario("openapi_fail", async function (log) {
      const { B, win, ctx } = load(log, { bridge: true, generation: GEN_A });
      win.V3 = { mountTranslateOverlay: function () { return recordingView(log); } };
      const ctxObj = B.createContext({ protocol: "https:", hostname: "127.0.0.1", origin: ORIGIN, href: ORIGIN + "/overlay-translate", search: "" });
      const doc = { getElementById: function () { return {}; }, addEventListener: function () {} };
      ctx.document = doc; // bootstrap.mountView 读全局 document
      await B.startPage("translate", {
        context: ctxObj,
        window: win,
        document: doc,
        fetch: function (url) { log.push({ k: "fetch", method: "GET", path: new URL(String(url), ORIGIN).pathname }); return Promise.resolve(jsonRes(500, {})); },
        origin: ORIGIN
      });
      log.push({ k: "end" });
    }),
    /* 浏览器直接打开（无桥）：页面照常呈现，不抛错 */
    scenario("no_bridge", async function (log) {
      const { B, win } = load(log, { bridge: false, generation: null });
      const ctl = controller(log, B, {
        "GET /api/v1/config": function () { return jsonRes(200, { translate_target_lang: "en" }); },
        "POST /api/v1/events/platform": ok,
        "POST /api/v1/ocr-translate": function () { return jsonRes(200, { ok: true, text: "Hello", ocr_text: "你好", target_lang: "en" }); }
      }, B.createPresentedNotifier({ window: win }));
      await ctl.start();
      log.push({ k: "end" });
    }),
    /* pagehide 之后（窗口关闭/导航离开）迟到结果不再通知 */
    scenario("pagehide", async function (log) {
      const { B, win } = load(log, { bridge: true, generation: GEN_A });
      let release;
      const ctl = controller(log, B, {
        "GET /api/v1/config": function () { return jsonRes(200, { translate_target_lang: "en" }); },
        "POST /api/v1/events/platform": ok,
        "POST /api/v1/ocr-translate": function () { return new Promise(function (r) { release = r; }); }
      }, B.createPresentedNotifier({ window: win }));
      const started = ctl.start();
      await new Promise(function (r) { setTimeout(r, 10); });
      win.dispatch("pagehide");
      log.push({ k: "pagehide" });
      release(jsonRes(200, { ok: true, text: "Late", ocr_text: "迟", target_lang: "en" }));
      await started;
      log.push({ k: "end" });
    }),
    /* 两个窗口：各自只用本窗口 generation */
    scenario("two_windows", async function (log) {
      const a = load(log, { bridge: true, generation: GEN_A });
      const b = load(log, { bridge: true, generation: GEN_B });
      a.B.createPresentedNotifier({ window: a.win })("result");
      b.B.createPresentedNotifier({ window: b.win })("error");
      log.push({ k: "end" });
    })
  ];
  for (const r of await Promise.all(runs)) out[r[0]] = r[1];
  process.stdout.write(JSON.stringify(out));
})();
