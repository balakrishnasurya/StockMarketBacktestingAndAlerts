"""
Interactive candlestick chart with on-chart drawing tools.

Renders a self-contained TradingView Lightweight Charts widget inside a
Streamlit HTML component, adding the drawing tools that the
``streamlit-lightweight-charts`` wrapper does not expose:

- Trend line (2 clicks)
- Ray (2 clicks, extends to the right)
- Horizontal line (1 click)
- Rectangle (2 clicks)
- Long position tool  (entry -> stop -> target; shaded risk/reward boxes)
- Short position tool (entry -> stop -> target; shaded risk/reward boxes)
- Eraser, Undo and Clear-all

Drawings are stored in the browser's ``localStorage`` per symbol/timeframe,
so they survive Streamlit reruns and page reloads.

The Lightweight Charts standalone bundle is bundled in ``assets/`` so the
browser does not need a CDN. If the file is missing it is downloaded once
and cached under ``data/vendor/`` (falling back to a CDN ``<script>`` tag).
"""
from __future__ import annotations

import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from config import BASE_DIR, DATA_DIR

_LWC_VERSION = "4.1.3"
_LWC_CDN_URL = (
    f"https://unpkg.com/lightweight-charts@{_LWC_VERSION}"
    "/dist/lightweight-charts.standalone.production.js"
)
_ASSET_FILE = BASE_DIR / "assets" / f"lightweight-charts.standalone.v{_LWC_VERSION}.js"
_VENDOR_FILE = DATA_DIR / "vendor" / f"lightweight-charts.standalone.v{_LWC_VERSION}.js"


@st.cache_data(show_spinner=False)
def _load_lightweight_charts_js() -> str:
    """Return the Lightweight Charts standalone source, or '' on failure."""
    for candidate in (_ASSET_FILE, _VENDOR_FILE):
        try:
            if candidate.exists() and candidate.stat().st_size > 50_000:
                return candidate.read_text(encoding="utf-8")
        except OSError:
            pass

    # Not vendored yet - fetch once from the CDN and cache on disk.
    try:
        import requests

        resp = requests.get(_LWC_CDN_URL, timeout=20)
        resp.raise_for_status()
        js = resp.text
        if len(js) > 50_000:
            try:
                _VENDOR_FILE.parent.mkdir(parents=True, exist_ok=True)
                _VENDOR_FILE.write_text(js, encoding="utf-8")
            except OSError:
                pass
            return js
    except Exception:
        pass
    return ""


def _script_tags() -> str:
    """Inline the chart engine, or fall back to a CDN script tag."""
    js = _load_lightweight_charts_js()
    if js:
        # Prevent the library source from prematurely closing the <script> tag.
        js = js.replace("</script", "<\\/script")
        return f"<script>{js}</script>"
    return f'<script src="{_LWC_CDN_URL}"></script>'


_TEMPLATE = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<style>
  html, body {
    margin: 0; padding: 0; width: 100%; height: 100%;
    background: #070b14; overflow: hidden;
    font-family: Inter, 'Space Grotesk', -apple-system, Segoe UI, sans-serif;
  }
  #root {
    display: flex; flex-direction: column; width: 100vw; height: 100vh;
    box-sizing: border-box;
    border: 1px solid rgba(148,163,184,0.16);
    border-radius: 12px;
    overflow: hidden;
    background: #090e1a;
  }

  .tc-toolbar {
    display: flex; align-items: center; gap: 5px;
    padding: 7px 10px;
    background: linear-gradient(180deg, #111b2e 0%, #0c1424 100%);
    border-bottom: 1px solid rgba(148,163,184,0.14);
    flex-wrap: wrap; flex: 0 0 auto;
  }
  .tc-title {
    color: #94a3b8; font-size: 10px; font-weight: 600; letter-spacing: .1em;
    text-transform: uppercase; margin-right: 4px; white-space: nowrap;
  }
  .tc-btn {
    appearance: none; border: 1px solid rgba(148,163,184,0.18);
    background: rgba(255,255,255,0.035); color: #cbd5e1;
    border-radius: 7px; padding: 3px 8px; font-size: 11px; font-weight: 500;
    font-family: inherit; cursor: pointer; line-height: 1.5;
    transition: all .14s ease; white-space: nowrap;
  }
  .tc-btn:hover {
    border-color: rgba(16,185,129,0.55); color: #f8fafc;
    background: rgba(16,185,129,0.12);
  }
  .tc-btn.active {
    border-color: #10b981; color: #04130d;
    background: linear-gradient(135deg, #00f5a0 0%, #10b981 100%);
    font-weight: 700;
    box-shadow: 0 2px 10px rgba(16,185,129,0.28);
  }
  .tc-btn.tc-danger:hover {
    border-color: rgba(244,63,94,0.6); color: #fda4af;
    background: rgba(244,63,94,0.12);
  }
  .tc-sep { width: 1px; height: 18px; background: rgba(148,163,184,0.16); margin: 0 3px; }
  .tc-spacer { flex: 1 1 auto; }

  .tc-wrap { position: relative; flex: 1 1 auto; min-height: 0; }
  #tc-chart { position: absolute; inset: 0; }
  #tc-overlay {
    position: absolute; left: 0; top: 0; z-index: 5;
    pointer-events: none;
  }
  #tc-overlay.drawing { pointer-events: auto; cursor: crosshair; }
  #tc-overlay.erasing { pointer-events: auto; cursor: not-allowed; }

  .tc-status {
    position: absolute; left: 10px; bottom: 8px; z-index: 6;
    color: #94a3b8; font-size: 11px;
    background: rgba(9,14,26,0.88);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(148,163,184,0.14);
    border-radius: 6px; padding: 3px 9px;
    pointer-events: none; max-width: 78%;
  }
  .tc-legend {
    position: absolute; left: 10px; top: 8px; z-index: 6;
    display: flex; align-items: center; gap: 10px; flex-wrap: wrap;
    color: #f1f5f9; font-size: 11.5px; font-weight: 600;
    background: rgba(9,14,26,0.84);
    backdrop-filter: blur(8px);
    border: 1px solid rgba(148,163,184,0.16);
    border-radius: 7px; padding: 4px 10px; pointer-events: none;
    max-width: calc(100% - 90px);
  }
  .tc-sym { color: #f8fafc; font-weight: 700; letter-spacing: 0.03em; }
  .tc-ohlc { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 11px; font-weight: 500; color: #cbd5e1; }
  .tc-ohlc b { color: #64748b; font-weight: 600; margin-right: 2px; }
  .tc-up { color: #00f5a0 !important; }
  .tc-dn { color: #ff5c7c !important; }
  .tc-err {
    position: absolute; inset: 0; z-index: 9;
    display: none; align-items: center; justify-content: center;
    color: #fda4af; font-size: 13px; padding: 24px; text-align: center;
  }
</style>
</head>
<body>
<div id="root">
  <div class="tc-toolbar">
    <span class="tc-title">✏️ Tools</span>
    <button class="tc-btn active" data-tool="cursor" title="Pan / zoom the chart">↖ Cursor</button>
    <button class="tc-btn" data-tool="trend" title="Trend line: click two points">╱ Trend</button>
    <button class="tc-btn" data-tool="ray" title="Ray: click two points, extends right">➚ Ray</button>
    <button class="tc-btn" data-tool="hline" title="Horizontal line: click a price level">― H-Line</button>
    <button class="tc-btn" data-tool="rect" title="Rectangle: click two opposite corners">▭ Rect</button>
    <span class="tc-sep"></span>
    <button class="tc-btn" data-tool="long" title="Long position: click entry, stop, then target">▲ Long</button>
    <button class="tc-btn" data-tool="short" title="Short position: click entry, stop, then target">▼ Short</button>
    <span class="tc-sep"></span>
    <button class="tc-btn tc-danger" data-tool="erase" title="Click a drawing to delete it">⌫ Erase</button>
    <button class="tc-btn" id="tc-undo" title="Undo last drawing">↶ Undo</button>
    <button class="tc-btn tc-danger" id="tc-clear" title="Remove all drawings">✕ Clear</button>
    <span class="tc-spacer"></span>
    <button class="tc-btn" id="tc-fit" title="Auto-fit visible candles">⊡ Fit View</button>
  </div>
  <div class="tc-wrap">
    <div id="tc-chart"></div>
    <canvas id="tc-overlay"></canvas>
    <div class="tc-legend" id="tc-legend"></div>
    <div class="tc-status" id="tc-status">Cursor mode — drag to pan, scroll to zoom</div>
    <div class="tc-err" id="tc-err"></div>
  </div>
</div>
__LWC_LIB__
<script>
(function () {
  "use strict";

  var PAYLOAD = __PAYLOAD__;

  var chartEl = document.getElementById("tc-chart");
  var overlay = document.getElementById("tc-overlay");
  var statusEl = document.getElementById("tc-status");
  var legendEl = document.getElementById("tc-legend");
  var errEl = document.getElementById("tc-err");
  var ctx = overlay.getContext("2d");

  var volByTime = {};
  (PAYLOAD.volume || []).forEach(function (v) {
    if (v && v.time !== undefined) volByTime[String(v.time)] = v.value;
  });

  function fmtNum(n) {
    return (typeof n === "number" && isFinite(n)) ? n.toFixed(2) : "—";
  }

  function fmtVol(v) {
    if (typeof v !== "number" || !isFinite(v) || v <= 0) return "";
    if (v >= 1e7) return (v / 1e7).toFixed(2) + "Cr";
    if (v >= 1e5) return (v / 1e5).toFixed(2) + "L";
    if (v >= 1e3) return (v / 1e3).toFixed(1) + "K";
    return String(Math.round(v));
  }

  function updateLegend(bar, volVal) {
    var titleText = PAYLOAD.title || "";
    if (!bar || typeof bar.close !== "number") {
      legendEl.textContent = titleText;
      return;
    }
    var chg = bar.close - bar.open;
    var pct = bar.open ? (chg / bar.open) * 100 : 0;
    var cls = chg >= 0 ? "tc-up" : "tc-dn";
    var sign = chg >= 0 ? "+" : "";
    var volStr = fmtVol(volVal);
    legendEl.innerHTML =
      '<span class="tc-sym"></span>' +
      '<span class="tc-ohlc">' +
        '<b>O</b><span class="' + cls + '">' + fmtNum(bar.open) + '</span> &nbsp;' +
        '<b>H</b><span class="' + cls + '">' + fmtNum(bar.high) + '</span> &nbsp;' +
        '<b>L</b><span class="' + cls + '">' + fmtNum(bar.low) + '</span> &nbsp;' +
        '<b>C</b><span class="' + cls + '">' + fmtNum(bar.close) + '</span> &nbsp;' +
        '<span class="' + cls + '">(' + sign + pct.toFixed(2) + '%)</span>' +
        (volStr ? ' &nbsp;<b>V</b><span>' + volStr + '</span>' : '') +
      '</span>';
    var symNode = legendEl.querySelector(".tc-sym");
    if (symNode) symNode.textContent = titleText;
  }

  var lastCandle = (PAYLOAD.candles && PAYLOAD.candles.length)
    ? PAYLOAD.candles[PAYLOAD.candles.length - 1]
    : null;
  updateLegend(lastCandle, lastCandle ? volByTime[String(lastCandle.time)] : null);

  function fail(msg) {
    errEl.style.display = "flex";
    errEl.textContent = msg;
    chartEl.style.display = "none";
    statusEl.style.display = "none";
    legendEl.style.display = "none";
  }

  if (!window.LightweightCharts) {
    fail("Chart engine could not be loaded. Check the connection and reload.");
    return;
  }

  var LC = window.LightweightCharts;
  var dpr = window.devicePixelRatio || 1;
  var cssW = 0, cssH = 0;
  var chart, candleSeries, volumeSeries;

  try {
    chart = LC.createChart(chartEl, PAYLOAD.chartOptions);
    candleSeries = chart.addCandlestickSeries(PAYLOAD.candleOptions);
    candleSeries.setData(PAYLOAD.candles);

    if (PAYLOAD.volume && PAYLOAD.volume.length) {
      volumeSeries = chart.addHistogramSeries({
        priceFormat: { type: "volume" },
        priceScaleId: "volume",
        priceLineVisible: false,
        lastValueVisible: false
      });
      volumeSeries.setData(PAYLOAD.volume);
      chart.priceScale("volume").applyOptions({
        scaleMargins: { top: 0.8, bottom: 0 }
      });
    }

    (PAYLOAD.levels || []).forEach(function (lvl) {
      candleSeries.createPriceLine({
        price: lvl.price,
        color: lvl.color,
        lineWidth: 1,
        lineStyle: 2,
        axisLabelVisible: true,
        title: lvl.title || ""
      });
    });

    if (PAYLOAD.markers && PAYLOAD.markers.length) {
      candleSeries.setMarkers(PAYLOAD.markers);
    }

    chart.subscribeCrosshairMove(function (param) {
      if (!param || !param.time || !param.seriesData) {
        updateLegend(lastCandle, lastCandle ? volByTime[String(lastCandle.time)] : null);
        return;
      }
      var cBar = param.seriesData.get(candleSeries);
      var vBar = volumeSeries ? param.seriesData.get(volumeSeries) : null;
      var vVal = vBar && typeof vBar.value === "number"
        ? vBar.value
        : volByTime[String(param.time)];
      if (cBar && typeof cBar.close === "number") {
        updateLegend(cBar, vVal);
      }
    });
  } catch (e) {
    fail("Could not render chart: " + (e && e.message ? e.message : e));
    return;
  }

  // ── Drawing state ──────────────────────────────────────────────────────
  var COLORS = {
    trend: "#00e5ff",
    ray: "#00e5ff",
    hline: "#f5a623",
    rect: "#7c8cff",
    entry: "#00e5ff",
    profit: "#00f5a0",
    loss: "#ff4b4b",
    handle: "#e8f4fd"
  };

  var TOOL_DEFS = {
    cursor: { points: 0, hints: ["Cursor mode — drag to pan, scroll to zoom"] },
    trend: {
      points: 2,
      hints: [
        "Trend line — click the first point",
        "Trend line — click the second point (Esc to cancel)"
      ]
    },
    ray: {
      points: 2,
      hints: [
        "Ray — click the start point",
        "Ray — click a point in the direction to extend (Esc to cancel)"
      ]
    },
    hline: { points: 1, hints: ["Horizontal line — click a price level"] },
    rect: {
      points: 2,
      hints: [
        "Rectangle — click the first corner",
        "Rectangle — click the opposite corner (Esc to cancel)"
      ]
    },
    long: {
      points: 3,
      hints: [
        "Long position — click the entry price",
        "Long position — click the stop-loss level",
        "Long position — click the target level (Esc to cancel)"
      ]
    },
    short: {
      points: 3,
      hints: [
        "Short position — click the entry price",
        "Short position — click the stop-loss level",
        "Short position — click the target level (Esc to cancel)"
      ]
    },
    erase: { points: 0, hints: ["Eraser — click a drawing to delete it"] }
  };

  var STORAGE_KEY = "sa_chart_drawings_v1|" + (PAYLOAD.storageKey || "default");
  var drawings = [];
  var activeTool = "cursor";
  var pending = [];
  var hover = null;

  function loadDrawings() {
    try {
      var raw = localStorage.getItem(STORAGE_KEY);
      if (!raw) return;
      var parsed = JSON.parse(raw);
      if (Array.isArray(parsed)) {
        drawings = parsed.filter(function (d) {
          return d && d.type && Array.isArray(d.points) && d.points.length;
        });
      }
    } catch (e) { /* private mode / no storage */ }
  }

  function saveDrawings() {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(drawings));
    } catch (e) { /* ignore */ }
  }

  // ── Coordinate helpers ────────────────────────────────────────────────
  function paneWidth() {
    try {
      var w = chart.timeScale().width();
      if (typeof w === "number" && w > 0) return w;
    } catch (e) {}
    return chartEl.clientWidth;
  }

  function xForTime(t) {
    try {
      var x = chart.timeScale().timeToCoordinate(t);
      return (x === null || x === undefined) ? null : x;
    } catch (e) { return null; }
  }

  function yForPrice(p) {
    try {
      var y = candleSeries.priceToCoordinate(p);
      return (y === null || y === undefined) ? null : y;
    } catch (e) { return null; }
  }

  function xy(pt) {
    if (!pt) return null;
    var x = xForTime(pt.time);
    var y = yForPrice(pt.price);
    if (x === null || y === null) return null;
    return { x: x, y: y };
  }

  function timeForX(x) {
    var t = null;
    try { t = chart.timeScale().coordinateToTime(x); } catch (e) {}
    if (t !== null && t !== undefined) return t;

    var data = PAYLOAD.candles || [];
    if (!data.length) return null;
    var last = data[data.length - 1].time;
    if (typeof last !== "number") return last;

    var prev = data.length > 1 ? data[data.length - 2].time : last - 300;
    var step = Math.max(1, last - prev);
    var spacing = (PAYLOAD.chartOptions.timeScale &&
                   PAYLOAD.chartOptions.timeScale.barSpacing) || 8;
    var xLast = xForTime(last);
    var bars = (xLast === null) ? 0 : Math.round((x - xLast) / Math.max(2, spacing));
    return last + bars * step;
  }

  function pointFromEvent(ev) {
    var rect = overlay.getBoundingClientRect();
    var x = ev.clientX - rect.left;
    var y = ev.clientY - rect.top;
    var price = null;
    try { price = candleSeries.coordinateToPrice(y); } catch (e) {}
    if (price === null || price === undefined || !isFinite(price)) return null;
    var time = timeForX(x);
    if (time === null || time === undefined) return null;
    return { time: time, price: Number(price) };
  }

  // ── Canvas primitives ─────────────────────────────────────────────────
  function line(a, b, color, width, dash) {
    ctx.save();
    ctx.strokeStyle = color;
    ctx.lineWidth = width || 1.5;
    ctx.setLineDash(dash || []);
    ctx.beginPath();
    ctx.moveTo(a.x, a.y);
    ctx.lineTo(b.x, b.y);
    ctx.stroke();
    ctx.restore();
  }

  function handle(x, y, color) {
    if (!isFinite(x) || !isFinite(y)) return;
    ctx.save();
    ctx.fillStyle = color;
    ctx.strokeStyle = "rgba(10,14,26,0.9)";
    ctx.lineWidth = 1.5;
    ctx.beginPath();
    ctx.arc(x, y, 3.5, 0, Math.PI * 2);
    ctx.fill();
    ctx.stroke();
    ctx.restore();
  }

  function fmt(v) {
    return (Math.round(Number(v) * 100) / 100).toFixed(2);
  }

  function pct(v) {
    return (v >= 0 ? "+" : "") + Number(v).toFixed(2) + "%";
  }

  function labelRight(x, y, text, color, bg) {
    if (!isFinite(x) || !isFinite(y)) return;
    ctx.save();
    ctx.font = "600 10px Inter, 'Space Grotesk', sans-serif";
    var padX = 5, h = 16;
    var w = ctx.measureText(text).width + padX * 2;
    var rx = Math.max(0, x - w);
    ctx.fillStyle = bg || "rgba(10,14,26,0.88)";
    ctx.beginPath();
    if (typeof ctx.roundRect === "function") {
      ctx.roundRect(rx, y - h / 2, w, h, 3);
    } else {
      ctx.rect(rx, y - h / 2, w, h);
    }
    ctx.fill();
    ctx.fillStyle = color;
    ctx.textAlign = "left";
    ctx.textBaseline = "middle";
    ctx.fillText(text, rx + padX, y + 0.5);
    ctx.restore();
  }

  function drawTrend(d) {
    var a = xy(d.points[0]), b = xy(d.points[1]);
    if (!a || !b) return;
    line(a, b, COLORS.trend, 1.6, []);
    handle(a.x, a.y, COLORS.trend);
    handle(b.x, b.y, COLORS.trend);
  }

  function drawRay(d) {
    var a = xy(d.points[0]), b = xy(d.points[1]);
    if (!a || !b) return;
    var end = { x: b.x, y: b.y };
    var dx = b.x - a.x, dy = b.y - a.y;
    if (Math.abs(dx) > 0.01) {
      var right = paneWidth() - 2;
      var tx = dx > 0 ? (right - a.x) / dx : (0 - a.x) / dx;
      if (tx > 1) end = { x: a.x + tx * dx, y: a.y + tx * dy };
    }
    line(a, end, COLORS.ray, 1.6, []);
    handle(a.x, a.y, COLORS.ray);
    handle(b.x, b.y, COLORS.ray);
  }

  function drawHLine(d) {
    var y = yForPrice(d.points[0].price);
    if (y === null) return;
    var right = paneWidth() - 2;
    line({ x: 0, y: y }, { x: right, y: y }, COLORS.hline, 1.3, [6, 4]);
    labelRight(right - 4, y, fmt(d.points[0].price), COLORS.hline);
  }

  function drawRectShape(d) {
    var a = xy(d.points[0]), b = xy(d.points[1]);
    if (!a || !b) return;
    var x = Math.min(a.x, b.x), y = Math.min(a.y, b.y);
    var w = Math.abs(b.x - a.x), h = Math.abs(b.y - a.y);
    ctx.save();
    ctx.fillStyle = "rgba(124,140,255,0.10)";
    ctx.strokeStyle = COLORS.rect;
    ctx.lineWidth = 1.4;
    ctx.setLineDash([]);
    ctx.fillRect(x, y, w, h);
    ctx.strokeRect(x, y, w, h);
    ctx.restore();
    handle(a.x, a.y, COLORS.rect);
    handle(b.x, b.y, COLORS.rect);
  }

  function drawPosition(d) {
    if (!d.points || d.points.length < 3) return;
    var entry = d.points[0], stop = d.points[1], target = d.points[2];
    var xe = xForTime(entry.time);
    if (xe === null) return;
    var yE = yForPrice(entry.price);
    var yS = yForPrice(stop.price);
    var yT = yForPrice(target.price);
    if (yE === null || yS === null || yT === null) return;

    var right = paneWidth() - 4;
    if (right - xe < 26) right = Math.min(paneWidth() - 4, xe + 26);

    var isLong = d.type === "long";
    var base = Math.abs(entry.price) > 0 ? Math.abs(entry.price) : 1;
    var targetPct = ((target.price - entry.price) / base) * 100;
    var stopPct = ((stop.price - entry.price) / base) * 100;
    var risk = Math.abs(entry.price - stop.price);
    var reward = Math.abs(target.price - entry.price);
    var rr = risk > 0 ? reward / risk : 0;

    // Profit zone (entry -> target)
    ctx.save();
    ctx.fillStyle = "rgba(0,245,160,0.14)";
    ctx.fillRect(xe, Math.min(yE, yT), Math.max(1, right - xe), Math.abs(yT - yE));
    ctx.strokeStyle = "rgba(0,245,160,0.85)";
    ctx.lineWidth = 1.2;
    ctx.strokeRect(xe, Math.min(yE, yT), Math.max(1, right - xe), Math.abs(yT - yE));

    // Risk zone (entry -> stop)
    ctx.fillStyle = "rgba(255,75,75,0.14)";
    ctx.fillRect(xe, Math.min(yE, yS), Math.max(1, right - xe), Math.abs(yS - yE));
    ctx.strokeStyle = "rgba(255,75,75,0.85)";
    ctx.strokeRect(xe, Math.min(yE, yS), Math.max(1, right - xe), Math.abs(yS - yE));
    ctx.restore();

    // Entry level + annotation
    line({ x: xe, y: yE }, { x: right, y: yE }, COLORS.entry, 1.6, [5, 3]);
    line(
      { x: xe, y: Math.min(yS, yT) },
      { x: xe, y: Math.max(yS, yT) },
      "rgba(232,244,253,0.55)", 1, [2, 3]
    );

    var tag = isLong ? "LONG" : "SHORT";
    labelRight(
      right - 4, yT,
      tag + " TP " + fmt(target.price) + "  " + pct(targetPct),
      COLORS.profit, "rgba(0,60,42,0.92)"
    );
    labelRight(
      right - 4, yE,
      "ENTRY " + fmt(entry.price),
      COLORS.entry, "rgba(0,48,66,0.92)"
    );
    labelRight(
      right - 4, yS,
      tag + " SL " + fmt(stop.price) + "  " + pct(stopPct),
      COLORS.loss, "rgba(66,16,16,0.92)"
    );
    labelRight(
      right - 4, (yE + yT) / 2,
      "R:R 1:" + rr.toFixed(2),
      COLORS.handle, "rgba(18,26,48,0.92)"
    );
    handle(xe, yE, COLORS.entry);
  }

  function drawDrawing(d) {
    if (!d || !d.points) return;
    if (d.type === "trend") drawTrend(d);
    else if (d.type === "ray") drawRay(d);
    else if (d.type === "hline") drawHLine(d);
    else if (d.type === "rect") drawRectShape(d);
    else if (d.type === "long" || d.type === "short") drawPosition(d);
  }

  function drawPreview() {
    if (!hover) return;
    if (activeTool === "cursor" || activeTool === "erase") return;
    var def = TOOL_DEFS[activeTool];
    if (!def || def.points === 0) return;

    if (activeTool === "hline") {
      if (pending.length === 0) {
        var y = yForPrice(hover.price);
        if (y !== null) {
          line(
            { x: 0, y: y }, { x: paneWidth() - 2, y: y },
            "rgba(245,166,35,0.7)", 1.2, [5, 4]
          );
        }
      }
      return;
    }

    if (pending.length === 0) return;

    if (activeTool === "long" || activeTool === "short") {
      var entry = pending[0];
      var e = xy(entry);
      if (!e) return;
      var right = paneWidth() - 4;

      if (pending.length === 1) {
        var s1 = xy(hover);
        if (!s1) return;
        ctx.save();
        ctx.fillStyle = "rgba(255,75,75,0.12)";
        ctx.fillRect(e.x, Math.min(e.y, s1.y), Math.max(1, right - e.x), Math.abs(s1.y - e.y));
        ctx.restore();
        line({ x: e.x, y: e.y }, { x: right, y: e.y }, "rgba(0,229,255,0.8)", 1.4, [5, 3]);
        handle(e.x, e.y, COLORS.entry);
        handle(s1.x, s1.y, COLORS.loss);
      } else if (pending.length === 2) {
        drawPosition({
          type: activeTool,
          points: [pending[0], pending[1], hover]
        });
      }
      return;
    }

    var a = xy(pending[0]);
    var b = xy(hover);
    if (!a || !b) return;
    var color = COLORS[activeTool] || "#00e5ff";

    if (activeTool === "trend") {
      line(a, b, color, 1.3, [4, 4]);
    } else if (activeTool === "ray") {
      line(a, b, "rgba(0,229,255,0.7)", 1.2, [4, 4]);
    } else if (activeTool === "rect") {
      ctx.save();
      ctx.strokeStyle = color;
      ctx.setLineDash([4, 4]);
      ctx.lineWidth = 1.2;
      ctx.strokeRect(
        Math.min(a.x, b.x), Math.min(a.y, b.y),
        Math.abs(b.x - a.x), Math.abs(b.y - a.y)
      );
      ctx.restore();
    }
    handle(a.x, a.y, color);
  }

  function draw() {
    if (!cssW || !cssH) return;
    ctx.save();
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, overlay.width, overlay.height);
    ctx.restore();
    for (var i = 0; i < drawings.length; i++) drawDrawing(drawings[i]);
    drawPreview();
  }

  // ── Interaction ───────────────────────────────────────────────────────
  function updateStatus() {
    var def = TOOL_DEFS[activeTool];
    if (!def) return;
    var idx = Math.min(pending.length, def.hints.length - 1);
    statusEl.textContent = def.hints[idx] || def.hints[0];
  }

  function setTool(name) {
    if (!TOOL_DEFS[name]) return;
    activeTool = name;
    pending = [];
    hover = null;
    document.querySelectorAll("[data-tool]").forEach(function (btn) {
      btn.classList.toggle("active", btn.getAttribute("data-tool") === name);
    });
    overlay.className = name === "erase" ? "erasing" : (name === "cursor" ? "" : "drawing");
    updateStatus();
    draw();
  }

  function distToSegment(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1, dy = y2 - y1;
    if (dx === 0 && dy === 0) return Math.hypot(px - x1, py - y1);
    var t = ((px - x1) * dx + (py - y1) * dy) / (dx * dx + dy * dy);
    t = Math.max(0, Math.min(1, t));
    return Math.hypot(px - (x1 + t * dx), py - (y1 + t * dy));
  }

  function hitTest(px, py) {
    for (var i = drawings.length - 1; i >= 0; i--) {
      var d = drawings[i];
      var pts = d.points || [];
      var a, b;

      if (d.type === "hline") {
        var y = yForPrice(pts[0].price);
        if (y !== null && Math.abs(py - y) <= 7) return i;
      } else if (d.type === "trend" || d.type === "ray") {
        a = xy(pts[0]); b = xy(pts[1]);
        if (a && b && distToSegment(px, py, a.x, a.y, b.x, b.y) <= 7) return i;
      } else if (d.type === "rect") {
        a = xy(pts[0]); b = xy(pts[1]);
        if (a && b) {
          var x1 = Math.min(a.x, b.x), x2 = Math.max(a.x, b.x);
          var y1 = Math.min(a.y, b.y), y2 = Math.max(a.y, b.y);
          var nearEdge =
            Math.abs(px - x1) <= 7 || Math.abs(px - x2) <= 7 ||
            Math.abs(py - y1) <= 7 || Math.abs(py - y2) <= 7;
          if (nearEdge && px >= x1 - 7 && px <= x2 + 7 && py >= y1 - 7 && py <= y2 + 7) {
            return i;
          }
        }
      } else if (d.type === "long" || d.type === "short") {
        a = xy(pts[0]); b = xy(pts[1]);
        var c = xy(pts[2]);
        if (a && b && c) {
          var xe = xForTime(pts[0].time);
          var right = paneWidth() - 4;
          var yTop = Math.min(a.y, b.y, c.y);
          var yBot = Math.max(a.y, b.y, c.y);
          if (xe !== null && px >= xe - 7 && px <= right + 7 &&
              py >= yTop - 7 && py <= yBot + 7) {
            return i;
          }
        }
      }
    }
    return -1;
  }

  document.querySelectorAll("[data-tool]").forEach(function (btn) {
    btn.addEventListener("click", function () {
      setTool(btn.getAttribute("data-tool"));
    });
  });

  document.getElementById("tc-undo").addEventListener("click", function () {
    if (!drawings.length) return;
    drawings.pop();
    saveDrawings();
    draw();
  });

  var fitBtn = document.getElementById("tc-fit");
  if (fitBtn) {
    fitBtn.addEventListener("click", function () {
      try {
        chart.timeScale().fitContent();
        draw();
      } catch (e) {}
    });
  }

  var clearBtn = document.getElementById("tc-clear");
  var clearArmed = false;
  var clearTimer = null;

  clearBtn.addEventListener("click", function () {
    if (!drawings.length) return;
    if (!clearArmed) {
      clearArmed = true;
      clearBtn.textContent = "Confirm?";
      clearBtn.classList.add("active");
      clearTimeout(clearTimer);
      clearTimer = setTimeout(function () {
        clearArmed = false;
        clearBtn.textContent = "✕ Clear";
        clearBtn.classList.remove("active");
      }, 3000);
      return;
    }
    drawings = [];
    saveDrawings();
    draw();
    clearArmed = false;
    clearTimeout(clearTimer);
    clearBtn.textContent = "✕ Clear";
    clearBtn.classList.remove("active");
  });

  overlay.addEventListener("pointerdown", function (ev) {
    if (ev.button !== 0) return;
    ev.preventDefault();

    if (activeTool === "erase") {
      var rect = overlay.getBoundingClientRect();
      var idx = hitTest(ev.clientX - rect.left, ev.clientY - rect.top);
      if (idx >= 0) {
        drawings.splice(idx, 1);
        saveDrawings();
        draw();
      }
      return;
    }

    var def = TOOL_DEFS[activeTool];
    if (!def || def.points === 0) return;

    var pt = pointFromEvent(ev);
    if (!pt) return;

    pending.push(pt);
    if (pending.length >= def.points) {
      drawings.push({
        id: "d" + Date.now() + "_" + Math.random().toString(36).slice(2, 7),
        type: activeTool,
        points: pending.slice(0, def.points)
      });
      pending = [];
      saveDrawings();
    }
    hover = pt;
    updateStatus();
    draw();
  });

  overlay.addEventListener("pointermove", function (ev) {
    var def = TOOL_DEFS[activeTool];
    if (!def || def.points === 0) return;
    hover = pointFromEvent(ev);
    draw();
  });

  overlay.addEventListener("pointerleave", function () {
    hover = null;
    draw();
  });

  window.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") {
      if (pending.length) {
        pending = [];
        hover = null;
        updateStatus();
        draw();
      } else {
        setTool("cursor");
      }
    } else if ((ev.ctrlKey || ev.metaKey) && (ev.key === "z" || ev.key === "Z")) {
      ev.preventDefault();
      if (drawings.length) {
        drawings.pop();
        saveDrawings();
        draw();
      }
    }
  });

  // ── Sizing / lifecycle ────────────────────────────────────────────────
  function resize() {
    var w = chartEl.clientWidth;
    var h = chartEl.clientHeight;
    if (!w || !h) return;
    cssW = w;
    cssH = h;
    try { chart.resize(w, h); } catch (e) {}
    overlay.width = Math.round(w * dpr);
    overlay.height = Math.round(h * dpr);
    overlay.style.width = w + "px";
    overlay.style.height = h + "px";
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    draw();
  }

  window.addEventListener("resize", resize);
  if (window.ResizeObserver) {
    new ResizeObserver(resize).observe(chartEl);
  }
  try {
    chart.timeScale().subscribeVisibleLogicalRangeChange(draw);
  } catch (e) {}

  loadDrawings();
  setTool("cursor");
  resize();
  setTimeout(resize, 60);
  setTimeout(resize, 250);
  setInterval(draw, 150);
})();
</script>
</body>
</html>
"""


def _embed_html(html: str, height: int) -> None:
    """Embed the chart HTML, preferring the modern ``st.iframe`` API."""
    if hasattr(st, "iframe"):
        # Streamlit >= 1.66: raw HTML strings are embedded in a JS-enabled iframe.
        st.iframe(html, height=height, width="stretch")
    else:  # Streamlit < 1.66
        components.html(html, height=height, scrolling=False)


def render_drawing_chart(
    *,
    symbol: str,
    unit: str,
    interval: int,
    chart_height: int,
    chart_options: dict,
    candle_options: dict,
    candles: list,
    volume: list | None = None,
    levels: list | None = None,
    markers: list | None = None,
) -> None:
    """Render the candlestick chart with long/short and line drawing tools."""
    payload = {
        "title": f"{symbol} | {unit}/{interval}",
        "storageKey": f"{symbol}|{unit}|{interval}",
        "chartOptions": chart_options,
        "candleOptions": candle_options,
        "candles": candles,
        "volume": volume or [],
        "levels": levels or [],
        "markers": markers or [],
    }

    payload_json = json.dumps(payload, allow_nan=False, separators=(",", ":"))
    # Keep the payload safe inside a <script> block.
    payload_json = payload_json.replace("</", "<\\/")

    html = (
        _TEMPLATE.replace("__LWC_LIB__", _script_tags())
        .replace("__PAYLOAD__", payload_json)
    )

    _embed_html(html, int(chart_height) + 52)