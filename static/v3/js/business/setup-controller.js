/* 设置页接线：check / tasks / lan-access / config GET+POST。任务轮询可取消，不把 202 或 injected 当成就绪。 */
(function (root) {
  "use strict";

  var OP = {
    check: "setup_check_api_setup_check_get",
    taskStart: "setup_task_start_api_setup_tasks_post",
    taskGet: "setup_task_get_api_setup_tasks__task_id__get",
    lanAccess: "lan_access_api_v1_lan_access_get",
    status: "status_api_v1_status_get",
    configGet: "get_config_api_v1_config_get",
    configPost: "post_config_api_v1_config_post",
    history: "history_api_v1_history_get"
  };

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) return labels.formatError(err.error, err.message);
    return (err && (err.message || err.error)) || "请求失败";
  }

  function schedule(fn, ms, opts) {
    var start = (opts && opts.setTimeout) || (typeof setTimeout === "function" ? setTimeout : null);
    if (!start) {
      fn();
      return 0;
    }
    return start(fn, ms);
  }

  function unschedule(id, opts) {
    var stop = (opts && opts.clearTimeout) || (typeof clearTimeout === "function" ? clearTimeout : null);
    if (stop && id != null) stop(id);
  }

  function createSetupController(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var catalog = opts.catalog;
    var clipboard = opts.clipboard || root.createClipboard(opts);
    var view = null;
    var destroyed = false;
    var pollTimer = null;
    var pollTaskId = null;
    var pollGeneration = 0;
    var lanToken = null;
    var lanUrls = [];
    var pollInterval = opts.pollInterval != null ? opts.pollInterval : 500;

    function setState(patch) {
      if (destroyed) return;
      if (view && typeof view.setState === "function") view.setState(patch);
    }

    function getState() {
      return view && view.getState ? view.getState() : {};
    }

    function stopPoll() {
      unschedule(pollTimer, opts);
      pollTimer = null;
      pollTaskId = null;
      pollGeneration += 1;
    }

    function destroy() {
      destroyed = true;
      stopPoll();
    }

    function storeLan(mapped) {
      lanToken = mapped && mapped.token ? mapped.token : null;
      lanUrls = [];
      var entries = (mapped && mapped.entries) || [];
      var publicEntries = entries.map(function (entry, index) {
        lanUrls[index] = entry.url || "";
        return {
          ip: entry.ip || "",
          urlMasked: entry.urlMasked || "",
          qrSvg: entry.qrSvg || ""
        };
      });
      return publicEntries;
    }

    function applyMapped(check, status, config, history, lan, extras) {
      var mapped = root.mapReadiness(check, status);
      var patch = Object.assign({
        connection: "connected",
        connectionError: null,
        statusNotice: mapped.statusNotice,
        unmetGates: mapped.gates,
        asr: mapped.asr,
        ollama: mapped.ollama,
        history: root.applyHistory(history),
        keywordsSaved: false,
        targetLangSaved: false,
        hotkeysSaved: false,
        capabilities: mapped.capabilities,
        apiPaths: root.mapApiPaths(catalog),
        aboutText: "本地运行。组件状态来自设置检查，不把检查成功或注入标记当成已就绪。"
      }, extras || {});
      if (opts.pageOrigin) patch.serviceUrl = String(opts.pageOrigin);
      root.applyConfig(patch, config);
      if (lan) {
        var lanMapped = root.mapLanAccess(lan);
        patch.lanEntries = storeLan(lanMapped);
        patch.tokenMasked = lanMapped.tokenMasked;
        patch.tokenPresent = lanMapped.tokenPresent;
        patch.lanMessage = lanMapped.message || "";
      }
      setState(patch);
      maybeResumePoll(mapped);
      return mapped;
    }

    function maybeResumePoll(mapped) {
      if (destroyed) return;
      var asrId = mapped && mapped.asr && mapped.asr.taskId && (mapped.asr.status === "downloading" || mapped.asr.action === "asr_start")
        ? mapped.asr.taskId : null;
      var olId = mapped && mapped.ollama && mapped.ollama.taskId && (mapped.ollama.modelStatus === "downloading" || mapped.ollama.action === "ollama_install" || mapped.ollama.action === "ollama_start")
        ? mapped.ollama.taskId : null;
      var taskId = asrId || olId;
      if (taskId) pollTask(taskId);
    }

    function applyTask(task) {
      if (destroyed || !task) return;
      var state = getState();
      var next = root.applyTaskToModels(state.asr, state.ollama, task);
      var notice = task.state === "failed"
        ? (task.message || (task.error ? String(task.error) : "任务失败"))
        : (task.message || state.statusNotice);
      setState({
        asr: next.asr,
        ollama: next.ollama,
        statusNotice: notice,
        connection: "connected"
      });
    }

    function overlayTaskFailure(mapped, task) {
      if (!mapped || !task || task.state !== "failed") return mapped;
      var message = task.message || (task.error ? String(task.error) : "任务失败");
      var kind = task.kind;
      if (kind === "asr_start") {
        mapped.asr = Object.assign({}, mapped.asr, {
          progress: null,
          message: message,
          action: kind,
          taskId: task.task_id || mapped.asr.taskId
        });
      } else if (kind === "asr_download") {
        if (mapped.asr.status !== "failed" && mapped.asr.status !== "ready") {
          mapped.asr = Object.assign({}, mapped.asr, {
            status: "failed",
            progress: null,
            message: message,
            action: kind,
            taskId: task.task_id || mapped.asr.taskId
          });
        } else {
          mapped.asr = Object.assign({}, mapped.asr, {
            progress: null,
            message: message,
            action: kind,
            taskId: task.task_id || mapped.asr.taskId
          });
        }
      } else if (kind === "ollama_install" || kind === "ollama_start" || kind === "ollama_pull") {
        var ol = Object.assign({}, mapped.ollama, { progress: null, message: message, action: kind, taskId: task.task_id || mapped.ollama.taskId });
        if (kind === "ollama_pull" && ol.modelStatus !== "ready") ol.modelStatus = "failed";
        if (kind === "ollama_install" && ol.installed !== true) ol.installed = false;
        if (kind === "ollama_start" && ol.running !== true) ol.running = false;
        mapped.ollama = ol;
      }
      mapped.statusNotice = message;
      return mapped;
    }

    function refreshCheck(afterTask) {
      if (destroyed || !context || !context.canCallLocal()) return Promise.resolve();
      return Promise.all([
        client.call(OP.check),
        client.call(OP.status)
      ]).then(function (results) {
        if (destroyed) return;
        var mapped = overlayTaskFailure(root.mapReadiness(results[0], results[1]), afterTask);
        setState({
          asr: mapped.asr,
          ollama: mapped.ollama,
          unmetGates: mapped.gates,
          capabilities: mapped.capabilities,
          statusNotice: mapped.statusNotice,
          connection: "connected"
        });
      }, function (err) {
        if (destroyed) return;
        setState({ statusNotice: formatErr(err) });
      });
    }

    function pollTask(taskId) {
      if (destroyed || !taskId) return Promise.resolve();
      if (pollTaskId === taskId && pollTimer != null) return Promise.resolve();
      unschedule(pollTimer, opts);
      pollTimer = null;
      pollTaskId = taskId;
      var gen = pollGeneration;
      return new Promise(function (resolve) {
        pollTimer = schedule(function () {
          pollTimer = null;
          if (destroyed || gen !== pollGeneration || pollTaskId !== taskId) {
            resolve();
            return;
          }
          client.call(OP.taskGet, { pathParams: { task_id: taskId } }).then(function (task) {
            if (destroyed || gen !== pollGeneration) {
              resolve(task);
              return;
            }
            applyTask(task);
            if (task && task.state === "running") {
              pollTaskId = null;
              return pollTask(task.task_id).then(resolve);
            }
            pollTaskId = null;
            return refreshCheck(task).then(function () { resolve(task); });
          }, function (err) {
            if (destroyed || gen !== pollGeneration) {
              resolve();
              return;
            }
            pollTaskId = null;
            setState({ statusNotice: formatErr(err), connection: "connected" });
            resolve();
          });
        }, pollInterval);
      });
    }

    function loadAll() {
      if (destroyed) return Promise.resolve();
      stopPoll();
      if (!context || !context.canCallLocal()) {
        var msg = !context
          ? "缺少访问上下文"
          : (context.kind === "file"
            ? "file:// 不能作为成功路径。请通过同源 HTTPS 在本机打开设置页。"
            : "设置页的状态与配置接口仅本机可访问。");
        lanToken = null;
        lanUrls = [];
        setState({
          connection: "error",
          connectionError: msg,
          statusNotice: msg,
          lanEntries: null,
          tokenPresent: false,
          tokenMasked: "",
          capabilities: null,
          apiPaths: root.mapApiPaths(catalog)
        });
        return Promise.resolve();
      }
      setState({ connection: "loading", history: { loading: true } });
      return Promise.all([
        client.call(OP.check),
        client.call(OP.status),
        client.call(OP.configGet),
        client.call(OP.history, { query: { limit: 50, offset: 0 } }),
        client.call(OP.lanAccess)
      ]).then(function (results) {
        if (destroyed) return;
        applyMapped(results[0], results[1], results[2], results[3], results[4]);
      }, function (err) {
        if (destroyed) return;
        setState({
          connection: "error",
          connectionError: formatErr(err),
          statusNotice: formatErr(err),
          history: { records: [], total: null, skipped_malformed: 0, loading: false, message: formatErr(err) }
        });
      });
    }

    function saveConfig(patch, flags) {
      var body = root.pickLegalConfig(patch);
      return client.call(OP.configPost, { body: body }).then(function (config) {
        if (destroyed) return config;
        var next = Object.assign({ connection: "connected" }, flags || {});
        root.applyConfig(next, config);
        setState(next);
        return config;
      }, function (err) {
        if (!destroyed) {
          var notice = formatErr(err);
          var errPatch = { statusNotice: notice, connection: "connected" };
          if (flags && Object.prototype.hasOwnProperty.call(flags, "hotkeysSaved")) {
            errPatch.hotkeySaveError = notice;
          }
          setState(errPatch);
        }
        throw err;
      });
    }

    function startTask(kind) {
      if (destroyed) return Promise.resolve();
      setState({ statusNotice: "正在启动任务…" });
      return client.call(OP.taskStart, { body: { kind: kind } }).then(function (task) {
        if (destroyed) return task;
        applyTask(task);
        if (task && task.state === "running") return pollTask(task.task_id).then(function () { return task; });
        if (task && (task.state === "succeeded" || task.state === "failed")) {
          return refreshCheck(task).then(function () { return task; });
        }
        return task;
      }, function (err) {
        if (destroyed) return;
        var message = formatErr(err);
        var state = getState();
        if (kind === "asr_download") {
          setState({
            asr: Object.assign({}, state.asr || {}, { status: "failed", progress: null, message: message, action: kind }),
            statusNotice: message,
            connection: "connected"
          });
        } else if (kind === "asr_start") {
          setState({
            asr: Object.assign({}, state.asr || {}, { progress: null, message: message, action: kind }),
            statusNotice: message,
            connection: "connected"
          });
        } else {
          var ol = Object.assign({}, state.ollama || {}, { progress: null, message: message, action: kind });
          if (kind === "ollama_pull") ol.modelStatus = "failed";
          setState({ ollama: ol, statusNotice: message, connection: "connected" });
        }
      });
    }

    function copy(payload) {
      payload = payload || {};
      var text = payload.text;
      if (payload.kind === "token") {
        text = lanToken;
        if (!text) {
          setState({ copyKind: "token", copyStatus: "当前没有可复制的访问令牌。" });
          return Promise.resolve();
        }
      }
      if (payload.kind === "lan_url" && !text && payload.index != null) {
        text = lanUrls[payload.index] || "";
      }
      if (!text) {
        setState({ copyKind: payload.kind || "text", copyStatus: "没有可复制的内容。" });
        return Promise.resolve();
      }
      return clipboard.writeText(text).then(function () {
        setState({ copyKind: payload.kind || "text", copyStatus: "已复制" });
      }, function (err) {
        setState({ copyKind: payload.kind || "text", copyStatus: (err && err.message) || "复制失败" });
      });
    }

    var hooks = {
      onRefreshStatus: function () { return loadAll(); },
      onDownloadAsr: function () { return startTask("asr_download"); },
      onRetryAsr: function () { return startTask("asr_download"); },
      onStartAsr: function () { return startTask("asr_start"); },
      onInstallOllama: function () { return startTask("ollama_install"); },
      onStartOllama: function () { return startTask("ollama_start"); },
      onDownloadOllamaModel: function () { return startTask("ollama_pull"); },
      onRetryOllamaModel: function () { return startTask("ollama_pull"); },
      onTogglePolish: function (enabled) {
        return saveConfig({ polish_enabled: !!enabled }, {});
      },
      onSaveKeywords: function (value) {
        return saveConfig({ custom_keywords: root.parseKeywords(value) }, { keywordsSaved: true });
      },
      onChangeTargetLang: function (value) {
        return saveConfig({ translate_target_lang: value }, { targetLangSaved: true });
      },
      onSaveHotkeys: function (keys) {
        return saveConfig({
          hotkey_voice: keys && keys.hotkey_voice,
          hotkey_screenshot: keys && keys.hotkey_screenshot,
          hotkey_repeat: keys && keys.hotkey_repeat
        }, {
          hotkeysSaved: true,
          hotkeyDrafts: null,
          hotkeyCapturing: null,
          hotkeyCaptureError: "",
          hotkeyCaptureHint: "",
          hotkeySaveError: ""
        });
      },
      onCopy: function (payload) { return copy(payload); },
      onLoadMoreHistory: function () {
        var hist = (getState().history) || {};
        var shown = (hist.records || []).length;
        setState({ history: { loading: true } });
        return client.call(OP.history, { query: { limit: 50, offset: shown } }).then(function (payload) {
          if (destroyed) return;
          var more = root.applyHistory(payload);
          setState({
            history: {
              records: (hist.records || []).concat(more.records),
              total: more.total,
              skipped_malformed: (hist.skipped_malformed || 0) + (more.skipped_malformed || 0),
              loading: false,
              message: null
            }
          });
        }, function (err) {
          if (!destroyed) setState({ history: { loading: false, message: formatErr(err) } });
        });
      }
    };

    return {
      hooks: hooks,
      bind: function (v) { view = v; },
      start: function () { return loadAll(); },
      loadAll: loadAll,
      saveConfig: saveConfig,
      startTask: startTask,
      destroy: destroy,
      _debug: {
        getLanToken: function () { return lanToken; },
        getLanUrls: function () { return lanUrls.slice(); },
        isDestroyed: function () { return destroyed; },
        polling: function () { return pollTaskId; }
      }
    };
  }

  root.createSetupController = createSetupController;
  root.SETUP_OPERATIONS = OP;
})(window.V3Business = window.V3Business || {});
