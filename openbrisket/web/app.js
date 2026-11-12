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
  const probes = (opts.noMeat ? [] : opts.probes && opts.probes.length ? opts.probes : [{ no: null, label: "Meat" }]).slice(0, 6);
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
  if (opts.floor) refs.push([opts.floor, "hold floor " + opts.floor.toFixed(0)]);
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

function startForm(recipes, reading, sources) {
  const valid = recipes.filter((r) => r.valid);
  const sel = h("select", { id: "f-recipe" }, valid.map((r) => h("option", { value: r.id }, `${r.name} v${r.version}`)));
  const weight = h("input", { id: "f-weight", type: "number", step: "0.1", min: "0.1", inputmode: "decimal", placeholder: "5.4" });
  const serve = h("input", { id: "f-serve", type: "datetime-local", required: true });
  const live = reading && reading.probes_c && reading.probes_c.length ? reading.probes_c : [null, null, null];
  const picks = live.map((temp, i) => {
    const no = i + 1;
    const box = h("input", { type: "checkbox", id: "f-probe-" + no });
    const name = h("input", { type: "text", id: "f-probe-name-" + no, placeholder: "name, e.g. flat", "aria-label": `Name for probe ${no}`, maxlength: "24" });
    const src = (sources || []).find((x) => x.no === no);
    return { no, box, name, row: h("div", { class: "row" }, h("label", { for: "f-probe-" + no, title: src ? src.entity : "" }, box, "Probe " + no,
        src ? h("span", { class: "muted src" }, src.name) : null),
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
  const probeHint = h("p", { class: "hint" });
  const sync = () => {
    const r = valid.find((x) => x.id === sel.value);
    hint.textContent = r ? `${r.description} Pit ${r.pit_target_c} °C` + (r.meat_target_c !== null ? `, meat to ${r.meat_target_c} °C` : "") +
      `. ${cap(timeText(r))}. Done by ${DONE_TEXT[r.done_by]}.` : "";
    weight.disabled = Boolean(r && !r.hours_per_kg);
    // which probes start ticked follows the recipe: a clock-run recipe needs none
    picks.forEach((p) => { p.box.checked = Boolean(r) && r.probe !== "none" && p.no === 1; });
    probeHint.textContent = r && r.probe === "none" ? "This recipe runs by the clock, so no probe is needed. Tick one only if you want its temperature drawn."
      : "Tick every probe that goes in the food. With several, the coldest one decides when it is done, so put one in the thickest part.";
  };
  sel.addEventListener("change", sync); sync();
  return box("Open a cook", h("div", { class: "body" },
    reading && reading.pit_c !== null ? h("p", { class: "sub" }, `Pit is at ${deg(reading.pit_c)} right now.`) : null,
    h("div", { class: "fields" },
      h("div", {}, h("label", { for: "f-recipe" }, "Recipe"), sel),
      h("div", {}, h("label", { for: "f-weight" }, "Raw weight, kg"), weight),
      h("div", {}, h("label", { for: "f-serve" }, "Serving time"), serve),
      h("div", { class: "field-wide" }, hint),
      h("div", { class: "field-wide" }, h("label", {}, "Probes in the food"), h("div", { class: "probe-pick" }, picks.map((p) => p.row)),
        probeHint)),
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
    h("span", { class: "badge open" }, "Open"), h("span", { class: "badge" }, c.hold ? "resting / holding" : c.phase + (c.wrapped ? ", wrapped" : "")), h("span", { class: "badge" }, c.style)),
    h("p", { class: "sub mono" }, `${c.id} · ${c.cut} · ${size}` + (c.serve_at ? ` · serving ${dayClock(c.serve_at)}` : "")));

  const sym = { done: "✓", active: "●", pending: "", failed: "✕", skipped: "–" };
  const pipe = h("ol", { class: "pipeline", "aria-label": "Stages of the cook" }, c.pipeline.map((p) =>
    h("li", { class: p.state }, h("span", { class: "dot", "aria-hidden": "true" }, sym[p.state]), h("span", { class: "name" }, p.title),
      h("span", { class: "when" }, p.at ? clock(p.at) : p.state === "active" ? "now" : p.state === "skipped" ? "not seen" : ""))));

  const est = c.estimate;
  const tiles = h("div", { class: "tiles" },
    h("div", { class: "tile" }, h("div", { class: "k" }, c.hold ? "Pit probe (holding box)" : "Pit"), h("div", { class: "v" }, r && r.pit_c !== null ? r.pit_c.toFixed(0) : "–", h("small", {}, " °C")),
      h("div", { class: "d" }, c.hold ? "where the pit probe sits now" : `recipe ${t.pit_c} ± ${t.pit_tolerance_c}`)),
    !c.probes.length ? h("div", { class: "tile" }, h("div", { class: "k" }, "Food"), h("div", { class: "v" }, "by the clock"),
      h("div", { class: "d" }, "no probe in the food" + (t.meat_c !== null ? `; check it is at least ${t.meat_c} °C before serving` : ""))) :
    h("div", { class: "tile" }, h("div", { class: "k" }, c.probes.filter((p) => p.in_meat).length > 1 ? `Meat (coldest of ${c.probes.filter((p) => p.in_meat).length})` : `Meat (${(c.probes.find((p) => p.in_meat) || c.probes[0]).label})`),
      h("div", { class: "v" }, c.meat_c !== null && c.meat_c !== undefined ? c.meat_c.toFixed(1) : "–", h("small", {}, " °C")),
      h("div", { class: "d" }, c.hold ? `holding, floor ${c.hold.floor_c} °C` : t.off_c === null ? "no target, the clock decides" :
        (c.done_by === "temperature" || c.done_by === "feel" ? `target ${t.off_c}` : `guide ${t.off_c}`) + (t.off_c !== t.meat_c && t.meat_c !== null ? ` (carries over to ${t.meat_c})` : "")),
      c.meat_on_at || c.probes.length > 1 ? h("ul", { class: "probes" }, c.probes.slice(0, 6).map((p, i) => h("li", { class: p.in_meat ? "" : "pulled", title: p.source ? `Probe ${p.no}: ${p.source}` : `Probe ${p.no}` }, h("span", { class: "sw s" + (i + 1) }),
        h("span", {}, cap(p.label), p.source ? h("span", { class: "muted src" }, p.source) : null),
        h("span", { class: "num" }, deg(p.temp_c, 1)),
        c.meat_on_at ? h("button", { type: "button", class: "mini", title: p.in_meat ? "Stop counting this probe (it is out of the meat)" : "Count this probe again (it is back in the meat)",
          onclick: (ev) => act(ev.target, async () => { await api("/api/probe", { probe: p.no, in_meat: !p.in_meat }); await refresh(true); }) }, p.in_meat ? "out" : "back in") : null))) : null,
      c.probes.some((p) => !p.in_meat) ? h("div", { class: "d" }, "Greyed probes are out of the meat and not counted.") : null),
    h("div", { class: "tile" }, h("div", { class: "k" }, "Fan"), h("div", { class: "v" }, r && r.fan_pct !== null ? r.fan_pct.toFixed(0) : "–", h("small", {}, " %")),
      h("div", { class: "d" }, r && r.pit_target_c ? `controller set to ${r.pit_target_c.toFixed(0)} °C` : "")),
    c.hold ? h("div", { class: "tile" }, h("div", { class: "k" }, "Hold"), h("div", { class: "v" }, dur(c.hold.hours * 60)),
      h("div", { class: "d" }, [c.hold.start_c !== null && c.hold.start_c !== undefined ? `from ${c.hold.start_c.toFixed(0)} °C` : null,
        c.hold.rate_c_per_h !== null ? `${c.hold.rate_c_per_h < 0 ? "falling" : "rising"} ${Math.abs(c.hold.rate_c_per_h).toFixed(1)} °C an hour` : null,
        c.hold.hours_until_floor ? `about ${dur(c.hold.hours_until_floor * 60)} until the ${c.hold.floor_c} °C floor` : `floor ${c.hold.floor_c} °C`,
        c.hold.minutes_below_floor ? `${dur(c.hold.minutes_below_floor)} below the floor` : null].filter(Boolean).join(", "))) :
    h("div", { class: "tile" }, h("div", { class: "k" }, "Off the pit"), h("div", { class: "v" }, est && est.off_at ? clock(est.off_at) : "–"),
      h("div", { class: "d" }, est && est.off_at ? (est.rest_available_min !== undefined ? `${dur(est.rest_available_min)} before serving` : "") +
        (c.done_by === "time" || c.done_by === "bend" ? ", by the clock" : est.data_weight ? `, ${Math.round(est.data_weight * 100)}% from this cook` : ", from the recipe") : "estimate after the meat is on")));

  const proj = est && est.off_at && t.off_c !== null ? { t: new Date(est.off_at).getTime(), value: t.off_c } : null;
  const chart = chartBox(detail.curve, { pitTarget: t.pit_c, meatTarget: c.probes.length ? t.off_c : null, stall: t.stall ? [t.stall_from_c, t.stall_to_c] : null, projection: c.probes.length && (c.done_by === "feel" || c.done_by === "temperature") ? proj : null, noMeat: !c.probes.length, floor: c.hold ? c.hold.floor_c : null,
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
      on && !resting && c.rests ? stepBtn("rest", "Off, resting / holding", true) : null,
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
      startForm(recipes, st.reading, st.probe_sources));
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
    rub.name ? h("div", { class: "fields block-label" }, field("r-change", "What changed and why", "", { wide: true, placeholder: "bark burnt, halve the sugar",
      hint: `Becomes the commit message. Now at version ${rub.version || "1.0.0"}; a changed rub gets the next number.` })) : null,
    h("div", { class: "actions" },
      h("button", { type: "button", onclick: () => addRow() }, "Add ingredient"),
      h("button", { type: "button", class: "primary", onclick: (ev) => act(ev.target, async () => {
        const ingredients = [...rows.querySelectorAll(".ing")].map((r) => ({ name: r.querySelector(".ing-name").value.trim(),
          parts: r.querySelector(".ing-parts").value === "" ? "" : Number(r.querySelector(".ing-parts").value), kind: r.querySelector(".ing-kind").value }));
        await api("/api/rubs", { name: fv("r-name"), description: fv("r-desc"), ingredients, change: document.getElementById("r-change") ? fv("r-change") : "" });
        location.hash = "#recipes"; await route(); }) }, "Save rub"),
      h("button", { type: "button", onclick: () => { location.hash = "#recipes"; } }, "Cancel"))));
}

const timeText = (r) => r.stages && r.stages.length ? "stages: " + r.stages.map((x) => `${x.name} ${x.hours} h`).join(", ")
  : r.hours_fixed ? `about ${r.hours_fixed} h` : `about ${r.hours_per_kg} h per kg`;
const DONE_TEXT = { feel: "feel (probe tender)", temperature: "temperature", bend: "the bend test", time: "the clock" };

function recipeForm(r, rubs, styles) {
  const fresh = !r;
  r = r || { name: "", version: "1.0.0", cut: "", description: "", style: "low-and-slow", done_by: "feel", probe: "needed", stall: true, pit_target_c: 121, pit_tolerance_c: 10,
    meat_target_c: 95, pull_c: "", hours_per_kg: "", hours_fixed: "", stages: [], finish: null, fridge_out_min: 60, dry_brine_hours: "", stall_from_c: 65, stall_to_c: 77,
    wrap_at_c: "", wrap_material: "", rest_min_minutes: 30, rest_ideal_minutes: 60, rub: null };
  const fin = r.finish || {};
  const rubOptions = [["", "no rub"]].concat(rubs.filter((x) => x.valid).map((x) => [x.id, x.name]));
  const group = (title, hint, ...kids) => h("fieldset", { class: "group" }, h("legend", {}, title), hint ? h("p", { class: "hint" }, hint) : null, h("div", { class: "fields" }, kids));

  const stageRows = h("div", { class: "ing-rows" });
  const addStage = (st) => {
    st = st || { name: "", hours: "", note: "" };
    const row = h("div", { class: "ing stage" },
      h("input", { type: "text", class: "st-name", value: st.name || "", placeholder: "stage, e.g. wrapped", "aria-label": "Stage" }),
      h("input", { type: "number", class: "st-hours", value: st.hours ?? "", placeholder: "hours", step: "0.25", min: "0", inputmode: "decimal", "aria-label": "Hours" }),
      h("input", { type: "text", class: "st-note", value: st.note || "", placeholder: "what to do at the start of it", "aria-label": "Note" }),
      h("button", { type: "button", "aria-label": "Remove stage", onclick: () => row.remove() }, "Remove"));
    stageRows.append(row);
  };
  (r.stages || []).forEach(addStage);

  const stallBox = h("input", { type: "checkbox", id: "c-stall", checked: Boolean(r.stall) });
  const stallFields = h("div", { class: "fields field-wide", hidden: !r.stall },
    field("c-stall1", "Stall from, °C", r.stall_from_c, { type: "number" }), field("c-stall2", "Stall to, °C", r.stall_to_c, { type: "number" }));
  stallBox.addEventListener("change", () => { stallFields.hidden = !stallBox.checked; });

  const el = box(fresh ? "New recipe" : `Edit recipe: ${r.name}`, h("div", { class: "body" },
    !fresh && !r.own ? msg("This is a built-in recipe. Saving makes your own copy, which is then used in its place.") : null,
    group("What it is", null,
      field("c-name", "Name", r.name, { placeholder: "beef-short-ribs" }),
      field("c-cut", "Cut", r.cut, { placeholder: "beef-short-ribs", hint: "Starts with the food: beef-, pork-, lamb-, chicken-, turkey-, duck-, fish-, sausage-, vegetable-, cheese-, nut-." }),
      field("c-desc", "Description", r.description, { wide: true }),
      field("c-rub", "Rub", r.rub ? r.rub.id : "", { options: rubOptions })),
    group("Kind of cook", "The style sets sensible defaults for the three choices beside it and for the stall and the rest below. Change any of them afterwards.",
      field("c-style", "Style", r.style, { options: Object.keys(styles).map((k) => [k, k]) }),
      field("c-done", "Done by", r.done_by, { options: Object.entries(DONE_TEXT) }),
      field("c-probe", "Probe in the food", r.probe, { options: [["needed", "needed"], ["optional", "optional"], ["none", "none (runs by the clock)"]] })),
    group("Heat", null,
      field("c-pit", "Pit temperature, °C", r.pit_target_c, { type: "number" }),
      field("c-tol", "Pit tolerance, ± °C", r.pit_tolerance_c, { type: "number" }),
      field("c-meat", "Meat target, °C", r.meat_target_c, { type: "number", hint: "Needed for any meat or fish, even when the clock ends the cook: it is the check before serving." }),
      field("c-pull", "Take off at, °C", r.pull_c, { type: "number", hint: "Optional. For roasts: a few degrees under the target, because it keeps rising while it rests." })),
    group("Time", "Fill in exactly one of the three: hours per kg, a fixed time, or timed stages.",
      field("c-perkg", "Hours per kg", r.hours_per_kg, { type: "number", step: "0.1" }),
      field("c-fixed", "Hours, whatever the weight", r.hours_fixed, { type: "number", step: "0.25" }),
      field("c-fridge", "Out of the fridge, minutes before", r.fridge_out_min, { type: "number" }),
      field("c-brine", "Dry brine or cure, hours before", r.dry_brine_hours, { type: "number" }),
      h("div", { class: "field-wide" }, h("label", {}, "Timed stages (for example ribs: smoke 3, wrapped 2, glaze 1)"), stageRows,
        h("div", { class: "actions" }, h("button", { type: "button", onclick: () => addStage() }, "Add stage")))),
    group("Stall and wrap", null,
      h("div", { class: "field-wide" }, h("label", { class: "check", for: "c-stall" }, stallBox, "Watch for a stall (big cuts that sit around 65 to 77 °C for hours)")),
      stallFields,
      field("c-wrap", "Wrap at, °C", r.wrap_at_c, { type: "number", hint: "Leave empty for an unwrapped cook." }),
      field("c-wrapm", "Wrap in", r.wrap_material, { placeholder: "butcher paper" })),
    group("Finish", "An optional last step such as a sear or a glaze. Give either the meat temperature that starts it or how long before the end.",
      field("c-fin", "Finish", fin.what || "", { options: [["", "none"], ["sear", "sear"], ["crisp", "crisp"], ["glaze", "glaze"], ["sauce", "sauce"]] }),
      field("c-finmin", "For, minutes", fin.minutes, { type: "number" }),
      field("c-finpit", "Pit for it, °C", fin.pit_c, { type: "number" }),
      field("c-finat", "Start when the meat is at, °C", fin.at_c, { type: "number" }),
      field("c-finbefore", "Or start, minutes before the end", fin.minutes_before_end, { type: "number" })),
    group("Rest", "Set both to 0 for food that is served straight away.",
      field("c-rest1", "Rest, minimum minutes", r.rest_min_minutes, { type: "number" }),
      field("c-rest2", "Rest, ideal minutes", r.rest_ideal_minutes, { type: "number" }),
      field("c-hold", "Hold floor, °C", r.hold_min_c ?? 60, { type: "number", hint: "During a long hold you are warned when the meat gets down to this." })),
    fresh ? null : h("div", { class: "fields" }, field("c-change", "What changed and why", "", { wide: true, placeholder: "less sugar, the bark burnt last time", hint: `Becomes the commit message. Now at version ${r.version}; a changed recipe gets the next number.` })),
    h("div", { class: "actions" },
      h("button", { type: "button", class: "primary", onclick: (ev) => act(ev.target, async () => {
        const stages = [...stageRows.querySelectorAll(".stage")].map((x) => ({ name: x.querySelector(".st-name").value.trim(),
          hours: x.querySelector(".st-hours").value === "" ? "" : Number(x.querySelector(".st-hours").value), note: x.querySelector(".st-note").value.trim() }));
        const finish = fv("c-fin") ? { what: fv("c-fin"), minutes: fnum("c-finmin"), pit_c: fnum("c-finpit"), at_c: fnum("c-finat"), minutes_before_end: fnum("c-finbefore") } : null;
        const out = await api("/api/recipes", { name: fv("c-name"), version: r.version, cut: fv("c-cut"), description: fv("c-desc"), rub: fv("c-rub") || null,
          style: fv("c-style"), done_by: fv("c-done"), probe: fv("c-probe"), stall: stallBox.checked,
          pit_target_c: fnum("c-pit"), pit_tolerance_c: fnum("c-tol"), meat_target_c: fnum("c-meat"), pull_c: fnum("c-pull"), hours_per_kg: fnum("c-perkg"), hours_fixed: fnum("c-fixed"),
          stages, finish, fridge_out_min: fnum("c-fridge"), dry_brine_hours: fnum("c-brine"), stall_from_c: fnum("c-stall1"), stall_to_c: fnum("c-stall2"), wrap_at_c: fnum("c-wrap"),
          wrap_material: fv("c-wrapm"), rest_min_minutes: fnum("c-rest1"), rest_ideal_minutes: fnum("c-rest2"), hold_min_c: fnum("c-hold"), change: fresh ? "" : fv("c-change") });
        if (out.warnings && out.warnings.length) { alert("Saved, with a warning:\n\n" + out.warnings.join("\n")); }
        location.hash = "#recipes"; await route(); }) }, "Save recipe"),
      h("button", { type: "button", onclick: () => { location.hash = "#recipes"; } }, "Cancel"))));
  // choosing a style fills in what that style normally means
  el.querySelector("#c-style").addEventListener("change", (ev) => {
    const d = styles[ev.target.value];
    if (!d) return;
    el.querySelector("#c-done").value = d.done_by;
    el.querySelector("#c-probe").value = d.probe;
    stallBox.checked = d.stall; stallFields.hidden = !d.stall;
    el.querySelector("#c-rest1").value = d.rest_min_minutes;
    el.querySelector("#c-rest2").value = d.rest_ideal_minutes;
  });
  return el;
}

function historyButton(kind, id, holder) {
  return h("button", { type: "button", onclick: (ev) => act(ev.target, async () => {
    const entries = await api(`/api/history/${kind}/${encodeURIComponent(id)}`);
    holder.replaceChildren(entries.length ? h("ul", { class: "rows history" }, entries.map((e) => h("li", {},
      h("span", { class: "time" }, new Date(e.at).toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" })),
      h("div", { class: "msgtext" }, h("div", {}, e.message.replace(/^(recipe|rub) \S+ /, "").replace(/^(\S+): /, "v$1: ")),
        e.created ? h("div", { class: "muted" }, "first version") : e.deleted ? h("div", { class: "muted" }, "deleted") :
        e.changes.length ? h("ul", { class: "changes" }, e.changes.map((c) => h("li", {}, h("b", {}, c.field), ": ",
          c.before === null ? h("span", { class: "muted" }, "added") : h("span", { class: "was" }, c.before), " → ",
          c.after === null ? h("span", { class: "muted" }, "removed") : h("span", {}, c.after)))) : h("div", { class: "muted" }, "no field changed"))))) :
      h("p", { class: "hint" }, "No saved versions: this one is built in. Customise it to start its history."));
  }) }, "History");
}
function removeButton(kind, id) {
  return h("button", { type: "button", class: "danger", onclick: (ev) => { if (confirm(`Delete your ${kind} ${id}? It stays in the file history.`)) act(ev.target, async () => {
    await api("/api/delete", { kind, id }); await route(); }); } }, "Delete");
}
async function renderRecipes(arg) {
  const [recipes, rubs, styles] = await Promise.all([api("/api/recipes"), api("/api/rubs"), api("/api/styles")]);
  if (arg === "new-recipe") return show(recipeForm(null, rubs, styles));
  if (arg === "new-rub") return show(rubForm(null));
  if (arg && arg.startsWith("edit-recipe:")) {
    const r = recipes.find((x) => x.id === arg.slice(12));
    return show(r && r.valid ? recipeForm(r, rubs, styles) : msg("No such recipe.", "err"));
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
      box(h("div", { class: "title-row" }, h("h2", {}, r.name), h("span", { class: "badge" }, "v" + r.version), tag(r), h("span", { class: "badge" }, r.style), h("span", { class: "muted mono" }, r.cut)),
        h("div", { class: "body" }, r.description ? h("p", { class: "sub" }, r.description) : null,
          r.warnings.map((w) => msg("Warning: " + w, "err")),
          h("dl", { class: "kv" },
            h("dt", {}, "Pit"), h("dd", {}, `${r.pit_target_c} °C ± ${r.pit_tolerance_c}`),
            h("dt", {}, "Meat"), h("dd", {}, r.meat_target_c === null ? "no temperature" : `to ${r.meat_target_c} °C` + (r.pull_c !== null ? `, taken off at ${r.pull_c} °C` : "")),
            h("dt", {}, "Done by"), h("dd", {}, DONE_TEXT[r.done_by] + (r.probe === "none" ? ", no probe in the food" : "")),
            h("dt", {}, "Time"), h("dd", {}, timeText(r)),
            r.stall ? [h("dt", {}, "Stall"), h("dd", {}, `watched, ${r.stall_from_c} to ${r.stall_to_c} °C`)] : null,
            r.wrap_at_c ? [h("dt", {}, "Wrap"), h("dd", {}, `at ${r.wrap_at_c} °C` + (r.wrap_material ? `, ${r.wrap_material}` : ""))] : null,
            r.finish ? [h("dt", {}, "Finish"), h("dd", {}, `${r.finish.what}, ${r.finish.minutes} min` + (r.finish.pit_c ? ` at ${r.finish.pit_c} °C` : "") +
              (r.finish.at_c !== undefined && r.finish.at_c !== null ? `, once the meat is at ${r.finish.at_c} °C` : `, ${r.finish.minutes_before_end} min before the end`))] : null,
            h("dt", {}, "Rest"), h("dd", {}, r.rest_ideal_minutes ? `${r.rest_min_minutes} to ${r.rest_ideal_minutes} min` : "none, serve straight away"),
            r.rub ? [h("dt", {}, "Rub"), h("dd", {}, `${r.rub.name} v${r.rub.version}: ` + r.rub.ingredients.map((i) => `${i.parts} ${i.name}`).join(", ") +
              ` (${Math.round(r.rub.sugar * 100)}% sugar, ${Math.round(r.rub.salt * 100)}% salt)`)] : null),
          r.results.length ? h("div", { class: "scroll results" }, h("table", {}, h("thead", {}, h("tr", {}, h("th", {}, "Version"), h("th", {}, "Cooks"), h("th", {}, "Average score"), h("th", {}, "Best"), h("th", {}, "Cooks on this version"))),
            h("tbody", {}, r.results.map((v) => h("tr", {}, h("td", {}, "v" + v.version + (v.version === r.version ? " (current)" : "")), h("td", {}, v.count),
              h("td", {}, v.avg_score === null ? "–" : v.avg_score), h("td", {}, v.best_score === null ? "–" : v.best_score),
              h("td", { class: "links" }, v.cooks.map((c, i) => [i ? ", " : "", h("a", { href: "#history/" + encodeURIComponent(c.id) }, (c.opened_at || "").slice(0, 10) + (c.score !== null && c.score !== undefined ? ` (${c.score})` : ""))]))))))) :
            h("p", { class: "hint" }, "No finished cooks on this recipe yet. Results will be listed here by recipe version."),
          h("div", { class: "actions" }, h("a", { class: "btn", href: "#recipes/edit-recipe:" + encodeURIComponent(r.id) }, r.own ? "Edit" : "Customise"),
            historyButton("recipe", r.id, hist), r.own ? removeButton("recipe", r.id) : null), hist),
        h("details", {}, h("summary", {}, r.id + ".yaml"), h("pre", {}, r.source))); }),
    h("h1", { class: "section" }, "Rubs"),
    rubs.map((r) => { const hist = h("div", {}); return !r.valid ? box(r.id, h("div", { class: "body" }, msg(r.error, "err"))) :
      box(h("div", { class: "title-row" }, h("h2", {}, r.name), h("span", { class: "badge" }, "v" + r.version), tag(r), h("span", { class: "muted" }, `${Math.round(r.sugar * 100)}% sugar, ${Math.round(r.salt * 100)}% salt`)),
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
    a.hold ? ["Hold", `${hrs(a.hold.hours)}, meat from ${a.hold.start_c ?? "–"} to ${a.hold.end_c ?? "–"} °C` +
      (a.hold.mean_rate_c_per_h !== null && a.hold.mean_rate_c_per_h !== undefined ? ` (${a.hold.mean_rate_c_per_h} °C an hour)` : "") +
      `, lowest ${a.hold.min_c ?? "–"} °C` + (a.hold.minutes_below_floor ? `, ${dur(a.hold.minutes_below_floor)} below the ${a.hold.floor_c} °C floor` : `, never below the ${a.hold.floor_c} °C floor`)] : null,
    amb.length || hum.length || a.rain_minutes ? ["Outdoors", [amb.length ? `${avg(amb).toFixed(0)} °C` : null, hum.length ? `${avg(hum).toFixed(0)}% humidity` : null,
      a.rain_minutes ? `rain for ${dur(a.rain_minutes)}` : (curve.some((p) => p.rain !== null && p.rain !== undefined) ? "no rain" : null)].filter(Boolean).join(", ")] : null,
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
      m.probes && m.probes.some((p) => p.source) ? h("p", { class: "sub" }, "Probes: " + m.probes.map((p) => `${p.label} = ${p.source || "probe " + p.no}`).join(", ")) : null,
      h("p", { class: "sub mono" }, `${m.recipe_name} v${m.recipe_version}` + (m.rub ? ` · rub ${m.rub.name} v${m.rub.version}` : "") + (m.weight_kg ? ` · ${m.weight_kg} kg` : "") + ` · opened ${fullDate(m.opened_at)}`),
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

// The API tab: every endpoint from the OpenAPI document, each with a Send button that calls this server.
async function renderApi() {
  const spec = await (await fetch("/api/openapi.json")).json();
  const groups = {};
  for (const [path, ops] of Object.entries(spec.paths)) {
    for (const [method, op] of Object.entries(ops)) (groups[op.tags[0]] = groups[op.tags[0]] || []).push({ path, method: method.toUpperCase(), op });
  }
  const row = (e) => {
    const body = e.op.requestBody ? JSON.stringify(e.op.requestBody.content["application/json"].example, null, 2) : null;
    const params = (e.op.parameters || []).map((p) => h("input", { type: "text", class: "api-param", "data-name": p.name, placeholder: p.name, "aria-label": p.name }));
    const input = body !== null ? h("textarea", { rows: String(Math.min(12, body.split("\n").length + 1)), spellcheck: "false", "aria-label": "Request body" }, body) : null;
    const out = h("div", {});
    const send = h("button", { type: "button", class: e.method === "GET" ? "" : "primary", onclick: async () => {
      let path = e.path;
      for (const p of params) path = path.replace("{" + p.dataset.name + "}", encodeURIComponent(p.value.trim()));
      if (path.includes("{")) { out.replaceChildren(msg("Fill in " + params.map((p) => p.dataset.name).join(", ") + " first.", "err")); return; }
      let payload;
      if (input) { try { payload = JSON.parse(input.value || "{}"); } catch (err) { out.replaceChildren(msg("The body is not valid JSON.", "err")); return; } }
      send.disabled = true;
      const started = performance.now();
      try {
        const opt = { method: e.method, headers: e.op.security ? { Authorization: "Bearer " + (token || "") } : {} };
        if (input) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(payload); }
        const res = await fetch(path, opt);
        const text = await res.text();
        let pretty = text;
        try { pretty = JSON.stringify(JSON.parse(text), null, 2); } catch (err) { /* not JSON */ }
        if (pretty.length > 20000) pretty = pretty.slice(0, 20000) + "\n… (cut; " + text.length + " characters in all)";
        out.replaceChildren(h("p", { class: "hint" }, `${res.status} ${res.statusText || ""} in ${Math.round(performance.now() - started)} ms`), h("pre", {}, pretty || "(empty)"));
      } catch (err) { out.replaceChildren(msg(String(err), "err")); }
      send.disabled = false;
    } }, "Send");
    return h("details", { class: "api" }, h("summary", {}, h("span", { class: "verb " + e.method.toLowerCase() }, e.method), h("code", {}, e.path), h("span", { class: "muted" }, e.op.summary),
      e.op.security ? null : h("span", { class: "badge" }, "no token")),
      h("div", { class: "body" }, params.length ? h("div", { class: "fields" }, params.map((p) => h("div", {}, h("label", {}, p.dataset.name), p))) : null,
        input ? [h("label", { class: params.length ? "block-label" : "" }, "Body"), input] : null,
        e.method !== "GET" && e.path !== "/api/plan" && e.path !== "/mcp" ? h("p", { class: "hint" }, "This acts on your real data.") : null,
        h("div", { class: "actions" }, send), out));
  };
  show(h("div", { class: "title-row" }, h("h1", {}, "API"), h("span", { class: "badge" }, "v" + spec.info.version), h("span", { class: "push" }),
      h("a", { class: "btn", href: "/api/openapi.json", target: "_blank", rel: "noopener" }, "openapi.json")),
    h("p", { class: "sub" }, "Everything the page does goes through these calls. Send a token as ", h("code", {}, "Authorization: Bearer <access token>"), ". The OpenAPI document opens in Swagger and similar tools."),
    ["Cook", "History", "Recipes", "Settings", "Agents"].filter((g) => groups[g]).map((g) => box(g, groups[g].map(row))));
}

async function renderSettings() {
  const v = await api("/api/settings");
  const e = v.source.entities || {};
  const f = (id, label, value, extra) => h("div", extra && extra.wide ? { class: "field-wide" } : {}, h("label", { for: id }, label),
    h("input", { id, type: (extra && extra.type) || "text", value: value || "", placeholder: (extra && extra.placeholder) || "", autocomplete: "off" }));
  const val = (id) => document.getElementById(id).value.trim();
  const probeLines = () => val("s-probes").split("\n").map((l) => l.split("|").map((x) => x.trim())).filter((l) => l[0]).slice(0, 9).map((l) => [l[0], l[1] || ""]);
  const save = (ev) => act(ev.target, async () => {
    await api("/api/settings", {
      source: { type: "homeassistant", url: val("s-url"), token: val("s-token"),
        entities: { pit: val("s-pit"), probes: probeLines().map((l) => l[0]), probe_names: probeLines().map((l) => l[1]), fan: val("s-fan"), fan_on: val("s-fanon"), pit_target: val("s-target"), connected: val("s-conn"), ambient: val("s-amb"), humidity: val("s-hum"), rain: val("s-rain") } },
      telegram: { bot_token: val("s-bot"), chat_id: val("s-chat") }, poll_seconds: Number(val("s-poll")) || 15 });
    return "Saved.";
  });
  show(h("h1", {}, "Settings"),
    box("Readings: Home Assistant", h("div", { class: "body" }, h("div", { class: "fields" },
      f("s-url", "Home Assistant address", v.source.url, { placeholder: "http://homeassistant.local:8123" }),
      f("s-token", "Long-lived access token", "", { type: "password", placeholder: v.source.token_set ? "saved; leave empty to keep" : "not set" }),
      f("s-pit", "Pit temperature entity", e.pit), f("s-conn", "Connected entity", e.connected),
      h("div", { class: "field-wide" }, h("label", { for: "s-probes" }, "Food probes, one per line (up to 9): entity | name"),
        h("textarea", { id: "s-probes", rows: "9", spellcheck: "false" }, (e.probes || []).map((p, i) => p + ((e.probe_names || [])[i] ? " | " + e.probe_names[i] : "")).join("\n")),
        h("p", { class: "hint" }, "The order is the probe number: the first line is probe 1. The name after the bar says which physical probe it is, for example “Fan controller 1” or “Thermometer 3”, and is shown wherever the probe is.")),
      f("s-fan", "Fan output entity", e.fan), f("s-fanon", "Fan switched on entity", e.fan_on), f("s-target", "Pit target entity", e.pit_target),
      f("s-amb", "Outdoor temperature entity (optional)", e.ambient), f("s-hum", "Outdoor humidity entity (optional)", e.humidity),
      f("s-rain", "Rain entity (optional)", e.rain),
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
    else if (tab === "api") await renderApi();
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
