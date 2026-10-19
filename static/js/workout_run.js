/* Workout run page: displays the counters and plays the rest-finished alarm.
   The server owns every timestamp. This script only counts from server-provided instants, using the difference between
   the server clock and this device's clock, so refreshes, throttled background tabs and wrong device clocks stay accurate. */
(function () {
  "use strict";

  var MUTE_KEY = "gym.workout.muted";
  var timer = document.getElementById("run-timer");
  var total = document.getElementById("run-total");
  var offset = 0;                       // serverNow - deviceNow
  if (timer && timer.dataset.serverNow) offset = Number(timer.dataset.serverNow) - Date.now();
  var now = function () { return Date.now() + offset; };

  function clock(seconds) {
    seconds = Math.max(0, Math.floor(seconds));
    var h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
    var mm = (m < 10 ? "0" : "") + m, ss = (s < 10 ? "0" : "") + s;
    return h ? h + ":" + mm + ":" + ss : mm + ":" + ss;
  }
  function stored(key) { try { return window.localStorage.getItem(key); } catch (e) { return null; } }
  function store(key, value) { try { window.localStorage.setItem(key, value); } catch (e) {} }

  // ---- sound: created lazily, resumed by the first user interaction (browsers block audio before that)
  var muted = stored(MUTE_KEY) === "1";
  var audio = null;
  function unlock() {
    try {
      var Ctx = window.AudioContext || window.webkitAudioContext;
      if (!Ctx) return;
      if (!audio) audio = new Ctx();
      if (audio.state === "suspended") audio.resume();
    } catch (e) {}
  }
  ["pointerdown", "keydown", "touchstart"].forEach(function (name) { document.addEventListener(name, unlock, { passive: true }); });
  unlock();

  function beeps() {
    if (muted || !audio || audio.state !== "running") return false;
    for (var i = 0; i < 3; i++) {
      var osc = audio.createOscillator(), gain = audio.createGain(), t = audio.currentTime + i * 0.28;
      osc.type = "sine"; osc.frequency.value = 880;
      gain.gain.setValueAtTime(0.0001, t); gain.gain.exponentialRampToValueAtTime(0.35, t + 0.02); gain.gain.exponentialRampToValueAtTime(0.0001, t + 0.2);
      osc.connect(gain); gain.connect(audio.destination); osc.start(t); osc.stop(t + 0.22);
    }
    return true;
  }

  var muteBtn = document.getElementById("mute-toggle");
  function paintMute() {
    if (!muteBtn) return;
    muteBtn.setAttribute("aria-pressed", muted ? "true" : "false");
    muteBtn.querySelector("span").textContent = muted ? "Sound off" : "Sound on";
    muteBtn.querySelector("i").className = "bi me-1 " + (muted ? "bi-volume-mute" : "bi-volume-up");
  }
  if (muteBtn) {
    muteBtn.addEventListener("click", function () { muted = !muted; store(MUTE_KEY, muted ? "1" : "0"); paintMute(); unlock(); });
    paintMute();
  }

  // ---- total workout time
  if (total) {
    var since = Number(total.dataset.since);
    var paintTotal = function () { total.textContent = clock((now() - since) / 1000); };
    paintTotal(); window.setInterval(paintTotal, 1000);
  }

  if (!timer) return;
  var text = document.getElementById("timer-text");
  var phase = timer.dataset.phase;

  if (phase === "working") {                      // counting up: time on the current set
    var started = Number(timer.dataset.since);
    var paintSet = function () { text.textContent = clock((now() - started) / 1000); };
    paintSet(); window.setInterval(paintSet, 250);
    return;
  }

  // ---- rest countdown (counting down to an absolute server-based deadline, never decremented tick by tick)
  var deadline = Number(timer.dataset.deadline);
  var startBtn = document.getElementById("start-set");
  var doneBox = document.getElementById("rest-done");
  var label = document.getElementById("timer-label");
  var finished = phase === "ready";               // already over when the page loaded: show it, but do not sound the alarm
  var originalTitle = document.title;

  function showReady() {
    if (startBtn) startBtn.disabled = false;
    if (doneBox) doneBox.classList.remove("d-none");
    if (label) label.textContent = "Rest finished";
    var badge = document.querySelector("#phase-badge .badge");
    if (badge) { badge.textContent = "Ready for next set"; badge.className = "badge text-bg-success"; }
    text.textContent = "00:00";
  }
  function tick() {
    var remaining = Math.ceil((deadline - now()) / 1000);
    if (remaining > 0) { text.textContent = clock(remaining); return; }
    showReady();
    if (!finished) {                              // exactly once, even if ticks or visibility events overlap
      finished = true;
      document.title = "Rest finished – " + originalTitle;
      if (!beeps() && navigator.vibrate) navigator.vibrate([200, 100, 200]);   // fallback: the banner above is always visible
      window.clearInterval(interval);
    }
  }
  if (startBtn && !finished) startBtn.disabled = true;   // the server also refuses an early start
  var interval = window.setInterval(tick, 250);
  document.addEventListener("visibilitychange", function () { if (!document.hidden) tick(); });
  tick();
})();
