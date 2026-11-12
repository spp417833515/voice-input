/* 设置页纯 UI：八个左侧菜单。按钮只调用注入回调，不 fetch、不写剪贴板。 */
(function (V3) {
  "use strict";

  const TABS = V3.labels.SETUP_MENUS;

  function call(hooks, name, payload) {
    const fn = hooks && hooks[name];
    if (typeof fn === "function") fn(payload);
  }

  function modelReady(model) {
    return !!(model && model.status === "ready" && model.sidecarRunning === true);
  }

  function ollamaReady(ollama) {
    return ollama && ollama.modelStatus === "ready" && ollama.running === true;
  }

  function isConnected(state) {
    return state.connection === "connected";
  }

  function currentHotkey(state, name) {
    const drafts = state.hotkeyDrafts;
    if (drafts && Object.prototype.hasOwnProperty.call(drafts, name) && drafts[name] != null) {
      return drafts[name];
    }
    return state[name] || "";
  }

  function desktopDaemonDetail(state) {
    const list = state.capabilities;
    if (!Array.isArray(list)) return "尚未读取守护进程状态";
    for (let i = 0; i < list.length; i++) {
      const item = list[i];
      if (item && item.id === "desktop_daemon") {
        const detail = item.detail != null && String(item.detail) !== "" ? item.detail : item.message;
        return detail ? String(detail) : "尚未读取守护进程状态";
      }
    }
    return "尚未读取守护进程状态";
  }

  function headerNotice(state) {
    if (state.connection === "disconnected") {
      return { tone: "muted", text: "未连接本地服务。当前为纯界面，尚未接到业务层。" };
    }
    if (state.connection === "loading") {
      return { tone: "muted", text: "正在读取状态…" };
    }
    if (state.connection === "error") {
      return { tone: "danger", text: "无法读取状态" };
    }
    const asr = state.asr || {};
    const ollama = state.ollama || {};
    if (asr.status === "failed" || ollama.modelStatus === "failed" || ollama.running === false || asr.status === "missing" || ollama.modelStatus === "missing" || asr.status === "downloading" || ollama.modelStatus === "downloading" || (asr.status === "ready" && asr.sidecarRunning !== true)) {
      return { tone: "warn", text: "有事项需要处理，完成前部分功能不可用" };
    }
    const gates = state.unmetGates || state.unmet_gates || [];
    if (gates.length) {
      return { tone: "warn", text: "有事项需要处理，完成前部分功能不可用" };
    }
    if (modelReady(asr) && ollamaReady(ollama)) {
      const voice = state.hotkey_voice;
      const shot = state.hotkey_screenshot;
      if (voice && shot) {
        return { tone: "muted", text: "全部就绪 · 按住 " + voice + " 说话，" + shot + " 截图翻译" };
      }
      return { tone: "muted", text: "全部就绪" };
    }
    return { tone: "muted", text: "已连接，模型状态尚未完整" };
  }

  function progressNode(progress) {
    if (!progress) return null;
    const percent = typeof progress.percent === "number" ? progress.percent : null;
    const received = progress.receivedLabel || "";
    const total = progress.totalLabel || "";
    const bits = [];
    if (received || total) bits.push((received || "?") + " / " + (total || "?"));
    if (percent != null) bits.push(Math.round(percent) + "%");
    const label = bits.length ? "下载中 " + bits.join(" · ") : "下载中…";
    const bar = V3.dom.el("div", { className: "progress", role: "progressbar", "aria-label": label });
    if (percent != null) {
      bar.setAttribute("aria-valuemin", "0");
      bar.setAttribute("aria-valuemax", "100");
      bar.setAttribute("aria-valuenow", String(Math.round(percent)));
      bar.appendChild(V3.dom.el("i", { style: "width:" + Math.max(0, Math.min(100, percent)) + "%" }));
    } else {
      bar.setAttribute("aria-valuetext", "进度未知");
      bar.appendChild(V3.dom.el("i", { style: "width:0" }));
    }
    return V3.dom.el("div", {}, [
      V3.dom.el("div", { className: "row-sub", text: label }),
      bar
    ]);
  }

  function actionLabel(action, model) {
    const failed = model && (model.status === "failed" || model.modelStatus === "failed");
    return ({
      download: "下载",
      retry: "重试",
      install: "安装 Ollama",
      start: "启动 Ollama",
      asr_download: failed ? "重试" : "下载",
      asr_start: failed ? "重试" : "启动识别服务",
      ollama_install: failed ? "重试" : "安装 Ollama",
      ollama_start: failed ? "重试" : "启动 Ollama",
      ollama_pull: failed ? "重试" : "下载模型"
    })[action] || null;
  }

  function actionHook(kind, action) {
    if (kind === "asr") {
      if (action === "download" || action === "retry" || action === "asr_download") return "onDownloadAsr";
      if (action === "asr_start") return "onStartAsr";
    }
    if (kind === "ollama") {
      if (action === "install" || action === "ollama_install") return "onInstallOllama";
      if (action === "start" || action === "ollama_start") return "onStartOllama";
      if (action === "download" || action === "retry" || action === "ollama_pull") return "onDownloadOllamaModel";
    }
    return null;
  }

  function modelBadge(status) {
    if (status === "ready") return V3.dom.el("span", { className: "badge ok", text: "已就绪" });
    if (status === "downloading") return V3.dom.el("span", { className: "badge warn", text: "下载中" });
    if (status === "failed") return V3.dom.el("span", { className: "badge danger", text: "失败" });
    if (status === "missing") return V3.dom.el("span", { className: "badge warn", text: "未就绪" });
    return V3.dom.el("span", { className: "badge", text: "未读取" });
  }

  function asrBadge(asr, connected) {
    if (!connected) return modelBadge("unknown");
    if (asr && asr.status === "ready" && asr.sidecarRunning === true) return modelBadge("ready");
    if (asr && asr.status === "ready") return V3.dom.el("span", { className: "badge warn", text: "未启动" });
    return modelBadge(asr && asr.status);
  }

  function asrSub(asr, connected) {
    if (!connected) return V3.dom.el("div", { className: "row-sub", text: "尚未读取状态" });
    if (asr.status === "downloading") return progressNode(asr.progress) || V3.dom.el("div", { className: "row-sub", text: "下载中…" });
    if (asr.status === "failed") {
      return V3.dom.el("div", { className: "row-sub err", text: asr.message || "下载或加载失败" });
    }
    if (asr.status === "missing") {
      return V3.dom.el("div", { className: "row-sub", text: asr.message || "尚未下载" });
    }
    if (asr.status === "ready") {
      if (asr.sidecarRunning === true) {
        const bits = ["已下载"];
        if (asr.sizeLabel) bits.push(asr.sizeLabel);
        if (asr.deviceLabel) bits.push(asr.deviceLabel);
        return V3.dom.el("div", { className: "row-sub", text: bits.join(" · ") });
      }
      const bits = [];
      bits.push(asr.message || "识别服务未启动");
      if (asr.sizeLabel) bits.push(asr.sizeLabel);
      if (asr.deviceLabel) bits.push(asr.deviceLabel);
      return V3.dom.el("div", { className: "row-sub", text: bits.join(" · ") });
    }
    return V3.dom.el("div", { className: "row-sub", text: "检测中…" });
  }

  function ollamaSub(ollama, connected) {
    if (!connected) return V3.dom.el("div", { className: "row-sub", text: "尚未读取状态" });
    if (ollama.modelStatus === "downloading") {
      return progressNode(ollama.progress) || V3.dom.el("div", { className: "row-sub", text: "下载中…" });
    }
    if (ollama.installed === false) {
      return V3.dom.el("div", { className: "row-sub err", text: ollama.message || "未安装 Ollama，纠错与翻译不可用" });
    }
    if (ollama.running === false) {
      return V3.dom.el("div", { className: "row-sub err", text: ollama.message || "Ollama 已安装但未运行，润色与截图翻译不可用" });
    }
    if (ollama.modelStatus === "failed") {
      return V3.dom.el("div", { className: "row-sub err", text: ollama.message || "模型不可用" });
    }
    if (ollama.modelStatus === "missing") {
      return V3.dom.el("div", { className: "row-sub", text: ollama.message || "模型尚未下载" });
    }
    if (ollama.modelStatus === "ready" && ollama.running === true) {
      const bits = [];
      if (ollama.version) bits.push("Ollama " + ollama.version + " 运行中");
      else bits.push("Ollama 运行中");
      if (ollama.sizeLabel) bits.push("模型已下载 " + ollama.sizeLabel);
      else bits.push("模型已就绪");
      return V3.dom.el("div", { className: "row-sub", text: bits.join(" · ") });
    }
    return V3.dom.el("div", { className: "row-sub", text: "检测中…" });
  }

  function mountSetup(root, options) {
    const opts = options || {};
    const hooks = opts.hooks || {};
    let state = V3.defaults.merge(V3.defaults.setupState(), opts.initialState);
    let tab = opts.tab || "models";
    let ignoreHotkeyBlur = false;
    const confirm = V3.mountConfirm(root);

    function setState(patch) {
      state = V3.defaults.merge(state, patch);
      renderAll();
    }

    V3.ensureIcons();
    V3.dom.empty(root);

    const titleEl = V3.dom.el("h1", { id: "v3-setup-title", text: "模型" });
    const noticeEl = V3.dom.el("p", { className: "muted", id: "v3-setup-notice" });
    const bannerEl = V3.dom.el("div", { className: "banner danger", hidden: true, role: "alert" });
    const paneEl = V3.dom.el("div", { id: "v3-setup-pane" });
    const navEl = V3.dom.el("nav", {
      className: "sa-nav",
      id: "v3-setup-nav",
      "aria-label": "设置分类"
    });
    const footEl = V3.dom.el("div", { className: "sa-nav-foot" });
    const refreshBtn = V3.dom.el("button", {
      type: "button",
      className: "btn",
      onClick: function () { call(hooks, "onRefreshStatus"); }
    }, [V3.dom.icon("refresh"), document.createTextNode("刷新状态")]);

    function navDotClass() {
      if (state.connection === "disconnected" || state.connection === "error") return "err";
      if (state.connection === "loading") return "";
      const asr = state.asr || {};
      const ollama = state.ollama || {};
      if (modelReady(asr) && ollamaReady(ollama)) return "ok";
      if (asr.status === "failed" || ollama.modelStatus === "failed" || ollama.running === false || asr.status === "downloading" || ollama.modelStatus === "downloading" || (asr.status === "ready" && asr.sidecarRunning !== true)) {
        return "err";
      }
      return "";
    }

    function renderNav() {
      V3.dom.empty(navEl);
      navEl.appendChild(V3.dom.el("div", { className: "sa-nav-title", text: "语音输入" }));
      TABS.forEach(function (item) {
        const current = item.id === tab;
        const btn = V3.dom.el("button", {
          type: "button",
          "aria-current": current ? "page" : null,
          dataset: { tab: item.id }
        }, [document.createTextNode(item.label)]);
        if (item.id === "models") {
          const cls = navDotClass();
          if (cls) btn.appendChild(V3.dom.el("span", { className: "dot " + cls, "aria-hidden": "true" }));
        }
        btn.addEventListener("click", function () { setTab(item.id); });
        navEl.appendChild(btn);
      });
      footEl.textContent = state.aboutText || "本地运行";
      navEl.appendChild(footEl);
    }

    function setTab(id) {
      tab = id;
      const found = TABS.filter(function (item) { return item.id === id; })[0];
      titleEl.textContent = found ? found.label : id;
      renderNav();
      renderPane();
    }

    function actionButton(kind, model, primary) {
      const action = model && model.action;
      const label = actionLabel(action, model);
      const hook = actionHook(kind, action);
      if (!label || !hook) return null;
      const busy = model.status === "downloading" || model.modelStatus === "downloading";
      return V3.dom.el("button", {
        type: "button",
        className: "btn" + (primary ? " primary" : ""),
        disabled: busy || !isConnected(state),
        text: busy ? "进行中…" : label,
        onClick: function () { call(hooks, hook); }
      });
    }

    function renderModels() {
      const connected = isConnected(state);
      const asr = state.asr || {};
      const ollama = state.ollama || {};
      const olStatus = connected ? ollama.modelStatus : "unknown";
      return [
        V3.dom.el("h2", { text: "两个本地模型" }),
        V3.dom.el("div", { className: "rows" }, [
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main" }, [
              V3.dom.el("div", { className: "row-title" }, [
                document.createTextNode("语音识别 "),
                V3.dom.el("span", { className: "mono muted", text: V3.labels.FIXED_MODELS.asr })
              ]),
              asrSub(asr, connected)
            ]),
            asrBadge(asr, connected),
            actionButton("asr", asr, asr.action === "download" || asr.action === "retry" || asr.action === "asr_download" || asr.action === "asr_start")
          ]),
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main" }, [
              V3.dom.el("div", { className: "row-title" }, [
                document.createTextNode("纠错与翻译 "),
                V3.dom.el("span", { className: "mono muted", text: V3.labels.FIXED_MODELS.ollama })
              ]),
              ollamaSub(ollama, connected)
            ]),
            modelBadge(olStatus === "ready" && ollama.running === true ? "ready" : olStatus),
            actionButton("ollama", ollama, true)
          ])
        ]),
        V3.dom.el("p", {
          className: "hint",
          style: "margin-top:var(--sp-2)",
          text: "模型固定，不提供切换。缺什么只出现对应按钮。失败时在该行显示原因和「重试」。没有进度数据时不会显示百分比。"
        })
      ];
    }

    function renderVoice() {
      const enabled = state.polish_enabled;
      const known = enabled === true || enabled === false;
      const keywords = Array.isArray(state.custom_keywords)
        ? state.custom_keywords.join("、")
        : (state.custom_keywords || "");
      const sw = V3.dom.el("button", {
        type: "button",
        className: "switch",
        role: "switch",
        "aria-checked": known ? String(enabled) : "mixed",
        "aria-label": "松开后纠错润色",
        disabled: !isConnected(state),
        onClick: function () {
          if (!isConnected(state)) return;
          call(hooks, "onTogglePolish", enabled !== true);
        }
      });
      const area = V3.dom.el("textarea", {
        className: "field",
        rows: "3",
        disabled: !isConnected(state),
        "aria-label": "关键词"
      });
      area.value = keywords;
      const save = V3.dom.el("button", {
        type: "button",
        className: "btn primary",
        disabled: !isConnected(state),
        text: "保存",
        onClick: function () { call(hooks, "onSaveKeywords", area.value); }
      });
      return [
        V3.dom.el("h2", { text: "松开后的处理" }),
        V3.dom.el("div", { className: "rows" }, [
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main" }, [
              V3.dom.el("div", { className: "row-title", text: "松开后纠错润色" }),
              V3.dom.el("div", {
                className: "row-sub",
                text: "只改错别字、同音词、专有名词、标点和口头禅，不改原意和语序。润色失败时不输入，可按重复键发送原文。"
              })
            ]),
            sw
          ]),
          V3.dom.el("div", { className: "row", style: "display:block" }, [
            V3.dom.el("div", { className: "row-title", text: "关键词" }),
            V3.dom.el("p", { className: "hint", text: "润色时按这里的写法修正专有名词。用逗号、顿号、空格或换行分隔。" }),
            area,
            V3.dom.el("div", { className: "actions" }, [
              state.keywordsSaved ? V3.dom.el("span", { className: "saved", text: "已保存" }) : null,
              save
            ])
          ])
        ])
      ];
    }

    function renderShot() {
      const options = state.targetLangOptions && state.targetLangOptions.length
        ? state.targetLangOptions
        : V3.labels.TARGET_LANG_OPTIONS;
      const select = V3.dom.el("select", {
        className: "field",
        "aria-label": "翻译成",
        disabled: !isConnected(state),
        onChange: function (ev) { call(hooks, "onChangeTargetLang", ev.target.value); }
      });
      if (state.translate_target_lang == null || state.translate_target_lang === "") {
        select.appendChild(V3.dom.el("option", { value: "", selected: true, text: "尚未读取" }));
      }
      options.forEach(function (item) {
        const opt = V3.dom.el("option", { value: item.value, text: item.label });
        if (item.value === state.translate_target_lang) opt.selected = true;
        select.appendChild(opt);
      });
      return [
        V3.dom.el("h2", { text: "翻译设置" }),
        V3.dom.el("div", { className: "rows" }, [
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main" }, [
              V3.dom.el("div", { className: "row-title", text: "翻译成" }),
              V3.dom.el("div", {
                className: "row-sub",
                text: "本地 " + V3.labels.FIXED_MODELS.ollama + " 翻译，不联网。选「仅识别文字」时只做文字识别。"
              })
            ]),
            select
          ])
        ]),
        state.targetLangSaved
          ? V3.dom.el("div", { className: "actions" }, [V3.dom.el("span", { className: "saved", text: "已保存" })])
          : null
      ];
    }

    function endHotkeyCapture(restoreSaved) {
      const name = state.hotkeyCapturing;
      const patch = { hotkeyCapturing: null, hotkeyCaptureError: "" };
      if (restoreSaved && name) {
        const drafts = Object.assign({}, state.hotkeyDrafts || {});
        delete drafts[name];
        patch.hotkeyDrafts = Object.keys(drafts).length ? drafts : null;
        patch.hotkeyCaptureHint = "";
      }
      setState(patch);
    }

    function renderKeys() {
      function keyInput(name, label) {
        const capturing = state.hotkeyCapturing === name;
        const input = V3.dom.el("input", {
          className: "field key",
          type: "text",
          "aria-label": label,
          spellcheck: "false",
          autocomplete: "off",
          readonly: true,
          disabled: !isConnected(state),
          "aria-describedby": "v3-hotkey-hint",
          onFocus: function () {
            if (!isConnected(state) || ignoreHotkeyBlur) return;
            if (state.hotkeyCapturing === name && state.hotkeyFocus === name) return;
            setState({
              hotkeyCapturing: name,
              hotkeyFocus: name,
              hotkeyCaptureError: "",
              hotkeysSaved: false
            });
          },
          onBlur: function () {
            if (ignoreHotkeyBlur) return;
            if (state.hotkeyCapturing === name) {
              setState({ hotkeyCapturing: null, hotkeyFocus: name });
            } else if (state.hotkeyFocus === name) {
              /* 保持上次焦点字段，供保存失败后恢复 */
            }
          },
          onKeyDown: function (ev) {
            if (!isConnected(state)) return;
            if (state.hotkeyCapturing && state.hotkeyCapturing !== name) return;
            const convert = V3.hotkey && typeof V3.hotkey.fromKeyboardEvent === "function"
              ? V3.hotkey.fromKeyboardEvent(ev)
              : { error: "快捷键转换模块未加载", consumed: false };
            if (convert.consumed) ev.preventDefault();
            if (convert.cancel) {
              endHotkeyCapture(true);
              return;
            }
            if (convert.navigate) {
              setState({ hotkeyCapturing: null, hotkeyFocus: name });
              return;
            }
            if (convert.pending) {
              if (state.hotkeyCapturing !== name) {
                setState({ hotkeyCapturing: name, hotkeyFocus: name, hotkeyCaptureError: "" });
              }
              return;
            }
            if (convert.error) {
              setState({ hotkeyCaptureError: convert.error, hotkeyFocus: name, hotkeyCapturing: name });
              return;
            }
            if (convert.value) {
              const drafts = Object.assign({}, state.hotkeyDrafts || {});
              drafts[name] = convert.value;
              setState({
                hotkeyDrafts: drafts,
                hotkeyCapturing: name,
                hotkeyFocus: name,
                hotkeyCaptureError: "",
                hotkeyCaptureHint: convert.hint || "",
                hotkeysSaved: false,
                hotkeySaveError: ""
              });
            }
          }
        });
        input.value = currentHotkey(state, name);
        input.dataset.hotkey = name;
        if (capturing) input.setAttribute("data-capturing", "true");
        return input;
      }
      const voice = keyInput("hotkey_voice", "语音输入快捷键");
      const shot = keyInput("hotkey_screenshot", "截图翻译快捷键");
      const repeat = keyInput("hotkey_repeat", "重复上次输入快捷键");
      const hintBits = [];
      hintBits.push(desktopDaemonDetail(state));
      hintBits.push("点选输入框后按下修饰键加主键录入。Esc 恢复原值，离开输入框结束录入且不自动保存。单独 Tab 用于移出焦点；Ctrl+Tab 等带修饰键的组合会被录入。部分系统或浏览器保留的组合不会送达此页，无法在这里录入，也不会被保存为已成功。");
      if (state.hotkeyCaptureHint) hintBits.push(state.hotkeyCaptureHint);
      const messages = [];
      if (state.hotkeyCaptureError) {
        messages.push(V3.dom.el("div", { className: "row-sub err", text: state.hotkeyCaptureError }));
      }
      if (state.hotkeySaveError) {
        messages.push(V3.dom.el("div", { className: "row-sub err", text: state.hotkeySaveError }));
      }
      return [
        V3.dom.el("h2", { text: "三个全局快捷键" }),
        V3.dom.el("div", { className: "rows" }, [
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main row-title", text: "语音输入（按住说话）" }),
            voice
          ]),
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main row-title", text: "截图翻译" }),
            shot
          ]),
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main" }, [
              V3.dom.el("div", { className: "row-title", text: "重复上次输入" }),
              V3.dom.el("div", { className: "row-sub", text: "润色失败或焦点切走时，用它补发" })
            ]),
            repeat
          ])
        ]),
        messages.length ? V3.dom.el("div", {}, messages) : null,
        V3.dom.el("div", { className: "actions" }, [
          V3.dom.el("span", {
            className: "hint",
            id: "v3-hotkey-hint",
            style: "margin:0 auto 0 0",
            text: hintBits.join(" ")
          }),
          state.hotkeysSaved ? V3.dom.el("span", { className: "saved", text: "已保存" }) : null,
          V3.dom.el("button", {
            type: "button",
            className: "btn primary",
            disabled: !isConnected(state),
            text: "保存快捷键",
            onClick: function () {
              call(hooks, "onSaveHotkeys", {
                hotkey_voice: currentHotkey(state, "hotkey_voice"),
                hotkey_screenshot: currentHotkey(state, "hotkey_screenshot"),
                hotkey_repeat: currentHotkey(state, "hotkey_repeat")
              });
            }
          })
        ])
      ];
    }

    function renderLan() {
      const steps = V3.dom.el("ol", {}, [
        V3.dom.el("li", { text: "手机和这台电脑连同一个 Wi-Fi / 局域网" }),
        V3.dom.el("li", { text: "扫码打开；提示「连接不是私密」时点「高级 → 继续」（本机自签证书）" }),
        V3.dom.el("li", { text: "按住按钮说话，松开后识别、润色并输入到电脑当前光标处" })
      ]);
      const entries = state.lanEntries;
      const list = V3.dom.el("div", { className: "lan-list" });
      if (!isConnected(state)) {
        list.appendChild(V3.dom.el("div", { className: "empty", text: "尚未读取局域网入口。" }));
      } else if (!entries || !entries.length) {
        list.appendChild(V3.dom.el("div", { className: "empty", text: "未检测到局域网 IP" }));
      } else {
        entries.forEach(function (entry, index) {
          const qr = V3.dom.el("div", { className: "qr", "aria-label": "入口二维码" });
          if (entry.qrSvg) qr.innerHTML = entry.qrSvg;
          else if (entry.qrDataUrl) {
            qr.appendChild(V3.dom.el("img", { src: entry.qrDataUrl, alt: "扫码打开录音页" }));
          } else {
            qr.appendChild(V3.dom.el("span", { className: "muted", style: "font-size:var(--fs-xs)", text: "二维码不可用" }));
          }
          const shown = entry.urlMasked || entry.url_masked || "";
          list.appendChild(V3.dom.el("div", { className: "lan" }, [
            qr,
            V3.dom.el("div", {}, [
              index === 0 ? steps : null,
              V3.dom.el("div", { className: "url", text: shown || "（无网址）" }),
              V3.dom.el("button", {
                type: "button",
                className: "btn",
                disabled: !shown && !entry.url,
                onClick: function () { call(hooks, "onCopy", { kind: "lan_url", index: index, text: entry.url || undefined }); }
              }, [V3.dom.icon("copy"), document.createTextNode("复制网址")])
            ])
          ]));
        });
      }
      return [
        V3.dom.el("h2", { text: "扫码打开录音页" }),
        list,
        V3.dom.el("details", {}, [
          V3.dom.el("summary", { text: "安全说明" }),
          V3.dom.el("p", {
            className: "muted",
            text: "网址带专属令牌，只有拿到链接的人能录入。令牌疑似泄露时，按说明刷新令牌后重启服务。安装软件、删除模型、修改配置等操作只允许本机访问。"
          })
        ]),
        V3.dom.el("p", {
          className: "hint",
          style: "margin-top:var(--sp-2)",
          text: "多块网卡时每个地址一组。没有二维码数据时只显示网址。"
        })
      ];
    }

    function renderHist() {
      const box = V3.dom.el("div", { className: "rows", id: "v3-setup-history" });
      const hist = Object.assign({}, state.history || {});
      if (!isConnected(state) && !(hist.records && hist.records.length) && !hist.message) {
        hist.message = "尚未读取处理记录。";
      }
      V3.history.renderList(box, hist, {
        emptyText: "还没有记录，按住快捷键说一句话试试",
        onCopy: function (text, record) {
          call(hooks, "onCopy", { kind: "history", text: text, record: record });
        }
      });
      const shown = (hist.records || []).length;
      const nodes = [V3.dom.el("h2", { text: "本机、手机、重发的全部处理记录" }), box];
      if (hist.total != null && shown < hist.total) {
        nodes.push(V3.dom.el("button", {
          type: "button",
          className: "btn more",
          disabled: !isConnected(state) || !!hist.loading,
          text: "加载更多（" + shown + " / " + hist.total + "）",
          onClick: function () { call(hooks, "onLoadMoreHistory"); }
        }));
      }
      nodes.push(V3.dom.el("p", {
        className: "hint",
        style: "margin-top:var(--sp-2)",
        text: "这里是处理记录，不表示文字已经输入到目标窗口。"
      }));
      return nodes;
    }

    function renderApi() {
      const tokenText = state.tokenMasked || (state.tokenPresent ? "••••••••" : "尚未读取");
      const rows = [
        { name: "STT", desc: "音频 → 原始识别文本（不润色、不输入）。契约：SttRequest / SttResponse。" },
        { name: "Polish", desc: "文本 → 只纠错的润色结果。契约：PolishRequest / PolishResponse。" },
        { name: "Translate", desc: "文本 → 本地翻译结果。契约：TranslateRequest / TranslateResponse。" },
        { name: "OCR Translate", desc: "图片 → 识别文字与译文。契约：OcrTranslateResponse（text 是译文，ocr_text 是原文）。" },
        { name: "Utterance / Job", desc: "提交文本并查询任务状态。投递未知为 delivery_unknown，不得显示为成功。" },
        { name: "History", desc: "处理记录列表。契约：HistoryListResponse。" }
      ];
      const table = V3.dom.el("table", { className: "api-table" });
      rows.forEach(function (row) {
        table.appendChild(V3.dom.el("tr", {}, [
          V3.dom.el("td", { className: "mono", text: row.name }),
          V3.dom.el("td", { text: row.desc })
        ]));
      });
      const addr = [];
      if (state.serviceUrl) addr.push("服务地址 " + state.serviceUrl);
      if (state.localUrl) addr.push("本机 " + state.localUrl);
      const derived = state.apiPaths;
      let pathTable = null;
      function apiHint(addrBits) {
        const prefix = addrBits.length ? addrBits.join("，") + "。" : "服务地址尚未读取。";
        if (!derived) return prefix + "尚未读取接口路径。";
        if (!derived.length) return prefix + "当前目录没有可展示的接口路径。";
        return prefix + "下列路径从当前 OpenAPI 快照派生。";
      }
      if (derived && derived.length) {
        pathTable = V3.dom.el("table", { className: "api-table" });
        derived.forEach(function (op) {
          pathTable.appendChild(V3.dom.el("tr", {}, [
            V3.dom.el("td", { className: "mono", text: (op.method || "") + " " + (op.path || "") }),
            V3.dom.el("td", { text: (op.access === "lan_token" ? "需令牌" : "仅本机") + (op.operationId ? " · " + op.operationId : "") })
          ]));
        });
      }
      return [
        V3.dom.el("h2", { text: "供其他项目调用" }),
        V3.dom.el("p", {
          className: "hint",
          text: apiHint(addr)
        }),
        V3.dom.el("div", { className: "rows" }, [
          V3.dom.el("div", { className: "row" }, [
            V3.dom.el("div", { className: "row-main" }, [
              V3.dom.el("div", { className: "row-title", text: "访问令牌" }),
              V3.dom.el("div", { className: "row-sub mono", text: tokenText })
            ]),
            V3.dom.el("button", {
              type: "button",
              className: "btn",
              disabled: !state.tokenPresent,
              onClick: function () { call(hooks, "onCopy", { kind: "token" }); }
            }, [V3.dom.icon("copy"), document.createTextNode("复制")])
          ]),
          V3.dom.el("div", { className: "row", style: "display:block" }, [pathTable || table])
        ])
      ];
    }

    function renderSys() {
      const list = state.capabilities;
      const rows = V3.dom.el("div", { className: "rows" });
      if (!list) {
        rows.appendChild(V3.dom.el("div", { className: "empty", text: "尚未读取平台能力。" }));
      } else if (!list.length) {
        rows.appendChild(V3.dom.el("div", { className: "empty", text: "当前平台没有可展示的检测项。" }));
      } else {
        list.forEach(function (item) {
          const ok = item.available === true;
          const known = item.available === true || item.available === false;
          rows.appendChild(V3.dom.el("div", { className: "row" }, [
            V3.dom.el("span", { className: "dot " + (known ? (ok ? "ok" : "err") : ""), "aria-hidden": "true" }),
            V3.dom.el("div", { className: "row-main", text: item.name || "未命名能力" }),
            V3.dom.el("span", {
              className: ok ? "muted" : "",
              style: known && !ok ? "color:var(--c-danger)" : "",
              text: known ? (ok ? "可用" : (item.message || "不可用")) : "未检测"
            })
          ]));
          if (!ok && item.installCommand) {
            rows.appendChild(V3.dom.el("div", { className: "row" }, [
              V3.dom.el("div", { className: "row-main row-sub", text: "安装命令" }),
              V3.dom.el("span", { className: "mono", style: "font-size:var(--fs-xs)", text: item.installCommand })
            ]));
          }
        });
      }
      return [
        V3.dom.el("h2", { text: "平台能力" }),
        rows,
        V3.dom.el("p", {
          className: "hint",
          style: "margin-top:var(--sp-2)",
          text: "列表项由后续业务按当前平台注入，页面不写死。"
        })
      ];
    }

    function renderPane() {
      let restoreName = null;
      if (tab === "keys") {
        if (state.hotkeyCapturing) restoreName = state.hotkeyCapturing;
        else if (state.hotkeySaveError && state.hotkeyFocus) restoreName = state.hotkeyFocus;
        else if (paneEl.contains(document.activeElement) && document.activeElement && document.activeElement.getAttribute("data-hotkey")) {
          restoreName = document.activeElement.getAttribute("data-hotkey");
        }
      }
      ignoreHotkeyBlur = true;
      V3.dom.empty(paneEl);
      const map = {
        models: renderModels,
        voice: renderVoice,
        shot: renderShot,
        keys: renderKeys,
        lan: renderLan,
        hist: renderHist,
        api: renderApi,
        sys: renderSys
      };
      (map[tab] || renderModels)().forEach(function (node) {
        if (node) paneEl.appendChild(node);
      });
      if (restoreName) {
        const el = paneEl.querySelector('input[data-hotkey="' + restoreName + '"]');
        if (el) el.focus();
      }
      ignoreHotkeyBlur = false;
    }

    function renderChrome() {
      const notice = headerNotice(state);
      noticeEl.className = notice.tone === "danger" ? "" : (notice.tone === "warn" ? "" : "muted");
      if (notice.tone === "danger") noticeEl.style.color = "var(--c-danger)";
      else if (notice.tone === "warn") noticeEl.style.color = "var(--c-warn)";
      else noticeEl.style.color = "";
      noticeEl.textContent = notice.text;

      const err = state.connectionError;
      if (state.connection === "error" || err) {
        bannerEl.hidden = false;
        const body = err
          ? V3.labels.formatError(err.error, err.message)
          : "无法连接本地服务。请确认后台服务已启动后点「刷新状态」。";
        bannerEl.textContent = body;
      } else if (state.statusNotice) {
        bannerEl.hidden = false;
        bannerEl.textContent = state.statusNotice;
        bannerEl.className = "banner warn";
      } else {
        bannerEl.hidden = true;
        bannerEl.className = "banner danger";
      }
      refreshBtn.disabled = state.connection === "loading";
    }

    function renderAll() {
      renderChrome();
      renderNav();
      const found = TABS.filter(function (item) { return item.id === tab; })[0];
      titleEl.textContent = found ? found.label : tab;
      renderPane();
    }

    const layout = V3.dom.el("div", { className: "sa-layout" }, [
      navEl,
      V3.dom.el("div", { className: "sa" }, [
        V3.dom.el("header", { className: "sa-head" }, [
          V3.dom.el("div", {}, [titleEl, noticeEl]),
          refreshBtn
        ]),
        bannerEl,
        paneEl
      ])
    ]);
    root.appendChild(layout);

    navEl.addEventListener("keydown", function (ev) {
      const buttons = Array.prototype.slice.call(navEl.querySelectorAll("button[data-tab]"));
      const index = buttons.indexOf(document.activeElement);
      if (index < 0) return;
      let next = index;
      if (ev.key === "ArrowDown" || ev.key === "ArrowRight") next = (index + 1) % buttons.length;
      else if (ev.key === "ArrowUp" || ev.key === "ArrowLeft") next = (index - 1 + buttons.length) % buttons.length;
      else if (ev.key === "Home") next = 0;
      else if (ev.key === "End") next = buttons.length - 1;
      else return;
      ev.preventDefault();
      buttons[next].focus();
    });

    renderAll();

    return {
      setState: setState,
      getState: function () { return state; },
      setTab: setTab,
      getTab: function () { return tab; },
      confirm: confirm,
      destroy: function () {
        confirm.destroy();
        V3.dom.empty(root);
      }
    };
  }

  V3.mountSetup = mountSetup;
})(window.V3 = window.V3 || {});
