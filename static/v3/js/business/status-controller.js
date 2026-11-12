/* 状态浮层：接入产品 SSE；断线显示未连接，不自动发送。确认投递未知时调用 resend。 */
(function (root) {
  "use strict";

  var OP = {
    events: "product_events_api_v1_events_get",
    configGet: "get_config_api_v1_config_get",
    resend: "resend_api_v1_utterances_resend_post"
  };

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) return labels.formatError(err.error, err.message, err.details);
    return (err && (err.message || err.error)) || "请求失败";
  }

  function createStatusController(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var catalog = opts.catalog;
    var sse = opts.sse || root.createSse(opts);
    var jobIds = opts.jobIds || root.createJobIds();
    var view = null;
    var connectionHandle = null;
    var events = [];
    var claimed = [];
    var claimLoop = null;

    function setState(patch) {
      if (view && view.setState) view.setState(patch);
    }

    function pushEvent(item) {
      events = [item].concat(events).slice(0, 8);
      setState({ events: events, connection: "connected" });
    }

    function disconnect(message) {
      if (connectionHandle) {
        connectionHandle.close();
        connectionHandle = null;
      }
      setState({
        connection: "disconnected",
        events: events.length ? events : [],
        statusNotice: message || "未连接"
      });
    }

    function connect() {
      if (!context || !context.canCallLocal()) {
        var msg = context && context.kind === "file"
          ? "file:// 不能作为成功路径。请通过同源 HTTPS 在本机打开状态浮层。"
          : "产品事件接口仅本机可访问。";
        setState({ connection: "disconnected", events: [], statusNotice: msg });
        return Promise.resolve();
      }
      if (connectionHandle) {
        connectionHandle.close();
        connectionHandle = null;
      }
      setState({ connection: "loading" });
      var origin = (opts.origin != null ? opts.origin : context.origin) || "";
      /* 状态栏只订一次产品事件流。会话会把 recording/partial/error/idle 镜像过来，不得再订 session events，否则字幕重复。 */
      var url = origin + catalog.expand(OP.events);
      return client.call(OP.configGet).then(function (config) {
        if (config && config.hotkey_repeat) setState({ hotkey_repeat: config.hotkey_repeat });
      }, function () { /* 快捷键读取失败不阻止事件流 */ }).then(function () {
        if (connectionHandle) {
          connectionHandle.close();
          connectionHandle = null;
        }
        connectionHandle = sse.connect(url, {
          onOpen: function () { setState({ connection: "connected" }); },
          onEvent: function (item) { pushEvent(item); },
          onDisconnect: function () { disconnect("事件连接已断开"); },
          onError: function (err) { disconnect((err && err.message) || "无法连接事件流"); }
        });
        startClaim();
      });
    }

    function startClaim() {
      if (opts.claim === false || !root.createClaimLoop) return;
      if (claimLoop) return;
      claimLoop = root.createClaimLoop({
        client: client,
        context: context,
        timeoutS: root.CLAIM_TIMEOUT_S,
        errorDelay: opts.claimErrorDelay != null ? opts.claimErrorDelay : 1000,
        setTimeout: opts.setTimeout,
        clearTimeout: opts.clearTimeout,
        onClaimed: function (job) {
          claimed = [job].concat(claimed).slice(0, 8);
          setState({ lastClaimed: job });
          if (opts.onClaimed) return opts.onClaimed(job);
        },
        onError: function (err) {
          if (err && err.blocked) setState({ claimNotice: formatErr(err) });
        }
      });
      claimLoop.start();
    }

    var hooks = {
      onClose: function () {
        if (connectionHandle) connectionHandle.close();
        if (opts.onClose) opts.onClose();
      },
      onConfirmDeliveryChoice: function (payload) {
        payload = payload || {};
        if (!payload.confirm) return;
        if (!context || !context.canCallLocal()) {
          setState({ connection: "disconnected" });
          return;
        }
        var original = payload.job_id;
        if (!original || !payload.text) return;
        var slot = "status-confirm:" + original;
        var newId = jobIds.fresh(slot);
        var req = {
          job_id: newId,
          text: payload.text,
          original_job_id: original,
          confirm: true
        };
        if (payload.focus_id) req.focus_id = payload.focus_id;
        return client.call(OP.resend, {
          body: req
        }).then(function (body) {
          if (body && body.requires_confirmation) {
            pushEvent({
              event: "delivery_unknown",
              job_id: body.original_job_id || original,
              text: payload.text,
              message: "仍需确认，文字可能已经输入。"
            });
            return;
          }
          pushEvent({
            event: "busy",
            job_id: body && body.job_id,
            text: body && body.text
          });
        }, function (err) {
          if (err && err.network) {
            /* 保留 newId，不自动换 id 重试 */
          }
          pushEvent({
            event: "error",
            error: err.error,
            message: formatErr(err),
            job_id: original
          });
        });
      }
    };

    function stopAll() {
      disconnect();
      if (claimLoop) {
        claimLoop.destroy();
        claimLoop = null;
      }
    }

    return {
      hooks: hooks,
      bind: function (v) { view = v; },
      start: function () { return connect(); },
      stop: stopAll,
      destroy: stopAll,
      pushEvent: pushEvent,
      claimedJobs: function () { return claimed.slice(); }
    };
  }

  root.createStatusController = createStatusController;
})(window.V3Business = window.V3Business || {});
