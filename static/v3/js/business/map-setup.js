/* 把 setup/check、status、config、history、任务进度、局域网入口映到设置页状态。
 * 组件就绪只看 asr.status / ollama.model_status 等真实字段，不用 injected，不以 check.ok / status.ok 冒充全部就绪。
 * 进度只展示服务端给出的 percent/字节/stage，不推算假百分比。
 */
(function (root) {
  "use strict";

  var LEGAL_CONFIG_KEYS = [
    "polish_enabled",
    "custom_keywords",
    "translate_target_lang",
    "hotkey_voice",
    "hotkey_screenshot",
    "hotkey_repeat"
  ];

  function unique(list) {
    var out = [];
    var seen = Object.create(null);
    (list || []).forEach(function (item) {
      var s = String(item);
      if (seen[s]) return;
      seen[s] = true;
      out.push(s);
    });
    return out;
  }

  function pickLegalConfig(patch) {
    var out = {};
    if (!patch || typeof patch !== "object") return out;
    LEGAL_CONFIG_KEYS.forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(patch, key)) out[key] = patch[key];
    });
    return out;
  }

  function parseKeywords(value) {
    if (Array.isArray(value)) {
      return value.map(function (v) { return String(v).trim(); }).filter(Boolean);
    }
    return String(value || "").split(/[,，、\s]+/).map(function (v) { return v.trim(); }).filter(Boolean);
  }

  function applyConfig(state, config) {
    if (!config || typeof config !== "object") return state;
    state.polish_enabled = config.polish_enabled;
    state.custom_keywords = Array.isArray(config.custom_keywords) ? config.custom_keywords.slice() : [];
    state.translate_target_lang = config.translate_target_lang != null ? config.translate_target_lang : null;
    state.hotkey_voice = config.hotkey_voice || "";
    state.hotkey_screenshot = config.hotkey_screenshot || "";
    state.hotkey_repeat = config.hotkey_repeat || "";
    return state;
  }

  function formatBytes(n) {
    if (typeof n !== "number" || !isFinite(n) || n < 0) return null;
    if (n < 1024) return String(Math.round(n)) + " B";
    if (n < 1024 * 1024) {
      var kb = n / 1024;
      return (Math.round(kb * 10) / 10).toString() + " KB";
    }
    var mb = n / (1024 * 1024);
    return (Math.round(mb * 10) / 10).toString() + " MB";
  }

  function mapProgress(progress) {
    if (!progress || typeof progress !== "object") return null;
    var percent = (typeof progress.percent === "number" && isFinite(progress.percent)) ? progress.percent : null;
    var receivedLabel = formatBytes(progress.completed_bytes);
    var totalLabel = formatBytes(progress.total_bytes);
    var stage = progress.stage != null && progress.stage !== "" ? String(progress.stage) : null;
    return {
      percent: percent,
      receivedLabel: receivedLabel,
      totalLabel: totalLabel,
      stage: stage
    };
  }

  function mapAsr(asr) {
    asr = asr || {};
    var status = asr.status || null;
    return {
      name: asr.model_name || (typeof V3 !== "undefined" && V3.labels && V3.labels.FIXED_MODELS.asr) || "Qwen3-ASR-1.7B",
      status: status,
      sizeLabel: asr.size_label != null ? asr.size_label : null,
      deviceLabel: asr.device_label != null ? asr.device_label : null,
      progress: status === "downloading" ? mapProgress(asr.progress) : (asr.progress ? mapProgress(asr.progress) : null),
      message: asr.message || "",
      action: asr.action || null,
      taskId: asr.task_id || null,
      weightsPresent: asr.weights_present,
      sidecarRunning: asr.sidecar_running
    };
  }

  function mapOllama(ollama) {
    ollama = ollama || {};
    var modelStatus = ollama.model_status || null;
    return {
      modelName: ollama.model_name || (typeof V3 !== "undefined" && V3.labels && V3.labels.FIXED_MODELS.ollama) || "qwen3.5:4b",
      installed: typeof ollama.installed === "boolean" ? ollama.installed : null,
      running: typeof ollama.running === "boolean" ? ollama.running : null,
      version: ollama.version != null ? ollama.version : null,
      modelStatus: modelStatus,
      sizeLabel: ollama.size_label != null ? ollama.size_label : null,
      progress: modelStatus === "downloading" ? mapProgress(ollama.progress) : (ollama.progress ? mapProgress(ollama.progress) : null),
      message: ollama.message || "",
      action: ollama.action || null,
      taskId: ollama.task_id || null
    };
  }

  function mapCapabilities(list) {
    if (!Array.isArray(list)) return null;
    return list.map(function (item) {
      item = item || {};
      return {
        id: item.id,
        name: item.label || item.id || "未命名能力",
        label: item.label,
        available: item.available,
        required: item.required,
        detail: item.detail != null ? item.detail : "",
        message: item.detail != null ? item.detail : ""
      };
    });
  }

  function mapLanAccess(lan) {
    lan = lan || {};
    var entries = Array.isArray(lan.entries) ? lan.entries.map(function (entry) {
      entry = entry || {};
      return {
        ip: entry.ip || "",
        url: entry.url || "",
        urlMasked: entry.url_masked || "",
        qrSvg: entry.qr_svg || ""
      };
    }) : [];
    return {
      port: lan.port,
      token: lan.token || "",
      tokenMasked: lan.token_masked || "",
      tokenPresent: !!(lan.token),
      message: lan.message || "",
      entries: entries
    };
  }

  function mapReadiness(check, status) {
    var checkGates = (check && check.unmet_gates) || [];
    var statusGates = (status && status.unmet_gates) || [];
    var gates = unique(checkGates.concat(statusGates));
    var asr = mapAsr(check && check.asr);
    var ollama = mapOllama(check && check.ollama);
    var capabilities = mapCapabilities(check && check.capabilities);
    var gateText = gates.length ? ("未达上线条件：" + gates.join("；")) : "";
    var asrMsg = asr.message || "";
    var notice = gateText || null;
    return {
      gates: gates,
      asr: asr,
      ollama: ollama,
      capabilities: capabilities,
      statusNotice: notice,
      sessionType: check && check.session_type ? check.session_type : null,
      asrMessage: asrMsg
    };
  }

  function applyHistory(payload) {
    payload = payload || {};
    return {
      records: Array.isArray(payload.records) ? payload.records : [],
      total: payload.total != null ? payload.total : 0,
      skipped_malformed: payload.skipped_malformed || 0,
      loading: false,
      message: null
    };
  }

  function publicLanEntries(entries) {
    return (entries || []).map(function (entry) {
      return {
        ip: entry.ip || "",
        urlMasked: entry.urlMasked || entry.url_masked || "",
        qrSvg: entry.qrSvg || entry.qr_svg || ""
      };
    });
  }

  function mapApiPaths(catalog) {
    if (!catalog || typeof catalog.all !== "function") return null;
    return catalog.all().slice().sort(function (a, b) {
      var p = String(a.path).localeCompare(String(b.path));
      return p !== 0 ? p : String(a.method).localeCompare(String(b.method));
    }).map(function (op) {
      return {
        method: op.method,
        path: op.path,
        operationId: op.operationId,
        access: op.access
      };
    });
  }

  function applyTaskToModels(asr, ollama, task) {
    if (!task || !task.kind) return { asr: asr, ollama: ollama };
    var nextAsr = Object.assign({}, asr || {});
    var nextOllama = Object.assign({}, ollama || {});
    var running = task.state === "running";
    var failed = task.state === "failed";
    var progress = running ? mapProgress(task.progress) : null;
    var message = task.message || (failed && task.error ? String(task.error) : "") || (failed ? "任务失败" : "");
    var kind = task.kind;
    if (kind === "asr_download" || kind === "asr_start") {
      if (kind === "asr_download") {
        if (running) nextAsr.status = "downloading";
        else if (failed) nextAsr.status = "failed";
      }
      nextAsr.progress = progress;
      nextAsr.message = message;
      nextAsr.action = kind;
      nextAsr.taskId = task.task_id || nextAsr.taskId;
    } else if (kind === "ollama_install" || kind === "ollama_start" || kind === "ollama_pull") {
      if (running) {
        if (kind === "ollama_pull") nextOllama.modelStatus = "downloading";
        else if (kind === "ollama_install") nextOllama.installed = nextOllama.installed === true ? true : false;
      } else if (failed) {
        if (kind === "ollama_pull") nextOllama.modelStatus = "failed";
        else if (kind === "ollama_install") nextOllama.installed = false;
        else if (kind === "ollama_start") nextOllama.running = false;
      }
      nextOllama.progress = progress;
      nextOllama.message = message;
      nextOllama.action = kind;
      nextOllama.taskId = task.task_id || nextOllama.taskId;
    }
    return { asr: nextAsr, ollama: nextOllama };
  }

  root.LEGAL_CONFIG_KEYS = LEGAL_CONFIG_KEYS;
  root.pickLegalConfig = pickLegalConfig;
  root.parseKeywords = parseKeywords;
  root.applyConfig = applyConfig;
  root.formatBytes = formatBytes;
  root.mapProgress = mapProgress;
  root.mapAsr = mapAsr;
  root.mapOllama = mapOllama;
  root.mapCapabilities = mapCapabilities;
  root.mapLanAccess = mapLanAccess;
  root.mapReadiness = mapReadiness;
  root.applyHistory = applyHistory;
  root.publicLanEntries = publicLanEntries;
  root.mapApiPaths = mapApiPaths;
  root.applyTaskToModels = applyTaskToModels;
})(window.V3Business = window.V3Business || {});
