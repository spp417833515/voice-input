/* 契约枚举与错误码的中文展示。字段名与 _contracts.py / OpenAPI 一致。 */
(function (V3) {
  "use strict";

  const ERROR_CODE = {
    invalid_request: "请求无效",
    not_found: "未找到",
    conflict: "冲突",
    unauthorized: "未授权",
    forbidden: "禁止访问",
    timeout: "超时",
    dependency_failed: "依赖失败",
    asr_unavailable: "语音识别不可用",
    polish_failed: "润色失败",
    translate_failed: "翻译失败",
    ocr_failed: "文字识别失败",
    ocr_language_missing: "缺少识别语言包",
    history_io: "历史记录读写失败",
    jobs_io: "任务读写失败",
    delivery_failed: "投递失败",
    config_io: "配置读写失败",
    internal: "内部错误"
  };

  const JOB_STATE = {
    queued: "已排队",
    claimed: "处理中",
    done: "已完成",
    failed: "失败",
    delivery_unknown: "投递结果未知",
    unknown: "任务不存在"
  };

  const UTTERANCE_SOURCE = {
    desktop: "本机",
    web: "手机",
    resend: "重发"
  };

  const HISTORY_POLISH = {
    on: "已润色",
    off: "未润色",
    failed: "润色失败",
    skipped: "已跳过"
  };

  const PRODUCT_EVENT = {
    idle: "就绪",
    recording: "录音中",
    partial: "识别中",
    polishing: "润色中",
    typed: "已输入",
    error: "出错",
    busy: "已排队",
    history_write_failed: "历史未写入",
    delivery_unknown: "投递结果未知"
  };

  const SESSION_CLOSED_REASON = {
    finished: "已结束",
    cancelled: "已取消",
    error: "出错",
    timeout: "超时",
    disconnected: "连接已断开"
  };

  const SETUP_MENUS = [
    { id: "models", label: "模型" },
    { id: "voice", label: "语音输入" },
    { id: "shot", label: "截图翻译" },
    { id: "keys", label: "快捷键" },
    { id: "lan", label: "手机录入" },
    { id: "hist", label: "最近记录" },
    { id: "api", label: "开放接口" },
    { id: "sys", label: "系统检测" }
  ];

  /* 现有设置页已使用的目标语言值；不是新猜的模型选项。仅识别对应既有值 raw。 */
  const TARGET_LANG_OPTIONS = [
    { value: "zh", label: "中文" },
    { value: "en", label: "英文" },
    { value: "ja", label: "日文" },
    { value: "ko", label: "韩文" },
    { value: "raw", label: "仅识别文字（不翻译）" }
  ];

  const FIXED_MODELS = {
    asr: "Qwen3-ASR-1.7B",
    ollama: "qwen3.5:4b"
  };

  function labelOf(map, value, fallback) {
    if (value == null || value === "") return fallback || "";
    if (Object.prototype.hasOwnProperty.call(map, value)) return map[value];
    return fallback != null ? fallback : String(value);
  }

  function errorCode(code) {
    return labelOf(ERROR_CODE, code, code ? String(code) : "错误");
  }

  function formatError(error, message, details) {
    const parts = [];
    if (error) parts.push(errorCode(error));
    if (message) parts.push(String(message));
    const jobId = details && details.job_id;
    if (jobId) parts.push("任务 " + jobId);
    return parts.join("：") || "发生错误";
  }

  function source(value) {
    return labelOf(UTTERANCE_SOURCE, value, "");
  }

  function polish(value) {
    return labelOf(HISTORY_POLISH, value, "");
  }

  function jobState(value) {
    return labelOf(JOB_STATE, value, value ? String(value) : "");
  }

  function productEvent(value) {
    return labelOf(PRODUCT_EVENT, value, "");
  }

  function targetLang(value, options) {
    const list = options && options.length ? options : TARGET_LANG_OPTIONS;
    for (let i = 0; i < list.length; i++) {
      if (list[i].value === value) return list[i].label;
    }
    return value ? String(value) : "";
  }

  function polishBadgeClass(value) {
    if (value === "on") return "accent";
    if (value === "failed") return "danger";
    if (value === "skipped" || value === "off") return "";
    return "";
  }

  V3.labels = {
    ERROR_CODE: ERROR_CODE,
    JOB_STATE: JOB_STATE,
    UTTERANCE_SOURCE: UTTERANCE_SOURCE,
    HISTORY_POLISH: HISTORY_POLISH,
    PRODUCT_EVENT: PRODUCT_EVENT,
    SESSION_CLOSED_REASON: SESSION_CLOSED_REASON,
    SETUP_MENUS: SETUP_MENUS,
    TARGET_LANG_OPTIONS: TARGET_LANG_OPTIONS,
    FIXED_MODELS: FIXED_MODELS,
    labelOf: labelOf,
    errorCode: errorCode,
    formatError: formatError,
    source: source,
    polish: polish,
    jobState: jobState,
    productEvent: productEvent,
    targetLang: targetLang,
    polishBadgeClass: polishBadgeClass
  };
})(window.V3 = window.V3 || {});
