/* 本机桌面平台壳上报 idle/busy/error，进入同一产品事件流。识别/润色/投递状态不得由此冒充。局域网不得调用。 */
(function (root) {
  "use strict";

  var OP = "platform_event_api_v1_events_platform_post";
  var ALLOWED = { idle: true, busy: true, error: true };

  function formatErr(err) {
    var labels = (typeof V3 !== "undefined" && V3.labels) ? V3.labels : null;
    if (labels && labels.formatError) return labels.formatError(err.error, err.message, err.details);
    return (err && (err.message || err.error)) || "请求失败";
  }

  function createPlatformReporter(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;

    function post(body) {
      body = body || {};
      if (!ALLOWED[body.event]) {
        return Promise.reject({
          error: "invalid_request",
          message: "平台事件只允许 idle / busy / error，不能上报 " + (body.event || "空事件")
        });
      }
      if (!context || !context.canCallLocal()) {
        return Promise.reject({
          error: "forbidden",
          message: !context
            ? "缺少访问上下文"
            : (context.kind === "file"
              ? "file:// 不能作为成功路径。"
              : "平台事件仅本机可上报，已阻止从当前环境发出请求。"),
          blocked: true
        });
      }
      var payload;
      if (body.event === "error") {
        payload = {
          event: "error",
          error: body.error || "internal",
          message: body.message || formatErr(body),
          details: body.details != null ? body.details : null
        };
      } else {
        payload = { event: body.event };
      }
      return client.call(OP, { body: payload });
    }

    return {
      post: post,
      postIdle: function () { return post({ event: "idle" }); },
      postBusy: function () { return post({ event: "busy" }); },
      postError: function (err) {
        err = err || {};
        return post({
          event: "error",
          error: err.error,
          message: err.message,
          details: err.details
        });
      }
    };
  }

  root.PLATFORM_OPERATION = OP;
  root.PLATFORM_EVENTS = ["idle", "busy", "error"];
  root.createPlatformReporter = createPlatformReporter;
})(window.V3Business = window.V3Business || {});
