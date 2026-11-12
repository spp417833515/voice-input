/* 本机桌面领取任务：长轮询 timeout_s=25（服务端上限）。手机/局域网不得调用，避免抢走本机 claim。不自动 ack 造成功。 */
(function (root) {
  "use strict";

  var OP = {
    claim: "claim_api_v1_jobs_claim_post",
    ack: "ack_api_v1_jobs__job_id__ack_post"
  };
  var CLAIM_TIMEOUT_S = 25;

  function createClaimLoop(options) {
    var opts = options || {};
    var client = opts.client;
    var context = opts.context;
    var timeoutS = opts.timeoutS != null ? opts.timeoutS : CLAIM_TIMEOUT_S;
    var errorDelay = opts.errorDelay != null ? opts.errorDelay : 1000;
    var schedule = opts.setTimeout || (typeof setTimeout === "function" ? setTimeout : null);
    var unschedule = opts.clearTimeout || (typeof clearTimeout === "function" ? clearTimeout : null);
    var destroyed = false;
    var running = false;
    var generation = 0;
    var timer = null;

    function stopTimer() {
      if (timer != null && unschedule) unschedule(timer);
      timer = null;
    }

    function stop() {
      generation += 1;
      running = false;
      stopTimer();
    }

    function destroy() {
      destroyed = true;
      stop();
    }

    function later(fn, ms, gen) {
      stopTimer();
      if (!schedule) {
        fn();
        return;
      }
      timer = schedule(function () {
        timer = null;
        if (destroyed || gen !== generation || !running) return;
        fn();
      }, ms);
    }

    function tick(gen) {
      if (destroyed || gen !== generation || !running) return Promise.resolve();
      if (!context || !context.canCallLocal()) {
        stop();
        var err = {
          error: "forbidden",
          message: !context
            ? "缺少访问上下文"
            : (context.kind === "file"
              ? "file:// 不能作为成功路径。"
              : "领取任务仅本机桌面可调用，已阻止从当前环境发出请求。"),
          blocked: true
        };
        if (opts.onError) opts.onError(err);
        return Promise.resolve();
      }
      return client.call(OP.claim, { body: { timeout_s: timeoutS } }).then(function (claimed) {
        if (destroyed || gen !== generation || !running) return;
        if (claimed && claimed.job_id) {
          return Promise.resolve(opts.onClaimed ? opts.onClaimed(claimed) : null).then(function () {
            if (destroyed || gen !== generation || !running) return;
            return tick(gen);
          }, function (hookErr) {
            if (opts.onError) opts.onError(hookErr);
            if (destroyed || gen !== generation || !running) return;
            later(function () { tick(gen); }, errorDelay, gen);
          });
        }
        return tick(gen);
      }, function (err) {
        if (destroyed || gen !== generation || !running) return;
        if (opts.onError) opts.onError(err);
        later(function () { tick(gen); }, errorDelay, gen);
      });
    }

    function start() {
      if (destroyed) return;
      if (running) return;
      if (!context || !context.canCallLocal()) {
        var err = {
          error: "forbidden",
          message: !context
            ? "缺少访问上下文"
            : (context.kind === "file"
              ? "file:// 不能作为成功路径。"
              : "领取任务仅本机桌面可调用，已阻止从当前环境发出请求。"),
          blocked: true
        };
        if (opts.onError) opts.onError(err);
        return;
      }
      running = true;
      tick(generation);
    }

    return {
      start: start,
      stop: stop,
      destroy: destroy,
      isRunning: function () { return running && !destroyed; },
      timeoutS: timeoutS
    };
  }

  root.CLAIM_TIMEOUT_S = CLAIM_TIMEOUT_S;
  root.CLAIM_OPERATIONS = OP;
  root.createClaimLoop = createClaimLoop;
})(window.V3Business = window.V3Business || {});
