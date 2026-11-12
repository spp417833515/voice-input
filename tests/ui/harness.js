/* 浏览器运行时断言。使用测试夹具，不碰真实配置、历史或剪贴板。 */
(function () {
  "use strict";
  const reportEl = document.getElementById("report");
  const failures = [];
  const passes = [];
  function fail(msg) { failures.push(msg); }
  function pass(msg) { passes.push(msg); }
  function assert(cond, msg) { if (cond) pass(msg); else fail(msg); }

  try {
    const copies = [];
    const confirms = [];
    const records = [];
    const hooks = {
      onCopy: function (payload) { copies.push(payload); },
      onConfirmDeliveryChoice: function (payload) { confirms.push(payload); },
      onConfirmResendChoice: function (payload) { confirms.push(payload); },
      onSendOriginal: function (payload) { confirms.push({ sendOriginal: true, payload: payload }); },
      onRecordStart: function () { records.push("start"); },
      onRecordEnd: function () { records.push("end"); },
      onRefreshStatus: function () { records.push("refresh"); }
    };

    const setup = V3.mountSetup(document.getElementById("setup-root"), { hooks: hooks });
    const setupText = document.getElementById("setup-root").innerText;
    assert(setupText.indexOf("未连接本地服务") !== -1, "设置页默认显示未连接");
    assert(setupText.indexOf("全部就绪") === -1, "设置页默认不显示全部就绪");
    assert(setupText.indexOf("Qwen3-ASR-1.7B") !== -1, "设置页展示固定识别模型");
    assert(setupText.indexOf("qwen3.5:4b") !== -1, "设置页展示固定纠错模型");
    assert(setup.getState().asr.status === "unknown", "默认识别状态不是 ready");

    document.getElementById("setup-root").querySelector("button[data-tab='api']").click();
    const apiText = document.getElementById("setup-root").innerText;
    assert(apiText.indexOf("尚未读取") !== -1, "离线接口页声明尚未读取路径");
    assert(apiText.indexOf("/api/") === -1, "离线接口页没有冒充 /api/ 路径");

    document.getElementById("setup-root").querySelector("button[data-tab='hist']").click();
    const histText = document.getElementById("setup-root").innerText;
    assert(histText.indexOf("处理记录") !== -1, "历史页说明这是处理记录");
    assert(histText.indexOf("已输入") === -1, "默认历史不出现已输入");

    setup.setState(V3Fixtures.setupFailedDownload);
    document.getElementById("setup-root").querySelector("button[data-tab='models']").click();
    const failText = document.getElementById("setup-root").innerText;
    assert(failText.indexOf("下载中断") !== -1, "下载失败显示后端原因");
    assert(failText.indexOf("重试") !== -1, "失败时出现重试");
    assert(failText.indexOf("启动 Ollama") !== -1, "Ollama 未运行时出现启动按钮");
    assert(failText.indexOf("已就绪") === -1 || (failText.indexOf("语音识别") !== -1 && failText.indexOf("失败") !== -1), "失败不得显示成成功进度");

    setup.setState(V3Fixtures.setupDownloading);
    const dlText = document.getElementById("setup-root").innerText;
    assert(dlText.indexOf("42%") !== -1 && dlText.indexOf("下载中") !== -1, "仅在注入进度时显示百分比");

    setup.setState({ history: V3Fixtures.setupConnected.history, connection: "connected" });
    document.getElementById("setup-root").querySelector("button[data-tab='hist']").click();
    const recText = document.getElementById("setup-root").innerText;
    assert(recText.indexOf("已润色") !== -1, "历史显示已润色");
    assert(recText.indexOf("润色失败") !== -1, "历史显示润色失败");
    assert(recText.indexOf("本机") !== -1 && recText.indexOf("手机") !== -1, "历史显示来源");
    assert(!/已输入(?!到)/.test(recText) && recText.indexOf("谎称") === -1, "历史不把记录写成已输入");

    const status = V3.mountStatusOverlay(document.getElementById("status-root"), { hooks: hooks });
    const idleText = document.getElementById("status-root").innerText;
    assert(idleText.indexOf("未连接") !== -1, "字幕默认未连接");

    status.setState(V3Fixtures.statusPartial);
    const partialHtml = document.getElementById("status-root").innerHTML;
    const partialText = document.getElementById("status-root").innerText;
    assert(partialText.indexOf("今天下午三点我们在会议室讨论") !== -1, "字幕呈现 stable_text");
    assert(partialText.indexOf("一下新版") !== -1, "字幕呈现 tail_text");
    assert(partialHtml.indexOf("class=\"tail\"") !== -1 || partialHtml.indexOf("class='tail'") !== -1, "未稳定尾部使用 tail 样式");

    status.setState(V3Fixtures.statusError);
    assert(document.getElementById("status-root").innerText.indexOf("语音模型未就绪") !== -1, "错误显示后端原文");

    status.setState(V3Fixtures.statusUnknown);
    const unk = document.getElementById("status-root").innerText;
    assert(unk.indexOf("可能已经输入") !== -1, "投递未知有显著提示");
    const confirmBtn = document.getElementById("status-root").querySelector(".hud-btn");
    assert(!!confirmBtn, "投递未知提供确认入口");
    confirmBtn.click();
    const dialog = document.querySelector(".v3-confirm");
    assert(!!dialog, "打开确认框而不是 alert");
    dialog.querySelector(".btn.primary").click();
    assert(confirms.length === 1 && confirms[0].confirm === true && confirms[0].job_id === "job-9", "确认回调只表达选择");
    assert(confirms[0].state !== "queued", "确认回调未自行标记已入队");

    const trans = V3.mountTranslateOverlay(document.getElementById("translate-root"), { hooks: hooks });
    trans.setState(V3Fixtures.translateError);
    const errT = document.getElementById("translate-root").innerText;
    assert(errT.indexOf("本地模型无响应") !== -1, "翻译失败显示错误原文");
    assert(errT.indexOf("Release notes 3.0") === -1, "失败不把识别原文当译文");

    trans.setState(V3Fixtures.translateResult);
    const copyBtn = document.getElementById("translate-root").querySelector(".hud-btn");
    copyBtn.click();
    assert(copies.length >= 1 && copies[copies.length - 1].text === "版本说明 3.0", "复制按钮只回调，不写剪贴板");
    assert(copies[copies.length - 1].kind === "translate", "复制回调带 kind");

    const voice = V3.mountVoice(document.getElementById("voice-root"), { hooks: hooks });
    const v0 = document.getElementById("voice-root").innerText;
    assert(v0.indexOf("未连接") !== -1, "手机页默认未连接");
    const mic0 = document.getElementById("voice-root").querySelector(".mic");
    assert(mic0 && mic0.disabled, "未连接时录音按钮禁用");

    voice.setState(V3Fixtures.voicePolishFail);
    const v1 = document.getElementById("voice-root").innerText;
    assert(v1.indexOf("识别原文（未输入）") !== -1, "润色失败展示原文");
    assert(v1.indexOf("发送原文") !== -1, "润色失败提供发送原文");
    document.getElementById("voice-root").querySelector(".btn.primary").click();
    assert(confirms.some(function (c) { return c.sendOriginal && c.payload.text.indexOf("把这个函数拆成两个") !== -1; }), "发送原文只走回调");

    setup.destroy();
    let rejectSave = false;
    const savedHotkeys = [];
    const keyRoot = document.getElementById("setup-root");
    const keySetup = V3.mountSetup(keyRoot, {
      hooks: {
        onSaveHotkeys: function (keys) {
          savedHotkeys.push(keys);
          if (rejectSave) {
            const message = "请求无效：语音输入、截图翻译、重复上次输入 不能使用相同组合";
            keySetup.setState({
              connection: "connected",
              statusNotice: message,
              hotkeySaveError: message
            });
            return Promise.reject({ error: "invalid_request", message: message });
          }
          keySetup.setState({
            hotkeysSaved: true,
            hotkeyDrafts: null,
            hotkeyCapturing: null,
            hotkeyCaptureError: "",
            hotkeySaveError: ""
          });
          return Promise.resolve();
        }
      },
      initialState: {
        connection: "connected",
        hotkey_voice: "ctrl+alt+z",
        hotkey_screenshot: "ctrl+alt+x",
        hotkey_repeat: "ctrl+alt+r"
      },
      tab: "keys"
    });
    function keyField(name) {
      return keyRoot.querySelector('input[data-hotkey="' + name + '"]');
    }
    function fireKey(el, init) {
      const ev = new KeyboardEvent("keydown", Object.assign({
        bubbles: true,
        cancelable: true,
        ctrlKey: false,
        altKey: false,
        shiftKey: false,
        metaKey: false
      }, init));
      return { prevented: !el.dispatchEvent(ev) };
    }

    assert(!!V3.hotkey && typeof V3.hotkey.fromKeyboardEvent === "function", "harness 已加载 hotkey-capture");
    const voice0 = keyField("hotkey_voice");
    assert(!!voice0 && voice0.value === "ctrl+alt+z", "已保存的合法组合仍显示");
    voice0.focus();
    assert(keySetup.getState().hotkeyCapturing === "hotkey_voice", "聚焦进入录入态");
    assert(document.activeElement === keyField("hotkey_voice"), "进入录入后焦点仍在该输入框");
    const rec = fireKey(keyField("hotkey_voice"), { key: "q", code: "KeyQ", ctrlKey: true, altKey: true });
    assert(rec.prevented, "录入完整组合时 preventDefault");
    assert(keySetup.getState().hotkeyDrafts && keySetup.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+q", "草稿写入 viewstate");
    assert(keyField("hotkey_voice").value === "ctrl+alt+q", "输入框显示草稿");
    assert(document.activeElement === keyField("hotkey_voice"), "录入后焦点不丢");

    const beforePending = keySetup.getState().hotkeyDrafts.hotkey_voice;
    const pend = fireKey(keyField("hotkey_voice"), { key: "Control", code: "ControlLeft", ctrlKey: true });
    assert(!pend.prevented, "仅修饰键不 preventDefault");
    assert(keySetup.getState().hotkeyCapturing === "hotkey_voice", "仅修饰键保持录入态");
    assert(keySetup.getState().hotkeyDrafts.hotkey_voice === beforePending, "仅修饰键不改草稿");

    const esc = fireKey(keyField("hotkey_voice"), { key: "Escape", code: "Escape" });
    assert(esc.prevented, "Esc 取消时 preventDefault");
    assert(keySetup.getState().hotkeyCapturing == null, "Esc 结束录入");
    assert(keyField("hotkey_voice").value === "ctrl+alt+z", "Esc 恢复已保存值");

    keyField("hotkey_voice").focus();
    fireKey(keyField("hotkey_voice"), { key: "q", code: "KeyQ", ctrlKey: true, altKey: true });
    assert((keySetup.getState().hotkeyDrafts || {}).hotkey_voice === "ctrl+alt+q", "Esc 后仍聚焦时可重新录入");
    assert(keySetup.getState().hotkeyCapturing === "hotkey_voice", "重新录入后回到录入态");
    const capBeforePoll = keySetup.getState().hotkeyCapturing;
    keySetup.setState({ asr: { message: "轮询刷新", status: "ready", sidecarRunning: false } });
    assert((keySetup.getState().hotkeyDrafts || {}).hotkey_voice === "ctrl+alt+q", "轮询重渲染不丢草稿");
    assert(keySetup.getState().hotkeyCapturing === capBeforePoll, "轮询不取消录入态");
    assert(document.activeElement === keyField("hotkey_voice"), "轮询不丢焦点");
    assert(keyField("hotkey_voice").value === "ctrl+alt+q", "轮询不改用户输入");

    const tabEv = fireKey(keyField("hotkey_voice"), { key: "Tab", code: "Tab" });
    assert(!tabEv.prevented, "单独 Tab 不 preventDefault，留给导航");
    assert(keySetup.getState().hotkeyCapturing == null, "单独 Tab 结束录入");
    assert(keySetup.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+q", "Tab 离开仍保留草稿");

    keyField("hotkey_voice").focus();
    const ctab = fireKey(keyField("hotkey_voice"), { key: "Tab", code: "Tab", ctrlKey: true });
    assert(ctab.prevented, "Ctrl+Tab 被录入并 preventDefault");
    assert(keySetup.getState().hotkeyDrafts.hotkey_voice === "ctrl+tab", "Ctrl+Tab 记为 ctrl+tab");

    keyField("hotkey_screenshot").focus();
    const stab = fireKey(keyField("hotkey_screenshot"), { key: "Tab", code: "Tab", shiftKey: true });
    assert(!stab.prevented, "Shift+Tab 无 ctrl/alt/super 时用于反向导航");
    assert(keySetup.getState().hotkeyCapturing == null, "Shift+Tab 结束录入");

    const savesBeforeBlur = savedHotkeys.length;
    keyField("hotkey_repeat").focus();
    fireKey(keyField("hotkey_repeat"), { key: "r", code: "KeyR", ctrlKey: true, shiftKey: true });
    assert(keySetup.getState().hotkeyDrafts.hotkey_repeat === "ctrl+shift+r", "重复键草稿");
    keyField("hotkey_repeat").blur();
    assert(keySetup.getState().hotkeyCapturing == null, "blur 结束录入");
    assert(keySetup.getState().hotkeyDrafts.hotkey_repeat === "ctrl+shift+r", "blur 保留草稿且不自动保存");
    assert(savedHotkeys.length === savesBeforeBlur, "blur 不调用保存");

    keyField("hotkey_voice").focus();
    const unsup = fireKey(keyField("hotkey_voice"), { key: "ArrowLeft", code: "ArrowLeft", ctrlKey: true });
    assert(!unsup.prevented, "不支持的键不 preventDefault、不伪造成功");
    assert(/不支持/.test(keySetup.getState().hotkeyCaptureError || keyRoot.innerText), "不支持按键显示说明");
    assert(keySetup.getState().hotkeyDrafts.hotkey_voice === "ctrl+tab", "不支持按键不改已有草稿");

    fireKey(keyField("hotkey_voice"), { key: "q", code: "KeyA", ctrlKey: true, altKey: true });
    assert(keySetup.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+a", "非 US 布局按物理 code 记录");
    assert(/键帽/.test(keySetup.getState().hotkeyCaptureHint || keyRoot.innerText), "物理键位与键帽不同时给出提示");

    rejectSave = true;
    keyRoot.querySelector("button.primary").click();
    assert(/不能使用相同组合|请求无效/.test(keyRoot.innerText + " " + (keySetup.getState().hotkeySaveError || "") + " " + (keySetup.getState().statusNotice || "")), "互相重复显示后端错误");
    assert(keySetup.getState().hotkeyDrafts.hotkey_voice === "ctrl+alt+a", "保存失败不丢草稿");
    assert(keySetup.getState().hotkeysSaved !== true, "失败不显示已保存");
    assert(document.activeElement && document.activeElement.getAttribute("data-hotkey"), "保存失败后快捷键区焦点仍在输入框");

    assert(keyRoot.innerText.indexOf("以后端返回的提示为准") === -1, "已删除空承诺提示");
    assert(keyRoot.innerText.indexOf("已生效") === -1, "保存文案不含已生效");
    assert(keyRoot.innerText.indexOf("尚未读取守护进程状态") !== -1, "未读到 desktop_daemon 时显示未知");
    keySetup.setState({
      capabilities: [
        { id: "desktop_daemon", label: "桌面守护进程", available: true, required: true, detail: "守护进程已加载当前快捷键，无需重启。" }
      ]
    });
    assert(keyRoot.innerText.indexOf("守护进程已加载当前快捷键，无需重启。") !== -1, "快捷键区只展示 desktop_daemon.detail");
    assert(keyRoot.innerText.indexOf("systemctl") === -1 && keyRoot.innerText.indexOf("restart.sh") === -1, "前端不自行推荐重启命令");

    rejectSave = false;
    const savedCount = savedHotkeys.length;
    keyRoot.querySelector("button.primary").click();
    assert(savedHotkeys.length === savedCount + 1, "保存提交当前草稿");
    assert(keyRoot.innerText.indexOf("已保存") !== -1, "保存成功只显示已保存");
    assert(keyRoot.innerText.indexOf("已生效") === -1, "已保存旁不出现已生效");

    keySetup.setState({
      hotkeyDrafts: null,
      hotkey_voice: "ctrl+alt+z",
      hotkey_screenshot: "ctrl+alt+x",
      hotkey_repeat: "ctrl+alt+r",
      hotkeysSaved: false
    });
    assert(
      keyField("hotkey_voice").value === "ctrl+alt+z"
        && keyField("hotkey_screenshot").value === "ctrl+alt+x"
        && keyField("hotkey_repeat").value === "ctrl+alt+r",
      "已保存的合法组合在无草稿时仍可显示和配置"
    );

    keySetup.setTab("models");
    keySetup.setState({
      connection: "connected",
      asr: { status: "ready", sidecarRunning: false, sizeLabel: "3.5 GB", message: "识别服务尚未启动" },
      ollama: { modelStatus: "ready", running: true, version: "0.12.6" }
    });
    const asrTextOff = keyRoot.querySelectorAll(".row")[0].innerText;
    assert(keyRoot.innerText.indexOf("全部就绪") === -1, "权重齐未启动 sidecar 不显示全部就绪");
    assert(asrTextOff.indexOf("已就绪") === -1, "权重齐未启动不显示已就绪");
    assert(asrTextOff.indexOf("未启动") !== -1, "权重齐未启动显示未启动角标");
    assert(asrTextOff.indexOf("识别服务尚未启动") !== -1, "未启动时展示后端原文，不吞消息");
    assert(keyRoot.innerText.indexOf("有事项需要处理") !== -1, "未启动时 header 为需处理而非全部就绪");

    keySetup.setState({ asr: { status: "ready", sidecarRunning: undefined, message: "底层未给出运行状态" } });
    const asrTextMissing = keyRoot.querySelectorAll(".row")[0].innerText;
    assert(asrTextMissing.indexOf("已就绪") === -1, "缺 sidecarRunning 字段时不是已就绪");
    assert(asrTextMissing.indexOf("底层未给出运行状态") !== -1, "缺字段时仍展示后端说明");

    keySetup.setState({
      asr: { status: "ready", sidecarRunning: true, sizeLabel: "3.5 GB", message: "" },
      ollama: { modelStatus: "ready", running: true, version: "0.12.6" }
    });
    const asrTextOn = keyRoot.querySelectorAll(".row")[0].innerText;
    assert(asrTextOn.indexOf("已就绪") !== -1, "status ready 且 sidecarRunning===true 才显示已就绪");
    assert(keyRoot.innerText.indexOf("全部就绪") !== -1, "两项都就绪时 header 全部就绪");

    keySetup.setState({ asr: { status: "unknown", sidecarRunning: null, message: "" } });
    assert(keyRoot.querySelectorAll(".row")[0].innerText.indexOf("已就绪") === -1, "底层未知不是 ready");
    keySetup.destroy();

    window.__V3_HARNESS__ = { passes: passes, failures: failures, copies: copies, confirms: confirms };
  } catch (err) {
    fail("harness 异常：" + err.stack);
  }

  reportEl.textContent = JSON.stringify({ passes: passes, failures: failures }, null, 2);
  document.title = failures.length ? "HARNESS_FAIL " + failures.length : "HARNESS_PASS " + passes.length;
})();
