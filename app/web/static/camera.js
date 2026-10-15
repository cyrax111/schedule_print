// Camera on/off, remembered by this browser. While on, the picture is reloaded every 2 s, but
// not while the tab is hidden, so the printer's camera can go back to sleep when nobody looks.
(function () {
  "use strict";
  var KEY = "camera", REFRESH_MS = 2000, RETRY_MS = 5000;
  var button = document.getElementById("camera-toggle");
  if (!button) return;
  var image = document.getElementById("camera-image");
  var note = document.getElementById("camera-note");
  var on = false, timer = null;

  function remembered() {
    try { return localStorage.getItem(KEY) === "on"; } catch (e) { return false; }
  }
  function remember(value) {
    try { localStorage.setItem(KEY, value ? "on" : "off"); } catch (e) { /* not kept */ }
  }
  function message(text) {
    note.textContent = text || "";
    note.hidden = !text;
  }
  function later(ms) {
    clearTimeout(timer);
    timer = setTimeout(load, ms);
  }
  function load() {
    if (on && !document.hidden) image.src = "/camera.jpg?t=" + Date.now();
  }

  image.addEventListener("load", function () {
    if (!on) return;
    message("");
    image.hidden = false;
    later(REFRESH_MS);
  });
  image.addEventListener("error", function () {
    if (!on) return;
    message("Camera not available.");
    image.hidden = true;
    later(RETRY_MS);
  });

  function show(value) {
    on = value;
    button.textContent = on ? "Camera off" : "Camera on";
    button.setAttribute("aria-pressed", String(on));
    clearTimeout(timer);
    if (on) {
      message("Starting the camera…");
      load();
    } else {
      message("");
      image.hidden = true;
      image.removeAttribute("src");
    }
  }

  button.addEventListener("click", function () {
    remember(!on);
    show(!on);
  });
  document.addEventListener("visibilitychange", function () {
    if (on && !document.hidden) load();
  });
  show(remembered());
})();
