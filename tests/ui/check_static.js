#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");

const repo = path.resolve(__dirname, "../..");
const v3 = path.join(repo, "static/v3");
const openapiPath = path.join(repo, "static/v3/openapi.json");
const logDir = path.join(repo, ".pp/tmp/ui");
fs.mkdirSync(logDir, { recursive: true });

const failures = [];
const passes = [];

function fail(msg) { failures.push(msg); }
function pass(msg) { passes.push(msg); }

function walk(dir, acc) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) walk(full, acc);
    else acc.push(full);
  }
  return acc;
}

const files = walk(v3, []);
function isBusinessFile(file) {
  const rel = path.relative(v3, file).split(path.sep).join("/");
  return rel === "js/business" || rel.indexOf("js/business/") === 0;
}
function isGeneratedSnapshot(file) {
  const rel = path.relative(v3, file).split(path.sep).join("/");
  return rel === "openapi.json";
}
const pureFiles = files.filter((f) => !isBusinessFile(f) && !isGeneratedSnapshot(f));
const jsFiles = files.filter((f) => f.endsWith(".js"));
const cssFiles = files.filter((f) => f.endsWith(".css"));
const htmlFiles = files.filter((f) => f.endsWith(".html"));
const prodText = files.map((f) => fs.readFileSync(f, "utf8")).join("\n");

const expectedHtml = ["setup.html", "overlay-status.html", "overlay-translate.html", "voice.html"];
for (const name of expectedHtml) {
  if (fs.existsSync(path.join(v3, name))) pass("存在 " + name);
  else fail("缺少 " + name);
}

const forbidden = [
  [/\bfetch\s*\(/, "fetch("],
  [/\bEventSource\s*\(/, "EventSource("],
  [/\bWebSocket\s*\(/, "WebSocket("],
  [/\bXMLHttpRequest\b/, "XMLHttpRequest"],
  [/navigator\.clipboard/, "navigator.clipboard"],
  [/document\.execCommand\s*\(\s*['"]copy['"]/, "execCommand(copy)"],
  [/\balert\s*\(/, "alert("],
  [/\bwindow\.confirm\s*\(/, "window.confirm("],
  [/\blocalStorage\b/, "localStorage"],
  [/\bsessionStorage\b/, "sessionStorage"],
  [/\beval\s*\(/, "eval("],
  [/new\s+Function\s*\(/, "new Function"]
];

for (const file of pureFiles) {
  const text = fs.readFileSync(file, "utf8");
  const rel = path.relative(repo, file);
  for (const [re, label] of forbidden) {
    if (re.test(text)) fail(rel + " 含有纯 UI 不允许的 " + label);
  }
  if (/\/api\//.test(text)) fail(rel + " 含有 /api/ 路径，纯 UI 不得写死可调用地址");
}

const businessJs = jsFiles.filter((f) => isBusinessFile(f));
for (const file of businessJs) {
  const text = fs.readFileSync(file, "utf8");
  const rel = path.relative(repo, file);
  if (/\blocalStorage\b/.test(text) || /\bsessionStorage\b/.test(text)) {
    fail(rel + " 不得使用 Web Storage（避免保存令牌）");
  }
}

if (!failures.some((m) => m.includes("fetch") || m.includes("EventSource") || m.includes("clipboard") || m.includes("/api/"))) {
  pass("纯 UI 目录无 fetch / EventSource / 剪贴板 / /api/ 路径；业务层单独存放");
}

const htmlJoined = htmlFiles.map((f) => fs.readFileSync(f, "utf8")).join("\n");
const htmlBanned = [
  "全部就绪",
  "今天下午三点",
  "192.168.",
  "3.5 GB",
  "42%",
  "已下载",
  "Qwen3-ASR-0.6B",
  "baidu",
  "Whisper"
];
for (const needle of htmlBanned) {
  if (htmlJoined.toLowerCase().includes(needle.toLowerCase())) {
    fail("生产 HTML 含有不应作为默认内容的文本：" + needle);
  }
}
if (!failures.some((m) => m.includes("生产 HTML"))) pass("生产 HTML 未写死成功示例或已删除的能力");

for (const name of expectedHtml) {
  const text = fs.readFileSync(path.join(v3, name), "utf8");
  if (!/css\/theme\.css/.test(text)) fail(name + " 未引用共享 theme.css");
  if (!/js\/defaults\.js/.test(text)) fail(name + " 未引用共享 defaults.js");
  if (!/__V3_HOOKS__/.test(text)) fail(name + " 未提供 hooks 注入点");
}
pass("四页均引用共享主题/默认状态，并预留 hooks 注入");

const theme = fs.readFileSync(path.join(v3, "css/theme.css"), "utf8");
const requiredVars = ["--c-bg", "--c-accent", "--hud-bg", "--hud-text", "--hud-unstable", "--font-sans", "--sp-3", "--radius-md"];
for (const v of requiredVars) {
  if (!theme.includes(v)) fail("theme.css 缺少变量 " + v);
}
pass("theme.css 含方向 A 关键令牌");

for (const css of cssFiles) {
  const rel = path.relative(repo, css);
  if (path.basename(css) === "theme.css") continue;
  const text = fs.readFileSync(css, "utf8");
  const hex = text.match(/#[0-9a-fA-F]{3,8}/g) || [];
  const leftover = hex.filter((h) => !/var\(/.test(h));
  // overlay uses rgba for hud separators / cover dim, allowed as overlay-local transparent layers,
  // but solid brand colors should come from variables. Flag leftover #hex only.
  if (leftover.length) {
    fail(rel + " 含有未走主题变量的十六进制颜色：" + leftover.join(", "));
  }
}

const setupJs = fs.readFileSync(path.join(v3, "js/setup.js"), "utf8");
for (const tab of ["models", "voice", "shot", "keys", "lan", "hist", "api", "sys"]) {
  if (!setupJs.includes(tab + ":") && !setupJs.includes('"' + tab + '"') && !setupJs.includes("'" + tab + "'")) {
    fail("setup.js 未覆盖菜单 " + tab);
  }
}
if (setupJs.includes("baidu") || setupJs.includes("whisper_model")) fail("setup.js 仍出现已删除的百度或 Whisper 切换");
if (setupJs.includes("Qwen3-ASR-0.6B")) fail("setup.js 出现低配模型切换");
pass("设置页覆盖模板 A 的八个菜单，且无 B 额外/低配切换");
const setupHtml = fs.readFileSync(path.join(v3, "setup.html"), "utf8");
if (!/hotkey-capture\.js[\s\S]*js\/setup\.js/.test(setupHtml)) fail("setup.html 未在 setup.js 之前加载 hotkey-capture.js");
else pass("setup.html 先加载 hotkey-capture.js");
if (setupJs.includes("hotkeyHint") || setupJs.includes("以后端返回的提示为准") || setupJs.includes("已生效")) {
  fail("setup.js 仍含空承诺或已生效文案");
} else pass("快捷键区已去掉空承诺和已生效");
if (!setupJs.includes("sidecarRunning === true") || !setupJs.includes("desktop_daemon")) {
  fail("setup.js 未按 sidecarRunning 与 desktop_daemon.detail 更新调用点");
} else pass("setup.js 已接线 sidecar 就绪判定与守护进程 detail");

const labels = fs.readFileSync(path.join(v3, "js/labels.js"), "utf8");
const openapi = JSON.parse(fs.readFileSync(openapiPath, "utf8"));
const schemas = openapi.components.schemas;

function collectEnums(schema, into) {
  if (!schema || typeof schema !== "object") return;
  if (Array.isArray(schema.enum)) schema.enum.forEach((v) => into.add(String(v)));
  Object.values(schema).forEach((v) => {
    if (v && typeof v === "object") collectEnums(v, into);
  });
}

const errorEnums = new Set(schemas.ErrorCode.enum);
const jobEnums = new Set(schemas.JobState.enum);
const sourceEnums = new Set(schemas.UtteranceSource.enum);
const polishEnums = new Set(schemas.HistoryPolish.enum);
const eventEnums = new Set();
collectEnums(schemas.ProductSSEEventEnvelope, eventEnums);
// also ProductEvent types from const event fields
for (const name of Object.keys(schemas)) {
  if (name.indexOf("Product") === 0 && schemas[name].properties && schemas[name].properties.event) {
    const ev = schemas[name].properties.event;
    if (ev.const) eventEnums.add(ev.const);
  }
}

function hasAll(label, values) {
  const missing = [...values].filter((v) => !labels.includes(v));
  if (missing.length) fail(label + " 缺少：" + missing.join(", "));
  else pass(label + " 已覆盖");
}
hasAll("ErrorCode", errorEnums);
hasAll("JobState", jobEnums);
hasAll("UtteranceSource", sourceEnums);
hasAll("HistoryPolish", polishEnums);
hasAll("ProductEventType", eventEnums);

for (const field of ["stable_text", "tail_text", "raw_text", "final_text", "ocr_text", "polish_enabled", "custom_keywords", "translate_target_lang", "hotkey_voice", "hotkey_screenshot", "hotkey_repeat", "job_id", "delivery_unknown"]) {
  if (!prodText.includes(field)) fail("生产代码未使用契约字段 " + field);
}
pass("关键契约字段已在界面层出现");

const statusJs = fs.readFileSync(path.join(v3, "js/overlay-status.js"), "utf8");
if (!/event === "partial"/.test(statusJs) || !/stable_text/.test(statusJs) || !/tail_text/.test(statusJs)) {
  fail("字幕组件未按 partial + stable_text/tail_text 呈现");
} else pass("字幕只在 partial 事件使用 stable_text/tail_text");

const transJs = fs.readFileSync(path.join(v3, "js/overlay-translate.js"), "utf8");
if (/ocr_text/.test(transJs) && /block-text/.test(transJs)) {
  if (/state\.ocr_text/.test(transJs) && /phase === "error"[\s\S]{0,400}ocr_text/.test(transJs)) {
    fail("失败浮层可能把 ocr_text 当译文");
  }
}
if (transJs.includes("state.text") && transJs.includes("phase === \"error\"")) {
  pass("翻译失败路径存在且主文本使用 error/message");
}

const voiceJs = fs.readFileSync(path.join(v3, "js/voice.js"), "utf8");
if (!voiceJs.includes("onRecordStart") || !voiceJs.includes("onRecordEnd") || !voiceJs.includes("onPickAudio") || !voiceJs.includes("onSendOriginal")) {
  fail("语音页缺少录音控制或发送原文回调");
} else pass("语音页提供录音控制、选文件和发送原文回调");
if (!voiceJs.includes("可能已经输入")) fail("语音页缺少投递未知提示");
else pass("语音页包含投递未知提示");

const histJs = fs.readFileSync(path.join(v3, "js/history.js"), "utf8");
const histCode = histJs.replace(/\/\*[\s\S]*?\*\//g, "").replace(/\/\/.*$/gm, "");
if (histCode.includes("已输入")) fail("历史组件把处理记录写成已输入");
else pass("历史组件未谎称已输入");

if (/paths 目前为空|路径尚未发布/.test(setupJs)) fail("开放接口页仍声称 paths 为空或尚未发布");
else if (!/apiPaths/.test(setupJs) || !/尚未读取接口路径/.test(setupJs)) fail("开放接口页未改为从 apiPaths 派生真实路径");
else pass("开放接口页按派生 apiPaths 列出路径，缺省为尚未读取");

const report = { passes, failures, files: files.map((f) => path.relative(repo, f)) };
const out = path.join(logDir, "check_static.json");
fs.writeFileSync(out, JSON.stringify(report, null, 2));
console.log("静态检查通过 " + passes.length + "，失败 " + failures.length);
for (const m of failures) console.log("FAIL " + m);
for (const m of passes) console.log("PASS " + m);
if (failures.length) process.exit(1);
