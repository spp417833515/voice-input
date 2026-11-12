/* 状态栏 + 实时字幕胶囊。只呈现注入的产品事件字段；字幕只用 stable_text / tail_text。 */
(function (V3) {
  "use strict";

  function call(hooks, name, payload) {
    const fn = hooks && hooks[name];
    if (typeof fn === "function") fn(payload);
  }

  function styleOf(event) {
    if (event === "recording" || event === "partial") return "rec";
    if (event === "polishing" || event === "busy") return "busy";
    if (event === "typed") return "done";
    if (event === "delivery_unknown" || event === "history_write_failed") return "warn";
    if (event === "error") return "err";
    return "";
  }

  function defaultLabel(item, state) {
    if (item.label) return item.label;
    const event = item.event || "idle";
    if (event === "recording" && item.elapsed_label) return item.elapsed_label;
    if (event === "idle" && state.connection === "disconnected") return "未连接";
    if (event === "error") return V3.labels.errorCode(item.error) || "出错";
    return V3.labels.productEvent(event) || "未连接";
  }

  function textNode(item) {
    const event = item.event;
    const wrap = V3.dom.el("span");
    if (event === "partial") {
      if (item.stable_text) wrap.appendChild(document.createTextNode(item.stable_text));
      if (item.tail_text) {
        wrap.appendChild(V3.dom.el("span", { className: "tail", text: item.tail_text }));
      }
      return wrap.childNodes.length ? wrap : null;
    }
    if (event === "polishing" && item.text) {
      wrap.appendChild(V3.dom.el("span", { className: "dim", text: item.text }));
      return wrap;
    }
    if (event === "typed" && item.text) {
      wrap.appendChild(document.createTextNode(item.text));
      return wrap;
    }
    if (event === "busy" && item.message) {
      wrap.appendChild(document.createTextNode(item.message));
      return wrap;
    }
    if (event === "error" || event === "history_write_failed") {
      const msg = V3.labels.formatError(item.error, item.message, item.details);
      wrap.appendChild(document.createTextNode(msg));
      if (item.hotkey_repeat) {
        wrap.appendChild(document.createTextNode(" · 按 "));
        wrap.appendChild(V3.dom.el("kbd", { text: item.hotkey_repeat }));
        wrap.appendChild(document.createTextNode(" 发送原文或重试"));
      }
      return wrap;
    }
    if (event === "delivery_unknown") {
      if (item.text) wrap.appendChild(document.createTextNode(item.text));
      return wrap.childNodes.length ? wrap : null;
    }
    return null;
  }

  function mountStatusOverlay(root, options) {
    const opts = options || {};
    const hooks = opts.hooks || {};
    let state = V3.defaults.merge(V3.defaults.statusState(), opts.initialState);
    const confirm = V3.mountConfirm(document.body);
    let unbindDrag = null;

    V3.ensureIcons();
    V3.dom.empty(root);
    const stack = V3.dom.el("div", {
      className: "hud-stack",
      id: "v3-hud-stack",
      role: "status",
      "aria-live": "polite"
    });
    root.appendChild(stack);

    function openUnknownConfirm(item) {
      confirm.open({
        title: "投递结果未知",
        warning: "此次文字可能已经输入。如果再次发送，可能会造成重复输入。确认框只记录你的选择，不会自行排队发送。",
        message: item.job_id ? ("任务 " + item.job_id) : "",
        text: item.text || "",
        confirmLabel: "仍要发送",
        cancelLabel: "取消",
        onConfirm: function () {
          call(hooks, "onConfirmDeliveryChoice", {
            confirm: true,
            job_id: item.job_id,
            text: item.text || "",
            event: "delivery_unknown"
          });
        },
        onCancel: function () {
          call(hooks, "onConfirmDeliveryChoice", {
            confirm: false,
            job_id: item.job_id,
            text: item.text || "",
            event: "delivery_unknown"
          });
        }
      });
    }

    function renderEvent(item) {
      const event = item.event || "idle";
      const cls = styleOf(event);
      const label = defaultLabel(item, state);
      const body = textNode(item);
      const labelEl = V3.dom.el("span", { className: "hud-label", text: label });
      const parts = [
        V3.dom.el("span", { className: "hud-dot", "aria-hidden": "true" }),
        labelEl
      ];
      if (body) {
        parts.push(V3.dom.el("span", { className: "sep", "aria-hidden": "true" }));
        const textWrap = V3.dom.el("span", { className: "hud-text" });
        textWrap.appendChild(body);
        if (event === "delivery_unknown") {
          textWrap.appendChild(
            V3.dom.el("div", {
              className: "hud-unknown",
              text: "投递结果未知，文字可能已经输入，请勿当成已经成功。"
            })
          );
          const actions = V3.dom.el("div", { className: "hud-actions" }, [
            V3.dom.el("button", {
              type: "button",
              className: "hud-btn",
              text: "确认处理",
              onClick: function (ev) {
                ev.stopPropagation();
                openUnknownConfirm(item);
              }
            })
          ]);
          textWrap.appendChild(actions);
        }
        parts.push(textWrap);
      } else if (event === "delivery_unknown") {
        parts.push(V3.dom.el("span", { className: "sep" }));
        const textWrap = V3.dom.el("span", { className: "hud-text" }, [
          V3.dom.el("div", {
            className: "hud-unknown",
            text: "投递结果未知，文字可能已经输入，请勿当成已经成功。"
          }),
          V3.dom.el("div", { className: "hud-actions" }, [
            V3.dom.el("button", {
              type: "button",
              className: "hud-btn",
              text: "确认处理",
              onClick: function (ev) {
                ev.stopPropagation();
                openUnknownConfirm(item);
              }
            })
          ])
        ]);
        parts.push(textWrap);
      }
      const hud = V3.dom.el("div", {
        className: "hud " + cls,
        tabindex: "0",
        "aria-label": label
      }, parts);
      return hud;
    }

    function render() {
      if (unbindDrag) {
        unbindDrag();
        unbindDrag = null;
      }
      V3.dom.empty(stack);
      const events = state.events && state.events.length
        ? state.events
        : [{ event: state.connection === "disconnected" ? null : "idle" }];
      events.forEach(function (item) {
        const ev = Object.assign({}, item);
        if (!ev.hotkey_repeat && state.hotkey_repeat) ev.hotkey_repeat = state.hotkey_repeat;
        stack.appendChild(renderEvent(ev));
      });
      unbindDrag = V3.dom.bindDrag(stack);
    }

    document.addEventListener("keydown", onKey);
    function onKey(ev) {
      if (ev.key === "Escape") call(hooks, "onClose");
    }

    render();

    return {
      setState: function (patch) {
        state = V3.defaults.merge(state, patch);
        render();
      },
      getState: function () { return state; },
      confirm: confirm,
      destroy: function () {
        document.removeEventListener("keydown", onKey);
        if (unbindDrag) unbindDrag();
        confirm.destroy();
        V3.dom.empty(root);
      }
    };
  }

  V3.mountStatusOverlay = mountStatusOverlay;
})(window.V3 = window.V3 || {});
