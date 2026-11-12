/* 产品事件 SSE：只呈现已冻结字段；断线后显式未连接，不自动发送。
 * EventSource 无法自设 referrerPolicy，依赖文档 Referrer-Policy=no-referrer，避免把地址栏查询带出去。
 */
(function (root) {
  "use strict";

  var PRODUCT_EVENTS = {
    idle: true,
    recording: true,
    partial: true,
    polishing: true,
    typed: true,
    error: true,
    busy: true,
    history_write_failed: true,
    delivery_unknown: true
  };

  function createSse(options) {
    var opts = options || {};
    var EventSourceImpl = opts.EventSource || (typeof EventSource !== "undefined" ? EventSource : null);
    var source = null;
    var closed = false;

    function close() {
      closed = true;
      if (source) {
        try { source.close(); } catch (err) {}
        source = null;
      }
    }

    function connect(url, handlers) {
      close();
      closed = false;
      handlers = handlers || {};
      if (!EventSourceImpl) {
        if (handlers.onError) handlers.onError(new Error("当前环境没有 EventSource"));
        return { close: close };
      }
      try {
        source = new EventSourceImpl(url);
      } catch (err) {
        if (handlers.onError) handlers.onError(err);
        return { close: close };
      }
      function handle(type, ev) {
        if (closed) return;
        var data = ev && ev.data;
        var payload = {};
        if (data) {
          try { payload = JSON.parse(data); } catch (err) { payload = { raw: data }; }
        }
        var eventName = type || (payload && payload.event) || "message";
        var item = Object.assign({}, payload);
        item.event = eventName;
        if (!PRODUCT_EVENTS[eventName] && eventName !== "message") {
          item.event = eventName;
        }
        if (handlers.onEvent) handlers.onEvent(item);
      }
      if (typeof source.addEventListener === "function") {
        Object.keys(PRODUCT_EVENTS).forEach(function (name) {
          source.addEventListener(name, function (ev) { handle(name, ev); });
        });
      }
      source.onmessage = function (ev) { handle((ev && ev.type) || "message", ev); };
      source.onopen = function () { if (!closed && handlers.onOpen) handlers.onOpen(); };
      source.onerror = function (ev) {
        if (closed) return;
        if (handlers.onDisconnect) handlers.onDisconnect(ev);
        close();
      };
      return { close: close };
    }

    return { connect: connect, close: close };
  }

  root.createSse = createSse;
  root.PRODUCT_EVENTS = PRODUCT_EVENTS;
})(window.V3Business = window.V3Business || {});
