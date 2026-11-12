/* 页面访问上下文：file / 本机 / 局域网。令牌只留在内存，读入后立即从地址栏剥离，不写入 Web Storage。 */
(function (root) {
  "use strict";

  /* 脚本若被加载两次，必须复用同一份内存，避免二次执行丢掉已剥离的令牌。不把令牌写进日志或 DOM。 */
  var adopted = root.__tokenAdoption;
  if (!adopted) {
    adopted = { token: null, done: false, stripped: false, failed: null };
    root.__tokenAdoption = adopted;
  }

  function isLoopbackHost(host) {
    if (!host) return false;
    var h = String(host).toLowerCase();
    if (h.charAt(0) === "[" && h.charAt(h.length - 1) === "]") h = h.slice(1, -1);
    return h === "127.0.0.1" || h === "localhost" || h === "::1" || h === "0:0:0:0:0:0:0:1";
  }

  function readTokenFromSearch(search) {
    if (!search) return null;
    var raw = String(search);
    if (raw.charAt(0) === "?") raw = raw.slice(1);
    var params = new URLSearchParams(raw);
    var token = params.get("token");
    if (!token) return null;
    token = String(token).trim();
    return token ? token : null;
  }

  function stripTokenFromSearch(search) {
    var raw = String(search || "");
    if (raw.charAt(0) === "?") raw = raw.slice(1);
    var params = new URLSearchParams(raw);
    params.delete("token");
    var s = params.toString();
    return s ? "?" + s : "";
  }

  function adoptionSnapshot() {
    return {
      token: adopted.token,
      done: adopted.done,
      stripped: adopted.stripped,
      failed: adopted.failed
    };
  }

  function adoptTokenFromLocation(options) {
    var opts = options || {};
    if (adopted.done) return adoptionSnapshot();

    var loc = opts.location || (typeof location !== "undefined" ? location : null);
    var hist = opts.history || (typeof history !== "undefined" ? history : null);
    var search = opts.search != null ? opts.search : (loc && loc.search) || "";
    var hash = opts.hash != null ? opts.hash : (loc && loc.hash) || "";
    var token = readTokenFromSearch(search);

    if (!token) {
      adopted.done = true;
      adopted.stripped = true;
      adopted.failed = null;
      adopted.token = null;
      return adoptionSnapshot();
    }

    if (!hist || typeof hist.replaceState !== "function") {
      adopted.done = true;
      adopted.stripped = false;
      adopted.token = null;
      adopted.failed = "无法从地址栏移除访问令牌，已停止使用该令牌。请重新打开录音页。";
      return adoptionSnapshot();
    }

    var path = (loc && loc.pathname) || "";
    var cleaned = stripTokenFromSearch(search);
    var newUrl = path + cleaned + (hash || "");
    try {
      hist.replaceState(hist.state, "", newUrl);
    } catch (err) {
      adopted.done = true;
      adopted.stripped = false;
      adopted.token = null;
      adopted.failed = "无法从地址栏移除访问令牌，已停止使用该令牌。请重新打开录音页。";
      return adoptionSnapshot();
    }

    adopted.token = token;
    adopted.done = true;
    adopted.stripped = true;
    adopted.failed = null;
    return adoptionSnapshot();
  }

  function createContext(options) {
    var opts = options || {};
    var loc = opts.location || (typeof location !== "undefined" ? location : {});
    var protocol = opts.protocol != null ? opts.protocol : (loc.protocol || "");
    var hostname = opts.hostname != null ? opts.hostname : (loc.hostname || "");
    var origin = opts.origin != null ? opts.origin : (loc.origin || "");
    var href = opts.href != null ? opts.href : (loc.href || "");
    var search = opts.search != null ? opts.search : (loc.search || "");

    var token;
    var tokenAdoptError = null;
    if (Object.prototype.hasOwnProperty.call(opts, "token")) {
      token = opts.token;
    } else if (adopted.done) {
      tokenAdoptError = adopted.failed || null;
      token = adopted.failed ? null : adopted.token;
    } else {
      token = readTokenFromSearch(search);
    }

    var kind;
    var allowFetch = false;
    if (protocol === "file:" || (href && href.indexOf("file:") === 0)) {
      kind = "file";
    } else if (isLoopbackHost(hostname)) {
      kind = "local";
      allowFetch = protocol === "https:" || protocol === "http:";
    } else if (protocol === "https:" || protocol === "http:") {
      kind = "lan";
      allowFetch = protocol === "https:" || (opts.allowHttp === true);
    } else {
      kind = "unknown";
    }

    if (kind === "lan" && protocol !== "https:" && opts.allowHttp !== true) {
      allowFetch = false;
    }

    var tokenValue = token || null;
    return {
      kind: kind,
      protocol: protocol,
      hostname: hostname,
      origin: origin || "",
      href: href,
      allowFetch: allowFetch,
      isLoopback: kind === "local",
      tokenPresent: !!tokenValue,
      tokenAdoptError: tokenAdoptError,
      getToken: function () { return tokenValue; },
      canCallLocal: function () { return kind === "local" && allowFetch; },
      canCallLanToken: function () { return (kind === "local" || kind === "lan") && allowFetch; }
    };
  }

  root.isLoopbackHost = isLoopbackHost;
  root.readTokenFromSearch = readTokenFromSearch;
  root.stripTokenFromSearch = stripTokenFromSearch;
  root.adoptTokenFromLocation = adoptTokenFromLocation;
  root.createContext = createContext;
})(window.V3Business = window.V3Business || {});
