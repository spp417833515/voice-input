/* 从运行时 OpenAPI 文档派生 operationId / 路径 / 方法 / 权限，不维护第二份接口表。 */
(function (root) {
  "use strict";

  function accessOf(operation) {
    var security = operation && operation.security;
    if (Array.isArray(security)) {
      for (var i = 0; i < security.length; i++) {
        var item = security[i];
        if (item && Object.prototype.hasOwnProperty.call(item, "VoiceToken")) {
          return "lan_token";
        }
      }
    }
    return "local";
  }

  function createCatalog(doc) {
    if (!doc || typeof doc !== "object" || !doc.paths) {
      throw new Error("缺少可用的 OpenAPI 文档");
    }
    var byId = Object.create(null);
    var paths = doc.paths;
    Object.keys(paths).forEach(function (path) {
      var item = paths[path] || {};
      Object.keys(item).forEach(function (method) {
        if (method === "parameters" || method.indexOf("x-") === 0) return;
        var op = item[method];
        if (!op || !op.operationId) return;
        byId[op.operationId] = {
          operationId: op.operationId,
          method: String(method).toUpperCase(),
          path: path,
          access: accessOf(op)
        };
      });
    });
    return {
      document: doc,
      size: Object.keys(byId).length,
      operation: function (operationId) {
        var op = byId[operationId];
        if (!op) throw new Error("OpenAPI 中没有 operationId：" + operationId);
        return op;
      },
      has: function (operationId) { return !!byId[operationId]; },
      all: function () {
        return Object.keys(byId).map(function (k) { return byId[k]; });
      },
      expand: function (operationId, pathParams) {
        var op = this.operation(operationId);
        var path = op.path;
        var params = pathParams || {};
        path = path.replace(/\{([^}]+)\}/g, function (_, name) {
          if (params[name] == null || params[name] === "") {
            throw new Error("缺少路径参数 " + name + "（" + operationId + "）");
          }
          return encodeURIComponent(String(params[name]));
        });
        return path;
      }
    };
  }

  root.createCatalog = createCatalog;
  root.accessOf = accessOf;
})(window.V3Business = window.V3Business || {});
