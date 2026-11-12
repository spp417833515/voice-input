/* 手机录音页：用户按下才开麦；松开后整段上传；失败可显式发原文。不从局域网硬调本机 config/status。 */
(function (root) {
  "use strict";

  var OP = {
    fromAudio: "from_audio_api_v1_utterances_from_audio_post",
    resend: "resend_api_v1_utterances_resend_post",
    job: "query_api_v1_jobs__job_id__get",
    history: "history_api_v1_history_get"
  };

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) return labels.formatError(err && err.error, err && err.message, err && err.details);
    return (err && (err.message || err.error)) || "请求失败";
  }

  function createVoiceController(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var jobIds = opts.jobIds || root.createJobIds();
    var audio = opts.audio || root.createAudioSession(opts);
    var documentRef = opts.document || (typeof document !== "undefined" ? document : null);
    var setTimeoutFn = opts.setTimeout || (typeof setTimeout === "function" ? setTimeout : function (fn) { fn(); return 0; });
    var clearTimeoutFn = opts.clearTimeout || (typeof clearTimeout === "function" ? clearTimeout : function () {});
    var view = null;
    var pollTimer = null;
    var elapsedTimer = null;
    var recordStartedAt = 0;
    var lastRaw = "";
    var lastJobId = null;
    var lastOriginalId = null;
    var visibilityHandler = null;
    var offlineHandler = null;
    var pagehideHandler = null;
    var destroyed = false;
    var windowRef = opts.window || (typeof window !== "undefined" ? window : null);

    function setState(patch) {
      if (!destroyed && view && view.setState) view.setState(patch);
    }

    function getState() {
      return view && view.getState ? view.getState() : {};
    }

    function tokenOk() {
      if (context && context.kind === "local") return true;
      return !!(context && context.tokenPresent);
    }

    function stopTimers() {
      if (pollTimer) { clearTimeoutFn(pollTimer); pollTimer = null; }
      if (elapsedTimer) { clearTimeoutFn(elapsedTimer); elapsedTimer = null; }
    }

    function audioBusy() {
      return !!(audio && ((audio.isAcquiring && audio.isAcquiring()) || (audio.isRecording && audio.isRecording())));
    }

    function interruptCapture(message, extra) {
      if (!audioBusy()) return false;
      if (audio && audio.cancel) audio.cancel();
      stopTimers();
      setState(Object.assign({
        phase: "idle",
        micEnabled: false,
        statusText: message,
        statusTone: "warn"
      }, extra || {}));
      return true;
    }

    function bindLifecycle() {
      if (documentRef && typeof documentRef.addEventListener === "function") {
        visibilityHandler = function () {
          if (documentRef.hidden) {
            interruptCapture("页面已切走，录音已中断，未提交。");
          }
        };
        documentRef.addEventListener("visibilitychange", visibilityHandler);
      }
      if (windowRef && typeof windowRef.addEventListener === "function") {
        pagehideHandler = function () {
          interruptCapture("页面已离开，录音已中断，未提交。");
        };
        windowRef.addEventListener("pagehide", pagehideHandler);
        offlineHandler = function () {
          interruptCapture("网络已断开，已停止录音，未自动重试。", {
            connection: "error",
            statusTone: "err"
          });
        };
        windowRef.addEventListener("offline", offlineHandler);
      }
    }

    function unbindLifecycle() {
      if (documentRef && visibilityHandler) {
        documentRef.removeEventListener("visibilitychange", visibilityHandler);
      }
      visibilityHandler = null;
      if (windowRef && offlineHandler) {
        windowRef.removeEventListener("offline", offlineHandler);
      }
      offlineHandler = null;
      if (windowRef && pagehideHandler) {
        windowRef.removeEventListener("pagehide", pagehideHandler);
      }
      pagehideHandler = null;
    }

    function mapJobPhase(job) {
      if (!job) return { phase: "waiting" };
      var state = job.state;
      if (state === "queued") return { phase: "queued", job_state: state };
      if (state === "claimed") return { phase: "claimed", job_state: state };
      if (state === "done") return { phase: "done", job_state: state };
      if (state === "failed") return { phase: "failed", job_state: state, message: job.detail || "处理失败" };
      if (state === "delivery_unknown") {
        return { phase: "delivery_unknown", job_state: state, text: job.text || "" };
      }
      return { phase: "waiting", job_state: state };
    }

    function pollJob(jobId) {
      stopTimers();
      function tick() {
        if (destroyed) return;
        client.call(OP.job, { pathParams: { job_id: jobId } }).then(function (body) {
          if (destroyed) return;
          if (body && (body.found === false || (body.state === "unknown" && !body.job))) {
            setState({
              phase: "failed",
              job_id: jobId,
              job_state: "unknown",
              error: "not_found",
              message: "任务不存在或尚未写入。"
            });
            return;
          }
          var job = body.job || body;
          var mapped = mapJobPhase(job);
          var patch = Object.assign({
            job_id: job.job_id || jobId,
            text: job.text || getState().text || "",
            error: mapped.phase === "failed" ? (mapped.error || "internal") : null
          }, mapped);
          if (mapped.phase === "failed" && lastRaw) patch.raw_text = lastRaw;
          setState(patch);
          if (mapped.phase === "queued" || mapped.phase === "claimed" || mapped.phase === "waiting") {
            pollTimer = setTimeoutFn(tick, 800);
          }
        }, function (err) {
          if (destroyed) return;
          if (err && err.network) {
            setState({
              phase: "waiting",
              job_id: jobId,
              statusText: "查询任务时网络中断，未更换 job_id。",
              statusTone: "warn"
            });
            pollTimer = setTimeoutFn(tick, 1600);
            return;
          }
          setState({
            phase: "failed",
            job_id: jobId,
            error: err.error,
            message: formatErr(err),
            raw_text: lastRaw || ""
          });
        });
      }
      tick();
    }

    function submitAudio(fileLike, slot) {
      if (!context || !context.canCallLanToken()) {
        setState({
          connection: "error",
          tokenValid: tokenOk() ? true : false,
          phase: "failed",
          message: context && context.kind === "file"
            ? "file:// 不能作为成功路径。请通过手机上的同源 HTTPS 打开录音页。"
            : "当前页面不能调用录音上传接口。"
        });
        return Promise.resolve();
      }
      if (context.kind === "lan" && !tokenOk()) {
        setState({
          connection: "error",
          tokenValid: false,
          phase: "failed",
          message: "缺少访问令牌，请从电脑设置页重新打开录音页。"
        });
        return Promise.resolve();
      }
      var jobId = jobIds.reuse(slot);
      lastJobId = jobId;
      setState({
        connection: "connected",
        tokenValid: true,
        phase: "uploading",
        job_id: jobId,
        job_state: null,
        error: null,
        message: "",
        statusText: "",
        micEnabled: false
      });
      var form = new FormData();
      form.append("job_id", jobId);
      var filename = fileLike.filename || (fileLike.file && fileLike.file.name) || "recording.webm";
      var blob = fileLike.blob || fileLike.file;
      form.append("audio", blob, filename);
      return client.call(OP.fromAudio, { body: form }).then(function (body) {
        lastRaw = (body && body.text) || lastRaw;
        setState({
          phase: "queued",
          job_id: body.job_id || jobId,
          job_state: body.state || "queued",
          text: body.text || "",
          raw_text: lastRaw
        });
        pollJob(body.job_id || jobId);
      }, function (err) {
        if (err && err.error === "polish_failed") {
          setState({
            phase: "polish_failed",
            job_id: jobId,
            error: "polish_failed",
            message: formatErr(err),
            details: err.details,
            raw_text: lastRaw || (err.details && err.details.raw_text) || "",
            text: lastRaw || ""
          });
          if (err.details && err.details.raw_text) lastRaw = err.details.raw_text;
          return;
        }
        setState({
          phase: "failed",
          job_id: jobId,
          error: err.error,
          message: formatErr(err),
          details: err.details,
          raw_text: lastRaw || "",
          statusText: err.network ? "网络失败，再次提交将沿用同一 job_id。" : ""
        });
      });
    }

    function submitResend(text, originalId, confirm, slot, focusId) {
      var newId = confirm ? jobIds.fresh(slot) : jobIds.reuse(slot);
      lastJobId = newId;
      lastOriginalId = originalId;
      setState({ phase: "uploading", job_id: newId, error: null, message: "" });
      var req = {
        job_id: newId,
        text: text,
        original_job_id: originalId,
        confirm: !!confirm
      };
      if (focusId) req.focus_id = focusId;
      return client.call(OP.resend, {
        body: req
      }).then(function (body) {
        if (body && body.requires_confirmation) {
          setState({
            phase: "delivery_unknown",
            job_id: body.new_job_id || newId,
            job_state: "delivery_unknown",
            text: text,
            raw_text: lastRaw || text,
            statusText: "文字可能已经输入。确认后将用新的 job_id 关联原任务再提交。",
            statusTone: "warn",
            details: { original_job_id: body.original_job_id, new_job_id: body.new_job_id }
          });
          return body;
        }
        setState({
          phase: "queued",
          job_id: body.job_id || newId,
          job_state: body.state || "queued",
          text: body.text || text,
          raw_text: lastRaw || text
        });
        pollJob(body.job_id || newId);
        return body;
      }, function (err) {
        setState({
          phase: "failed",
          job_id: newId,
          error: err.error,
          message: formatErr(err),
          raw_text: lastRaw || text
        });
      });
    }

    function loadHistory() {
      if (!context || !context.canCallLanToken()) return Promise.resolve();
      if (context.kind === "lan" && !tokenOk()) {
        setState({ history: { records: [], total: null, loading: false, message: "未提供令牌，不能读取历史。", skipped_malformed: 0 } });
        return Promise.resolve();
      }
      setState({ history: { loading: true } });
      return client.call(OP.history, { query: { limit: 5, offset: 0 } }).then(function (payload) {
        setState({ history: root.applyHistory(payload) });
      }, function (err) {
        if (err && (err.status === 403 || err.error === "forbidden" || err.error === "unauthorized")) {
          setState({
            tokenValid: false,
            history: { records: [], total: null, loading: false, skipped_malformed: 0, message: formatErr(err) }
          });
          return;
        }
        setState({ history: { loading: false, message: formatErr(err) } });
      });
    }

    var hooks = {
      onRecordStart: function () {
        if (context && context.kind === "lan" && !tokenOk()) {
          setState({ connection: "error", tokenValid: false, phase: "failed", message: "缺少访问令牌，不能开始录音。" });
          return;
        }
        jobIds.fresh("from-audio");
        recordStartedAt = Date.now();
        setState({ phase: "recording", statusText: "", error: null, message: "", micEnabled: true, elapsed_label: "0:00" });
        function tickElapsed() {
          if (!audio.isRecording()) return;
          var sec = Math.floor((Date.now() - recordStartedAt) / 1000);
          var mm = String(Math.floor(sec / 60));
          var ss = String(sec % 60);
          if (ss.length < 2) ss = "0" + ss;
          setState({ elapsed_label: mm + ":" + ss });
          elapsedTimer = setTimeoutFn(tickElapsed, 250);
        }
        return audio.start().then(function (result) {
          if (!result || result.cancelled) return;
          elapsedTimer = setTimeoutFn(tickElapsed, 250);
        }, function (err) {
          stopTimers();
          setState({
            phase: "idle",
            micEnabled: false,
            useFileUpload: !root.supportedRecorderType(opts.MediaRecorder),
            fileUploadHint: err.message || "无法开始录音",
            statusText: err.message || "无法开始录音",
            statusTone: "err"
          });
        });
      },
      onRecordEnd: function () {
        stopTimers();
        return audio.stop().then(function (result) {
          if (!result || !result.blob) {
            setState({ phase: "idle", micEnabled: false, statusText: "没有采集到音频，未提交。", statusTone: "warn" });
            return;
          }
          return submitAudio(result, "from-audio");
        }, function (err) {
          audio.cancel();
          setState({ phase: "failed", micEnabled: false, message: err.message || "结束录音失败" });
        });
      },
      onRecordCancel: function () {
        stopTimers();
        audio.cancel();
        setState({ phase: "idle", micEnabled: false, statusText: "录音已取消，未提交。", statusTone: "warn" });
      },
      onPickAudio: function (payload) {
        var file = payload && (payload.file || payload);
        if (!root.isAllowedPickFile(file)) {
          setState({
            phase: "failed",
            message: "所选文件不是明确允许的音频格式（webm / ogg / wav / mp3 / m4a / flac）。"
          });
          return;
        }
        jobIds.fresh("from-audio");
        return submitAudio({ blob: file, filename: file.name || root.filenameForType(file.type) }, "from-audio");
      },
      onSendOriginal: function (payload) {
        var text = (payload && payload.text) || lastRaw || getState().raw_text || getState().text || "";
        var original = (payload && payload.job_id) || lastJobId;
        if (!text || !original) {
          setState({ statusText: "没有可发送的原文。", statusTone: "err" });
          return;
        }
        lastRaw = text;
        return submitResend(text, original, false, "send-original:" + original, payload && payload.focus_id);
      },
      onConfirmResendChoice: function (payload) {
        payload = payload || {};
        if (!payload.confirm) return;
        var original = payload.job_id || (payload.record && payload.record.job_id) || lastOriginalId || lastJobId;
        var text = payload.text || (payload.record && (payload.record.final_text || payload.record.raw_text)) || lastRaw || "";
        if (!original || !text) return;
        var slot = "confirm-resend:" + original;
        return submitResend(text, original, true, slot, payload && payload.focus_id);
      },
      onRefreshHistory: function () { return loadHistory(); },
      onStartChangeHotkey: function () { setState({ capturingHotkey: true }); },
      onChangeHotkey: function (payload) {
        var code = payload && (payload.code || payload.recordHotkeyCode);
        var label = payload && (payload.label || payload.recordHotkeyLabel);
        var patch = { capturingHotkey: false };
        if (code) patch.recordHotkeyCode = code;
        if (label) patch.recordHotkeyLabel = label;
        setState(patch);
      }
    };

    function start() {
      bindLifecycle();
      if (context && context.tokenAdoptError) {
        setState({
          connection: "error",
          tokenValid: false,
          phase: "idle",
          message: context.tokenAdoptError
        });
        return Promise.resolve();
      }
      if (context && context.kind === "file") {
        setState({
          connection: "error",
          tokenValid: null,
          phase: "idle",
          message: "file:// 不能作为成功路径。请通过同源 HTTPS 打开录音页。"
        });
        return Promise.resolve();
      }
      if (context && context.kind === "lan" && !tokenOk()) {
        setState({
          connection: "error",
          tokenValid: false,
          phase: "idle",
          message: "缺少访问令牌，请从电脑设置页重新打开录音页。"
        });
        return Promise.resolve();
      }
      var mime = root.supportedRecorderType(opts.MediaRecorder);
      setState({
        connection: "connected",
        tokenValid: true,
        phase: "idle",
        useFileUpload: !mime,
        fileUploadHint: mime ? "" : "当前浏览器不能录制可上传的音频格式，请选择音频文件。",
        micEnabled: false
      });
      return loadHistory();
    }

    function destroy() {
      destroyed = true;
      stopTimers();
      unbindLifecycle();
      if (audio && audio.close) audio.close();
      else if (audio && audio.release) audio.release();
    }

    return {
      hooks: hooks,
      bind: function (v) { view = v; },
      start: start,
      destroy: destroy,
      submitAudio: submitAudio,
      pollJob: pollJob
    };
  }

  root.createVoiceController = createVoiceController;
})(window.V3Business = window.V3Business || {});
