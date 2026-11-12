"use strict";
/* OpenBrisket web page. No framework, no build step. All data arrives through /api with the access
   token; nothing here inserts data as HTML (every text node goes through textContent). */

const SVGNS = "http://www.w3.org/2000/svg";
const main = document.getElementById("main");
const tip = document.getElementById("tip");
let token = null;
try { token = localStorage.getItem("openbrisket.token"); } catch (e) { /* private window */ }
let timer = null;
let lastCurve = { id: null, at: 0, data: null };

// ---------------------------------------------------------------------------------------- helpers
function h(tag, attrs, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid === null || kid === undefined || kid === false) continue;
    el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
function s(tag, attrs, ...kids) {
  const el = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) el.setAttribute(k, v);
  for (const kid of kids.flat(Infinity)) if (kid !== null && kid !== undefined && kid !== false) el.append(kid.nodeType ? kid : document.createTextNode(String(kid)));
  return el;
}
const only = (...nodes) => nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false);
function show(...nodes) { main.replaceChildren(...only(nodes)); }
async function api(path, body) {
  const opt = { headers: { Authorization: "Bearer " + (token || "") } };
  if (body !== undefined) {
    opt.method = "POST";
    opt.headers["Content-Type"] = "application/json";
    opt.body = JSON.stringify(body);
  }
  const res = await fetch(path, opt);
  if (res.status === 401) { token = null; throw new Error("401"); }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(data.error || ("request failed (" + res.status + ")"));
  return data;
}
const deg = (v, digits = 0) => (v === null || v === undefined) ? "–" : v.toFixed(digits) + " °C";
const clock = (iso) => iso ? new Date(iso).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
const dayClock = (iso) => iso ? new Date(iso).toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" }) : "";
const fullDate = (iso) => iso ? new Date(iso).toLocaleString([], { day: "numeric", month: "short", year: "numeric", hour: "2-digit", minute: "2-digit" }) : "";
function dur(minutes) {
  minutes = Math.round(minutes);
  const sign = minutes < 0 ? "-" : "";
  minutes = Math.abs(minutes);
  return sign + (minutes >= 60 ? Math.floor(minutes / 60) + " h " + String(minutes % 60).padStart(2, "0") + " min" : minutes + " min");
}
function msg(text, kind) { return h("div", { class: "msg " + (kind || ""), role: kind === "err" ? "alert" : "status" }, text); }
function box(title, ...body) {
  return h("section", { class: "box" }, title ? h("header", {}, typeof title === "string" ? h("h2", {}, title) : title) : null, ...body);
}
function stateCell(state) {
  const sym = { pass: "✓", fail: "✕", pending: "", skip: "–" }[state] ?? "";
  const word = { pass: "passing", fail: "failing", pending: "pending", skip: "skipped" }[state] || state;
  return h("span", { class: "state " + state }, h("i", { "aria-hidden": "true" }, sym), word);
}
async function act(button, fn) {
  const holder = button.closest(".box, form") || main;
  holder.querySelectorAll(".msg").forEach((m) => m.remove());
  button.disabled = true;
  try {
    const text = await fn();
    if (text) holder.append(msg(text, "ok"));
  } catch (e) {
    if (e.message === "401") return route();
    holder.append(msg(e.message, "err"));
  } finally {
    button.disabled = false;
  }
}

// ------------------------------------------------------------------------------------------ chart
function niceStep(range, target) {
  const raw = range / target;
  const pow = Math.pow(10, Math.floor(Math.log10(raw)));
  for (const m of [1, 2, 2.5, 5, 10]) if (raw <= m * pow) return m * pow;
  return 10 * pow;
}
const cap = (t) => t ? t.charAt(0).toUpperCase() + t.slice(1) : t;
function seriesFor(opts) {
  // Pit is always the first colour; each probe in the meat takes the next one, in order, never reshuffled.
  const probes = (opts.probes && opts.probes.length ? opts.probes : [{ no: null, label: "Meat" }]).slice(0, 6);
  return [{ cls: "s0", name: "Pit", digits: 0, get: (p) => p.pit_c }].concat(probes.map((pr, i) => ({
    cls: "s" + (i + 1), name: cap(pr.label), digits: 1, meat: true,
    get: (p) => pr.no === null ? p.meat_c : (p.probes_c ? p.probes_c[pr.no] : null) ?? null })));
}
function drawChart(host, curve, opts) {
  host.replaceChildren();
  const series = seriesFor(opts);
  const pts = curve.map((p) => ({ ...p, t: new Date(p.at).getTime() })).filter((p) => series.some((q) => q.get(p) !== null && q.get(p) !== undefined));
  if (pts.length < 2) {
    host.append(h("div", { class: "empty" }, "The curve appears here once the meat is on and a few minutes have passed."));
    return;
  }
  const W = Math.max(320, host.clientWidth - 32), H = 280, FH = 84;
  const m = { l: 40, r: 92, t: 12, b: 24 };
  const t0 = pts[0].t;
  const proj = opts.projection;                     // {t, value} or null
  const t1 = Math.max(pts[pts.length - 1].t, proj ? proj.t : 0);
  const hi = Math.max(...pts.flatMap((p) => series.map((q) => q.get(p) ?? 0)), opts.pitTarget || 0, opts.meatTarget || 0);
  const step = niceStep(hi, 5);
  const yMax = Math.ceil((hi + 4) / step) * step;
  const x = (t) => m.l + (t - t0) / Math.max(1, t1 - t0) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - v / yMax) * (H - m.t - m.b);
  const svg = s("svg", { viewBox: `0 0 ${W} ${H}`, height: H, role: "img",
    "aria-label": "Pit and meat temperature over the cook. Values are in the table below." });

  for (let v = 0; v <= yMax; v += step) {
    svg.append(s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }),
      s("text", { class: "axis", x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, v));
  }
  const hours = (t1 - t0) / 3600000;
  const room = Math.max(2, Math.floor((W - m.l - m.r) / 58));      // labels that fit without touching
  const tickH = [0.5, 1, 2, 3, 4, 6, 12, 24].find((c) => hours / c <= room) || 24;
  const first = new Date(t0); first.setMinutes(0, 0, 0);
  for (let t = first.getTime(); t <= t1; t += tickH * 3600000) {
    if (t < t0) continue;
    svg.append(s("text", { class: "axis", x: x(t), y: H - 6, "text-anchor": "middle" },
      new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hourCycle: "h23" })));
  }
  if (opts.stall) {
    svg.append(s("rect", { class: "bandfill", x: m.l, width: W - m.l - m.r, y: y(opts.stall[1]), height: y(opts.stall[0]) - y(opts.stall[1]) }));
  }
  const refs = [];
  if (opts.pitTarget) refs.push([opts.pitTarget, "pit " + opts.pitTarget.toFixed(0)]);
  if (opts.meatTarget) refs.push([opts.meatTarget, "target " + opts.meatTarget.toFixed(0)]);
  for (const [v] of refs) svg.append(s("line", { class: "ref", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }));

  const ends = [];
  for (const q of series) {
    let d = "", pen = false, last = null;
    for (const p of pts) {
      const v = q.get(p);
      if (v === null || v === undefined) { pen = false; continue; }
      d += (pen ? "L" : "M") + x(p.t).toFixed(1) + " " + y(v).toFixed(1);
      pen = true; last = { t: p.t, v };
    }
    if (!last) continue;
    svg.append(s("path", { class: "line " + q.cls, d }));
    ends.push({ q, last, yy: y(last.v), xx: x(last.t), text: q.name + " " + last.v.toFixed(0) });
  }
  // Direct labels are sparing: the pit, and the meat probes when there are few; with many, only the coldest.
  const meatEnds = ends.filter((e) => e.q.meat);
  const coldest = meatEnds.reduce((a, b) => (a && a.last.v <= b.last.v ? a : b), null);
  if (coldest && proj && proj.t > coldest.last.t) {
    svg.append(s("line", { class: "line proj " + coldest.q.cls, x1: coldest.xx, y1: coldest.yy, x2: x(proj.t), y2: y(proj.value) }));
  }
  for (const e of ends) svg.append(s("circle", { class: "end " + e.q.cls, cx: e.xx, cy: e.yy, r: 4.5 }));
  const labelled = ends.filter((e) => !e.q.meat || meatEnds.length <= 3 || e === coldest).sort((a, b) => a.yy - b.yy);
  for (let i = 1; i < labelled.length; i++) if (labelled[i].yy - labelled[i - 1].yy < 13) labelled[i].yy = labelled[i - 1].yy + 13;
  for (const e of labelled) svg.append(s("text", { class: "end-label", x: Math.min(e.xx + 9, W - m.r + 6), y: e.yy + 4 }, e.text));
  for (const [v, label] of refs) {
    if (labelled.some((e) => Math.abs(e.yy - y(v)) < 12 && e.xx > W - m.r - 40)) continue;
    svg.append(s("text", { class: "ref-label", x: W - m.r + 6, y: y(v) + 4 }, label));
  }
  const cross = s("line", { class: "cross", y1: m.t, y2: H - m.b, visibility: "hidden" });
  svg.append(cross);

  // fan: its own small chart on its own scale (never a second axis on the temperature chart)
  const fanPts = pts.filter((p) => p.fan_pct !== null);
  let fsvg = null, fcross = null;
  if (fanPts.length > 1) {
    const fy = (v) => 8 + (1 - v / 100) * (FH - 8 - 14);
    fsvg = s("svg", { viewBox: `0 0 ${W} ${FH}`, height: FH, role: "img", "aria-label": "Fan output over the cook, in percent." });
    for (const v of [0, 50, 100]) {
      fsvg.append(s("line", { class: "grid", x1: m.l, x2: W - m.r, y1: fy(v), y2: fy(v) }),
        s("text", { class: "axis", x: m.l - 6, y: fy(v) + 4, "text-anchor": "end" }, v));
    }
    let d = "";
    fanPts.forEach((p, i) => { d += (i ? "L" : "M") + x(p.t).toFixed(1) + " " + fy(p.fan_pct).toFixed(1); });
    const lastF = fanPts[fanPts.length - 1];
    fsvg.append(s("path", { class: "fanfill", d: d + `L${x(lastF.t).toFixed(1)} ${fy(0)}L${x(fanPts[0].t).toFixed(1)} ${fy(0)}Z` }),
      s("path", { class: "line fan", d }), s("circle", { class: "end fan", cx: x(lastF.t), cy: fy(lastF.fan_pct), r: 4.5 }),
      s("text", { class: "end-label", x: x(lastF.t) + 9, y: fy(lastF.fan_pct) + 4 }, "Fan " + lastF.fan_pct.toFixed(0) + "%"));
    fcross = s("line", { class: "cross", y1: 8, y2: FH - 14, visibility: "hidden" });
    fsvg.append(fcross);
  }

  const move = (ev) => {
    const rect = svg.getBoundingClientRect();
    const px = (ev.clientX - rect.left) * (W / rect.width);
    const t = t0 + (px - m.l) / (W - m.l - m.r) * (t1 - t0);
    let best = pts[0];
    for (const p of pts) if (Math.abs(p.t - t) < Math.abs(best.t - t)) best = p;
    for (const c of [cross, fcross]) if (c) { c.setAttribute("x1", x(best.t)); c.setAttribute("x2", x(best.t)); c.setAttribute("visibility", "visible"); }
    const row = (cls, name, value) => h("div", { class: "r" }, h("span", { class: "sw " + cls }), name, h("span", { class: "num" }, value));
    tip.replaceChildren(...only(h("b", {}, dayClock(best.at)), series.map((q) => row(q.cls, q.name, deg(q.get(best) ?? null, q.digits))),
      best.fan_pct !== null ? row("fan", "Fan", best.fan_pct.toFixed(0) + "%") : null));
    tip.hidden = false;
    const tw = tip.offsetWidth;
    tip.style.left = Math.min(window.innerWidth - tw - 8, Math.max(8, ev.clientX + 14)) + "px";
    tip.style.top = Math.max(8, ev.clientY - 20) + "px";
  };
  const leave = () => { tip.hidden = true; for (const c of [cross, fcross]) if (c) c.setAttribute("visibility", "hidden"); };
  for (const el of [svg, fsvg]) if (el) { el.addEventListener("pointermove", move); el.addEventListener("pointerleave", leave); }
  host.append(svg);
  if (fsvg) host.append(fsvg);
}
function curveTable(curve, opts) {
  const series = seriesFor(opts);
  const rows = [];
  let next = 0;
  for (const p of curve) {
    const t = new Date(p.at).getTime();
    if (t >= next) { rows.push(p); next = t + 30 * 60000; }
  }
  if (curve.length && rows[rows.length - 1] !== curve[curve.length - 1]) rows.push(curve[curve.length - 1]);
  const cell = (v, digits) => (v === null || v === undefined) ? "–" : v.toFixed(digits);
  return h("details", {}, h("summary", {}, "Data table (every 30 minutes)"), h("div", { class: "scroll" },
    h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Time"), series.map((q) => h("th", {}, q.name + " °C")), h("th", {}, "Fan %"))),
      h("tbody", {}, rows.map((p) => h("tr", {}, h("td", {}, dayClock(p.at)), series.map((q) => h("td", {}, cell(q.get(p), q.digits))),
        h("td", {}, cell(p.fan_pct, 0))))))));
}
function chartBox(curve, opts, title) {
  const host = h("div", { class: "chart" });
  const series = seriesFor(opts);
  const legend = h("div", { class: "legend" }, series.map((q) => h("span", {}, h("span", { class: "sw " + q.cls }), q.name)),
    opts.stall ? h("span", {}, h("span", { class: "sw band" }), "Stall band") : null,
    curve.some((p) => p.fan_pct !== null) ? h("span", {}, h("span", { class: "sw fan" }), "Fan (lower chart, %)") : null);
  const el = box(title || "Temperature", legend, host, curve.length > 1 ? curveTable(curve, opts) : null);
  requestAnimationFrame(() => drawChart(host, curve, opts));
  el._redraw = () => drawChart(host, curve, opts);
  return el;
}
function eventLog(events) {
  const list = [...events].reverse();
  if (!list.length) return h("div", { class: "empty" }, "Nothing logged yet.");
  return h("ul", { class: "rows" }, list.map((e) => h("li", {}, h("span", { class: "time" }, dayClock(e.at)),
    h("span", { class: "msgtext" }, e.message), h("span", { class: "kind" }, e.event))));
}

// ------------------------------------------------------------------------------------------ views
function loginView() {
  const input = h("input", { type: "password", id: "tok", autocomplete: "off", required: true });
  const form = h("form", { class: "box", onsubmit: async (ev) => {
    ev.preventDefault();
    token = input.value.trim();
    try { await api("/api/status"); try { localStorage.setItem("openbrisket.token", token); } catch (e) { /* ignore */ } route(); }
    catch (e) { form.querySelectorAll(".msg").forEach((x) => x.remove()); form.querySelector(".body").append(msg("That token was not accepted.", "err")); }
  } }, h("header", {}, h("h2", {}, "Access token")), h("div", { class: "body" },
    h("p", { class: "sub" }, "OpenBrisket prints its access token when it starts (look at the container log). Paste it here once; this browser remembers it."),
    h("div", { class: "fields" }, h("div", {}, h("label", { for: "tok" }, "Token"), input)),
    h("div", { class: "actions" }, h("button", { class: "primary", type: "submit" }, "Open"))));
  show(form);
  input.focus();
}

function startForm(recipes, reading) {
  const valid = recipes.filter((r) => r.valid);
  const sel = h("select", { id: "f-recipe" }, valid.map((r) => h("option", { value: r.id }, `${r.name} v${r.version}`)));
  const weight = h("input", { id: "f-weight", type: "number", step: "0.1", min: "0.1", inputmode: "decimal", placeholder: "5.4" });
  const serve = h("input", { id: "f-serve", type: "datetime-local", required: true });
  const live = reading && reading.probes_c && reading.probes_c.length ? reading.probes_c : [null, null, null];
  const picks = live.map((temp, i) => {
    const no = i + 1;
    const box = h("input", { type: "checkbox", id: "f-probe-" + no, checked: no === 1 });
    const name = h("input", { type: "text", id: "f-probe-name-" + no, placeholder: "name, e.g. flat", "aria-label": `Name for probe ${no}`, maxlength: "24" });
    return { no, box, name, row: h("div", { class: "row" }, h("label", { for: "f-probe-" + no }, box, "Probe " + no),
      h("span", { class: "num muted" }, temp === null || temp === undefined ? "no reading" : deg(temp, 1)), name) };
  });
  const out = h("div", {});
  const body = () => {
    const chosen = picks.filter((p) => p.box.checked);
    const labels = {};
    for (const p of chosen) if (p.name.value.trim()) labels[p.no] = p.name.value.trim();
    return { recipe: sel.value, weight_kg: weight.value ? Number(weight.value) : null, serve_at: serve.value,
      probes: chosen.map((p) => p.no), probe_labels: labels };
  };
  const hint = h("p", { class: "hint" });
  const sync = () => {
    const r = valid.find((x) => x.id === sel.value);
    hint.textContent = r ? `${r.description} Pit ${r.pit_target_c} °C, meat to ${r.meat_target_c} °C. ` +
      (r.hours_fixed ? `About ${r.hours_fixed} h whatever the weight.` : `About ${r.hours_per_kg} h per kg.`) : "";
    weight.disabled = Boolean(r && r.hours_fixed);
  };
  sel.addEventListener("change", sync); sync();
  return box("Open a cook", h("div", { class: "body" },
    reading && reading.pit_c !== null ? h("p", { class: "sub" }, `Pit is at ${deg(reading.pit_c)} right now.`) : null,
    h("div", { class: "fields" },
      h("div", {}, h("label", { for: "f-recipe" }, "Recipe"), sel),
      h("div", {}, h("label", { for: "f-weight" }, "Raw weight, kg"), weight),
      h("div", {}, h("label", { for: "f-serve" }, "Serving time"), serve),
      h("div", { class: "field-wide" }, hint),
      h("div", { class: "field-wide" }, h("label", {}, "Probes in the meat"), h("div", { class: "probe-pick" }, picks.map((p) => p.row)),
        h("p", { class: "hint" }, "Tick every probe that goes in the meat. With several, the coldest one decides when it is done, so put one in the thickest part."))),
    h("div", { class: "actions" },
      h("button", { type: "button", onclick: (ev) => act(ev.target, async () => {
        const p = await api("/api/plan", body()); out.replaceChildren(h("pre", {}, p.text)); }) }, "Plan"),
      h("button", { class: "primary", type: "button", onclick: (ev) => act(ev.target, async () => {
        await api("/api/cooks", body()); await route(); }) }, "Apply")),
    out));
}

function cookView(st, detail) {
  const c = st.cook;
  const r = st.reading;
  const t = c.targets;
  const size = c.weight_kg ? `${c.weight_kg} kg` : "sized by time";
  const head = h("div", {}, h("div", { class: "title-row" }, h("h1", {}, h("b", {}, c.recipe), " ", h("span", { class: "muted" }, `v${c.recipe_version}`)),
    h("span", { class: "badge open" }, "Open"), h("span", { class: "badge" }, c.phase + (c.wrapped ? ", wrapped" : ""))),
    h("p", { class: "sub mono" }, `${c.id} · ${c.cut} · ${size}` + (c.serve_at ? ` · serving ${dayClock(c.serve_at)}` : "")));

  const sym = { done: "✓", active: "●", pending: "", failed: "✕", skipped: "–" };
  const pipe = h("ol", { class: "pipeline", "aria-label": "Stages of the cook" }, c.pipeline.map((p) =>
    h("li", { class: p.state }, h("span", { class: "dot", "aria-hidden": "true" }, sym[p.state]), h("span", { class: "name" }, p.title),
      h("span", { class: "when" }, p.at ? clock(p.at) : p.state === "active" ? "now" : p.state === "skipped" ? "not seen" : ""))));

  const est = c.estimate;
  const tiles = h("div", { class: "tiles" },
    h("div", { class: "tile" }, h("div", { class: "k" }, "Pit"), h("div", { class: "v" }, r && r.pit_c !== null ? r.pit_c.toFixed(0) : "–", h("small", {}, " °C")),
      h("div", { class: "d" }, `recipe ${t.pit_c} ± ${t.pit_tolerance_c}`)),
    h("div", { class: "tile" }, h("div", { class: "k" }, c.probes.length > 1 ? `Meat (coldest of ${c.probes.length})` : `Meat (${c.probes[0].label})`),
      h("div", { class: "v" }, c.meat_c !== null && c.meat_c !== undefined ? c.meat_c.toFixed(1) : "–", h("small", {}, " °C")),
      h("div", { class: "d" }, `target ${t.meat_c}`),
      c.probes.length > 1 ? h("ul", { class: "probes" }, c.probes.slice(0, 6).map((p, i) => h("li", {}, h("span", { class: "sw s" + (i + 1) }), cap(p.label),
        h("span", { class: "num" }, deg(p.temp_c, 1))))) : null),
    h("div", { class: "tile" }, h("div", { class: "k" }, "Fan"), h("div", { class: "v" }, r && r.fan_pct !== null ? r.fan_pct.toFixed(0) : "–", h("small", {}, " %")),
      h("div", { class: "d" }, r && r.pit_target_c ? `controller set to ${r.pit_target_c.toFixed(0)} °C` : "")),
    h("div", { class: "tile" }, h("div", { class: "k" }, "Off the pit"), h("div", { class: "v" }, est && est.off_at ? clock(est.off_at) : "–"),
      h("div", { class: "d" }, est && est.off_at ? (est.rest_available_min !== undefined ? `${dur(est.rest_available_min)} before serving` : "") +
        (est.data_weight ? `, ${Math.round(est.data_weight * 100)}% from this cook` : ", from the recipe") : "estimate after the meat is on")));

  const proj = est && est.off_at ? { t: new Date(est.off_at).getTime(), value: t.meat_c } : null;
  const chart = chartBox(detail.curve, { pitTarget: t.pit_c, meatTarget: t.meat_c, stall: [t.stall_from_c, t.stall_to_c], projection: proj,
    probes: c.probes.map((p) => ({ no: p.no, label: p.label })) });

  const checks = box("Checks", h("ul", { class: "rows" }, c.checks.map((k) => h("li", {}, stateCell(k.state), h("span", { class: "name" }, k.name), h("span", { class: "detail" }, k.detail)))));

  const asked = (c.asks || []).map((a) => h("li", {}, stateCell("pending"), h("span", { class: "name" }, a.check),
    h("span", { class: "detail" }, `photo wanted since ${clock(a.since)}`)));
  const seen = Object.entries(c.photo_checks || {}).map(([kind, k]) => h("li", {}, stateCell("pass"), h("span", { class: "name" }, kind),
    h("span", { class: "detail" }, Object.entries(k.data).map(([f, v]) => `${f.replace(/_/g, " ")} ${v}`).join(", ") +
      (k.verdict ? ` → ${k.verdict}` : "") + ` (${clock(k.at)}` + (k.by ? `, ${k.by}` : "") + ")")));
  const photos = box("Photo checks", asked.length || seen.length ? h("ul", { class: "rows" }, asked, seen) :
    h("div", { class: "empty" }, "Nothing asked for yet."),
    h("p", { class: "hint photo-hint" }, "Send the photo to your agent. It reads the picture and stores only the data points; the photo itself is never kept."));
  const note = h("input", { id: "f-note", type: "text", placeholder: "optional note", "aria-label": "Note" });
  const stepBtn = (step, label, primary) => h("button", { type: "button", class: primary ? "primary" : "", onclick: (ev) => act(ev.target, async () => {
    const res = await api("/api/log", { step, note: note.value }); note.value = ""; await route(); return res.logged; }) }, label);
  const smokeOut = h("div", {});
  const on = Boolean(c.meat_on_at), resting = c.phase === "resting";
  const actions = box("Log a step", h("div", { class: "body" },
    h("div", { class: "fields" }, h("div", { class: "field-wide" }, note)),
    h("div", { class: "actions" },
      !on ? h("button", { type: "button", onclick: (ev) => act(ev.target, async () => {
        const res = await api("/api/smoke-test", {}); smokeOut.replaceChildren(h("pre", {}, res.text)); await refresh(true); }) }, "Run smoke test") : null,
      !on ? stepBtn("meat-on", "Meat on", true) : null,
      on && !resting && !c.wrapped && t.wrap_at_c !== null ? stepBtn("wrap", "Wrap") : null,
      on && !resting ? stepBtn("spritz", "Spritz") : null,
      on && !resting ? stepBtn("rest", "Off, resting", true) : null,
      stepBtn("note", "Add note")),
    smokeOut));

  const score = h("input", { id: "f-score", type: "number", min: "0", max: "10", step: "0.5", inputmode: "decimal" });
  const cooked = h("input", { id: "f-cooked", type: "number", min: "0", step: "0.1", inputmode: "decimal" });
  const notes = h("input", { id: "f-notes", type: "text" });
  const close = box("Close the cook", h("div", { class: "body" },
    h("div", { class: "fields" }, h("div", {}, h("label", { for: "f-score" }, "Score, 0 to 10"), score),
      h("div", {}, h("label", { for: "f-cooked" }, "Cooked weight, kg"), cooked),
      h("div", { class: "field-wide" }, h("label", { for: "f-notes" }, "What to change next time"), notes)),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "primary", onclick: (ev) => act(ev.target, async () => {
        await api("/api/close", { score: score.value ? Number(score.value) : null, cooked_weight_kg: cooked.value ? Number(cooked.value) : null, notes: notes.value });
        location.hash = "#history"; }) }, "Merge cook"),
      h("button", { type: "button", class: "danger", onclick: (ev) => { if (confirm("Close this cook as abandoned? Its log is kept.")) act(ev.target, async () => {
        await api("/api/close", { abandon: true, notes: notes.value }); location.hash = "#history"; }); } }, "Abandon"))));

  const log = box("Commits", eventLog(detail.events));
  show(head, box(null, pipe), tiles, h("div", { class: "grid two" }, h("div", {}, chart, log), h("div", {}, checks, photos, actions, close)));
  main._chart = chart;
}

async function renderCook(force) {
  const st = await api("/api/status");
  const live = document.getElementById("live");
  live.hidden = false;
  live.textContent = st.reading && st.reading.pit_c !== null ? `pit ${st.reading.pit_c.toFixed(0)} °C` : (st.source_error ? "no readings" : "waiting for readings");
  if (!st.cook) {
    const recipes = await api("/api/recipes");
    if (!force && main.querySelector("#f-recipe")) return;      // do not wipe a form being filled in
    show(h("h1", {}, "No cook is open"),
      st.source_error ? msg("No readings: " + st.source_error + " Check Settings.", "err") : null,
      startForm(recipes, st.reading));
    return;
  }
  const now = Date.now();
  if (force || lastCurve.id !== st.cook.id || now - lastCurve.at > 60000) {
    lastCurve = { id: st.cook.id, at: now, data: await api("/api/cooks/" + encodeURIComponent(st.cook.id)) };
  }
  const active = document.activeElement;
  const typing = active && main.contains(active) && ["INPUT", "SELECT", "TEXTAREA"].includes(active.tagName);
  if (typing && !force) return;                                  // do not redraw under someone typing
  const keep = {};
  main.querySelectorAll("input[id]:not([type=checkbox])").forEach((i) => { keep[i.id] = i.value; });
  cookView(st, lastCurve.data);
  for (const [id, v] of Object.entries(keep)) { const el = document.getElementById(id); if (el && v) el.value = v; }
}

// Recipes and rubs: read them, and write your own. Everything saved here is a YAML file with a commit.
function field(id, label, value, extra) {
  extra = extra || {};
  const input = extra.options
    ? h("select", { id }, extra.options.map((o) => h("option", { value: o[0], selected: String(o[0]) === String(value ?? "") }, o[1])))
    : h("input", { id, type: extra.type || "text", value: value ?? "", placeholder: extra.placeholder || "", step: extra.step, min: extra.min, autocomplete: "off" });
  return h("div", extra.wide ? { class: "field-wide" } : {}, h("label", { for: id }, label), input, extra.hint ? h("p", { class: "hint" }, extra.hint) : null);
}
const fv = (id) => document.getElementById(id).value.trim();
const fnum = (id) => (fv(id) === "" ? null : Number(fv(id)));

function rubForm(rub) {
  rub = rub || { name: "", description: "", ingredients: [{ name: "", parts: "", kind: "" }, { name: "", parts: "", kind: "" }, { name: "", parts: "", kind: "" }] };
  const rows = h("div", { class: "ing-rows" });
  const addRow = (it) => {
    it = it || { name: "", parts: "", kind: "" };
    const row = h("div", { class: "ing" },
      h("input", { type: "text", class: "ing-name", value: it.name || "", placeholder: "ingredient", "aria-label": "Ingredient" }),
      h("input", { type: "number", class: "ing-parts", value: it.parts ?? "", placeholder: "parts", step: "0.25", min: "0", inputmode: "decimal", "aria-label": "Parts" }),
      h("select", { class: "ing-kind", "aria-label": "Kind" }, [["", "other"], ["salt", "salt"], ["sugar", "sugar"]].map((o) => h("option", { value: o[0], selected: (it.kind || "") === o[0] }, o[1]))),
      h("button", { type: "button", "aria-label": "Remove ingredient", onclick: () => row.remove() }, "Remove"));
    rows.append(row);
  };
  (rub.ingredients || []).forEach(addRow);
  return box(rub.name ? `Edit rub: ${rub.name}` : "New rub", h("div", { class: "body" },
    h("div", { class: "fields" }, field("r-name", "Name", rub.name, { placeholder: "coffee-chilli" }),
      field("r-desc", "Description", rub.description, { placeholder: "what it is for, anything to watch" })),
    h("label", { class: "block-label" }, "Ingredients, in parts"), rows,
    h("p", { class: "hint" }, "Parts are a ratio: 2 pepper to 1 salt. Mark salt and sugar so OpenBrisket can warn about scorching and double salting."),
    h("div", { class: "actions" },
      h("button", { type: "button", onclick: () => addRow() }, "Add ingredient"),
      h("button", { type: "button", class: "primary", onclick: (ev) => act(ev.target, async () => {
        const ingredients = [...rows.querySelectorAll(".ing")].map((r) => ({ name: r.querySelector(".ing-name").value.trim(),
          parts: r.querySelector(".ing-parts").value === "" ? "" : Number(r.querySelector(".ing-parts").value), kind: r.querySelector(".ing-kind").value }));
        await api("/api/rubs", { name: fv("r-name"), description: fv("r-desc"), ingredients });
        location.hash = "#recipes"; await route(); }) }, "Save rub"),
      h("button", { type: "button", onclick: () => { location.hash = "#recipes"; } }, "Cancel"))));
}

function recipeForm(r, rubs) {
  const fresh = !r;
  r = r || { name: "", version: "1.0.0", cut: "", description: "", pit_target_c: 121, pit_tolerance_c: 10, meat_target_c: 95, hours_per_kg: "", hours_fixed: "",
    fridge_out_min: 60, dry_brine_hours: "", stall_from_c: 65, stall_to_c: 77, wrap_at_c: "", wrap_material: "", rest_min_minutes: 30, rest_ideal_minutes: 60, rub: null };
  const rubOptions = [["", "no rub"]].concat(rubs.filter((x) => x.valid).map((x) => [x.id, x.name]));
  return box(fresh ? "New recipe" : `Edit recipe: ${r.name}`, h("div", { class: "body" },
    !fresh && !r.own ? msg("This is a built-in recipe. Saving makes your own copy, which is then used in its place.") : null,
    h("div", { class: "fields" },
      field("c-name", "Name", r.name, { placeholder: "beef-short-ribs" }),
      field("c-cut", "Cut", r.cut, { placeholder: "beef-short-ribs", hint: "Starts with the meat: beef-, pork-, lamb-, chicken-, turkey-, duck-, fish-, sausage-." }),
      field("c-desc", "Description", r.description, { wide: true }),
      field("c-rub", "Rub", r.rub ? r.rub.id : "", { options: rubOptions }),
      field("c-pit", "Pit temperature, °C", r.pit_target_c, { type: "number" }),
      field("c-tol", "Pit tolerance, ± °C", r.pit_tolerance_c, { type: "number" }),
      field("c-meat", "Meat target, °C", r.meat_target_c, { type: "number" }),
      field("c-perkg", "Hours per kg", r.hours_per_kg, { type: "number", step: "0.1", hint: "For cuts sized by weight. Leave empty if you fill in the next one." }),
      field("c-fixed", "Hours, whatever the weight", r.hours_fixed, { type: "number", step: "0.25", hint: "For cuts sized by time, like ribs." }),
      field("c-fridge", "Out of the fridge, minutes before", r.fridge_out_min, { type: "number" }),
      field("c-brine", "Dry brine, hours before", r.dry_brine_hours, { type: "number" }),
      field("c-stall1", "Stall from, °C", r.stall_from_c, { type: "number" }),
      field("c-stall2", "Stall to, °C", r.stall_to_c, { type: "number" }),
      field("c-wrap", "Wrap at, °C", r.wrap_at_c, { type: "number", hint: "Leave empty for an unwrapped cook." }),
      field("c-wrapm", "Wrap in", r.wrap_material, { placeholder: "butcher paper" }),
      field("c-rest1", "Rest, minimum minutes", r.rest_min_minutes, { type: "number" }),
      field("c-rest2", "Rest, ideal minutes", r.rest_ideal_minutes, { type: "number" }),
      fresh ? null : field("c-change", "What changed and why", "", { wide: true, placeholder: "less sugar, the bark burnt last time", hint: `Becomes the commit message. Now at version ${r.version}; a changed recipe gets the next number.` })),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "primary", onclick: (ev) => act(ev.target, async () => {
        const out = await api("/api/recipes", { name: fv("c-name"), version: r.version, cut: fv("c-cut"), description: fv("c-desc"), rub: fv("c-rub") || null,
          pit_target_c: fnum("c-pit"), pit_tolerance_c: fnum("c-tol"), meat_target_c: fnum("c-meat"), hours_per_kg: fnum("c-perkg"), hours_fixed: fnum("c-fixed"),
          fridge_out_min: fnum("c-fridge"), dry_brine_hours: fnum("c-brine"), stall_from_c: fnum("c-stall1"), stall_to_c: fnum("c-stall2"), wrap_at_c: fnum("c-wrap"),
          wrap_material: fv("c-wrapm"), rest_min_minutes: fnum("c-rest1"), rest_ideal_minutes: fnum("c-rest2"), change: fresh ? "" : fv("c-change") });
        if (out.warnings && out.warnings.length) { alert("Saved, with a warning:\n\n" + out.warnings.join("\n")); }
        location.hash = "#recipes"; await route(); }) }, "Save recipe"),
      h("button", { type: "button", onclick: () => { location.hash = "#recipes"; } }, "Cancel"))));
}

function historyButton(kind, id, holder) {
  return h("button", { type: "button", onclick: (ev) => act(ev.target, async () => {
    const lines = await api(`/api/history/${kind}/${encodeURIComponent(id)}`);
    holder.replaceChildren(h("pre", {}, lines.length ? lines.join("\n") : "No saved versions yet (built-in)."));
  }) }, "History");
}
function removeButton(kind, id) {
  return h("button", { type: "button", class: "danger", onclick: (ev) => { if (confirm(`Delete your ${kind} ${id}? It stays in the file history.`)) act(ev.target, async () => {
    await api("/api/delete", { kind, id }); await route(); }); } }, "Delete");
}
async function renderRecipes(arg) {
  const [recipes, rubs] = await Promise.all([api("/api/recipes"), api("/api/rubs")]);
  if (arg === "new-recipe") return show(recipeForm(null, rubs));
  if (arg === "new-rub") return show(rubForm(null));
  if (arg && arg.startsWith("edit-recipe:")) {
    const r = recipes.find((x) => x.id === arg.slice(12));
    return show(r && r.valid ? recipeForm(r, rubs) : msg("No such recipe.", "err"));
  }
  if (arg && arg.startsWith("edit-rub:")) {
    const r = rubs.find((x) => x.id === arg.slice(9));
    return show(r && r.valid ? [r.own ? null : msg("This is a built-in rub. Saving makes your own copy, which is then used in its place."), rubForm(r)] : msg("No such rub.", "err"));
  }
  const tag = (x) => h("span", { class: "badge" }, x.own ? "yours" : "built-in");
  show(h("div", { class: "title-row" }, h("h1", {}, "Recipes"),
      h("span", { class: "push" }), h("a", { class: "btn primary", href: "#recipes/new-recipe" }, "New recipe"), h("a", { class: "btn", href: "#recipes/new-rub" }, "New rub")),
    h("p", { class: "sub" }, "Recipes and rubs are YAML files. Yours are saved here with a commit for every change."),
    recipes.map((r) => { const hist = h("div", {}); return !r.valid ? box(r.id, h("div", { class: "body" }, msg(r.error, "err"), r.own ? h("div", { class: "actions" }, removeButton("recipe", r.id)) : null)) :
      box(h("div", { class: "title-row" }, h("h2", {}, r.name), h("span", { class: "badge" }, "v" + r.version), tag(r), h("span", { class: "muted mono" }, r.cut)),
        h("div", { class: "body" }, r.description ? h("p", { class: "sub" }, r.description) : null,
          r.warnings.map((w) => msg("Warning: " + w, "err")),
          h("dl", { class: "kv" },
            h("dt", {}, "Pit"), h("dd", {}, `${r.pit_target_c} °C ± ${r.pit_tolerance_c}`),
            h("dt", {}, "Meat"), h("dd", {}, `to ${r.meat_target_c} °C`),
            h("dt", {}, "Time"), h("dd", {}, r.hours_fixed ? `about ${r.hours_fixed} h` : `about ${r.hours_per_kg} h per kg`),
            h("dt", {}, "Wrap"), h("dd", {}, r.wrap_at_c ? `at ${r.wrap_at_c} °C` + (r.wrap_material ? `, ${r.wrap_material}` : "") : "not wrapped"),
            h("dt", {}, "Rest"), h("dd", {}, `${r.rest_min_minutes} to ${r.rest_ideal_minutes} min`),
            r.rub ? [h("dt", {}, "Rub"), h("dd", {}, `${r.rub.name}: ` + r.rub.ingredients.map((i) => `${i.parts} ${i.name}`).join(", ") +
              ` (${Math.round(r.rub.sugar * 100)}% sugar, ${Math.round(r.rub.salt * 100)}% salt)`)] : null),
          h("div", { class: "actions" }, h("a", { class: "btn", href: "#recipes/edit-recipe:" + encodeURIComponent(r.id) }, r.own ? "Edit" : "Customise"),
            historyButton("recipe", r.id, hist), r.own ? removeButton("recipe", r.id) : null), hist),
        h("details", {}, h("summary", {}, r.id + ".yaml"), h("pre", {}, r.source))); }),
    h("h1", { class: "section" }, "Rubs"),
    rubs.map((r) => { const hist = h("div", {}); return !r.valid ? box(r.id, h("div", { class: "body" }, msg(r.error, "err"))) :
      box(h("div", { class: "title-row" }, h("h2", {}, r.name), tag(r), h("span", { class: "muted" }, `${Math.round(r.sugar * 100)}% sugar, ${Math.round(r.salt * 100)}% salt`)),
        h("div", { class: "body" }, r.description ? h("p", { class: "sub" }, r.description) : null,
          h("div", { class: "scroll" }, h("table", { class: "ingredients" }, h("thead", {}, h("tr", {}, h("th", {}, "Ingredient"), h("th", {}, "Parts"), h("th", {}, "Share"))),
            h("tbody", {}, r.ingredients.map((i) => h("tr", {}, h("td", {}, i.name + (i.kind ? ` (${i.kind})` : "")), h("td", {}, i.parts),
              h("td", {}, Math.round(i.parts / r.ingredients.reduce((a, b) => a + b.parts, 0) * 100) + "%")))))),
          h("p", { class: "hint" }, r.used_by.length ? "Used by: " + r.used_by.join(", ") : "Not used by any recipe yet."),
          h("div", { class: "actions" }, h("a", { class: "btn", href: "#recipes/edit-rub:" + encodeURIComponent(r.id) }, r.own ? "Edit" : "Customise"),
            historyButton("rub", r.id, hist), r.own && !r.used_by.length ? removeButton("rub", r.id) : null), hist),
        h("details", {}, h("summary", {}, "rubs/" + r.id + ".yaml"), h("pre", {}, r.source))); }));
}

function cookGraph(cooks) {
  const counts = {};
  for (const c of cooks) { const d = (c.opened_at || "").slice(0, 10); if (d) counts[d] = (counts[d] || 0) + 1; }
  const today = new Date(); today.setHours(12, 0, 0, 0);
  const start = new Date(today); start.setDate(start.getDate() - 52 * 7 - today.getDay());
  const cells = [];
  for (const d = new Date(start); d <= today; d.setDate(d.getDate() + 1)) {
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
    const n = counts[key] || 0;
    cells.push(h("i", { class: n ? "l" + Math.min(n, 3) : "", title: `${d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })}: ${n} cook${n === 1 ? "" : "s"}` }));
  }
  const total = cooks.length;
  return box(`${total} cook${total === 1 ? "" : "s"} in the log`, h("div", { class: "heat", role: "img", "aria-label": `Cooks per day over the last year, ${total} in total` }, cells),
    h("div", { class: "heat-key" }, "Fewer", h("i", {}), h("i", { class: "l1" }), h("i", { class: "l2" }), h("i", { class: "l3" }), "More"));
}
function analysisBox(a, curve) {
  if (!a || !a.meat_on_at) return null;
  const hrs = (v) => (v === null || v === undefined) ? "–" : dur(v * 60);
  const amb = curve.map((p) => p.ambient_c).filter((v) => v !== null && v !== undefined);
  const hum = curve.map((p) => p.humidity_pct).filter((v) => v !== null && v !== undefined);
  const avg = (xs) => xs.reduce((x, y) => x + y, 0) / xs.length;
  const rows = [
    ["Time on the pit", hrs(a.hours_on_pit) + (a.planned_hours ? ` (recipe expected ${hrs(a.planned_hours)})` : "")],
    ["Reached target after", hrs(a.hours_to_target)],
    ["Stall", a.stall && a.stall.start_h !== null ? `began at ${hrs(a.stall.start_h)}` + (a.stall.hours ? `, lasted ${hrs(a.stall.hours)}` : ", did not finish") : "none seen"],
    ["Wrapped at", a.wrapped_at_h !== null && a.wrapped_at_h !== undefined ? hrs(a.wrapped_at_h) : "not wrapped"],
    a.pit ? ["Pit", `averaged ${a.pit.mean_c} °C (recipe ${a.pit.recipe_c}), from ${a.pit.min_c} to ${a.pit.max_c}, varying ±${a.pit.stdev_c}`] : null,
    a.model ? ["Heating rate", a.model.data_weight ? `${Math.round(a.model.k_ratio * 100)}% of what the recipe assumes` : "not enough readings to tell"] : null,
    a.model && a.stall && a.stall.start_h !== null ? ["Stall slowdown", `meat heated at ${Math.round((a.wrapped_at_h !== null && a.wrapped_at_h !== undefined ? a.model.wrapped_factor : a.model.stall_factor) * 100)}% of its normal rate`] : null,
    amb.length ? ["Outdoors", `${avg(amb).toFixed(0)} °C` + (hum.length ? `, ${avg(hum).toFixed(0)}% humidity` : "")] : null,
    a.estimate ? ["Estimate error", ["at_25pct", "at_50pct", "at_75pct"].filter((k) => a.estimate[k]).map((k) =>
      `${k.slice(3, 5)}% through: ${a.estimate[k].error_h > 0 ? "+" : ""}${dur(a.estimate[k].error_h * 60)}`).join(", ") || "–"] : null,
    a.events && Object.keys(a.events).length ? ["Events", Object.entries(a.events).map(([k, n]) => `${k} ×${n}`).join(", ")] : null,
  ].filter(Boolean);
  return box("What this cook showed", h("div", { class: "body" }, h("dl", { class: "kv" }, rows.map(([k, v]) => [h("dt", {}, k), h("dd", {}, v)]))));
}
async function renderHistory(id) {
  if (id) {
    const d = await api("/api/cooks/" + encodeURIComponent(id));
    const m = d.meta, res = m.result || {};
    show(h("p", { class: "sub" }, h("a", { href: "#history" }, "← History")),
      h("div", { class: "title-row" }, h("h1", {}, h("b", {}, m.id)), h("span", { class: "badge " + m.status }, m.status === "done" ? "Merged" : m.status === "open" ? "Open" : "Abandoned")),
      h("p", { class: "sub mono" }, `${m.recipe_name} v${m.recipe_version}` + (m.weight_kg ? ` · ${m.weight_kg} kg` : "") + ` · opened ${fullDate(m.opened_at)}`),
      box("Result", h("div", { class: "body" }, h("dl", { class: "kv" },
        h("dt", {}, "Score"), h("dd", {}, res.score !== undefined ? `${res.score} / 10` : "–"),
        h("dt", {}, "Yield"), h("dd", {}, res.yield ? `${Math.round(res.yield * 100)}% (${res.cooked_weight_kg} kg cooked)` : "–"),
        h("dt", {}, "Notes"), h("dd", {}, res.notes || "–"),
        h("dt", {}, "Closed"), h("dd", {}, m.closed_at ? fullDate(m.closed_at) : "–")))),
      analysisBox(m.analysis, d.curve),
      chartBox(d.curve, { probes: m.probes }, "Temperature"), box(`Commits (${d.events.length})`, eventLog(d.events)));
    return;
  }
  const cooks = await api("/api/cooks");
  show(h("h1", {}, "History"), cookGraph(cooks),
    box("Cooks", cooks.length ? h("div", { class: "list" }, cooks.map((c) => h("a", { href: "#history/" + encodeURIComponent(c.id) },
      h("span", { class: "badge " + c.status }, c.status === "done" ? "Merged" : c.status === "open" ? "Open" : "Abandoned"),
      h("span", { class: "id" }, c.id),
      h("span", { class: "meta" }, [c.weight_kg ? c.weight_kg + " kg" : null, c.result.score !== undefined ? c.result.score + " / 10" : null,
        c.result.yield ? Math.round(c.result.yield * 100) + "% yield" : null].filter(Boolean).join(" · "))))) :
      h("div", { class: "empty" }, "No cooks yet. Open one from the Cook tab.")));
}

async function renderSettings() {
  const v = await api("/api/settings");
  const e = v.source.entities || {};
  const f = (id, label, value, extra) => h("div", extra && extra.wide ? { class: "field-wide" } : {}, h("label", { for: id }, label),
    h("input", { id, type: (extra && extra.type) || "text", value: value || "", placeholder: (extra && extra.placeholder) || "", autocomplete: "off" }));
  const val = (id) => document.getElementById(id).value.trim();
  const save = (ev) => act(ev.target, async () => {
    await api("/api/settings", {
      source: { type: "homeassistant", url: val("s-url"), token: val("s-token"),
        entities: { pit: val("s-pit"), probes: val("s-probes").split(/\s+/).filter(Boolean).slice(0, 9), fan: val("s-fan"), fan_on: val("s-fanon"), pit_target: val("s-target"), connected: val("s-conn"), ambient: val("s-amb"), humidity: val("s-hum") } },
      telegram: { bot_token: val("s-bot"), chat_id: val("s-chat") }, poll_seconds: Number(val("s-poll")) || 15 });
    return "Saved.";
  });
  show(h("h1", {}, "Settings"),
    box("Readings: Home Assistant", h("div", { class: "body" }, h("div", { class: "fields" },
      f("s-url", "Home Assistant address", v.source.url, { placeholder: "http://homeassistant.local:8123" }),
      f("s-token", "Long-lived access token", "", { type: "password", placeholder: v.source.token_set ? "saved; leave empty to keep" : "not set" }),
      f("s-pit", "Pit temperature entity", e.pit), f("s-conn", "Connected entity", e.connected),
      h("div", { class: "field-wide" }, h("label", { for: "s-probes" }, "Food probe entities, one per line (up to 9)"),
        h("textarea", { id: "s-probes", rows: "6", spellcheck: "false" }, (e.probes || []).join("\n")),
        h("p", { class: "hint" }, "The order here is the probe number: the first line is probe 1.")),
      f("s-fan", "Fan output entity", e.fan), f("s-fanon", "Fan switched on entity", e.fan_on), f("s-target", "Pit target entity", e.pit_target),
      f("s-amb", "Outdoor temperature entity (optional)", e.ambient), f("s-hum", "Outdoor humidity entity (optional)", e.humidity),
      f("s-poll", "Seconds between readings", v.poll_seconds, { type: "number" })),
      h("div", { class: "actions" }, h("button", { class: "primary", type: "button", onclick: save }, "Save"),
        h("button", { type: "button", onclick: (ev) => act(ev.target, async () => {
          const r = await api("/api/settings/test-source", {}); if (!r.ok) throw new Error(r.error);
          return `Reading: pit ${deg(r.reading.pit_c)}, probes ${r.reading.probes_c.map((p) => deg(p, 1)).join(", ")}, fan ${r.reading.fan_pct === null ? "–" : r.reading.fan_pct + "%"}.`; }) }, "Test saved settings")))),
    box("Notes: Telegram", h("div", { class: "body" }, h("p", { class: "sub" }, "Without a bot, notes are only written to the log."), h("div", { class: "fields" },
      f("s-bot", "Bot token", "", { type: "password", placeholder: v.telegram.bot_token_set ? "saved; leave empty to keep" : "not set" }), f("s-chat", "Chat id", v.telegram.chat_id)),
      h("div", { class: "actions" }, h("button", { class: "primary", type: "button", onclick: save }, "Save"),
        h("button", { type: "button", onclick: (ev) => act(ev.target, async () => { const r = await api("/api/settings/test-notify", {}); if (!r.ok) throw new Error(r.error); return "Test note sent."; }) }, "Send a test note")))),
    box("Agents", h("div", { class: "body" }, h("p", { class: "sub" }, "An agent can follow and run a cook through the MCP endpoint or the JSON API, with this token."),
      h("dl", { class: "kv" }, h("dt", {}, "MCP endpoint"), h("dd", { class: "mono" }, location.origin + "/mcp"),
        h("dt", {}, "API"), h("dd", { class: "mono" }, location.origin + "/api/status"),
        h("dt", {}, "Access token"), h("dd", {}, h("details", {}, h("summary", {}, "Show"), h("span", { class: "mono" }, v.access_token))),
        h("dt", {}, "Cook log"), h("dd", { class: "mono" }, v.cooks_path)),
      h("div", { class: "actions" }, h("button", { type: "button", onclick: () => { try { localStorage.removeItem("openbrisket.token"); } catch (e) { /* ignore */ } token = null; route(); } }, "Forget the token in this browser")))));
}

// ----------------------------------------------------------------------------------------- router
async function refresh(force) {
  const hash = location.hash.slice(1) || "cook";
  const cut = hash.indexOf("/");
  const tab = cut < 0 ? hash : hash.slice(0, cut), arg = cut < 0 ? null : hash.slice(cut + 1);
  try {
    if (tab === "recipes") await renderRecipes(arg ? decodeURIComponent(arg) : null);
    else if (tab === "history") await renderHistory(arg ? decodeURIComponent(arg) : null);
    else if (tab === "settings") await renderSettings();
    else await renderCook(force);
  } catch (e) {
    if (e.message === "401") return loginView();
    if (force) show(msg(e.message, "err"));
  }
}
async function route() {
  clearInterval(timer);
  tip.hidden = true;
  const tab = (location.hash.slice(1) || "cook").split("/")[0];
  document.querySelectorAll("#tabs a").forEach((a) => { if (a.dataset.tab === tab) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  if (!token) return loginView();
  await refresh(true);
  if (tab === "cook") timer = setInterval(() => refresh(false), 10000);
}
function setTheme(t) {
  document.documentElement.setAttribute("data-theme", t);
  try { localStorage.setItem("openbrisket.theme", t); } catch (e) { /* private window */ }
  document.querySelectorAll("#theme button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.themeSet === t)));
}
document.querySelectorAll("#theme button").forEach((b) => b.addEventListener("click", () => setTheme(b.dataset.themeSet)));
setTheme(document.documentElement.getAttribute("data-theme") || "light");
window.addEventListener("hashchange", route);
let resizeT = null;
window.addEventListener("resize", () => { clearTimeout(resizeT); resizeT = setTimeout(() => { main.querySelectorAll(".box").forEach((b) => b._redraw && b._redraw()); }, 150); });
route();
