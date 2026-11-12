/* 客户端 job_id：同逻辑请求复用，手动确认或新一次提交换新 id。网络失败不自动换 id 重试。 */
(function (root) {
  "use strict";

  function randomId() {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
    var bytes = new Uint8Array(16);
    if (typeof crypto !== "undefined" && typeof crypto.getRandomValues === "function") {
      crypto.getRandomValues(bytes);
    } else {
      for (var i = 0; i < 16; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    bytes[6] = (bytes[6] & 0x0f) | 0x40;
    bytes[8] = (bytes[8] & 0x3f) | 0x80;
    var hex = [];
    for (var j = 0; j < 16; j++) hex.push((bytes[j] + 0x100).toString(16).slice(1));
    return (
      hex.slice(0, 4).join("") + "-" +
      hex.slice(4, 6).join("") + "-" +
      hex.slice(6, 8).join("") + "-" +
      hex.slice(8, 10).join("") + "-" +
      hex.slice(10, 16).join("")
    );
  }

  function createJobIds() {
    var slots = Object.create(null);
    return {
      fresh: function (slot) {
        var id = randomId();
        slots[slot] = id;
        return id;
      },
      reuse: function (slot) {
        if (!slots[slot]) return this.fresh(slot);
        return slots[slot];
      },
      peek: function (slot) { return slots[slot] || null; },
      clear: function (slot) { delete slots[slot]; }
    };
  }

  root.randomJobId = randomId;
  root.createJobIds = createJobIds;
})(window.V3Business = window.V3Business || {});
