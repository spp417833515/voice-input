#!/usr/bin/env node
"use strict";

const { spawnSync } = require("child_process");
const fs = require("fs");
const path = require("path");

const here = __dirname;
const repo = path.resolve(here, "../..");
const logDir = path.join(repo, ".pp/tmp/ui");
const gateDir = path.join(repo, ".pp/gate/ui");
fs.mkdirSync(logDir, { recursive: true });
fs.mkdirSync(gateDir, { recursive: true });

function runNode(script) {
  const result = spawnSync(process.execPath, [script], { encoding: "utf8", cwd: repo });
  const name = path.basename(script);
  fs.writeFileSync(path.join(logDir, name + ".out"), (result.stdout || "") + (result.stderr || ""));
  return result;
}

const staticResult = runNode(path.join(here, "check_static.js"));
const unitResult = runNode(path.join(here, "check_unit.js"));
const businessResult = runNode(path.join(here, "check_business.js"));

let chrome = { ran: false, pass: null, visual: false, detail: "未运行视觉检查" };
const chromeBin = process.env.CHROME_BIN || "google-chrome";
const harness = path.join(here, "harness.html");
const which = spawnSync("which", [chromeBin], { encoding: "utf8" });
if (which.status === 0) {
  const url = "file://" + harness;
  const profile = path.join(logDir, "chrome-profile");
  fs.mkdirSync(profile, { recursive: true });
  const args = [
    "--headless=new",
    "--disable-gpu",
    "--no-first-run",
    "--no-default-browser-check",
    "--allow-file-access-from-files",
    "--user-data-dir=" + profile,
    "--virtual-time-budget=8000",
    "--dump-dom",
    url
  ];
  const result = spawnSync(chromeBin, args, { encoding: "utf8", timeout: 20000 });
  fs.writeFileSync(path.join(logDir, "chrome-dump.html"), (result.stdout || "") + "\n" + (result.stderr || ""));
  chrome.ran = true;
  const out = (result.stdout || "") + (result.stderr || "");
  const title = (out.match(/<title>([^<]*)<\/title>/) || [])[1] || "";
  const reportMatch = out.match(/<pre id="report">([^<]*)<\/pre>/);
  let report = null;
  if (reportMatch) {
    try { report = JSON.parse(reportMatch[1]); } catch (err) { chrome.detail = "无法解析 harness 报告：" + err.message; }
  }
  if (title.indexOf("HARNESS_PASS") === 0 && report) {
    chrome.pass = true;
    chrome.detail = title + "；断言 " + report.passes.length + " 通过。未做像素对比。";
  } else if (title.indexOf("HARNESS_FAIL") === 0 && report) {
    chrome.pass = false;
    chrome.detail = title + "\n" + report.failures.join("\n");
  } else if (result.status !== 0) {
    chrome.pass = false;
    chrome.detail = "Chrome 退出码 " + result.status + "：" + (result.stderr || result.stdout || "").slice(0, 800);
  } else {
    chrome.pass = false;
    chrome.detail = "未能从 dump-dom 读取 harness 结果。title=" + title;
  }
} else {
  chrome.detail = "未找到 google-chrome，跳过 DOM 运行时检查；未做视觉/像素验收。";
}

const ok = staticResult.status === 0 && unitResult.status === 0 && businessResult.status === 0 && (chrome.pass !== false);
const summary = [
  "v3 UI + 业务接线检查",
  "static: " + (staticResult.status === 0 ? "PASS" : "FAIL"),
  "unit: " + (unitResult.status === 0 ? "PASS" : "FAIL"),
  "business: " + (businessResult.status === 0 ? "PASS" : "FAIL"),
  "chrome harness: " + (chrome.ran ? (chrome.pass ? "PASS（无像素对比）" : "FAIL") : "SKIP"),
  chrome.detail,
  "",
  "--- static ---",
  staticResult.stdout || staticResult.stderr,
  "--- unit ---",
  unitResult.stdout || unitResult.stderr,
  "--- business ---",
  businessResult.stdout || businessResult.stderr
].join("\n");
fs.writeFileSync(path.join(logDir, "summary.txt"), summary);
fs.writeFileSync(path.join(gateDir, "summary.txt"), summary);
console.log(summary);
process.exit(ok ? 0 : 1);
