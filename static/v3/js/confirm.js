/* 确认框：只把用户选择交给回调，不自行入队或重发。 */
(function (V3) {
  "use strict";

  function mountConfirm(root) {
    let current = null;
    let lastFocus = null;

    function close() {
      if (current && current.node && current.node.parentNode) {
        current.node.parentNode.removeChild(current.node);
      }
      const restore = lastFocus;
      current = null;
      lastFocus = null;
      if (restore && typeof restore.focus === "function") restore.focus();
    }

    function onKey(ev) {
      if (!current) return;
      if (ev.key === "Escape") {
        ev.preventDefault();
        const cancel = current.onCancel;
        close();
        if (cancel) cancel();
        return;
      }
      if (ev.key !== "Tab") return;
      const focusable = current.node.querySelectorAll("button");
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (ev.shiftKey && document.activeElement === first) {
        ev.preventDefault();
        last.focus();
      } else if (!ev.shiftKey && document.activeElement === last) {
        ev.preventDefault();
        first.focus();
      }
    }

    function open(opts) {
      close();
      lastFocus = document.activeElement;
      const ev = opts || {};
      const node = V3.dom.el("div", {
        className: "v3-confirm",
        role: "dialog",
        "aria-modal": "true",
        "aria-labelledby": "v3-confirm-title",
        "aria-describedby": "v3-confirm-desc"
      });
      const card = V3.dom.el("div", { className: "v3-confirm-card" }, [
        V3.dom.el("h2", { id: "v3-confirm-title", text: ev.title || "请确认" }),
        ev.warning
          ? V3.dom.el("div", { className: "v3-confirm-warn", id: "v3-confirm-desc", text: ev.warning })
          : null,
        ev.message
          ? V3.dom.el("p", { id: ev.warning ? "v3-confirm-extra" : "v3-confirm-desc", text: ev.message })
          : null,
        ev.text
          ? V3.dom.el("p", { className: "muted", text: ev.text })
          : null,
        V3.dom.el("div", { className: "actions" }, [
          V3.dom.el("button", {
            type: "button",
            className: "btn",
            text: ev.cancelLabel || "取消",
            onClick: function () {
              const cancel = ev.onCancel;
              close();
              if (cancel) cancel();
            }
          }),
          V3.dom.el("button", {
            type: "button",
            className: "btn primary",
            text: ev.confirmLabel || "确认",
            onClick: function () {
              const ok = ev.onConfirm;
              close();
              if (ok) ok();
            }
          })
        ])
      ]);
      node.appendChild(card);
      node.addEventListener("click", function (e) {
        if (e.target === node) {
          const cancel = ev.onCancel;
          close();
          if (cancel) cancel();
        }
      });
      root.appendChild(node);
      current = { node: node, onCancel: ev.onCancel };
      document.addEventListener("keydown", onKey);
      const primary = card.querySelector(".btn.primary") || card.querySelector("button");
      if (primary) primary.focus();
    }

    function destroy() {
      document.removeEventListener("keydown", onKey);
      close();
    }

    function isOpen() {
      return !!current;
    }

    return { open: open, close: close, destroy: destroy, isOpen: isOpen };
  }

  V3.mountConfirm = mountConfirm;
})(window.V3 = window.V3 || {});
