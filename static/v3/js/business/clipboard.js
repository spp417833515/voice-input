/* 仅在用户点击回调里 writeText，不读剪贴板。 */
(function (root) {
  "use strict";

  function createClipboard(deps) {
    var impl = (deps && deps.clipboard) || null;
    return {
      writeText: function (text) {
        var clip = impl;
        if (!clip && typeof navigator !== "undefined") clip = navigator.clipboard;
        if (!clip || typeof clip.writeText !== "function") {
          return Promise.reject(new Error("当前环境不能写入剪贴板"));
        }
        return Promise.resolve(clip.writeText(text == null ? "" : String(text)));
      }
    };
  }

  root.createClipboard = createClipboard;
})(window.V3Business = window.V3Business || {});
