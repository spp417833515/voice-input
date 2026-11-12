/* 翻译浮层：把真实 ocr-translate 响应填回原位置。截图采集不在本层。复制仅在点击时 writeText。 */
(function (root) {
  "use strict";

  var OP = {
    ocr: "ocr_translate_api_v1_ocr_translate_post",
    configGet: "get_config_api_v1_config_get"
  };

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) return labels.formatError(err.error, err.message, err.details);
    return (err && (err.message || err.error)) || "请求失败";
  }

  /* 译文浮层“结果已呈现”本地宿主通知（唯一接缝）。只在桌面宿主为本窗口注册了 WebKit script-message
     handler 且注入了本窗口 generation 时才发；浏览器直接打开（无桥）时是空操作。每页最多一次，pagehide 后失效。
     payload 严格白名单：{v:1, generation, state:"result"|"error"}，不含 OCR 文本、译文、令牌或错误详情。 */
  var PRESENTED_HANDLER = "oviTranslatePresented";
  var PRESENTED_STATES = { result: true, error: true };

  function createPresentedNotifier(options) {
    var w = (options && options.window) || (typeof window !== "undefined" ? window : null);
    var done = false;
    if (w && typeof w.addEventListener === "function") {
      w.addEventListener("pagehide", function () { done = true; });
    }
    return function presented(state) {
      if (done || !PRESENTED_STATES[state] || !w) return false;
      var gen = w.__V3_TRANSLATE_GENERATION__;
      var handlers = w.webkit && w.webkit.messageHandlers;
      var handler = handlers && handlers[PRESENTED_HANDLER];
      if (typeof gen !== "string" || !gen || !handler || typeof handler.postMessage !== "function") return false;
      done = true;
      try {
        handler.postMessage(JSON.stringify({ v: 1, generation: gen, state: state }));
      } catch (e) { /* 宿主已注销：页面照常显示 */ }
      return true;
    };
  }

  function createTranslateController(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var clipboard = opts.clipboard || root.createClipboard(opts);
    var view = null;
    var targetLang = opts.targetLang || "";
    var platform = opts.platform || (root.createPlatformReporter ? root.createPlatformReporter({
      client: client,
      context: context
    }) : null);
    var presented = typeof opts.presented === "function" ? opts.presented : function () {};
    var platformChain = Promise.resolve();

    /* 平台事件串行发出（busy 一定先于 idle/error 到达），但从不阻塞 DOM 呈现：调用方不 await。 */
    function notifyPlatform(body) {
      if (!platform) return platformChain;
      platformChain = platformChain.then(function () {
        return platform.post(body);
      }).then(function () {}, function () { /* 上报失败不阻断翻译，也不假装已进入产品事件流 */ });
      return platformChain;
    }

    function setState(patch) {
      if (view && view.setState) view.setState(patch);
    }

    /* setState 同步重绘真实 DOM（overlay-translate.js render），之后才通知宿主“已呈现”。 */
    function applyResponse(body, usedLang) {
      if (!body) return;
      setState({
        connection: "connected",
        phase: "result",
        text: body.text || "",
        ocr_text: body.ocr_text || "",
        target_lang: body.target_lang != null ? body.target_lang : (usedLang || ""),
        error: null,
        message: "",
        details: null
      });
      presented("result");
    }

    function applyError(err) {
      setState({
        connection: err && err.blocked ? "error" : "connected",
        phase: "error",
        error: err && err.error,
        message: formatErr(err),
        details: err && err.details,
        text: "",
        ocr_text: ""
      });
      presented("error");
    }

    function submitImage(image, lang) {
      if (!context || !context.canCallLocal()) {
        applyError({
          error: "forbidden",
          message: context && context.kind === "file"
            ? "file:// 不能作为成功路径。请通过同源 HTTPS 在本机打开翻译浮层。"
            : "截图翻译接口仅本机可访问。",
          blocked: true
        });
        return Promise.resolve();
      }
      var used = lang != null && lang !== "" ? lang : targetLang;
      if (!used) {
        applyError({ error: "invalid_request", message: "缺少 target_lang，无法提交翻译。" });
        return Promise.resolve();
      }
      if (!image) {
        applyError({ error: "invalid_request", message: "没有可提交的截图。" });
        return Promise.resolve();
      }
      setState({ connection: "loading", phase: "loading", error: null, message: "正在识别并翻译…", target_lang: used });
      var form = new FormData();
      form.append("target_lang", used);
      var filename = (image && image.name) || "capture.png";
      if (image instanceof Blob || (typeof File !== "undefined" && image instanceof File)) {
        form.append("image", image, filename);
      } else {
        form.append("image", image);
      }
      notifyPlatform({ event: "busy" });
      return Promise.resolve().then(function () {
        return client.call(OP.ocr, { body: form });
      }).then(function (body) {
        applyResponse(body, used);
        notifyPlatform({ event: "idle" });
      }, function (err) {
        var report = {
          event: "error",
          error: err && err.error,
          message: formatErr(err),
          details: err && err.details
        };
        applyError(err);
        notifyPlatform(report);
      });
    }

    function start() {
      if (opts.initialResponse) {
        applyResponse(opts.initialResponse, opts.initialResponse.target_lang);
        return Promise.resolve();
      }
      if (!context || !context.canCallLocal()) {
        applyError({
          error: "forbidden",
          message: context && context.kind === "file"
            ? "file:// 不能作为成功路径。请通过同源 HTTPS 在本机打开翻译浮层。"
            : "截图翻译接口仅本机可访问。",
          blocked: true
        });
        return Promise.resolve();
      }
      var pending = opts.initialImage;
      return client.call(OP.configGet).then(function (config) {
        if (config && config.translate_target_lang) targetLang = config.translate_target_lang;
        setState({ connection: "connected", target_lang: targetLang, phase: pending ? "loading" : "empty" });
        if (pending) return submitImage(pending, targetLang);
      }, function (err) {
        if (pending && opts.targetLang) return submitImage(pending, opts.targetLang);
        applyError(err);
      });
    }

    var hooks = {
      onCopy: function (payload) {
        var text = (payload && payload.text != null) ? payload.text : ((view && view.getState && view.getState().text) || "");
        if (!text) {
          setState({ copyStatus: "没有可复制的译文" });
          return;
        }
        return clipboard.writeText(text).then(function () {
          setState({ copyStatus: "已复制" });
        }, function (err) {
          setState({ copyStatus: (err && err.message) || "复制失败" });
        });
      },
      onClose: function () { if (opts.onClose) opts.onClose(); }
    };

    return {
      hooks: hooks,
      bind: function (v) { view = v; },
      start: start,
      submitImage: submitImage,
      applyResponse: applyResponse,
      platformSettled: function () { return platformChain; }
    };
  }

  root.createTranslateController = createTranslateController;
  root.createPresentedNotifier = createPresentedNotifier;
  root.TRANSLATE_PRESENTED_HANDLER = PRESENTED_HANDLER;
})(window.V3Business = window.V3Business || {});
