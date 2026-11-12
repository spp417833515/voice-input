/* 四页显式业务启动。已有 __V3_HOOKS__ 夹具且未要求启动业务时保持离线。 */
(function (root) {
  "use strict";

  var SCRIPT_PAGE = (typeof document !== "undefined" && document.currentScript && document.currentScript.getAttribute("data-page")) || "";

  function shouldStartBusiness() {
    if (window.__V3_DISABLE_BUSINESS__ === true) return false;
    if (window.__V3_START_BUSINESS__ === false) return false;
    if (window.__V3_START_BUSINESS__ === true) return true;
    if (window.__V3_HOOKS__) return false;
    return true;
  }

  function mountView(page, hooks, initialState) {
    var app = document.getElementById("app");
    var V3ns = window.V3;
    if (!app || !V3ns) throw new Error("页面尚未准备好挂载");
    var opts = { hooks: hooks || {}, initialState: initialState || {} };
    if (page === "setup") {
      window.__v3Setup = V3ns.mountSetup(app, opts);
      return window.__v3Setup;
    }
    if (page === "status") {
      window.__v3Status = V3ns.mountStatusOverlay(app, opts);
      return window.__v3Status;
    }
    if (page === "translate") {
      window.__v3Translate = V3ns.mountTranslateOverlay(app, opts);
      return window.__v3Translate;
    }
    if (page === "voice") {
      window.__v3Voice = V3ns.mountVoice(app, opts);
      return window.__v3Voice;
    }
    throw new Error("未知页面：" + page);
  }

  function loadOpenApi(deps, context, fetchImpl) {
    if (deps.openapi) return Promise.resolve(deps.openapi);
    if (!context.allowFetch) {
      return Promise.reject(new Error("当前页面不能加载接口说明。"));
    }
    function getJson(url) {
      return Promise.resolve(fetchImpl(url, {
        headers: { Accept: "application/json" },
        referrerPolicy: root.FETCH_REFERRER_POLICY || "no-referrer"
      })).then(function (res) {
        if (!res || !res.ok) throw new Error("加载失败 " + (res && res.status));
        return res.json();
      });
    }
    var origin = (deps.origin != null ? deps.origin : context.origin) || "";
    var snapshotUrl = deps.snapshotUrl || "/openapi.snapshot.json";
    if (context.kind === "local") {
      return getJson(origin + "/openapi.json").then(function (doc) { return doc; }, function () {
        return getJson(snapshotUrl);
      });
    }
    /* 局域网不得请求仅本机的 /openapi.json，改用随页面发布的冻结快照。 */
    if (deps.openapiSnapshot) return Promise.resolve(deps.openapiSnapshot);
    return getJson(snapshotUrl);
  }

  function createClient(deps, context, catalog) {
    return root.createHttp({
      catalog: catalog,
      context: context,
      fetch: deps.fetch,
      origin: deps.origin != null ? deps.origin : context.origin
    });
  }

  function startPage(page, deps) {
    deps = deps || window.__V3_BUSINESS_DEPS__ || {};
    /* 任何 fetch 之前先从地址栏取走令牌。失败则令牌作废，不得带着 URL 里的密钥继续请求。 */
    if (!deps.context && root.adoptTokenFromLocation) {
      root.adoptTokenFromLocation({
        location: deps.location || (typeof location !== "undefined" ? location : null),
        history: deps.history || (typeof history !== "undefined" ? history : null)
      });
    }
    var contextOpts = {
      location: deps.location || (typeof location !== "undefined" ? location : {}),
      allowHttp: !!deps.allowHttp,
      origin: deps.origin
    };
    if (Object.prototype.hasOwnProperty.call(deps, "token")) contextOpts.token = deps.token;
    var context = deps.context || root.createContext(contextOpts);
    var fetchImpl = deps.fetch || (typeof fetch === "function" ? fetch.bind(window) : null);
    /* 译文页“已呈现”宿主通知：在任何请求之前创建，接口说明/配置早期失败也能通知。 */
    var presented = page === "translate" && root.createPresentedNotifier
      ? (deps.presented || root.createPresentedNotifier({ window: deps.window || (typeof window !== "undefined" ? window : null) }))
      : null;
    return loadOpenApi(deps, context, fetchImpl).then(function (doc) {
      var catalog = root.createCatalog(doc);
      var client = createClient(deps, context, catalog);
      var controller;
      var extraState = { connection: "loading" };
      if (page === "setup") {
        controller = root.createSetupController({
          client: client,
          context: context,
          catalog: catalog,
          clipboard: root.createClipboard(deps),
          pageOrigin: context.protocol === "https:" || context.kind === "local" ? context.origin : "",
          pollInterval: deps.pollInterval,
          setTimeout: deps.setTimeout,
          clearTimeout: deps.clearTimeout
        });
      } else if (page === "status") {
        controller = root.createStatusController({
          client: client,
          context: context,
          catalog: catalog,
          sse: root.createSse({ EventSource: deps.EventSource }),
          jobIds: root.createJobIds(),
          origin: deps.origin != null ? deps.origin : context.origin
        });
      } else if (page === "translate") {
        controller = root.createTranslateController({
          client: client,
          context: context,
          clipboard: root.createClipboard(deps),
          targetLang: deps.targetLang,
          initialImage: deps.initialImage || window.__V3_TRANSLATE_IMAGE__,
          initialResponse: deps.initialResponse || window.__V3_TRANSLATE_RESPONSE__,
          presented: presented
        });
      } else if (page === "voice") {
        controller = root.createVoiceController({
          client: client,
          context: context,
          jobIds: root.createJobIds(),
          MediaRecorder: deps.MediaRecorder,
          mediaDevices: deps.mediaDevices,
          document: deps.document,
          window: deps.window
        });
      } else {
        throw new Error("未知页面：" + page);
      }
      var view = mountView(page, controller.hooks, extraState);
      controller.bind(view);
      if (controller.destroy) {
        var hideTarget = deps.document || (typeof document !== "undefined" ? document : null);
        if (hideTarget && hideTarget.addEventListener) {
          hideTarget.addEventListener("pagehide", function () { controller.destroy(); });
        }
      }
      window.__v3Business = {
        page: page,
        context: context,
        catalog: catalog,
        client: client,
        controller: controller,
        view: view
      };
      return controller.start();
    }, function (err) {
      var message = (err && err.message) || "无法加载接口说明";
      var hooks = {};
      var view = mountView(page, hooks, {
        connection: "error",
        connectionError: message,
        message: message,
        statusNotice: message
      });
      if (presented && page === "translate") {
        /* 可见错误已挂载进 DOM 后才通知宿主 */
        if (view && view.setState) view.setState({ phase: "error", error: "dependency_failed", message: message });
        presented("error");
      }
      window.__v3Business = { page: page, error: message, view: view };
    });
  }

  function boot(page) {
    page = page || SCRIPT_PAGE;
    if (!shouldStartBusiness()) {
      mountView(page, window.__V3_HOOKS__ || {}, (window.__V3_HOOKS__ && window.__V3_HOOKS__.initialState) || {});
      return Promise.resolve();
    }
    return startPage(page, window.__V3_BUSINESS_DEPS__ || {});
  }

  root.boot = boot;
  root.startPage = startPage;
  root.loadOpenApi = loadOpenApi;
  root.shouldStartBusiness = shouldStartBusiness;

  if (SCRIPT_PAGE && typeof document !== "undefined") {
    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", function () { boot(SCRIPT_PAGE); });
    } else {
      boot(SCRIPT_PAGE);
    }
  }
})(window.V3Business = window.V3Business || {});
