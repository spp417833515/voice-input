/* 同源 HTTP 客户端：按 catalog 的 path/method/access 发请求，不另写接口枚举。 */
(function (root) {
  "use strict";

  function ApiError(fields) {
    fields = fields || {};
    this.name = "ApiError";
    this.ok = false;
    this.error = fields.error || "internal";
    this.message = fields.message || "请求失败";
    this.details = fields.details != null ? fields.details : null;
    this.status = fields.status;
    this.requires_confirmation = !!fields.requires_confirmation;
    this.original_job_id = fields.original_job_id || null;
    this.new_job_id = fields.new_job_id || null;
    this.blocked = !!fields.blocked;
    this.network = !!fields.network;
  }
  ApiError.prototype = Object.create(Error.prototype);
  ApiError.prototype.constructor = ApiError;

  function unwrapBody(body) {
    if (!body || typeof body !== "object") return body;
    if (body.detail && typeof body.detail === "object" && !Array.isArray(body.detail)) {
      return body.detail;
    }
    return body;
  }

  function encodeQuery(query) {
    if (!query) return "";
    var params = new URLSearchParams();
    Object.keys(query).forEach(function (key) {
      var value = query[key];
      if (value == null || value === "") return;
      if (key === "token") return;
      params.set(key, String(value));
    });
    var s = params.toString();
    return s ? "?" + s : "";
  }

  function createHttp(options) {
    var opts = options || {};
    var catalog = opts.catalog;
    var context = opts.context;
    var fetchImpl = opts.fetch || (typeof fetch === "function" ? fetch.bind(typeof window !== "undefined" ? window : globalThis) : null);
    var origin = opts.origin != null ? opts.origin : (context && context.origin) || "";

    function call(operationId, args) {
      args = args || {};
      if (!context || context.kind === "file") {
        return Promise.reject(new ApiError({
          error: "forbidden",
          message: "file:// 不能作为成功路径。请通过同源 HTTPS 打开页面。",
          blocked: true
        }));
      }
      if (!context.allowFetch) {
        return Promise.reject(new ApiError({
          error: "forbidden",
          message: "当前页面不是可用的同源地址，已阻止发出请求。",
          blocked: true
        }));
      }
      var op = catalog.operation(operationId);
      if (op.access === "local" && context.kind !== "local") {
        return Promise.reject(new ApiError({
          error: "forbidden",
          message: "该接口仅本机可访问，已阻止从当前环境发出请求。",
          blocked: true,
          details: { operationId: operationId, path: op.path }
        }));
      }
      if (op.access === "lan_token" && !context.canCallLanToken()) {
        return Promise.reject(new ApiError({
          error: "forbidden",
          message: "当前环境不能调用需令牌的接口。",
          blocked: true,
          details: { operationId: operationId }
        }));
      }
      if (!fetchImpl) {
        return Promise.reject(new ApiError({ error: "internal", message: "当前环境没有 fetch。" }));
      }
      var path = catalog.expand(operationId, args.pathParams);
      var url = String(origin || "") + path + encodeQuery(args.query);
      var headers = Object.assign({}, args.headers || {});
      if (op.access === "lan_token" && context.kind === "lan") {
        var token = context.getToken && context.getToken();
        if (token) {
          headers["X-Voice-Token"] = token;
        }
      }
      var init = { method: op.method, headers: headers, referrerPolicy: root.FETCH_REFERRER_POLICY };
      if (args.body instanceof FormData) {
        init.body = args.body;
      } else if (args.body != null && typeof args.body === "object") {
        headers["Content-Type"] = "application/json";
        init.body = JSON.stringify(args.body);
      } else if (args.body != null) {
        init.body = args.body;
      }
      return Promise.resolve(fetchImpl(url, init)).then(function (res) {
        return Promise.resolve(res.text()).then(function (text) {
          var parsed;
          var hasJson = false;
          if (text) {
            try {
              parsed = JSON.parse(text);
              hasJson = true;
            } catch (err) {
              parsed = null;
            }
          }
          var payload = hasJson ? unwrapBody(parsed) : null;
          if (payload && payload.requires_confirmation === true) {
            return {
              ok: false,
              requires_confirmation: true,
              original_job_id: payload.original_job_id,
              new_job_id: payload.new_job_id,
              status: res.status,
              body: payload
            };
          }
          if (!res.ok) {
            throw new ApiError({
              error: (payload && payload.error) || (res.status === 403 ? "forbidden" : res.status === 401 ? "unauthorized" : "internal"),
              message: (payload && payload.message) || ("HTTP " + res.status),
              details: payload && payload.details,
              status: res.status
            });
          }
          if (hasJson) return payload;
          return {};
        });
      }, function (err) {
        throw new ApiError({
          error: "timeout",
          message: (err && err.message) ? ("网络请求失败：" + err.message) : "网络请求失败",
          network: true
        });
      });
    }

    return { call: call, origin: origin, ApiError: ApiError };
  }

  root.FETCH_REFERRER_POLICY = "no-referrer";
  root.ApiError = ApiError;
  root.createHttp = createHttp;
})(window.V3Business = window.V3Business || {});
