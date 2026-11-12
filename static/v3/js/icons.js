/* 共享图标精灵，各页启动时注入一次。 */
(function (V3) {
  "use strict";

  const SPRITE = '' +
    '<svg width="0" height="0" style="position:absolute" aria-hidden="true" focusable="false">' +
    '<symbol id="i-mic" viewBox="0 0 24 24"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0M12 18v3"/></symbol>' +
    '<symbol id="i-copy" viewBox="0 0 24 24"><rect x="9" y="9" width="11" height="11" rx="2"/><path d="M5 15V5a2 2 0 0 1 2-2h8"/></symbol>' +
    '<symbol id="i-alert" viewBox="0 0 24 24"><path d="M12 3 2 21h20L12 3zM12 10v5M12 18h.01"/></symbol>' +
    '<symbol id="i-refresh" viewBox="0 0 24 24"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></symbol>' +
    '<symbol id="i-check" viewBox="0 0 24 24"><path d="M5 12l5 5 9-10"/></symbol>' +
    '<symbol id="i-upload" viewBox="0 0 24 24"><path d="M12 16V4M6 10l6-6 6 6M4 20h16"/></symbol>' +
    '</svg>';

  function ensureIcons() {
    if (document.getElementById("v3-icon-sprite")) return;
    const wrap = document.createElement("div");
    wrap.id = "v3-icon-sprite";
    wrap.innerHTML = SPRITE;
    document.body.insertBefore(wrap, document.body.firstChild);
  }

  V3.ensureIcons = ensureIcons;
})(window.V3 = window.V3 || {});
