/* 整块原位置译文浮层。失败只显示错误，不把 ocr_text 当作译文。复制只走回调。 */
(function (V3) {
  "use strict";

  function call(hooks, name, payload) {
    const fn = hooks && hooks[name];
    if (typeof fn === "function") fn(payload);
  }

  function mountTranslateOverlay(root, options) {
    const opts = options || {};
    const hooks = opts.hooks || {};
    let state = V3.defaults.merge(V3.defaults.translateState(), opts.initialState);

    V3.ensureIcons();
    V3.dom.empty(root);
    const holder = V3.dom.el("div", { className: "block-root", id: "v3-translate-root" });
    root.appendChild(holder);

    function copyText() {
      if (state.phase === "result" || state.phase === "ocronly") {
        call(hooks, "onCopy", {
          kind: "translate",
          text: state.text || "",
          target_lang: state.target_lang || ""
        });
      }
    }

    function render() {
      V3.dom.empty(holder);
      const phase = state.phase || "empty";
      if (phase === "empty" || phase === "idle") {
        const msg = state.connection === "disconnected"
          ? "未连接。等待截图翻译结果。"
          : "等待截图翻译。";
        holder.appendChild(
          V3.dom.el("div", { className: "cover" }, [
            V3.dom.el("div", { className: "loading-chip", text: msg })
          ])
        );
        return;
      }
      if (phase === "loading") {
        holder.appendChild(
          V3.dom.el("div", { className: "cover" }, [
            V3.dom.el("div", { className: "loading-chip", role: "status", "aria-live": "polite" }, [
              document.createTextNode("识别翻译中…"),
              V3.dom.el("div", { className: "indet", "aria-hidden": "true" })
            ])
          ])
        );
        return;
      }

      const footBits = [];
      if (phase === "result" || phase === "ocronly") {
        const copied = state.copyStatus === "copied";
        footBits.push(V3.dom.el("button", {
          type: "button",
          className: "hud-btn",
          disabled: !state.text,
          onClick: copyText
        }, [
          V3.dom.icon("copy"),
          document.createTextNode(copied ? "已复制" : "复制")
        ]));
      }
      footBits.push(V3.dom.el("span", { text: state.closeHint || "Esc / 点击外部关闭" }));

      if (phase === "error") {
        const msg = V3.labels.formatError(state.error, state.message || "翻译或识别失败");
        holder.appendChild(
          V3.dom.el("div", { className: "block", role: "alert" }, [
            V3.dom.el("div", { className: "block-text err" }, [
              V3.dom.icon("alert"),
              V3.dom.el("span", { text: msg })
            ]),
            V3.dom.el("div", { className: "block-foot" }, footBits)
          ])
        );
        return;
      }

      const title = phase === "ocronly" || state.target_lang === "raw"
        ? "识别文字（未翻译）"
        : null;
      /* 成功体只展示 text；ocr_text 不得充当译文。仅识别模式由 phase=ocronly 或 target_lang=raw 标明。 */
      holder.appendChild(
        V3.dom.el("div", { className: "block" }, [
          title ? V3.dom.el("div", { className: "block-title", text: title }) : null,
          V3.dom.el("div", { className: "block-text", text: state.text || "（无文本）" }),
          V3.dom.el("div", { className: "block-foot" }, footBits)
        ])
      );
    }

    function onKey(ev) {
      if (ev.key === "Escape") call(hooks, "onClose");
    }
    function onPointer(ev) {
      if (ev.target === root || ev.target === holder) call(hooks, "onClose");
    }
    document.addEventListener("keydown", onKey);
    root.addEventListener("mousedown", onPointer);

    render();

    return {
      setState: function (patch) {
        state = V3.defaults.merge(state, patch);
        render();
      },
      getState: function () { return state; },
      destroy: function () {
        document.removeEventListener("keydown", onKey);
        root.removeEventListener("mousedown", onPointer);
        V3.dom.empty(root);
      }
    };
  }

  V3.mountTranslateOverlay = mountTranslateOverlay;
})(window.V3 = window.V3 || {});
