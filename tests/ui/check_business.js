#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync, spawn, spawnSync } = require("child_process");

const repo = path.resolve(__dirname, "../..");
const v3js = path.join(repo, "static/v3/js");
const bizDir = path.join(v3js, "business");
const snapshotPath = path.join(repo, "static/v3/openapi.json");
const logDir = path.join(repo, ".pp/tmp/ui");
fs.mkdirSync(logDir, { recursive: true });

const failures = [];
const passes = [];
function fail(msg) { failures.push(msg); }
function pass(msg) { passes.push(msg); }

const businessFiles = fs.readdirSync(bizDir).filter((n) => n.endsWith(".js")).map((n) => path.join(bizDir, n));
for (const file of businessFiles) {
  try {
    execFileSync(process.execPath, ["--check", file], { stdio: "pipe" });
    pass("syntax " + path.basename(file));
  } catch (err) {
    fail("syntax " + path.basename(file) + ": " + (err.stderr || err.message).toString());
  }
}

if (!fs.existsSync(snapshotPath)) {
  fail("缺少唯一生成快照 static/v3/openapi.json");
}
if (fs.existsSync(path.join(bizDir, "openapi.snapshot.json"))) {
  fail("应删除第二份手写快照 static/v3/js/business/openapi.snapshot.json");
} else pass("已删除业务目录第二份 OpenAPI 快照");

const snapshot = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));

function operationSet(doc) {
  const ids = [];
  Object.keys(doc.paths || {}).forEach((p) => {
    Object.keys(doc.paths[p]).forEach((method) => {
      const op = doc.paths[p][method];
      if (op && op.operationId) ids.push(op.operationId);
    });
  });
  return ids.sort();
}

const snapIds = operationSet(snapshot);
const snapPaths = Object.keys(snapshot.paths || {});
if (snapPaths.length !== 24) fail("唯一快照 paths 应为 24，实际 " + snapPaths.length);
else pass("唯一生成快照 static/v3/openapi.json 有 " + snapPaths.length + " 条路径");
if (snapIds.length !== 25) fail("唯一快照 operationId 应为 25，实际 " + snapIds.length);
else pass("唯一快照 operationId 集合为 " + snapIds.length + " 个");

const requiredOps = [
  "setup_check_api_setup_check_get",
  "setup_task_start_api_setup_tasks_post",
  "setup_task_get_api_setup_tasks__task_id__get",
  "lan_access_api_v1_lan_access_get",
  "get_config_api_v1_config_get",
  "post_config_api_v1_config_post",
  "platform_event_api_v1_events_platform_post"
];
const missingOps = requiredOps.filter((id) => snapIds.indexOf(id) < 0);
if (missingOps.length) fail("快照缺少前端所需 operationId：" + missingOps.join(", "));
else pass("快照含 check/tasks/lan-access/config GET+POST 与 platform 事件");
const removedOps = [
  "setup_set_config_api_setup_set_config_post",
  "setup_unsupported_pull_model_api_setup_pull_model_post",
  "setup_unsupported_install_ollama_api_setup_install_ollama_post",
  "setup_unsupported_start_ollama_api_setup_start_ollama_post"
];
const stillPresent = removedOps.filter((id) => snapIds.indexOf(id) >= 0);
if (stillPresent.length) fail("快照仍含已删除 operationId：" + stillPresent.join(", "));
else pass("快照已去掉 set_config 与旧 setup 未实现接口");

const bootstrapJs = fs.readFileSync(path.join(bizDir, "bootstrap.js"), "utf8");
if (!bootstrapJs.includes('"/openapi.snapshot.json"') || bootstrapJs.includes("js/business/openapi.snapshot.json")) {
  fail("bootstrap 未改为请求 /openapi.snapshot.json");
} else pass("bootstrap 默认快照地址为 /openapi.snapshot.json");

for (const file of businessFiles) {
  const text = fs.readFileSync(file, "utf8");
  const rel = path.relative(repo, file);
  if (/localStorage|sessionStorage/.test(text)) fail(rel + " 使用了 Web Storage");
  if (/\/api\/v1\/|\/api\/setup\//.test(text)) fail(rel + " 手写了 /api/ 路径，应只从 OpenAPI 派生");
}
if (!failures.some((m) => m.includes("手写了") || m.includes("Web Storage"))) {
  pass("业务 JS 未手写 /api 路径，也未使用 Web Storage");
}

function jsonRes(status, body) {
  const text = arguments.length < 2 ? "" : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status: status,
    text: function () { return Promise.resolve(text); },
    json: function () { return Promise.resolve(body); }
  };
}

function createWorld() {
  const calls = [];
  const world = {
    calls: calls,
    routes: [],
    fetch: function (url, init) {
      const rec = { url: String(url), method: (init && init.method) || "GET", headers: (init && init.headers) || {}, body: init && init.body, init: init || {}, referrerPolicy: init && init.referrerPolicy };
      calls.push(rec);
      const route = world.routes.find(function (r) {
        if (r.method && r.method !== rec.method) return false;
        if (typeof r.match === "function") return r.match(rec.url, rec);
        return rec.url.indexOf(r.path) >= 0;
      });
      if (!route) return Promise.resolve(jsonRes(404, { ok: false, error: "not_found", message: "no fake route " + rec.url }));
      return Promise.resolve(route.handler(rec, jsonRes));
    }
  };
  return world;
}

function FakeEventSource(url) {
  this.url = url;
  this.readyState = 0;
  this.onopen = null;
  this.onerror = null;
  this.onmessage = null;
  this._listeners = {};
  FakeEventSource.instances.push(this);
}
FakeEventSource.instances = [];
FakeEventSource.CONNECTING = 0;
FakeEventSource.OPEN = 1;
FakeEventSource.CLOSED = 2;
FakeEventSource.prototype.addEventListener = function (type, fn) {
  this._listeners[type] = this._listeners[type] || [];
  this._listeners[type].push(fn);
};
FakeEventSource.prototype.close = function () { this.readyState = 2; this.closed = true; };
FakeEventSource.prototype.open = function () {
  this.readyState = 1;
  if (this.onopen) this.onopen();
};
FakeEventSource.prototype.emit = function (type, data) {
  const ev = { type: type, data: typeof data === "string" ? data : JSON.stringify(data) };
  (this._listeners[type] || []).forEach(function (fn) { fn(ev); });
  if (type === "message" && this.onmessage) this.onmessage(ev);
};

function FakeTrack() { this.stopped = 0; this.stop = function () { this.stopped += 1; }; }
function FakeStream() {
  this._tracks = [new FakeTrack(), new FakeTrack()];
  this.getTracks = function () { return this._tracks; };
}
function createGumQueue() {
  const calls = [];
  return {
    calls: calls,
    mediaDevices: {
      getUserMedia: function () {
        let resolveFn, rejectFn;
        const stream = new FakeStream();
        const promise = new Promise(function (res, rej) { resolveFn = res; rejectFn = rej; });
        calls.push({
          stream: stream,
          resolve: function () { resolveFn(stream); },
          reject: function (err) { rejectFn(err || new Error("gum failed")); }
        });
        return promise;
      }
    }
  };
}
function tracksLive(stream) {
  return stream.getTracks().some(function (tr) { return tr.stopped === 0; });
}
function tracksStopped(stream) {
  return stream.getTracks().every(function (tr) { return tr.stopped > 0; });
}
function FakeMediaRecorder(stream, opts) {
  this.stream = stream;
  this.mimeType = (opts && opts.mimeType) || "audio/webm";
  this.state = "inactive";
  this.ondataavailable = null;
  this.onstop = null;
  this.onerror = null;
  FakeMediaRecorder.instances.push(this);
}
FakeMediaRecorder.instances = [];
FakeMediaRecorder.isTypeSupported = function (t) { return String(t).indexOf("audio/webm") === 0; };
FakeMediaRecorder.prototype.start = function () { this.state = "recording"; };
FakeMediaRecorder.prototype.stop = function () {
  this.state = "inactive";
  if (this.ondataavailable) {
    this.ondataavailable({ data: new Blob([new Uint8Array([1, 2, 3, 4])], { type: this.mimeType }) });
  }
  if (this.onstop) this.onstop();
};

function loadBusiness(extra) {
  const context = {
    window: {},
    console: console,
    fetch: extra && extra.fetch,
    EventSource: FakeEventSource,
    MediaRecorder: FakeMediaRecorder,
    FormData: FormData,
    Blob: Blob,
    File: typeof File !== "undefined" ? File : Blob,
    URLSearchParams: URLSearchParams,
    crypto: crypto,
    setTimeout: setTimeout,
    clearTimeout: clearTimeout,
    Promise: Promise,
    Object: Object,
    Array: Array,
    JSON: JSON,
    Error: Error,
    Uint8Array: Uint8Array
  };
  context.window = context.window;
  context.globalThis = context;
  context.navigator = {
    clipboard: extra && extra.clipboard,
    mediaDevices: extra && extra.mediaDevices
  };
  context.document = extra && extra.document;
  context.location = extra && extra.location;
  context.window.V3 = {};
  context.window.V3Business = {};
  vm.createContext(context);
  ["dom.js", "labels.js", "icons.js", "defaults.js"].forEach(function (name) {
    vm.runInContext(fs.readFileSync(path.join(v3js, name), "utf8"), context, { filename: name });
  });
  const order = [
    "catalog.js", "context.js", "job-id.js", "http.js", "sse.js", "clipboard.js",
    "audio.js", "platform.js", "claim.js", "map-setup.js", "setup-controller.js", "status-controller.js",
    "translate-controller.js", "voice-controller.js", "bootstrap.js"
  ];
  order.forEach(function (name) {
    vm.runInContext(fs.readFileSync(path.join(bizDir, name), "utf8"), context, { filename: name });
  });
  context.V3 = context.window.V3;
  context.V3Business = context.window.V3Business;
  return context;
}

function fakeView(initial) {
  let state = Object.assign({}, initial || {});
  return {
    setState: function (patch) {
      const next = Object.assign({}, state);
      Object.keys(patch || {}).forEach(function (k) {
        const v = patch[k];
        if (v && typeof v === "object" && !Array.isArray(v) && state[k] && typeof state[k] === "object" && !Array.isArray(state[k])) {
          next[k] = Object.assign({}, state[k], v);
        } else next[k] = v;
      });
      state = next;
    },
    getState: function () { return state; }
  };
}

function wait(ms) { return new Promise(function (resolve) { setTimeout(resolve, ms); }); }

async function runUnit() {
  const catalogCtx = loadBusiness();
  const B = catalogCtx.window.V3Business;
  const catalog = B.createCatalog(snapshot);
  if (catalog.size !== snapIds.length) fail("catalog 数量 " + catalog.size + " != " + snapIds.length);
  else pass("catalog 从唯一快照派生 " + catalog.size + " 个 operation");
  const fromAudio = catalog.operation("from_audio_api_v1_utterances_from_audio_post");
  if (fromAudio.method !== "POST" || fromAudio.path !== "/api/v1/utterances/from-audio" || fromAudio.access !== "lan_token") {
    fail("from-audio 派生错误 " + JSON.stringify(fromAudio));
  } else pass("from-audio 路径/方法/权限由 OpenAPI 派生");
  const statusOp = catalog.operation("status_api_v1_status_get");
  if (statusOp.access !== "local" || statusOp.method !== "GET") fail("status 权限派生错误");
  else pass("status 默认无 VoiceToken，按本机接口处理");
  const eventsOp = catalog.operation("product_events_api_v1_events_get");
  if (eventsOp.path !== "/api/v1/events" || eventsOp.access !== "local") fail("events 派生错误");
  else pass("产品事件路径由 OpenAPI 派生且为本机接口");
  const platformOp = catalog.operation("platform_event_api_v1_events_platform_post");
  if (platformOp.method !== "POST" || platformOp.path !== "/api/v1/events/platform" || platformOp.access !== "local") {
    fail("platform 事件派生错误 " + JSON.stringify(platformOp));
  } else pass("平台事件 POST /api/v1/events/platform 由 OpenAPI 派生且仅本机");
  const claimOp = catalog.operation("claim_api_v1_jobs_claim_post");
  if (claimOp.method !== "POST" || claimOp.path !== "/api/v1/jobs/claim" || claimOp.access !== "local") {
    fail("claim 派生错误 " + JSON.stringify(claimOp));
  } else pass("claim 由 OpenAPI 派生且仅本机，手机不能领取");
  if (B.CLAIM_TIMEOUT_S !== 25) fail("桌面 claim 长轮询应为 25s，实际 " + B.CLAIM_TIMEOUT_S);
  else pass("桌面 claim 长轮询超时为 25s");

  const fileCtx = B.createContext({ protocol: "file:", hostname: "", origin: "file://", href: "file:///tmp/voice.html", search: "" });
  if (fileCtx.kind !== "file" || fileCtx.allowFetch) fail("file:// 上下文未关闭请求");
  else pass("file:// 上下文不允许成功请求");

  const localCtx = B.createContext({ protocol: "https:", hostname: "127.0.0.1", origin: "https://127.0.0.1:17945", href: "https://127.0.0.1:17945/static/v3/setup.html", search: "" });
  if (localCtx.kind !== "local" || !localCtx.canCallLocal()) fail("本机上下文不正确");
  else pass("本机 https 上下文可调用仅本机接口");

  const lanCtx = B.createContext({ protocol: "https:", hostname: "192.168.1.8", origin: "https://192.168.1.8:17945", href: "https://192.168.1.8:17945/static/v3/voice.html?token=secret-fixture-not-for-log", search: "?token=secret-fixture-not-for-log" });
  if (lanCtx.kind !== "lan" || lanCtx.canCallLocal() || !lanCtx.tokenPresent) fail("局域网上下文不正确");
  else pass("局域网上下文带内存令牌，且不能调用仅本机接口");

  const world = createWorld();
  const httpLan = B.createHttp({ catalog: catalog, context: lanCtx, fetch: world.fetch, origin: "https://192.168.1.8:17945" });
  world.routes.push({
    path: "/api/v1/config",
    handler: function () { return jsonRes(200, { polish_enabled: true }); }
  });
  try {
    await httpLan.call("get_config_api_v1_config_get");
    fail("局域网不应发出 config 请求");
  } catch (err) {
    if (err.blocked && world.calls.length === 0) pass("局域网调用仅本机接口在客户端被阻止，未发出 fetch");
    else fail("局域网阻止行为异常：" + (err && err.message) + " calls=" + world.calls.length);
  }

  world.calls.length = 0;
  world.routes = [{
    path: "/api/v1/history",
    method: "GET",
    handler: function (rec) {
      if (!rec.headers["X-Voice-Token"]) return jsonRes(403, { ok: false, error: "forbidden", message: "缺少令牌" });
      return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    }
  }];
  const hist = await httpLan.call("history_api_v1_history_get", { query: { limit: 5, offset: 0 } });
  if (hist.total === 0 && world.calls[0].headers["X-Voice-Token"] && world.calls[0].url.indexOf("token=") < 0) {
    pass("局域网 history 使用请求头令牌，不把令牌写进 URL");
  } else fail("局域网 history 授权方式不符合既有设计");
  if (world.calls[0].init && world.calls[0].init.referrerPolicy === "no-referrer") {
    pass("业务 fetch 使用 referrerPolicy=no-referrer");
  } else fail("业务 fetch 未设置 no-referrer");

  const httpFile = B.createHttp({ catalog: catalog, context: fileCtx, fetch: world.fetch, origin: "" });
  world.calls.length = 0;
  try {
    await httpFile.call("history_api_v1_history_get");
    fail("file:// 不应成功调用接口");
  } catch (err) {
    if (err.blocked && world.calls.length === 0) pass("file:// 无成功请求路径");
    else fail("file:// 阻止失败");
  }

  function setupCheckBody(overrides) {
    const base = {
      ok: true,
      session_type: "local",
      asr: {
        model_name: "Qwen/Qwen3-ASR-1.7B",
        status: "missing",
        weights_present: false,
        sidecar_running: false,
        size_label: null,
        device_label: null,
        progress: null,
        message: "尚未下载识别模型",
        action: "asr_download",
        task_id: null
      },
      ollama: {
        model_name: "qwen3.5:4b",
        installed: false,
        version: null,
        running: false,
        model_status: "missing",
        size_label: null,
        progress: null,
        message: "尚未安装 Ollama",
        action: "ollama_install",
        task_id: null
      },
      capabilities: [
        { id: "clipboard", label: "剪贴板", available: false, required: true, detail: "当前环境无法写入剪贴板" }
      ],
      unmet_gates: ["v3 尚未切流"]
    };
    return Object.assign(base, overrides || {});
  }

  /* setup controller: real schema mapping, config POST, tasks, LAN */
  const setupWorld = createWorld();
  setupWorld.check = setupCheckBody();
  setupWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () {
      return jsonRes(200, setupWorld.check);
    } },
    { path: "/api/v1/status", method: "GET", handler: function () {
      return jsonRes(200, { ok: true, service: "ollama-voice-input", unmet_gates: ["v3 尚未切流"], injected: { transcribe: true, polish: true, translate: true } });
    } },
    { path: "/api/v1/config", method: "GET", handler: function () {
      return jsonRes(200, { polish_enabled: false, custom_keywords: ["Alpha"], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" });
    } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      setupWorld.lastConfig = body;
      if (Object.prototype.hasOwnProperty.call(body, "baidu_appkey") || Object.prototype.hasOwnProperty.call(body, "ollama_model")) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "未知或只读字段" });
      }
      return jsonRes(200, Object.assign({ polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }, body));
    } },
    { path: "/api/v1/history", method: "GET", handler: function () {
      return jsonRes(200, { records: [{ timestamp: "t", raw_text: "a", final_text: "a", job_id: "j1", source: "web", polish: "off" }], total: 1, skipped_malformed: 0 });
    } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () {
      setupWorld.lanCalls = (setupWorld.lanCalls || 0) + 1;
      return jsonRes(200, {
        port: 8443,
        token: "secret-lan-token-do-not-log",
        token_masked: "sec****log",
        entries: [{
          ip: "192.0.2.8",
          url: "https://192.0.2.8:8443/voice?token=secret-lan-token-do-not-log",
          url_masked: "https://192.0.2.8:8443/voice?token=sec****log",
          qr_svg: "<svg xmlns='http://www.w3.org/2000/svg'></svg>"
        }],
        message: ""
      });
    } },
    { path: "/api/setup/set_config", method: "POST", handler: function () {
      setupWorld.setConfigCalled = true;
      return jsonRes(404, { ok: false, error: "not_found", message: "set_config 已删除" });
    } },
    { path: "/api/setup/tasks", method: "POST", handler: function (rec) {
      const urlPath = rec.url.split("?")[0];
      if (/\/api\/setup\/tasks\/[^/]+$/.test(urlPath) && rec.method === "POST") return jsonRes(404, { ok: false, error: "not_found", message: "no post task id" });
      if (urlPath.indexOf("/api/setup/tasks/") >= 0 && urlPath !== "/api/setup/tasks" && !urlPath.endsWith("/api/setup/tasks")) {
        return null;
      }
      const body = JSON.parse(rec.body || "{}");
      setupWorld.started = (setupWorld.started || []).concat([body.kind]);
      if (setupWorld.failStart) {
        return jsonRes(500, { ok: false, error: "dependency_failed", message: setupWorld.failStart });
      }
      setupWorld.task = {
        task_id: "task-asr-1",
        kind: body.kind,
        state: "running",
        progress: setupWorld.startProgress || { completed_bytes: 10, total_bytes: 100, percent: 10, stage: "download" },
        message: "downloading",
        error: null,
        started_at: "2026-11-10T00:00:00+00:00",
        finished_at: null
      };
      setupWorld.polls = 0;
      return jsonRes(202, setupWorld.task);
    } },
    { match: function (url, rec) {
      return rec.method === "GET" && /\/api\/setup\/tasks\/[^/]+/.test(String(url).split("?")[0]);
    }, handler: function (rec) {
      setupWorld.polls = (setupWorld.polls || 0) + 1;
      if (typeof setupWorld.onPoll === "function") return setupWorld.onPoll(rec, setupWorld.polls);
      const cur = Object.assign({}, setupWorld.task || {});
      if (setupWorld.polls === 1) {
        cur.state = "running";
        cur.progress = { completed_bytes: 40, total_bytes: 100, percent: 40, stage: "download" };
      } else {
        cur.state = "succeeded";
        cur.progress = { completed_bytes: 100, total_bytes: 100, percent: 100, stage: "done" };
        cur.finished_at = "2026-11-10T00:00:01+00:00";
        cur.message = "downloaded";
        setupWorld.check = setupCheckBody({
          unmet_gates: ["v3 尚未切流"],
          asr: Object.assign({}, setupCheckBody().asr, {
            status: "missing",
            weights_present: true,
            message: "权重已在，等待启动",
            action: "asr_start",
            task_id: null
          })
        });
      }
      setupWorld.task = cur;
      return jsonRes(200, cur);
    } }
  ];
  const setupCtl = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const setupView = fakeView();
  setupCtl.bind(setupView);
  await setupCtl.start();
  const st = setupView.getState();
  if (st.connection === "connected" && st.asr && st.asr.status === "missing" && st.asr.action === "asr_download" && st.ollama.modelStatus === "missing" && !st.asr.progress && st.history.records.length === 1 && st.polish_enabled === false) {
    pass("设置页已连接，按 asr.status/model_status 映射，不用 injected 猜就绪，也无假进度");
  } else fail("设置页就绪映射错误：" + JSON.stringify({ connection: st.connection, asr: st.asr, ollama: st.ollama }));
  if (st.statusNotice && (st.statusNotice.indexOf("未达上线") >= 0 || st.statusNotice.indexOf("尚未切流") >= 0)) pass("设置页展示 unmet_gates 说明");
  else fail("设置页未展示上线条件：" + st.statusNotice);
  if (st.capabilities && st.capabilities[0] && st.capabilities[0].available === false && /剪贴板/.test(st.capabilities[0].name) && /无法写入剪贴板/.test(st.capabilities[0].message)) {
    pass("平台能力按真实 capabilities 呈现不可用原因");
  } else fail("能力映射错误：" + JSON.stringify(st.capabilities));
  if (Array.isArray(st.apiPaths) && st.apiPaths.some(function (op) { return op.path === "/api/setup/check" && op.method === "GET"; }) && st.apiPaths.length === snapIds.length) {
    pass("renderApi 数据为派生真实 paths，不是空表");
  } else fail("apiPaths 未派生真实路径：" + JSON.stringify(st.apiPaths && st.apiPaths.slice(0, 3)));
  if (st.tokenMasked === "sec****log" && st.tokenPresent === true && st.lanEntries && st.lanEntries[0] && st.lanEntries[0].urlMasked.indexOf("token=sec****log") >= 0 && !st.lanEntries[0].url && JSON.stringify(st).indexOf("secret-lan-token-do-not-log") < 0) {
    pass("局域网入口只把遮罩写入视图状态，原始令牌不进 setState");
  } else fail("LAN 状态泄露或映射错误");
  const copied = [];
  const copyCtl = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    clipboard: { writeText: function (text) { copied.push(text); return Promise.resolve(); } },
    pollInterval: 10
  });
  const copyView = fakeView();
  copyCtl.bind(copyView);
  await copyCtl.start();
  await copyCtl.hooks.onCopy({ kind: "token" });
  await copyCtl.hooks.onCopy({ kind: "lan_url", index: 0 });
  if (copied[0] === "secret-lan-token-do-not-log" && copied[1].indexOf("token=secret-lan-token-do-not-log") >= 0 && JSON.stringify(copyView.getState()).indexOf("secret-lan-token-do-not-log") < 0) {
    pass("令牌与入口网址仅在用户点击复制时写出，不进入 DOM 状态");
  } else fail("复制路径不正确：" + JSON.stringify(copied));
  copyCtl.destroy();

  await setupCtl.hooks.onSaveKeywords("Alpha，Beta  Gamma");
  const saved = setupWorld.lastConfig;
  if (setupWorld.setConfigCalled) fail("保存配置仍调用了已删除的 set_config");
  if (saved && Array.isArray(saved.custom_keywords) && saved.custom_keywords.join(",") === "Alpha,Beta,Gamma" && !Object.prototype.hasOwnProperty.call(saved, "baidu_appkey") && !Object.prototype.hasOwnProperty.call(saved, "whisper_model") && !Object.prototype.hasOwnProperty.call(saved, "ollama_model")) {
    pass("保存只经 config POST 提交合法字段");
  } else fail("保存字段不合法：" + JSON.stringify(saved));

  const downloadP = setupCtl.hooks.onDownloadAsr();
  await downloadP;
  const after = setupView.getState();
  if (setupWorld.started && setupWorld.started[0] === "asr_download" && setupWorld.polls >= 2 && after.asr.action === "asr_start" && after.asr.status !== "ready" && after.asr.progress == null) {
    pass("下载任务经 202 与轮询完成后再查 check，不以任务成功冒充模型就绪");
  } else fail("任务生命周期映射错误：" + JSON.stringify({ started: setupWorld.started, polls: setupWorld.polls, asr: after.asr }));

  /* injected 不得覆盖真实 missing */
  const injWorld = createWorld();
  injWorld.routes = setupWorld.routes.map(function (r) { return r; });
  const injCheck = setupCheckBody();
  injWorld.routes = injWorld.routes.filter(function (r) { return r.path !== "/api/setup/check"; });
  injWorld.routes.unshift({ path: "/api/setup/check", method: "GET", handler: function () { return jsonRes(200, injCheck); } });
  const injCtl = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: injWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const injView = fakeView();
  injCtl.bind(injView);
  await injCtl.start();
  if (injView.getState().asr.status === "missing" && injView.getState().ollama.modelStatus === "missing") {
    pass("status.injected=true 时仍按 check.asr.status 显示未就绪");
  } else fail("injected 干扰了就绪判断：" + JSON.stringify(injView.getState().asr));
  injCtl.destroy();

  /* 无 percent 时不编造百分比 */
  const nopWorld = createWorld();
  nopWorld.check = setupCheckBody();
  nopWorld.failStart = null;
  nopWorld.startProgress = { completed_bytes: 5, total_bytes: null, percent: null, stage: "fetch" };
  nopWorld.routes = setupWorld.routes;
  const origOnStart = setupWorld.startProgress;
  setupWorld.startProgress = { completed_bytes: 5, total_bytes: null, percent: null, stage: "fetch" };
  setupWorld.check = setupCheckBody();
  setupWorld.onPoll = function (rec, n) {
    if (n === 1) {
      return jsonRes(200, {
        task_id: "task-asr-1",
        kind: "asr_download",
        state: "running",
        progress: { completed_bytes: 5, total_bytes: null, percent: null, stage: "fetch" },
        message: "fetching",
        error: null,
        started_at: "2026-11-10T00:00:00+00:00",
        finished_at: null
      });
    }
    return jsonRes(200, {
      task_id: "task-asr-1",
      kind: "asr_download",
      state: "failed",
      progress: null,
      message: "download failed",
      error: "dependency_failed",
      started_at: "2026-11-10T00:00:00+00:00",
      finished_at: "2026-11-10T00:00:02+00:00"
    });
  };
  const nopCtl = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const nopView = fakeView();
  nopCtl.bind(nopView);
  await nopCtl.start();
  const midP = nopCtl.hooks.onDownloadAsr();
  await wait(5);
  const mid = nopView.getState();
  if (mid.asr && mid.asr.progress && mid.asr.progress.percent == null && mid.asr.progress.receivedLabel && !mid.asr.progress.totalLabel && mid.asr.status === "downloading") {
    pass("服务端未给 percent 时不展示假百分比，只显示已给字节");
  } else fail("假百分比或进度映射错误：" + JSON.stringify(mid.asr && mid.asr.progress));
  await midP;
  if (nopView.getState().asr.status === "failed" && /failed|失败/.test(nopView.getState().asr.message || nopView.getState().statusNotice || "")) {
    pass("任务失败如实呈现，不造成功");
  } else fail("失败任务未正确呈现：" + JSON.stringify(nopView.getState().asr));
  nopCtl.destroy();
  setupWorld.onPoll = null;
  setupWorld.startProgress = origOnStart;
  setupWorld.check = setupCheckBody();

  /* destroy 停止轮询且不重复启动 */
  const desWorldPolls = { n: 0, afterDestroy: 0 };
  setupWorld.onPoll = function (rec, n) {
    desWorldPolls.n += 1;
    if (setupDes.destroyed) desWorldPolls.afterDestroy += 1;
    return jsonRes(200, {
      task_id: "task-asr-1",
      kind: "asr_download",
      state: "running",
      progress: { completed_bytes: 1, total_bytes: 100, percent: 1, stage: "download" },
      message: "still running",
      error: null,
      started_at: "2026-11-10T00:00:00+00:00",
      finished_at: null
    });
  };
  const setupDes = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: setupWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const desView = fakeView();
  setupDes.bind(desView);
  await setupDes.start();
  setupDes.hooks.onDownloadAsr();
  await wait(25);
  const pollsBefore = desWorldPolls.n;
  setupDes.destroy();
  await wait(40);
  if (setupDes._debug.isDestroyed() && desWorldPolls.afterDestroy === 0 && desWorldPolls.n >= 1 && pollsBefore === desWorldPolls.n) {
    pass("destroy 后停止任务轮询，不再重新启动");
  } else fail("destroy 未停轮询：" + JSON.stringify(desWorldPolls) + " before=" + pollsBefore);
  setupWorld.onPoll = null;

  /* LAN setup must not call local endpoints */
  const lanSetupWorld = createWorld();
  const lanSetup = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: lanSetupWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx
  });
  const lanSetupView = fakeView();
  lanSetup.bind(lanSetupView);
  await lanSetup.start();
  if (lanSetupWorld.calls.length === 0 && lanSetupView.getState().connection === "error") pass("设置页在局域网不硬调 config/status");
  else fail("设置页在局域网发出了请求：" + lanSetupWorld.calls.length);

  /* translate */
  const trWorld = createWorld();
  trWorld.platform = [];
  trWorld.routes = [
    { path: "/api/v1/config", method: "GET", handler: function () { return jsonRes(200, { translate_target_lang: "en" }); } },
    { path: "/api/v1/events/platform", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      trWorld.platform.push(body);
      return jsonRes(202, { ok: true });
    } },
    { path: "/api/v1/ocr-translate", method: "POST", handler: function (rec) {
      const fd = rec.body;
      if (!(fd instanceof FormData) || !fd.get("target_lang") || !fd.get("image")) return jsonRes(422, { ok: false, error: "invalid_request", message: "缺少字段" });
      return jsonRes(200, { ok: true, text: "Hello", ocr_text: "你好", target_lang: fd.get("target_lang") });
    } }
  ];
  const trCtl = B.createTranslateController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: trWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    clipboard: { writeText: function (t) { trWorld.copied = t; return Promise.resolve(); } }
  });
  const trView = fakeView();
  trCtl.bind(trView);
  await trCtl.submitImage(new Blob([new Uint8Array([1])], { type: "image/png" }), "en");
  await trCtl.platformSettled(); /* 平台上报串行且不阻塞 DOM；这里等它落定再核对顺序 */
  const trs = trView.getState();
  if (trs.phase === "result" && trs.text === "Hello" && trs.ocr_text === "你好") pass("翻译浮层按响应呈现 text / ocr_text");
  else fail("翻译呈现错误：" + JSON.stringify(trs));
  if (trWorld.platform.length >= 2 && trWorld.platform[0].event === "busy" && trWorld.platform[1].event === "idle" && !trWorld.platform.some(function (e) { return e.event === "recording" || e.event === "partial"; })) {
    pass("翻译在本机上报 platform busy/idle，不冒充识别事件");
  } else fail("翻译平台事件不正确：" + JSON.stringify(trWorld.platform));
  await trCtl.hooks.onCopy({ text: trs.text });
  if (trWorld.copied === "Hello") pass("复制仅在点击时 writeText");
  else fail("复制未走到 writeText");

  /* status SSE: single product stream, mirrored session events, claim loop */
  FakeEventSource.instances = [];
  const stWorld = createWorld();
  stWorld.claims = [];
  stWorld.resends = [];
  stWorld.routes = [
    { path: "/api/v1/config", method: "GET", handler: function () { return jsonRes(200, { hotkey_repeat: "ctrl+alt+r" }); } },
    { path: "/api/v1/jobs/claim", method: "POST", handler: function (rec) {
      const body = rec.body ? JSON.parse(rec.body) : {};
      stWorld.claims.push(body);
      if (stWorld.claimJob) {
        const job = stWorld.claimJob;
        stWorld.claimJob = null;
        return jsonRes(200, job);
      }
      if (stWorld.claims.length === 1) return jsonRes(200, null);
      return new Promise(function () { /* 后续长轮询挂起，避免测试里空转 */ });
    } },
    { path: "/api/v1/utterances/resend", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      stWorld.resends.push(body);
      return jsonRes(200, { ok: true, state: "queued", job_id: body.job_id, text: body.text });
    } }
  ];
  const statusCtl = B.createStatusController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: stWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    origin: "https://127.0.0.1:17945",
    sse: B.createSse({ EventSource: FakeEventSource }),
    jobIds: B.createJobIds(),
    claimErrorDelay: 60 * 1000
  });
  const statusView = fakeView({ events: [] });
  statusCtl.bind(statusView);
  await statusCtl.start();
  await wait(5);
  const src = FakeEventSource.instances[0];
  if (!src || src.url.indexOf("/api/v1/events") < 0) fail("未按 OpenAPI 路径连接 SSE");
  else pass("状态浮层按派生路径连接产品事件");
  if (FakeEventSource.instances.length === 1 && FakeEventSource.instances.every(function (s) { return String(s.url).indexOf("/recognize/sessions") < 0; })) {
    pass("状态浮层只订阅一次产品事件，不订 session events，避免双字幕");
  } else fail("状态浮层重复订阅或订了会话事件：" + FakeEventSource.instances.map(function (s) { return s.url; }).join(", "));
  await statusCtl.start();
  await wait(5);
  const openSources = FakeEventSource.instances.filter(function (s) { return !s.closed; });
  if (openSources.length === 1 && openSources[0].url.indexOf("/api/v1/events") >= 0) {
    pass("重复 start 不会同时打开两条产品事件流");
  } else fail("重复 start 后打开的事件流数=" + openSources.length);
  src.open();
  openSources[0].open();
  const live = openSources[0];
  live.emit("recording", { event: "recording", session_id: "sess-1" });
  live.emit("partial", { event: "partial", session_id: "sess-1", stable_text: "你好", tail_text: "啊" });
  live.emit("error", { event: "error", error: "delivery_failed", message: "输入目标已消失", details: { job_id: "job-delivery-1" } });
  live.emit("idle", { event: "idle" });
  const evs = statusView.getState().events || [];
  if (evs[0] && evs[0].event === "idle" && evs[1] && evs[1].event === "error" && evs[1].error === "delivery_failed" && evs[1].details && evs[1].details.job_id === "job-delivery-1" && evs[2] && evs[2].event === "partial" && evs[2].stable_text === "你好" && evs[2].tail_text === "啊" && evs[3] && evs[3].event === "recording") {
    pass("产品事件流呈现会话镜像的 recording/partial/error/idle，并保留 delivery_failed 的 job_id");
  } else fail("产品事件镜像未正确呈现：" + JSON.stringify(evs));
  if (stWorld.claims.length && stWorld.claims[0].timeout_s === 25) pass("桌面状态路径以 timeout_s=25 长轮询 claim");
  else fail("claim 未按 25s 长轮询：" + JSON.stringify(stWorld.claims));
  const beforeCalls = stWorld.calls.filter(function (c) { return String(c.url).indexOf("/api/v1/jobs/claim") < 0; }).length;
  live.onerror && live.onerror({});
  if (statusView.getState().connection === "disconnected") pass("SSE 断线后显式未连接");
  else fail("断线未显示未连接：" + statusView.getState().connection);
  const afterNonClaim = stWorld.calls.filter(function (c) { return String(c.url).indexOf("/api/v1/jobs/claim") < 0; }).length;
  if (afterNonClaim === beforeCalls) pass("断线后不自动发送（不含桌面 claim 长轮询）");
  else fail("断线后发生了额外请求");

  await statusCtl.hooks.onConfirmDeliveryChoice({ confirm: true, job_id: "job-old", text: "再发一次", focus_id: "focus-desk" });
  if (stWorld.resends[0] && stWorld.resends[0].focus_id === "focus-desk" && stWorld.resends[0].original_job_id === "job-old" && stWorld.resends[0].confirm === true) {
    pass("状态栏确认重发可附带可选 focus_id");
  } else fail("focus_id 未随 resend 提交：" + JSON.stringify(stWorld.resends));
  statusCtl.destroy();

  const lanClaimWorld = createWorld();
  lanClaimWorld.routes = [{ path: "/api/v1/jobs/claim", method: "POST", handler: function () { lanClaimWorld.hit = true; return jsonRes(200, null); } }];
  const lanLoop = B.createClaimLoop({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: lanClaimWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    errorDelay: 60 * 1000
  });
  lanLoop.start();
  await wait(5);
  if (!lanClaimWorld.hit && lanClaimWorld.calls.length === 0) pass("局域网/手机无法启动 claim，未发出领取请求");
  else fail("手机路径发出了 claim：" + lanClaimWorld.calls.length);
  lanLoop.destroy();

  const deskClaimWorld = createWorld();
  deskClaimWorld.n = 0;
  deskClaimWorld.routes = [{ path: "/api/v1/jobs/claim", method: "POST", handler: function (rec) {
    deskClaimWorld.n += 1;
    const body = rec.body ? JSON.parse(rec.body) : {};
    if (deskClaimWorld.n === 1) {
      return jsonRes(200, { job_id: "job-c1", claim_id: "cl-1", text: "你好", source: "desktop", focus_id: "focus-1", state: "claimed" });
    }
    return new Promise(function () { /* 第二次长轮询挂起，便于观察 destroy 是否停发 */ });
  } }];
  const got = [];
  const deskLoop = B.createClaimLoop({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: deskClaimWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    errorDelay: 60 * 1000,
    onClaimed: function (job) { got.push(job.job_id); }
  });
  deskLoop.start();
  await wait(20);
  deskLoop.destroy();
  await wait(20);
  const afterDestroy = deskClaimWorld.n;
  await wait(20);
  if (got[0] === "job-c1" && deskClaimWorld.n >= 2 && JSON.parse(deskClaimWorld.calls[0].body).timeout_s === 25 && afterDestroy === deskClaimWorld.n) {
    pass("桌面 claim 在 25s 长轮询中领取任务，destroy 后不再继续，且未自动 ack");
  } else fail("桌面 claim 生命周期不正确：" + JSON.stringify({ got: got, n: deskClaimWorld.n, afterDestroy: afterDestroy }));

  const platWorld = createWorld();
  platWorld.events = [];
  platWorld.routes = [{ path: "/api/v1/events/platform", method: "POST", handler: function (rec) {
    platWorld.events.push(JSON.parse(rec.body));
    return jsonRes(202, { ok: true });
  } }];
  const plat = B.createPlatformReporter({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: platWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx
  });
  await plat.postBusy();
  await plat.postError({ error: "asr_unavailable", message: "开麦失败" });
  await plat.postIdle();
  let rejected = false;
  try { await plat.post({ event: "recording" }); } catch (e) { rejected = true; }
  const lanPlat = B.createPlatformReporter({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: platWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx
  });
  const beforePlat = platWorld.calls.length;
  let lanBlocked = false;
  try { await lanPlat.postBusy(); } catch (e) { lanBlocked = !!(e && e.blocked); }
  if (platWorld.events.length === 3 && platWorld.events[0].event === "busy" && platWorld.events[1].event === "error" && platWorld.events[1].error === "asr_unavailable" && platWorld.events[2].event === "idle" && rejected && lanBlocked && platWorld.calls.length === beforePlat) {
    pass("本机平台事件只上报 idle/busy/error，拒绝冒充识别事件，局域网不可调用");
  } else fail("平台事件约束不正确：" + JSON.stringify({ events: platWorld.events, rejected: rejected, lanBlocked: lanBlocked, extra: platWorld.calls.length - beforePlat }));

  /* voice: idempotent submit, confirm, 403, unknown, resource release */
  const voiceWorld = createWorld();
  let jobState = "queued";
  voiceWorld.routes = [
    { path: "/api/v1/utterances/from-audio", method: "POST", handler: function (rec) {
      const fd = rec.body;
      voiceWorld.jobIds = voiceWorld.jobIds || [];
      voiceWorld.jobIds.push(fd.get("job_id"));
      if (!fd.get("job_id")) return jsonRes(400, { ok: false, error: "invalid_request", message: "job_id 必传，服务器不会代为生成" });
      if (!fd.get("audio")) return jsonRes(400, { ok: false, error: "invalid_request", message: "必须提供音频文件" });
      return jsonRes(200, { ok: true, state: "queued", text: "识别文本", job_id: fd.get("job_id") });
    } },
    { path: "/api/v1/jobs/", method: "GET", handler: function (rec) {
      if (voiceWorld.jobUnknown) return jsonRes(200, { found: false, state: "unknown" });
      var parts = rec.url.split("?")[0].split("/");
      var jid = parts[parts.length - 1] || (voiceWorld.jobIds && voiceWorld.jobIds[0]) || "unknown";
      return jsonRes(200, { found: true, job: { job_id: jid, state: jobState, text: "识别文本", created_at: "t", updated_at: "t" } });
    } },
    { path: "/api/v1/utterances/resend", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      voiceWorld.resends = voiceWorld.resends || [];
      voiceWorld.resends.push(body);
      if (!body.confirm) {
        return jsonRes(200, { ok: false, requires_confirmation: true, original_job_id: body.original_job_id, new_job_id: body.job_id });
      }
      return jsonRes(200, { ok: true, state: "queued", job_id: body.job_id, text: body.text });
    } },
    { path: "/api/v1/history", method: "GET", handler: function (rec) {
      if (voiceWorld.forbidHistory) return jsonRes(403, { ok: false, error: "forbidden", message: "禁止访问" });
      return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 });
    } }
  ];
  const stream = new FakeStream();
  FakeMediaRecorder.instances = [];
  const docFake = {
    hidden: false,
    listeners: {},
    addEventListener: function (t, fn) { this.listeners[t] = fn; },
    removeEventListener: function (t) { delete this.listeners[t]; }
  };
  const voiceCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds(),
    MediaRecorder: FakeMediaRecorder,
    mediaDevices: { getUserMedia: function () { return Promise.resolve(stream); } },
    document: docFake,
    setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
    clearTimeout: function (id) { clearTimeout(id); }
  });
  const voiceView = fakeView();
  voiceCtl.bind(voiceView);
  await voiceCtl.start();
  if (voiceView.getState().connection === "connected" && voiceView.getState().tokenValid === true) pass("手机页在带令牌的局域网上下文可连接");
  else fail("手机页连接状态错误：" + JSON.stringify(voiceView.getState()));

  await voiceCtl.hooks.onRecordStart();
  if (!FakeMediaRecorder.instances.length || stream.getTracks().some(function (t) { return t.stopped; })) {
    /* start shouldn't stop tracks yet */
  }
  if (voiceView.getState().phase === "recording") pass("仅在用户按下后开始录音");
  else fail("录音未进入 recording：" + voiceView.getState().phase);
  await voiceCtl.hooks.onRecordEnd();
  await wait(20);
  const firstId = voiceWorld.jobIds && voiceWorld.jobIds[0];
  if (firstId && voiceView.getState().job_id === firstId && voiceView.getState().text === "识别文本") pass("松开后上传原音频并提交必传 job_id");
  else fail("from-audio 提交异常：" + JSON.stringify(voiceView.getState()) + " ids=" + JSON.stringify(voiceWorld.jobIds));

  /* repeat same logical request keeps job_id: simulate network fail then reuse */
  const jobs = B.createJobIds();
  const idA = jobs.reuse("from-audio");
  const idA2 = jobs.reuse("from-audio");
  const idB = jobs.fresh("from-audio");
  if (idA && idA === idA2 && idB !== idA) pass("重复提交复用同一 client job_id，新一次手势换新 id");
  else fail("job_id 幂等不正确");

  /* confirm path */
  await voiceCtl.hooks.onSendOriginal({ text: "识别文本", job_id: firstId });
  const confirmBody = voiceWorld.resends && voiceWorld.resends[0];
  if (confirmBody && confirmBody.confirm === false && confirmBody.original_job_id === firstId && confirmBody.job_id !== firstId) {
    pass("发送原文调用 resend，且未自动带 confirm");
  } else fail("发送原文请求不正确：" + JSON.stringify(confirmBody));
  if (voiceView.getState().phase === "delivery_unknown") pass("ok=false requires_confirmation 呈现可能已输入，不自动入队");
  else fail("确认响应未呈现：" + voiceView.getState().phase);
  const pendingNew = confirmBody.job_id;
  await voiceCtl.hooks.onConfirmResendChoice({ confirm: true, job_id: firstId, text: "识别文本" });
  const confirmed = voiceWorld.resends[1];
  if (confirmed && confirmed.confirm === true && confirmed.original_job_id === firstId && confirmed.job_id !== firstId && confirmed.job_id !== pendingNew) {
    pass("手动确认使用新的 job_id 明确关联 original_job_id");
  } else fail("手动确认请求不正确：" + JSON.stringify(confirmed));

  /* unknown job */
  voiceWorld.jobUnknown = true;
  const unknownCtlView = fakeView();
  const unknownCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds()
  });
  unknownCtl.bind(unknownCtlView);
  unknownCtl.pollJob("missing-job");
  await wait(20);
  if (unknownCtlView.getState().job_state === "unknown" || unknownCtlView.getState().phase === "failed") pass("任务不存在时呈现 unknown/失败，不谎称已输入");
  else fail("unknown job 呈现错误：" + JSON.stringify(unknownCtlView.getState()));
  unknownCtl.destroy();

  /* 403 history */
  voiceWorld.forbidHistory = true;
  const v403 = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds()
  });
  const v403view = fakeView();
  v403.bind(v403view);
  await v403.start();
  if (v403view.getState().tokenValid === false || /禁止|forbidden/.test((v403view.getState().history && v403view.getState().history.message) || "")) {
    pass("403 时可见未授权，不假装已登录");
  } else fail("403 呈现错误：" + JSON.stringify(v403view.getState()));
  v403.destroy();

  /* cancel releases tracks */
  const stream2 = new FakeStream();
  FakeMediaRecorder.instances = [];
  const cancelCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds(),
    MediaRecorder: FakeMediaRecorder,
    mediaDevices: { getUserMedia: function () { return Promise.resolve(stream2); } },
    document: docFake
  });
  const cancelView = fakeView();
  cancelCtl.bind(cancelView);
  await cancelCtl.hooks.onRecordStart();
  await wait(5);
  await cancelCtl.hooks.onRecordCancel();
  if (stream2.getTracks().every(function (t) { return t.stopped > 0; }) && cancelView.getState().phase === "idle") {
    pass("取消录音后停止 tracks 并回到空闲");
  } else fail("取消未释放音轨：" + JSON.stringify(stream2.getTracks().map(function (t) { return t.stopped; })));
  docFake.hidden = true;
  await cancelCtl.hooks.onRecordStart();
  await wait(5);
  if (docFake.listeners.visibilitychange) docFake.listeners.visibilitychange();
  if (stream2.getTracks().every(function (t) { return t.stopped > 0; })) pass("页面不可见时停止录音并释放音轨");
  else fail("visibility 未释放音轨");
  cancelCtl.destroy();

  /* pick audio rejects video */
  const pickView = fakeView();
  const pickCtl = B.createVoiceController({
    client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
    context: lanCtx,
    jobIds: B.createJobIds()
  });
  pickCtl.bind(pickView);
  const beforePick = (voiceWorld.jobIds || []).length;
  await pickCtl.hooks.onPickAudio({ file: { type: "video/webm", name: "clip.webm" } });
  if (pickView.getState().phase === "failed" && (voiceWorld.jobIds || []).length === beforePick) pass("选文件拒绝不支持的视频容器");
  else fail("选文件限制失效");
  pickCtl.destroy();


  /* ---- B1: URL token stripped, referrer locked, no leak ---- */
  const fixtureTok = "fixture-not-for-log";
  function leak(value) {
    if (value == null) return false;
    if (typeof value === "string") return value.indexOf(fixtureTok) >= 0;
    try { return JSON.stringify(value).indexOf(fixtureTok) >= 0; } catch (err) { return false; }
  }

  const htmlDir = path.join(repo, "static/v3");
  ["setup.html", "voice.html", "overlay-status.html", "overlay-translate.html"].forEach(function (name) {
    const html = fs.readFileSync(path.join(htmlDir, name), "utf8");
    const head = html.split("</head>")[0] || "";
    const refIdx = head.search(/<meta[^>]*name=["']referrer["'][^>]*content=["']no-referrer["']/i);
    const altRef = head.search(/<meta[^>]*content=["']no-referrer["'][^>]*name=["']referrer["']/i);
    const pos = refIdx >= 0 ? refIdx : altRef;
    const cssIdx = head.indexOf('rel="stylesheet"');
    const ctxIdx = head.indexOf("js/business/context.js");
    const adoptIdx = head.indexOf("adoptTokenFromLocation");
    if (pos >= 0 && pos < (ctxIdx >= 0 ? ctxIdx : cssIdx) && ctxIdx >= 0 && ctxIdx < adoptIdx && adoptIdx < cssIdx) {
      pass(name + " 在任何外部样式前声明 no-referrer，并先加载 context.js 再剥离令牌");
    } else fail(name + " 的 Referrer-Policy / 令牌剥离脚本顺序不正确");
  });

  const loc = {
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    pathname: "/static/v3/voice.html",
    search: "?foo=1&token=" + fixtureTok + "&bar=2",
    hash: "#mic",
    href: "https://192.168.1.8/static/v3/voice.html?foo=1&token=" + fixtureTok + "&bar=2#mic"
  };
  const histCalls = [];
  const urlHistory = {
    state: { page: "voice" },
    replaceState: function (state, title, url) {
      histCalls.push({ state: state, title: title, url: String(url) });
      const qIndex = String(url).indexOf("?");
      const hIndex = String(url).indexOf("#");
      loc.search = qIndex >= 0 ? String(url).slice(qIndex, hIndex >= 0 ? hIndex : undefined) : "";
      loc.hash = hIndex >= 0 ? String(url).slice(hIndex) : "";
      loc.href = loc.origin + url;
    }
  };
  const adoptFresh = loadBusiness();
  const adopted = adoptFresh.window.V3Business.adoptTokenFromLocation({ location: loc, history: urlHistory });
  const ctxAfter = adoptFresh.window.V3Business.createContext({
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    href: loc.href,
    search: loc.search
  });
  if (histCalls.length !== 1) fail("应只 replaceState 一次，实际 " + histCalls.length);
  else if (leak(histCalls[0].url) || /(?:\?|&)token=/.test(histCalls[0].url)) fail("replaceState 目标仍含令牌参数");
  else if (histCalls[0].url.indexOf("/static/v3/voice.html") < 0 || histCalls[0].url.indexOf("foo=1") < 0 || histCalls[0].url.indexOf("bar=2") < 0 || histCalls[0].url.indexOf("#mic") < 0) {
    fail("replaceState 未保留其他查询参数或 hash");
  } else if (histCalls[0].state && histCalls[0].state.page !== "voice") fail("replaceState 不应丢掉原有 history.state");
  else pass("读入令牌后立即 replaceState 去掉 token，保留其他参数和 hash");
  if (adopted.stripped && adopted.token && ctxAfter.tokenPresent && ctxAfter.getToken() && ctxAfter.getToken().length > 0 && !ctxAfter.tokenAdoptError) {
    pass("令牌留在内存，createContext 不再依赖地址栏查询串");
  } else fail("令牌未进入内存或剥离后上下文丢失");
  if (leak(ctxAfter.tokenAdoptError) || leak(ctxAfter.href) || leak(ctxAfter.kind)) fail("上下文字段泄露了夹具令牌");
  else pass("上下文公开字段不包含令牌值");

  const failLoc = { pathname: "/static/v3/voice.html", search: "?token=" + fixtureTok, hash: "", href: "https://192.168.1.8/static/v3/voice.html?token=" + fixtureTok };
  const adoptFail = loadBusiness();
  const failed = adoptFail.window.V3Business.adoptTokenFromLocation({
    location: failLoc,
    history: { replaceState: function () { throw new Error("replace blocked"); } }
  });
  const failCtx = adoptFail.window.V3Business.createContext({
    protocol: "https:",
    hostname: "192.168.1.8",
    origin: "https://192.168.1.8",
    href: failLoc.href,
    search: failLoc.search
  });
  if (failed.failed && failCtx.tokenAdoptError && !failCtx.tokenPresent && !failCtx.getToken()) {
    if (leak(failed.failed) || leak(failCtx.tokenAdoptError)) fail("剥离失败信息泄露了令牌");
    else pass("无法 replaceState 时令牌作废，并给出不含令牌的明确失败");
  } else fail("剥离失败时未安全失败");

  const schemaWorld = createWorld();
  schemaWorld.routes.push({
    path: "/openapi.snapshot.json",
    handler: function () { return jsonRes(200, snapshot); }
  });
  const bootBiz = loadBusiness();
  bootBiz.window.V3Business.adoptTokenFromLocation({ location: loc, history: urlHistory });
  await bootBiz.window.V3Business.loadOpenApi({ fetch: schemaWorld.fetch }, { kind: "lan", allowFetch: true, origin: "https://192.168.1.8" }, schemaWorld.fetch);
  if (schemaWorld.calls[0] && String(schemaWorld.calls[0].url).indexOf("/openapi.snapshot.json") >= 0) pass("局域网加载默认 /openapi.snapshot.json，不请求本机 /openapi.json");
  else fail("局域网快照地址不正确：" + (schemaWorld.calls[0] && schemaWorld.calls[0].url));
  if (schemaWorld.calls[0] && schemaWorld.calls[0].referrerPolicy === "no-referrer") pass("加载 OpenAPI/快照的 fetch 也使用 no-referrer");
  else fail("schema fetch 未设置 no-referrer");

  /* ---- B2: getUserMedia pending vs pointerup/cancel/visibility/destroy ---- */
  function voiceWithGum(gum, extraDoc, extraWin) {
    const ctl = B.createVoiceController({
      client: B.createHttp({ catalog: catalog, context: lanCtx, fetch: voiceWorld.fetch, origin: "https://192.168.1.8:17945" }),
      context: lanCtx,
      jobIds: B.createJobIds(),
      MediaRecorder: FakeMediaRecorder,
      mediaDevices: gum.mediaDevices,
      document: extraDoc || { hidden: false, addEventListener: function () {}, removeEventListener: function () {} },
      window: extraWin,
      setTimeout: function (fn, ms) { return setTimeout(fn, ms); },
      clearTimeout: function (id) { clearTimeout(id); }
    });
    const view = fakeView();
    ctl.bind(view);
    return { ctl: ctl, view: view };
  }

  FakeMediaRecorder.instances = [];
  const gumEnd = createGumQueue();
  const endPair = voiceWithGum(gumEnd);
  const fromBefore = (voiceWorld.jobIds || []).length;
  endPair.ctl.hooks.onRecordStart();
  await wait(5);
  if (FakeMediaRecorder.instances.length !== 0) fail("权限未完成时不应创建 MediaRecorder");
  await endPair.ctl.hooks.onRecordEnd();
  await gumEnd.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length !== 0) fail("pointerup 发生在 getUserMedia 待决时，完成后仍启动了 MediaRecorder");
  else if (!tracksStopped(gumEnd.calls[0].stream)) fail("pointerup 待决完成后音轨未停止");
  else if (endPair.view.getState().phase === "recording" || (voiceWorld.jobIds || []).length !== fromBefore) {
    fail("待决结束后不应继续录音或上传");
  } else pass("pointerup 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  endPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumCancel = createGumQueue();
  const cancelPair = voiceWithGum(gumCancel);
  cancelPair.ctl.hooks.onRecordStart();
  await wait(5);
  cancelPair.ctl.hooks.onRecordCancel();
  await gumCancel.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumCancel.calls[0].stream) && cancelPair.view.getState().phase === "idle") {
    pass("pointercancel 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("cancel 待决完成后仍有录音或活音轨");
  cancelPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumVis = createGumQueue();
  const visDoc = { hidden: false, listeners: {}, addEventListener: function (n, fn) { this.listeners[n] = fn; }, removeEventListener: function (n) { delete this.listeners[n]; } };
  const visPair = voiceWithGum(gumVis, visDoc);
  await visPair.ctl.start();
  visPair.ctl.hooks.onRecordStart();
  await wait(5);
  visDoc.hidden = true;
  if (visDoc.listeners.visibilitychange) visDoc.listeners.visibilitychange();
  await gumVis.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumVis.calls[0].stream) && visPair.view.getState().phase === "idle") {
    pass("visibilitychange 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("visibility 待决完成后仍有录音或活音轨");
  visPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumHide = createGumQueue();
  const hideWin = { listeners: {}, addEventListener: function (n, fn) { this.listeners[n] = fn; }, removeEventListener: function (n) { delete this.listeners[n]; } };
  const hidePair = voiceWithGum(gumHide, { hidden: false, addEventListener: function () {}, removeEventListener: function () {} }, hideWin);
  await hidePair.ctl.start();
  hidePair.ctl.hooks.onRecordStart();
  await wait(5);
  if (hideWin.listeners.pagehide) hideWin.listeners.pagehide();
  await gumHide.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumHide.calls[0].stream)) {
    pass("pagehide 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("pagehide 待决完成后仍有录音或活音轨");
  hidePair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumDes = createGumQueue();
  const desPair = voiceWithGum(gumDes);
  desPair.ctl.hooks.onRecordStart();
  await wait(5);
  desPair.ctl.destroy();
  await gumDes.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && tracksStopped(gumDes.calls[0].stream)) {
    pass("destroy 发生在 getUserMedia 待决时：完成后不启动录音，轨道被停止");
  } else fail("destroy 待决完成后仍有录音或活音轨");

  FakeMediaRecorder.instances = [];
  const gumOld = createGumQueue();
  const oldPair = voiceWithGum(gumOld);
  oldPair.ctl.hooks.onRecordStart();
  await wait(5);
  oldPair.ctl.hooks.onRecordStart();
  await wait(5);
  if (gumOld.calls.length !== 2) fail("两次 start 应各发起一次 getUserMedia，实际 " + gumOld.calls.length);
  await gumOld.calls[0].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length !== 0) fail("旧 getUserMedia 在新录音之后 resolve 时不应启动录音器");
  else if (!tracksStopped(gumOld.calls[0].stream)) fail("被取代的旧音轨未停止");
  await gumOld.calls[1].resolve();
  await wait(40);
  if (FakeMediaRecorder.instances.length === 1 && tracksLive(gumOld.calls[1].stream) && tracksStopped(gumOld.calls[0].stream) && oldPair.view.getState().phase === "recording") {
    pass("旧 getUserMedia 晚于新录音 resolve 时只保留新会话，旧轨道已停止");
  } else fail("新旧录音代次处理不正确");
  oldPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumOk = createGumQueue();
  const okPair = voiceWithGum(gumOk);
  const startP = okPair.ctl.hooks.onRecordStart();
  await wait(5);
  await gumOk.calls[0].resolve();
  await startP;
  if (FakeMediaRecorder.instances.length === 1 && tracksLive(gumOk.calls[0].stream) && okPair.view.getState().phase === "recording") {
    pass("正常权限授予后仍能开始录音");
  } else fail("正常录制被回归破坏");
  await okPair.ctl.hooks.onRecordEnd();
  await wait(20);
  if (tracksStopped(gumOk.calls[0].stream)) pass("正常结束录音后释放音轨");
  else fail("正常结束未释放音轨");
  okPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumRej = createGumQueue();
  const rejPair = voiceWithGum(gumRej);
  const rejP = rejPair.ctl.hooks.onRecordStart();
  await wait(5);
  const denied = new Error("denied");
  denied.name = "NotAllowedError";
  gumRej.calls[0].reject(denied);
  await rejP;
  await wait(20);
  if (FakeMediaRecorder.instances.length === 0 && rejPair.view.getState().phase === "idle" && /权限被拒绝/.test(rejPair.view.getState().statusText || rejPair.view.getState().fileUploadHint || "")) {
    pass("权限拒绝时不启动录音，并给出可见错误");
  } else fail("权限拒绝路径不正确：" + JSON.stringify({ phase: rejPair.view.getState().phase, text: rejPair.view.getState().statusText }));
  rejPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumBoom = createGumQueue();
  const boomPair = voiceWithGum(gumBoom);
  const boomP = boomPair.ctl.hooks.onRecordStart();
  await wait(5);
  gumBoom.calls[0].reject(new Error("device exploded"));
  await boomP;
  await wait(20);
  if (FakeMediaRecorder.instances.length === 0 && boomPair.view.getState().phase === "idle") {
    pass("getUserMedia 异常时不启动录音并回收会话");
  } else fail("getUserMedia 异常路径不正确");
  boomPair.ctl.destroy();

  FakeMediaRecorder.instances = [];
  const gumStaleRej = createGumQueue();
  const stalePair = voiceWithGum(gumStaleRej);
  stalePair.ctl.hooks.onRecordStart();
  await wait(5);
  stalePair.ctl.hooks.onRecordCancel();
  const staleErr = new Error("denied later");
  staleErr.name = "NotAllowedError";
  gumStaleRej.calls[0].reject(staleErr);
  await wait(40);
  if (FakeMediaRecorder.instances.length === 0 && stalePair.view.getState().phase === "idle") {
    pass("已取消会话的迟到权限拒绝不重新拉起录音，也不覆盖取消状态");
  } else fail("迟到拒绝干扰了已取消的会话");
  stalePair.ctl.destroy();


  /* LAN voice must not call config/status */
  const sneak = (voiceWorld.calls || []).filter(function (c) {
    return /\/api\/v1\/(config|status|events)(\b|\/)/.test(c.url) || /\/api\/setup\//.test(c.url) || /\/api\/v1\/jobs\/claim\b/.test(c.url);
  });
  if (sneak.length === 0) pass("手机页未从局域网请求仅本机的 config/status/setup/events/platform/claim");
  else fail("手机页误请求仅本机接口：" + sneak.map(function (c) { return c.url; }).join(", "));

  voiceCtl.destroy();

  const mappedCaps = B.mapCapabilities([
    { id: "desktop_daemon", label: "桌面守护进程", available: true, required: true, detail: "守护进程正在运行，快捷键已加载。" },
    { id: "clipboard", label: "剪贴板", available: false, required: true, detail: "当前环境无法写入剪贴板" }
  ]);
  if (
    mappedCaps
    && mappedCaps[0]
    && mappedCaps[0].id === "desktop_daemon"
    && mappedCaps[0].detail === "守护进程正在运行，快捷键已加载。"
    && mappedCaps[0].message === mappedCaps[0].detail
    && mappedCaps[1].id === "clipboard"
    && mappedCaps[1].detail.indexOf("无法写入剪贴板") >= 0
  ) {
    pass("mapCapabilities 保留 id 与 detail");
  } else fail("mapCapabilities 未保留 id/detail：" + JSON.stringify(mappedCaps));
  if (B.mapCapabilities(null) === null && Array.isArray(B.mapCapabilities([])) && B.mapCapabilities([]).length === 0) {
    pass("能力列表缺省或空数组时不编造 desktop_daemon");
  } else fail("空能力列表映射错误");

  const startFailModels = B.applyTaskToModels(
    { status: "ready", sidecarRunning: false, message: "权重已在", action: "asr_start" },
    { modelStatus: "ready", running: true },
    { kind: "asr_start", state: "failed", message: "启动识别服务失败：未授权", task_id: "t-start-1", error: "unauthorized" }
  );
  if (
    startFailModels.asr.status === "ready"
    && startFailModels.asr.sidecarRunning === false
    && startFailModels.asr.message.indexOf("未授权") >= 0
    && startFailModels.asr.action === "asr_start"
    && startFailModels.asr.taskId === "t-start-1"
  ) {
    pass("asr_start 失败不把 weights status 写成 failed，只叠加任务说明");
  } else fail("applyTaskToModels asr_start 失败路径错误：" + JSON.stringify(startFailModels.asr));

  const dlFail = B.applyTaskToModels(
    { status: "downloading" },
    {},
    { kind: "asr_download", state: "failed", message: "下载中断", task_id: "t-dl" }
  );
  if (dlFail.asr.status === "failed" && dlFail.asr.message === "下载中断") pass("asr_download 失败仍写入 weights status=failed");
  else fail("asr_download 失败路径被误改：" + JSON.stringify(dlFail.asr));

  const startReqWorld = createWorld();
  startReqWorld.check = setupCheckBody({
    asr: Object.assign({}, setupCheckBody().asr, {
      status: "ready",
      weights_present: true,
      sidecar_running: false,
      message: "权重已就绪，识别服务未启动",
      action: "asr_start",
      task_id: null
    })
  });
  startReqWorld.failStart = "启动识别服务失败：端口被占用";
  startReqWorld.routes = [
    { path: "/api/setup/check", method: "GET", handler: function () { return jsonRes(200, startReqWorld.check); } },
    { path: "/api/v1/status", method: "GET", handler: function () { return jsonRes(200, { ok: true, service: "ollama-voice-input", unmet_gates: [], injected: {} }); } },
    { path: "/api/v1/config", method: "GET", handler: function () { return jsonRes(200, { polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }); } },
    { path: "/api/v1/config", method: "POST", handler: function (rec) {
      const body = JSON.parse(rec.body);
      if (body.hotkey_voice && body.hotkey_voice === body.hotkey_screenshot) {
        return jsonRes(400, { ok: false, error: "invalid_request", message: "语音输入、截图翻译、重复上次输入 不能使用相同组合" });
      }
      return jsonRes(200, Object.assign({ polish_enabled: false, custom_keywords: [], translate_target_lang: "zh", hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+x", hotkey_repeat: "ctrl+alt+r", ollama_model: "qwen3.5:4b" }, body));
    } },
    { path: "/api/v1/history", method: "GET", handler: function () { return jsonRes(200, { records: [], total: 0, skipped_malformed: 0 }); } },
    { path: "/api/v1/lan-access", method: "GET", handler: function () { return jsonRes(200, { port: 8443, token: "secret-lan-token-do-not-log", token_masked: "sec****log", entries: [], message: "" }); } },
    { path: "/api/setup/tasks", method: "POST", handler: function (rec) {
      const urlPath = rec.url.split("?")[0];
      if (urlPath.indexOf("/api/setup/tasks/") >= 0 && urlPath !== "/api/setup/tasks") return null;
      const body = JSON.parse(rec.body || "{}");
      startReqWorld.started = (startReqWorld.started || []).concat([body.kind]);
      if (startReqWorld.failStart) {
        return jsonRes(500, { ok: false, error: "dependency_failed", message: startReqWorld.failStart });
      }
      startReqWorld.task = {
        task_id: "task-start-1",
        kind: body.kind,
        state: "running",
        progress: null,
        message: "starting",
        error: null,
        started_at: "2026-11-11T00:00:00+00:00",
        finished_at: null
      };
      startReqWorld.polls = 0;
      return jsonRes(202, startReqWorld.task);
    } },
    { match: function (url, rec) {
      return rec.method === "GET" && /\/api\/setup\/tasks\/[^/]+/.test(String(url).split("?")[0]);
    }, handler: function () {
      startReqWorld.polls = (startReqWorld.polls || 0) + 1;
      if (typeof startReqWorld.onPoll === "function") return startReqWorld.onPoll(startReqWorld.polls);
      const cur = Object.assign({}, startReqWorld.task || {});
      cur.state = "failed";
      cur.message = "sidecar 未能监听";
      cur.error = "dependency_failed";
      cur.finished_at = "2026-11-11T00:00:01+00:00";
      startReqWorld.task = cur;
      return jsonRes(200, cur);
    } }
  ];
  const startReqCtl = B.createSetupController({
    client: B.createHttp({ catalog: catalog, context: localCtx, fetch: startReqWorld.fetch, origin: "https://127.0.0.1:17945" }),
    context: localCtx,
    catalog: catalog,
    pollInterval: 10
  });
  const startReqView = fakeView();
  startReqView.setState({
    hotkeyDrafts: { hotkey_voice: "ctrl+alt+q" },
    hotkeyCapturing: "hotkey_voice",
    hotkeyFocus: "hotkey_voice"
  });
  startReqCtl.bind(startReqView);
  await startReqCtl.start();
  if (startReqView.getState().hotkeyDrafts && startReqView.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+q" && startReqView.getState().hotkeyCapturing === "hotkey_voice") {
    pass("loadAll 不重置快捷键草稿与录入态");
  } else fail("loadAll 丢掉了草稿或录入态：" + JSON.stringify({ drafts: startReqView.getState().hotkeyDrafts, cap: startReqView.getState().hotkeyCapturing }));
  if (startReqView.getState().asr && startReqView.getState().asr.status === "ready" && startReqView.getState().asr.sidecarRunning === false) {
    pass("mapAsr 保留 sidecarRunning=false，不把缺运行当成已就绪");
  } else fail("sidecarRunning 映射错误：" + JSON.stringify(startReqView.getState().asr));
  await startReqCtl.hooks.onStartAsr();
  const afterReqFail = startReqView.getState();
  if (afterReqFail.asr.status === "ready" && /端口被占用/.test((afterReqFail.asr.message || "") + " " + (afterReqFail.statusNotice || "")) && afterReqFail.asr.action === "asr_start") {
    pass("启动请求失败不把 status 写成 failed，并展示后端说明");
  } else fail("启动请求失败路径错误：" + JSON.stringify({ asr: afterReqFail.asr, notice: afterReqFail.statusNotice }));

  startReqWorld.failStart = null;
  await startReqCtl.hooks.onStartAsr();
  const afterOverlay = startReqView.getState();
  if (afterOverlay.asr.status === "ready" && /未能监听/.test((afterOverlay.asr.message || "") + " " + (afterOverlay.statusNotice || "")) && afterOverlay.asr.action === "asr_start") {
    pass("asr_start 任务失败叠加路径不改 weights status，只保留任务说明");
  } else fail("overlayTaskFailure asr_start 路径错误：" + JSON.stringify({ asr: afterOverlay.asr, notice: afterOverlay.statusNotice, polls: startReqWorld.polls }));

  try {
    await startReqCtl.hooks.onSaveHotkeys({ hotkey_voice: "ctrl+alt+z", hotkey_screenshot: "ctrl+alt+z", hotkey_repeat: "ctrl+alt+r" });
    fail("快捷键互相重复时不应保存成功");
  } catch (err) {
    const st = startReqView.getState();
    if (
      st.hotkeyDrafts && st.hotkeyDrafts.hotkey_voice === "ctrl+alt+q"
      && /不能使用相同组合/.test((st.hotkeySaveError || "") + " " + (st.statusNotice || ""))
      && st.hotkeysSaved !== true
    ) {
      pass("快捷键互相重复时展示后端错误且不丢草稿");
    } else fail("保存冲突后草稿或错误不正确：" + JSON.stringify({ drafts: st.hotkeyDrafts, err: st.hotkeySaveError, notice: st.statusNotice, saved: st.hotkeysSaved, thrown: err && err.message }));
  }
  startReqCtl.destroy();
}

function runAsgi() {
  const python = path.join(repo, ".venv/bin/python");
  if (!fs.existsSync(python)) {
    fail("未找到项目 .venv/bin/python，隔离 ASGI 集成无法运行");
    return Promise.resolve();
  }
  const tmp = path.join(logDir, "business");
  fs.mkdirSync(tmp, { recursive: true });
  const metaPath = path.join(tmp, "asgi-meta.json");
  const sandboxRoot = path.join(tmp, "sandbox-" + process.pid);
  fs.mkdirSync(sandboxRoot, { recursive: true });
  if (fs.existsSync(metaPath)) fs.unlinkSync(metaPath);
  const child = spawn(python, [path.join(repo, "tests/ui/business/asgi_sandbox.py"), metaPath, sandboxRoot], {
    cwd: repo,
    stdio: ["ignore", "pipe", "pipe"]
  });
  let stderr = "";
  child.stderr.on("data", function (buf) { stderr += buf.toString(); });
  child.stdout.on("data", function (buf) { stderr += buf.toString(); });
  const started = Date.now();
  let listening = false;
  return new Promise(function (resolve) {
    let settled = false;
    function settle(okMsg, errMsg) {
      if (settled) return;
      settled = true;
      if (errMsg) fail(errMsg + (stderr ? " / " + stderr.slice(0, 400) : ""));
      else pass(okMsg);
      resolve();
    }
    function processGone() {
      return child.exitCode != null || child.signalCode != null;
    }
    function finish(okMsg, errMsg) {
      if (settled) return;
      function afterStop() {
        if (okMsg && !processGone()) {
          settle(null, "已发出停止信号，但 PID " + child.pid + " 仍在运行");
          return;
        }
        settle(okMsg, errMsg);
      }
      if (processGone()) {
        afterStop();
        return;
      }
      try { if (child.pid) process.kill(child.pid, "SIGTERM"); } catch (e) {}
      const killer = setTimeout(function () {
        try { if (!processGone() && child.pid) process.kill(child.pid, "SIGKILL"); } catch (e) {}
      }, 1000);
      child.once("exit", function () {
        clearTimeout(killer);
        afterStop();
      });
    }
    function waitMeta() {
      if (settled || listening) return;
      if (fs.existsSync(metaPath)) {
        let meta = null;
        try { meta = JSON.parse(fs.readFileSync(metaPath, "utf8")); } catch (err) { meta = null; }
        if (meta && meta.ready === true && meta.port && meta.pid) {
          listening = true;
          return probe(meta);
        }
      }
      if (Date.now() - started > 8000) {
        return finish(null, "ASGI 沙箱未能在时限内进入监听（ready meta 未出现）");
      }
      setTimeout(waitMeta, 50);
    }
    function probe(meta) {
      if (meta.port === 17945 || meta.port === 11434) return finish(null, "沙箱误用保留端口 " + meta.port);
      const origin = "http://127.0.0.1:" + meta.port;
      const catalog = (function () {
        const ctx = loadBusiness();
        return ctx.window.V3Business.createCatalog(JSON.parse(fs.readFileSync(snapshotPath, "utf8")));
      })();
      const context = { kind: "local", allowFetch: true, origin: origin, canCallLocal: function () { return true; }, canCallLanToken: function () { return true; }, getToken: function () { return null; }, tokenPresent: false };
      const Bmod = loadBusiness();
      const client = Bmod.window.V3Business.createHttp({ catalog: catalog, context: context, fetch: fetch, origin: origin });
      Promise.resolve().then(async function () {
        const check = await client.call("setup_check_api_setup_check_get");
        if (!check || check.ok !== true || !check.asr || !check.ollama) throw new Error("setup_check 未返回真实 SetupCheckResponse");
        if (check.asr.status === "ready") throw new Error("沙箱默认不应把识别标为已就绪");
        if (!Array.isArray(check.unmet_gates) || !check.unmet_gates.length) throw new Error("setup_check 应保留 unmet_gates，ok 只表示检查成功");
        if (!Array.isArray(check.capabilities)) throw new Error("setup_check 缺少 capabilities");
        const snapRes = await fetch(origin + "/openapi.snapshot.json");
        if (!snapRes.ok) throw new Error("页面快照 /openapi.snapshot.json HTTP " + snapRes.status);
        const snapDoc = await snapRes.json();
        const expectedSnap = JSON.parse(fs.readFileSync(snapshotPath, "utf8"));
        if (JSON.stringify(Object.keys(snapDoc.paths || {}).sort()) !== JSON.stringify(Object.keys(expectedSnap.paths || {}).sort())) {
          throw new Error("发布快照 paths 与 static/v3/openapi.json 不一致，条数 " + Object.keys(snapDoc.paths || {}).length);
        }
        const liveOpenapi = await fetch(origin + "/openapi.json");
        if (!liveOpenapi.ok) throw new Error("运行时 openapi.json HTTP " + liveOpenapi.status);
        const openapi = await liveOpenapi.json();
        if (Object.keys(openapi.paths).length !== 24) throw new Error("运行时 openapi paths=" + Object.keys(openapi.paths).length);
        const pages = ["/setup", "/voice", "/overlay-status", "/overlay-translate"];
        for (const page of pages) {
          const res = await fetch(origin + page);
          if (!res.ok) throw new Error("新页面路由 " + page + " HTTP " + res.status);
          const html = await res.text();
          if (html.indexOf("js/business/bootstrap.js") < 0 && html.indexOf("/js/business/bootstrap.js") < 0) {
            throw new Error("页面 " + page + " 未加载业务脚本");
          }
        }
        const jsRes = await fetch(origin + "/js/business/bootstrap.js");
        if (!jsRes.ok) throw new Error("/js 资源 HTTP " + jsRes.status);
        const cssRes = await fetch(origin + "/css/theme.css");
        if (!cssRes.ok) throw new Error("/css 资源 HTTP " + cssRes.status);
        const started = await client.call("setup_task_start_api_setup_tasks_post", { body: { kind: "asr_download" } });
        if (!started || started.state !== "running" || !started.task_id) throw new Error("启动任务未返回 running");
        const same = await client.call("setup_task_start_api_setup_tasks_post", { body: { kind: "asr_download" } });
        if (!same || same.task_id !== started.task_id) throw new Error("同 kind 运行中任务应幂等返回");
        const mid = await client.call("setup_task_get_api_setup_tasks__task_id__get", { pathParams: { task_id: started.task_id } });
        if (!mid || (mid.state !== "running" && mid.state !== "succeeded")) throw new Error("任务查询状态异常");
        if (mid.progress && mid.progress.percent != null && (mid.progress.percent < 0 || mid.progress.percent > 100)) {
          throw new Error("任务进度超出范围");
        }
        const lan = await client.call("lan_access_api_v1_lan_access_get");
        if (!lan || !lan.token_masked || !Array.isArray(lan.entries) || !lan.entries[0] || !lan.entries[0].url_masked) {
          throw new Error("lan-access 响应缺少遮罩字段");
        }
        if (!lan.entries[0].url_masked || lan.entries[0].url_masked === lan.entries[0].url) {
          throw new Error("lan-access 未提供区别于原址的遮罩网址");
        }
        const posted = await client.call("post_config_api_v1_config_post", { body: { custom_keywords: ["Alpha"] } });
        if (!posted || !Array.isArray(posted.custom_keywords) || posted.custom_keywords[0] !== "Alpha") throw new Error("config POST 未回读合法字段");
        const fd = new FormData();
        fd.append("job_id", "11111111-1111-4111-8111-111111111111");
        fd.append("audio", new Blob([new Uint8Array([1, 2, 3, 4, 5])]), "recording.webm");
        const accepted = await client.call("from_audio_api_v1_utterances_from_audio_post", { body: fd });
        if (!accepted || accepted.state !== "queued" || accepted.text !== "sandbox transcript") throw new Error("from-audio 未按沙箱转写入队");
        const hist = await client.call("history_api_v1_history_get", { query: { limit: 5, offset: 0 } });
        if (!hist || !Array.isArray(hist.records)) throw new Error("history 响应不符合契约");
        const plat = await client.call("platform_event_api_v1_events_platform_post", { body: { event: "busy" } });
        if (!plat || plat.ok !== true) throw new Error("platform 事件未被本机接受");
        const claimed = await client.call("claim_api_v1_jobs_claim_post", { body: { timeout_s: 0 } });
        if (claimed != null && claimed.job_id == null) throw new Error("无任务时应返回 null");
        if (claimed != null && claimed.job_id) {
          /* 沙箱若已有任务也只确认形状，不得把空队列编造成功 */
        } else if (claimed != null) throw new Error("claim 空响应不是 null");
        finish("隔离 ASGI 集成：新路由/快照/check/tasks/lan-access/config/platform/claim/from-audio/history 对齐，已停止 PID " + meta.pid, null);
      }).catch(function (err) {
        finish(null, "ASGI 集成失败：" + err.message);
      });
    }
    child.on("exit", function (code, signal) {
      if (settled || listening) return;
      finish(null, "ASGI 进程在进入监听前退出，码 " + code + (signal ? " 信号 " + signal : ""));
    });
    waitMeta();
  });
}

runUnit()
  .then(runAsgi)
  .then(function () {
    const report = { passes: passes, failures: failures };
    fs.writeFileSync(path.join(logDir, "check_business.json"), JSON.stringify(report, null, 2));
    console.log("业务接线检查通过 " + passes.length + "，失败 " + failures.length);
    failures.forEach(function (m) { console.log("FAIL " + m); });
    passes.forEach(function (m) { console.log("PASS " + m); });
    if (failures.length) process.exit(1);
  })
  .catch(function (err) {
    console.error(err);
    fail("测试运行器异常：" + err.message);
    fs.writeFileSync(path.join(logDir, "check_business.json"), JSON.stringify({ passes: passes, failures: failures }, null, 2));
    process.exit(1);
  });
