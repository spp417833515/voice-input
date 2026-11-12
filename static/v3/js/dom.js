/* 纯 UI DOM 辅助。不含业务规则，不发起网络请求。 */
(function (V3) {
  "use strict";

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (ch) {
      return ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[ch];
    });
  }

  function text(value) {
    return value == null ? "" : String(value);
  }

  function el(tag, attrs, children) {
    const node = document.createElement(tag);
    if (attrs) {
      Object.keys(attrs).forEach(function (key) {
        const val = attrs[key];
        if (val == null || val === false) return;
        if (key === "className") node.className = val;
        else if (key === "dataset") {
          Object.keys(val).forEach(function (dk) {
            if (val[dk] != null) node.dataset[dk] = String(val[dk]);
          });
        } else if (key === "text") node.textContent = val;
        else if (key === "html") node.innerHTML = val;
        else if (key.slice(0, 2) === "on" && typeof val === "function") {
          node.addEventListener(key.slice(2).toLowerCase(), val);
        } else if (key === "checked" || key === "disabled" || key === "hidden" || key === "selected") {
          node[key] = !!val;
        } else if (val === true) node.setAttribute(key, "");
        else node.setAttribute(key, String(val));
      });
    }
    (children || []).forEach(function (child) {
      if (child == null || child === false) return;
      node.appendChild(typeof child === "string" ? document.createTextNode(child) : child);
    });
    return node;
  }

  function empty(node) {
    while (node && node.firstChild) node.removeChild(node.firstChild);
    return node;
  }

  function icon(name, className) {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("class", className || "ic");
    svg.setAttribute("aria-hidden", "true");
    svg.setAttribute("focusable", "false");
    const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
    use.setAttribute("href", "#i-" + name);
    svg.appendChild(use);
    return svg;
  }

  function bindDrag(target) {
    let dragging = false;
    let startX = 0;
    let startY = 0;
    let origLeft = 0;
    let origTop = 0;

    function onMove(ev) {
      if (!dragging) return;
      const point = ev.touches ? ev.touches[0] : ev;
      const dx = point.clientX - startX;
      const dy = point.clientY - startY;
      target.style.left = origLeft + dx + "px";
      target.style.top = origTop + dy + "px";
      target.style.bottom = "auto";
      ev.preventDefault();
    }
    function onUp() {
      dragging = false;
      document.removeEventListener("mousemove", onMove);
      document.removeEventListener("mouseup", onUp);
      document.removeEventListener("touchmove", onMove);
      document.removeEventListener("touchend", onUp);
    }
    function onDown(ev) {
      if (ev.target.closest && ev.target.closest("button, a, input, textarea, select")) return;
      const point = ev.touches ? ev.touches[0] : ev;
      const rect = target.getBoundingClientRect();
      dragging = true;
      startX = point.clientX;
      startY = point.clientY;
      origLeft = rect.left;
      origTop = rect.top;
      target.style.position = "fixed";
      target.style.left = origLeft + "px";
      target.style.top = origTop + "px";
      target.style.bottom = "auto";
      document.addEventListener("mousemove", onMove);
      document.addEventListener("mouseup", onUp);
      document.addEventListener("touchmove", onMove, { passive: false });
      document.addEventListener("touchend", onUp);
    }
    target.addEventListener("mousedown", onDown);
    target.addEventListener("touchstart", onDown, { passive: true });
    return function unbind() {
      target.removeEventListener("mousedown", onDown);
      target.removeEventListener("touchstart", onDown);
      onUp();
    };
  }

  V3.dom = {
    escapeHtml: escapeHtml,
    text: text,
    el: el,
    empty: empty,
    icon: icon,
    bindDrag: bindDrag
  };
})(window.V3 = window.V3 || {});
