"use strict";
/* Runs in <head> so the page never flashes the wrong theme. The choice is per browser. */
(function () {
  var t = "light";
  try { t = localStorage.getItem("openbrisket.theme") || "light"; } catch (e) { /* private window */ }
  if (t !== "light" && t !== "dark" && t !== "auto") t = "light";
  document.documentElement.setAttribute("data-theme", t);
})();
