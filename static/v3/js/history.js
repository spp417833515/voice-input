/* 历史记录展示：处理记录，不把记录写成已输入。复制与再次发送只走回调。 */
(function (V3) {
  "use strict";

  function recordText(record) {
    if (!record) return "";
    if (record.final_text) return record.final_text;
    return record.raw_text || "";
  }

  function formatTime(value) {
    return value ? String(value) : "";
  }

  function renderRecord(record, opts) {
    const options = opts || {};
    const sourceLabel = V3.labels.source(record.source);
    const polishLabel = V3.labels.polish(record.polish);
    const main = recordText(record);
    const raw = record.raw_text || "";
    const showRaw = raw && raw !== main;
    const children = [
      V3.dom.el("div", { className: "rec-head" }, [
        formatTime(record.timestamp)
          ? V3.dom.el("span", { text: formatTime(record.timestamp) })
          : null,
        sourceLabel ? V3.dom.el("span", { className: "badge", text: sourceLabel }) : null,
        polishLabel
          ? V3.dom.el("span", {
              className: "badge " + V3.labels.polishBadgeClass(record.polish),
              text: polishLabel
            })
          : null,
        options.onCopy
          ? V3.dom.el("button", {
              type: "button",
              className: "btn",
              "aria-label": "复制这段处理结果",
              onClick: function (ev) {
                ev.preventDefault();
                ev.stopPropagation();
                options.onCopy(main, record);
              }
            }, [V3.dom.icon("copy"), document.createTextNode("复制")])
          : null
      ]),
      V3.dom.el("div", { className: "rec-text", text: main || "（无文本）" })
    ];
    if (showRaw) {
      children.push(
        V3.dom.el("details", {}, [
          V3.dom.el("summary", { text: "原文" }),
          V3.dom.el("p", { className: "muted", text: raw })
        ])
      );
    }
    return V3.dom.el("div", { className: "rec" }, children);
  }

  function renderList(container, history, opts) {
    const options = opts || {};
    const data = history || {};
    V3.dom.empty(container);
    if (data.loading && (!data.records || !data.records.length)) {
      container.appendChild(V3.dom.el("div", { className: "empty", text: "加载中…" }));
      return;
    }
    if (data.message) {
      container.appendChild(V3.dom.el("div", { className: "empty", text: data.message }));
    }
    const records = data.records || [];
    if (!records.length && !data.loading && !data.message) {
      container.appendChild(
        V3.dom.el("div", {
          className: "empty",
          text: options.emptyText || "还没有记录，按住快捷键说一句话试试"
        })
      );
      return;
    }
    records.forEach(function (record) {
      container.appendChild(renderRecord(record, options));
    });
    if (data.skipped_malformed > 0) {
      container.appendChild(
        V3.dom.el("div", {
          className: "empty",
          text: "另有 " + data.skipped_malformed + " 条格式损坏已跳过"
        })
      );
    }
  }

  function renderVoiceItems(container, history, opts) {
    const options = opts || {};
    const data = history || {};
    V3.dom.empty(container);
    if (data.loading && (!data.records || !data.records.length)) {
      container.appendChild(V3.dom.el("div", { className: "empty", text: "加载中…" }));
      return;
    }
    if (data.message) {
      container.appendChild(V3.dom.el("div", { className: "empty", text: data.message }));
    }
    const records = data.records || [];
    if (!records.length && !data.loading && !data.message) {
      container.appendChild(
        V3.dom.el("div", { className: "empty", text: options.emptyText || "还没有处理记录" })
      );
      return;
    }
    records.forEach(function (record) {
      const main = recordText(record);
      const sourceLabel = V3.labels.source(record.source);
      const polishLabel = V3.labels.polish(record.polish);
      const btn = V3.dom.el("button", {
        type: "button",
        className: "hist-item",
        disabled: options.disabled,
        onClick: function () {
          if (options.onSelect) options.onSelect(record, main);
        }
      }, [
        document.createTextNode(main || "（无文本）"),
        V3.dom.el("div", { className: "hist-meta" }, [
          sourceLabel ? V3.dom.el("span", { className: "badge", text: sourceLabel }) : null,
          polishLabel
            ? V3.dom.el("span", {
                className: "badge " + V3.labels.polishBadgeClass(record.polish),
                text: polishLabel
              })
            : null,
          record.timestamp ? V3.dom.el("span", { text: record.timestamp }) : null
        ])
      ]);
      container.appendChild(btn);
    });
  }

  V3.history = {
    recordText: recordText,
    renderList: renderList,
    renderVoiceItems: renderVoiceItems
  };
})(window.V3 = window.V3 || {});
