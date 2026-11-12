#!/usr/bin/env node
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");
const { execFileSync } = require("child_process");

const repo = path.resolve(__dirname, "../..");
const v3js = path.join(repo, "static/v3/js");
const logDir = path.join(repo, ".pp/tmp/ui");
fs.mkdirSync(logDir, { recursive: true });

const failures = [];
const passes = [];
function fail(msg) { failures.push(msg); }
function pass(msg) { passes.push(msg); }

const jsFiles = fs.readdirSync(v3js).filter((n) => n.endsWith(".js")).map((n) => path.join(v3js, n));
for (const file of jsFiles) {
  try {
    execFileSync("node", ["--check", file], { stdio: "pipe" });
    pass("syntax " + path.basename(file));
  } catch (err) {
    fail("syntax " + path.basename(file) + ": " + (err.stderr || err.message).toString());
  }
}

const context = { window: {}, console: console };
context.window.V3 = {};
vm.createContext(context);
for (const name of ["dom.js", "labels.js", "icons.js", "defaults.js", "hotkey-capture.js"]) {
  vm.runInContext(fs.readFileSync(path.join(v3js, name), "utf8"), context, { filename: name });
}
const V3 = context.window.V3 || context.V3;

if (V3.labels.FIXED_MODELS.asr !== "Qwen3-ASR-1.7B") fail("固定识别模型名不正确");
else pass("固定识别模型为 Qwen3-ASR-1.7B");
if (V3.labels.FIXED_MODELS.ollama !== "qwen3.5:4b") fail("固定纠错模型名不正确");
else pass("固定纠错模型为 qwen3.5:4b");

if (V3.labels.SETUP_MENUS.length !== 8) fail("菜单不是 8 项：" + V3.labels.SETUP_MENUS.length);
else pass("模板 A 八个菜单齐全");

if (V3.labels.source("desktop") !== "本机" || V3.labels.source("web") !== "手机" || V3.labels.source("resend") !== "重发") {
  fail("来源标签不正确");
} else pass("UtteranceSource 中文标签正确");

if (V3.labels.polish("failed") !== "润色失败" || V3.labels.polish("on") !== "已润色") fail("润色标签不正确");
else pass("HistoryPolish 中文标签正确");

if (V3.labels.errorCode("ocr_language_missing") !== "缺少识别语言包") fail("错误码标签缺失");
else pass("错误码中文标签可用");

if (V3.labels.formatError("translate_failed", "本地模型无响应") !== "翻译失败：本地模型无响应") {
  fail("错误格式化不正确：" + V3.labels.formatError("translate_failed", "本地模型无响应"));
} else pass("错误展示包含错误码与后端原文");

if (V3.labels.productEvent("delivery_unknown") !== "投递结果未知") fail("delivery_unknown 标签不正确");
else pass("delivery_unknown 标签正确");

if (V3.labels.errorCode("delivery_failed") !== "投递失败") fail("delivery_failed 标签不正确");
else pass("delivery_failed 中文标签正确");
if (V3.labels.formatError("delivery_failed", "输入目标已消失", { job_id: "job-delivery-1" }) !== "投递失败：输入目标已消失：任务 job-delivery-1") {
  fail("delivery_failed 未展示 details.job_id：" + V3.labels.formatError("delivery_failed", "输入目标已消失", { job_id: "job-delivery-1" }));
} else pass("delivery_failed 展示错误码、原文和 details.job_id");

const setup = V3.defaults.setupState();
if (setup.connection !== "disconnected") fail("设置默认不是未连接");
if (setup.asr.status !== "unknown" || setup.ollama.modelStatus !== "unknown") fail("默认模型状态不得为 ready");
if (setup.asr.sidecarRunning === true) fail("默认 sidecarRunning 不得当作已运行");
if (Object.prototype.hasOwnProperty.call(setup, "hotkeyHint")) fail("defaults 仍含已删除的 hotkeyHint");
if (setup.polish_enabled !== null) fail("未读取时不能把润色开关写成已选");
if (setup.translate_target_lang !== null) fail("未读取时不能把目标语言写成已选");
if (setup.hotkey_voice !== "") fail("未读取时不能写死快捷键");
if (setup.history.records.length !== 0) fail("默认历史非空");
else pass("默认设置状态为未连接且无成功数据");

function ev(init) {
  return Object.assign({ ctrlKey: false, altKey: false, shiftKey: false, metaKey: false, key: "", code: "" }, init || {});
}
if (!V3.hotkey || typeof V3.hotkey.fromKeyboardEvent !== "function") fail("未加载 V3.hotkey.fromKeyboardEvent");
else {
  const rec = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, altKey: true, key: "z", code: "KeyZ" }));
  if (rec.value === "ctrl+alt+z" && rec.consumed === true) pass("Ctrl+Alt+Z 转为 ctrl+alt+z 且 consumed");
  else fail("完整组合转换失败：" + JSON.stringify(rec));

  const pend = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, key: "Control", code: "ControlLeft" }));
  if (pend.pending === true && pend.consumed === false && !pend.value) pass("仅修饰键不结束录入、不消费");
  else fail("仅修饰键转换失败：" + JSON.stringify(pend));

  const esc = V3.hotkey.fromKeyboardEvent(ev({ key: "Escape", code: "Escape" }));
  if (esc.cancel === true && esc.consumed === true) pass("Escape 取消并消费");
  else fail("Escape 转换失败：" + JSON.stringify(esc));

  const tab = V3.hotkey.fromKeyboardEvent(ev({ key: "Tab", code: "Tab" }));
  if (tab.navigate === true && tab.consumed === false) pass("单独 Tab 用于导航、不消费");
  else fail("单独 Tab 转换失败：" + JSON.stringify(tab));

  const shiftTab = V3.hotkey.fromKeyboardEvent(ev({ key: "Tab", code: "Tab", shiftKey: true }));
  if (shiftTab.navigate === true && shiftTab.consumed === false) pass("Shift+Tab 无 ctrl/alt/super 时用于导航");
  else fail("Shift+Tab 转换失败：" + JSON.stringify(shiftTab));

  const ctrlTab = V3.hotkey.fromKeyboardEvent(ev({ key: "Tab", code: "Tab", ctrlKey: true }));
  if (ctrlTab.value === "ctrl+tab" && ctrlTab.consumed === true) pass("Ctrl+Tab 录为 ctrl+tab 并消费");
  else fail("Ctrl+Tab 转换失败：" + JSON.stringify(ctrlTab));

  const needMod = V3.hotkey.fromKeyboardEvent(ev({ key: "a", code: "KeyA" }));
  if (needMod.error && needMod.consumed === false && !needMod.value) pass("无修饰键的主键报错且不消费、不伪造成功");
  else fail("无修饰键转换失败：" + JSON.stringify(needMod));

  const unsup = V3.hotkey.fromKeyboardEvent(ev({ key: "ArrowLeft", code: "ArrowLeft", ctrlKey: true }));
  if (unsup.error && /不支持/.test(unsup.error) && unsup.consumed === false && !unsup.value) pass("不支持按键报错且不伪造成功");
  else fail("不支持按键转换失败：" + JSON.stringify(unsup));

  const layout = V3.hotkey.fromKeyboardEvent(ev({ ctrlKey: true, altKey: true, key: "q", code: "KeyA" }));
  if (layout.value === "ctrl+alt+a" && layout.hint && /键帽/.test(layout.hint)) pass("非 US 布局按物理 code 记录并提示键帽差异");
  else fail("布局提示转换失败：" + JSON.stringify(layout));

  const superA = V3.hotkey.fromKeyboardEvent(ev({ metaKey: true, key: "a", code: "KeyA" }));
  if (superA.value === "super+a" && superA.consumed === true) pass("Meta 转为 super");
  else fail("super 转换失败：" + JSON.stringify(superA));
}

const merged = V3.defaults.merge(setup, { asr: { status: "failed", message: "下载中断" } });
if (merged.asr.status !== "failed" || merged.asr.name !== "Qwen3-ASR-1.7B") fail("状态合并不正确");
if (setup.asr.status !== "unknown") fail("合并污染了默认对象");
else pass("状态合并保留固定模型名且不污染默认值");

const trans = V3.defaults.translateState();
if (trans.phase !== "empty" || trans.text) fail("翻译浮层默认不得有译文");
else pass("翻译浮层默认空/未连接");

const report = { passes, failures };
fs.writeFileSync(path.join(logDir, "check_unit.json"), JSON.stringify(report, null, 2));
console.log("单元检查通过 " + passes.length + "，失败 " + failures.length);
for (const m of failures) console.log("FAIL " + m);
for (const m of passes) console.log("PASS " + m);
if (failures.length) process.exit(1);
