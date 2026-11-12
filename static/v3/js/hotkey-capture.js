/* 把键盘事件转成与 _hotkey 相同的 ctrl+alt+x 语法。只做转换，不做后端校验。 */
(function (V3) {
  "use strict";

  var MOD_ORDER = ["ctrl", "alt", "shift", "super"];

  var CODE_MAIN = {
    Backquote: "grave",
    Space: "space",
    Tab: "tab",
    Enter: "enter",
    NumpadEnter: "enter"
  };

  var MODIFIER_CODES = {
    ControlLeft: true,
    ControlRight: true,
    AltLeft: true,
    AltRight: true,
    ShiftLeft: true,
    ShiftRight: true,
    MetaLeft: true,
    MetaRight: true,
    OSLeft: true,
    OSRight: true
  };

  function modifiersOf(ev) {
    var present = {
      ctrl: !!(ev && ev.ctrlKey),
      alt: !!(ev && ev.altKey),
      shift: !!(ev && ev.shiftKey),
      super: !!(ev && ev.metaKey)
    };
    return MOD_ORDER.filter(function (name) { return present[name]; });
  }

  function isModifierCode(code) {
    return !!(code && MODIFIER_CODES[code]);
  }

  function mainKeyFromCode(code) {
    if (!code) return null;
    if (CODE_MAIN[code]) return CODE_MAIN[code];
    if (/^Key[A-Z]$/.test(code)) return code.slice(3).toLowerCase();
    if (/^Digit[0-9]$/.test(code)) return code.slice(5);
    if (/^F([1-9]|1[0-2])$/.test(code)) return code.toLowerCase();
    return null;
  }

  function layoutHint(ev, main) {
    if (!ev || !main || !ev.key) return null;
    var key = String(ev.key);
    if (key === "Unidentified" || key.length !== 1) return null;
    if (!/^[a-z0-9]$/i.test(key)) return null;
    if (!/^[a-z0-9]$/.test(main)) return null;
    if (key.toLowerCase() === main) return null;
    return "按物理键位记录为 " + main + "（键帽字符是 " + key + "），不是按打印字符。";
  }

  function isTabNavigation(ev, mods) {
    var code = ev && ev.code;
    if (code !== "Tab") return false;
    return mods.indexOf("ctrl") < 0 && mods.indexOf("alt") < 0 && mods.indexOf("super") < 0;
  }

  function fromKeyboardEvent(ev) {
    if (!ev) return { error: "没有按键事件", consumed: false };
    if (ev.key === "Escape" || ev.code === "Escape") {
      return { cancel: true, consumed: true };
    }
    var mods = modifiersOf(ev);
    if (isTabNavigation(ev, mods)) {
      return { navigate: true, consumed: false };
    }
    if (isModifierCode(ev.code)) {
      return { pending: true, consumed: false };
    }
    var main = mainKeyFromCode(ev.code);
    if (!main) {
      return {
        error: "不支持按键 " + (ev.code || ev.key || "未知") + "，未录入。",
        consumed: false
      };
    }
    if (!mods.length) {
      return {
        error: "需要至少一枚修饰键（Ctrl、Alt、Shift 或 Super）再加上主键，未录入。",
        consumed: false
      };
    }
    var value = mods.concat([main]).join("+");
    return {
      value: value,
      hint: layoutHint(ev, main),
      consumed: true
    };
  }

  V3.hotkey = {
    fromKeyboardEvent: fromKeyboardEvent,
    mainKeyFromCode: mainKeyFromCode,
    modifiersOf: modifiersOf
  };
})(window.V3 = window.V3 || {});
