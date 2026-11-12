/* 录音与选文件。OpenAPI 未枚举 from-audio MIME，只约束常见音频容器，拒绝不支持格式与默认随缘输出。 */
(function (root) {
  "use strict";

  var RECORDER_TYPES = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4"
  ];

  var PICK_ACCEPT = [
    "audio/webm",
    "audio/ogg",
    "audio/wav",
    "audio/wave",
    "audio/x-wav",
    "audio/mpeg",
    "audio/mp3",
    "audio/mp4",
    "audio/aac",
    "audio/flac",
    "audio/x-m4a",
    "audio/opus"
  ];

  var PICK_EXT = [".webm", ".ogg", ".wav", ".mp3", ".m4a", ".mp4", ".aac", ".flac", ".opus"];

  function supportedRecorderType(MediaRecorderImpl) {
    var MR = MediaRecorderImpl || (typeof MediaRecorder !== "undefined" ? MediaRecorder : null);
    if (!MR || typeof MR.isTypeSupported !== "function") return "";
    for (var i = 0; i < RECORDER_TYPES.length; i++) {
      try {
        if (MR.isTypeSupported(RECORDER_TYPES[i])) return RECORDER_TYPES[i];
      } catch (err) {}
    }
    return "";
  }

  function extensionForType(type) {
    var t = String(type || "").toLowerCase();
    if (t.indexOf("wav") >= 0) return "wav";
    if (t.indexOf("mpeg") >= 0 || t.indexOf("mp3") >= 0) return "mp3";
    if (t.indexOf("mp4") >= 0 || t.indexOf("m4a") >= 0 || t.indexOf("aac") >= 0) return "m4a";
    if (t.indexOf("ogg") >= 0) return "ogg";
    if (t.indexOf("flac") >= 0) return "flac";
    return "webm";
  }

  function filenameForType(type, fallback) {
    return (fallback || "recording") + "." + extensionForType(type);
  }

  function isAllowedPickFile(file) {
    if (!file) return false;
    var type = String(file.type || "").toLowerCase();
    var name = String(file.name || "").toLowerCase();
    if (type.indexOf("video/") === 0) return false;
    if (type) {
      for (var i = 0; i < PICK_ACCEPT.length; i++) {
        if (type === PICK_ACCEPT[i] || type.indexOf(PICK_ACCEPT[i]) === 0) return true;
      }
      if (type.indexOf("audio/") === 0) {
        return PICK_ACCEPT.indexOf(type) >= 0;
      }
      return false;
    }
    for (var j = 0; j < PICK_EXT.length; j++) {
      if (name.length >= PICK_EXT[j].length && name.slice(-PICK_EXT[j].length) === PICK_EXT[j]) return true;
    }
    return false;
  }

  function acceptAttribute() {
    return PICK_ACCEPT.concat(PICK_EXT).join(",");
  }

  function stopStream(stream) {
    if (!stream || typeof stream.getTracks !== "function") return;
    stream.getTracks().forEach(function (track) {
      try { track.stop(); } catch (err) {}
    });
  }

  function createAudioSession(deps) {
    var mediaDevices = (deps && deps.mediaDevices) || (typeof navigator !== "undefined" ? navigator.mediaDevices : null);
    var MediaRecorderImpl = (deps && deps.MediaRecorder) || (typeof MediaRecorder !== "undefined" ? MediaRecorder : null);
    var stream = null;
    var recorder = null;
    var chunks = [];
    var mimeType = "";
    var started = false;
    var liveGeneration = 0;
    var pendingGeneration = 0;
    var closed = false;

    function abandonLive() {
      liveGeneration += 1;
      pendingGeneration = 0;
      started = false;
      var rec = recorder;
      var s = stream;
      recorder = null;
      stream = null;
      chunks = [];
      if (rec) {
        try {
          if (rec.state && rec.state !== "inactive") rec.stop();
        } catch (err) {}
      }
      stopStream(s);
      return liveGeneration;
    }

    function isCurrent(gen) {
      return !closed && gen === liveGeneration;
    }

    function release() {
      abandonLive();
    }

    return {
      supportedType: function () { return supportedRecorderType(MediaRecorderImpl); },
      isRecording: function () { return started; },
      isAcquiring: function () { return pendingGeneration !== 0 && pendingGeneration === liveGeneration; },
      generation: function () { return liveGeneration; },
      release: release,
      start: function () {
        if (closed) {
          return Promise.reject(new Error("录音会话已关闭。"));
        }
        mimeType = supportedRecorderType(MediaRecorderImpl);
        if (!mimeType) {
          return Promise.reject(new Error("当前浏览器无法录制可上传的音频格式，请改用选择音频文件。"));
        }
        if (!mediaDevices || typeof mediaDevices.getUserMedia !== "function") {
          return Promise.reject(new Error("当前环境没有可用的麦克风接口。"));
        }
        var myGen = abandonLive();
        pendingGeneration = myGen;
        return Promise.resolve(mediaDevices.getUserMedia({ audio: true, video: false })).then(function (s) {
          if (!isCurrent(myGen)) {
            stopStream(s);
            return { cancelled: true };
          }
          stream = s;
          chunks = [];
          recorder = new MediaRecorderImpl(stream, { mimeType: mimeType });
          recorder.ondataavailable = function (ev) {
            if (ev && ev.data && ev.data.size) chunks.push(ev.data);
          };
          pendingGeneration = 0;
          started = true;
          recorder.start();
          return { mimeType: mimeType, generation: myGen };
        }, function (err) {
          if (!isCurrent(myGen)) {
            return { cancelled: true };
          }
          pendingGeneration = 0;
          started = false;
          stopStream(stream);
          stream = null;
          var name = err && err.name;
          if (name === "NotAllowedError" || name === "PermissionDeniedError") {
            return Promise.reject(new Error("麦克风权限被拒绝，无法录音。"));
          }
          if (name === "NotFoundError" || name === "DevicesNotFoundError") {
            return Promise.reject(new Error("未找到可用麦克风。"));
          }
          return Promise.reject(new Error((err && err.message) || "无法打开麦克风。"));
        });
      },
      stop: function () {
        return new Promise(function (resolve, reject) {
          if (!recorder || !started) {
            abandonLive();
            resolve(null);
            return;
          }
          var rec = recorder;
          var type = rec.mimeType || mimeType;
          var heldStream = stream;
          var heldChunks = chunks;
          var myGen = liveGeneration;
          rec.onerror = function (ev) {
            if (heldStream) stopStream(heldStream);
            if (isCurrent(myGen)) {
              started = false;
              recorder = null;
              stream = null;
              chunks = [];
            }
            reject(new Error((ev && ev.error && ev.error.message) || "录音失败"));
          };
          rec.onstop = function () {
            var blob = heldChunks.length ? new Blob(heldChunks, { type: type || "audio/webm" }) : null;
            stopStream(heldStream);
            if (isCurrent(myGen)) {
              stream = null;
              recorder = null;
              started = false;
              chunks = [];
            }
            resolve(blob ? { blob: blob, type: blob.type || type, filename: filenameForType(blob.type || type) } : null);
          };
          try { rec.stop(); } catch (err) {
            abandonLive();
            reject(err);
          }
        });
      },
      cancel: function () {
        abandonLive();
      },
      close: function () {
        closed = true;
        abandonLive();
      }
    };
  }

  root.RECORDER_TYPES = RECORDER_TYPES;
  root.PICK_ACCEPT = PICK_ACCEPT;
  root.supportedRecorderType = supportedRecorderType;
  root.filenameForType = filenameForType;
  root.isAllowedPickFile = isAllowedPickFile;
  root.acceptAttribute = acceptAttribute;
  root.stopStream = stopStream;
  root.createAudioSession = createAudioSession;
})(window.V3Business = window.V3Business || {});
