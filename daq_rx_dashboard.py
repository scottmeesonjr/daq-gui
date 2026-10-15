"""
Project Aurelian DAQ: receiver + live dashboard (runs on the Pi 4, at standoff).

Listens for packets from daq_tx.py and serves a live dashboard at
    http://aurelian-rx.local:5000   (any browser on the same network)
    http://localhost:5000           (a monitor plugged into this Pi)

Two kinds of CSV files are written:
  rx_log_<date>.csv     everything received, always on, as a backup
  recordings/*.csv      only while "Start Data Acquisition" is pressed;
                        downloadable from the dashboard
The Arduino's SD card is still the official, full-rate record.

Usage:
    python daq_rx_dashboard.py         # normal: radio -> dashboard
    python daq_rx_dashboard.py --sim   # fake data, no radio needed
"""

import argparse
import csv
import json
import math
import random
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from flask import Flask, jsonify, request, send_from_directory

# ---------- radio settings: must match daq_tx.py ----------
RADIO_FREQ_MHZ = 915.0
ENCRYPTION_KEY = b"AurelianDAQ_k_01"   # exactly 16 bytes, same on both Pis
MY_NODE = 2

CS_PIN = "CE1"     # physical pin 26
RESET_PIN = "D25"  # physical pin 22

# ---------- sensors ----------
# (key, label shown on dashboard, unit, over-pressure limit or None)
# Order here must match the Arduino/transmitter field order.
# Dashboard layout is two columns, filled left to right:
#     PT-1 | PT-2
#     PT-3 | PT-4
#     Load Cell | Thermocouple
#
# TODO: set each PT's limit from your Range Safety Plan abort criteria.
#       600 psi is a placeholder, not a real number for this system.
SENSORS = [
    ("PT1", "PT-1", "psi", 600),
    ("PT2", "PT-2", "psi", 600),
    ("PT3", "PT-3", "psi", 600),
    ("PT4", "PT-4", "psi", 600),
    ("LC1", "Load Cell", "lbf", None),
    ("TC1", "Thermocouple", "°C", None),
]
FIELDS = [s[0] for s in SENSORS]

STALE_AFTER_S = 1.5    # status turns red if no packet for this long
WEB_PORT = 5000
REC_DIR = Path(__file__).resolve().parent / "recordings"
EASTERN = ZoneInfo("America/New_York")   # handles EST/EDT switch automatically
# -----------------------------------------------------

state_lock = threading.Lock()
state = {
    "values": {f: None for f in FIELDS},  # None = no data yet, "NC" = unplugged
    "last_rx": None,
    "rssi": None,
    "received": 0,
    "lost": 0,
    "last_seq": None,
    "peaks": {f: None for f in FIELDS},   # highest value seen; reset on Start
}


# ---------- data acquisition recording ----------
rec_lock = threading.Lock()
rec = {"active": False, "file": None, "writer": None, "path": None,
       "t0": None, "rows": 0, "clock_offset": 0.0, "events": []}


def eastern_now(offset=0.0):
    return datetime.fromtimestamp(time.time() + offset, tz=EASTERN)


def fmt_eastern(dt):
    """10/14/2026 02:31:07.412 PM EDT"""
    return (dt.strftime("%m/%d/%Y %I:%M:%S.") + f"{dt.microsecond // 1000:03d} "
            + dt.strftime("%p %Z"))


def reset_peaks():
    with state_lock:
        state["peaks"] = {f: None for f in FIELDS}


def rec_start(name, client_ms):
    with rec_lock:
        if rec["active"]:
            return
        # The Pi has no battery clock, so with no internet at the test site its
        # time can be wrong. Borrow the clock of the browser that pressed Start.
        rec["clock_offset"] = (client_ms / 1000 - time.time()) if client_ms else 0.0
        stamp = eastern_now(rec["clock_offset"])
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name or "").strip("_")[:40]
        REC_DIR.mkdir(exist_ok=True)
        path = REC_DIR / (f"aurelian_{stamp:%Y%m%d_%H%M%S}" + (f"_{safe}" if safe else "") + ".csv")
        f = open(path, "w", newline="", encoding="utf-8-sig")   # BOM so Excel shows the degree sign
        w = csv.writer(f)
        w.writerow(["Time (s)", "Date/Time (Eastern)", "Packet #", "Signal (dBm)"]
                   + [f"{label} ({unit})" for _, label, unit, _ in SENSORS] + ["Event"])
        f.flush()
        rec.update(active=True, file=f, writer=w, path=path, t0=time.time(), rows=0,
                   events=[])
        print(f"Recording started: {path.name}")
    reset_peaks()                                    # peaks are per-recording


def rec_stop():
    with rec_lock:
        if not rec["active"]:
            return
        rec["file"].close()
        print(f"Recording stopped: {rec['path'].name}, {rec['rows']} rows")
        rec.update(active=False, file=None, writer=None)


def rec_write(seq, vals, rssi):
    with rec_lock:
        if not rec["active"]:
            return
        now = time.time()
        rec["writer"].writerow(
            [f"{now - rec['t0']:.3f}", fmt_eastern(eastern_now(rec["clock_offset"])), seq, rssi]
            + [vals[f] for f in FIELDS] + [""])     # unplugged sensors are written as NC
        rec["file"].flush()                         # nothing lost if power drops
        rec["rows"] += 1


def rec_event(label):
    """Write an event row (time + label, sensor columns blank) into the recording."""
    label = " ".join((label or "").split())[:60] or "Event"
    with rec_lock:
        if not rec["active"]:
            return
        elapsed = time.time() - rec["t0"]
        rec["writer"].writerow([f"{elapsed:.3f}", fmt_eastern(eastern_now(rec["clock_offset"])),
                                "", ""] + [""] * len(FIELDS) + [label])
        rec["file"].flush()
        rec["events"].append({"t": round(elapsed, 1), "label": label})
        print(f"Event at {elapsed:.1f} s: {label}")


def rec_status():
    with rec_lock:
        r = {"active": rec["active"], "rows": rec["rows"],
             "file": rec["path"].name if rec["path"] else None,
             "elapsed": (time.time() - rec["t0"]) if rec["active"] else None,
             "events": list(rec["events"][-12:])}
    files = sorted(REC_DIR.glob("*.csv"), reverse=True)[:8] if REC_DIR.exists() else []
    r["files"] = [{"name": p.name, "kb": round(p.stat().st_size / 1024, 1)} for p in files]
    return r


def unpack(payload):
    """'<seq>|v1,v2,...' -> (seq, {'PT1': 452.1 or 'NC', ...}) or None."""
    try:
        seq_s, body = payload.split("|", 1)
        raw = body.split(",")
        if len(raw) != len(FIELDS):
            return None
        vals = {}
        for f, v in zip(FIELDS, raw):
            vals[f] = "NC" if v == "NC" else float(v)
        return int(seq_s), vals
    except ValueError:
        return None


def record(seq, vals, rssi, writer, logfile):
    with state_lock:
        last = state["last_seq"]
        if last is not None:
            gap = (seq - last - 1) % 100000
            if gap < 1000:          # ignore huge jumps (transmitter restarted)
                state["lost"] += gap
        state["last_seq"] = seq
        state["values"] = vals
        state["last_rx"] = time.time()
        state["rssi"] = rssi
        state["received"] += 1
        for f, v in vals.items():
            if isinstance(v, float):
                pk = state["peaks"][f]
                state["peaks"][f] = v if pk is None else max(pk, v)
    writer.writerow([datetime.now().isoformat(timespec="milliseconds"), seq, rssi]
                    + [vals[f] for f in FIELDS])
    logfile.flush()
    rec_write(seq, vals, rssi)


def open_log():
    name = f"rx_log_{datetime.now():%Y%m%d_%H%M%S}.csv"
    f = open(name, "w", newline="")
    w = csv.writer(f)
    w.writerow(["pi_time", "seq", "rssi_dbm"] + FIELDS)
    print(f"Backup log: {name}")
    return f, w


def radio_loop():
    """Keep retrying so the dashboard still runs if the radio isn't wired yet."""
    while True:
        try:
            _radio_loop()
        except Exception as e:
            print(f"Radio error: {e}. Retrying in 5 s.")
            time.sleep(5)


def _radio_loop():
    import board
    import busio
    import digitalio
    import adafruit_rfm69

    spi = busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO)
    cs = digitalio.DigitalInOut(getattr(board, CS_PIN))
    reset = digitalio.DigitalInOut(getattr(board, RESET_PIN))
    rfm = adafruit_rfm69.RFM69(spi, cs, reset, RADIO_FREQ_MHZ)
    rfm.encryption_key = ENCRYPTION_KEY
    rfm.node = MY_NODE
    print(f"Radio listening: {RADIO_FREQ_MHZ} MHz, node {MY_NODE}")

    logfile, writer = open_log()
    while True:
        packet = rfm.receive(timeout=0.5)
        if packet is None:
            continue
        parsed = unpack(packet.decode(errors="ignore"))
        if parsed:
            record(parsed[0], parsed[1], rfm.last_rssi, writer, logfile)


def sim_loop():
    """Fake transmitter so the dashboard can be tested with no radio.
    PT-1 swings past its limit every ~25 s to show the over-pressure warning."""
    logfile, writer = open_log()
    seq, t0 = 0, time.time()
    while True:
        t = time.time() - t0
        vals = {
            "PT1": round(450 + 200 * math.sin(t / 4) + random.uniform(-3, 3), 2),
            "PT2": "NC",
            "PT3": round(88 + random.uniform(-1, 1), 2),
            "PT4": "NC" if int(t / 10) % 2 else round(300 + random.uniform(-5, 5), 2),
            "LC1": round(max(0, 120 * math.sin(t / 5)) + random.uniform(-1, 1), 3),
            "TC1": round(24 + random.uniform(-0.25, 0.25), 2),
        }
        if random.random() > 0.03:            # drop ~3% to exercise the loss counter
            record(seq, vals, -40 - random.randint(0, 25), writer, logfile)
        seq += 1
        time.sleep(0.1)


app = Flask(__name__)


@app.route("/data")
def data():
    with state_lock:
        s = dict(state)
    age = None if s["last_rx"] is None else time.time() - s["last_rx"]
    total = s["received"] + s["lost"]
    return jsonify({
        "values": s["values"],
        "age": age,
        "stale": age is None or age > STALE_AFTER_S,
        "rssi": s["rssi"],
        "loss_pct": (100 * s["lost"] / total) if total else 0,
        "peaks": s["peaks"],
        "rec": rec_status(),
    })


@app.route("/record/start", methods=["POST"])
def record_start():
    body = request.get_json(silent=True) or {}
    rec_start(body.get("name", ""), body.get("client_ms"))
    return jsonify(rec_status())


@app.route("/record/stop", methods=["POST"])
def record_stop():
    rec_stop()
    return jsonify(rec_status())


@app.route("/record/event", methods=["POST"])
def record_event():
    rec_event((request.get_json(silent=True) or {}).get("label", ""))
    return jsonify(rec_status())


@app.route("/peaks/reset", methods=["POST"])
def peaks_reset():
    reset_peaks()
    return jsonify({"ok": True})


@app.route("/recordings/<path:name>")
def download(name):
    return send_from_directory(REC_DIR, name, as_attachment=True)


@app.route("/")
def index():
    return PAGE.replace("__SENSORS__", json.dumps(SENSORS))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Project Aurelian | DAQ Live Data</title>
<style>
:root {
  --panel: #E6EAEE; --face: #FFFFFF; --ink: #15202B; --dim: #5B6773;
  --rule: #C3CBD3; --live: #1F7A4D; --lost: #B3122E; --trace: #2C5E8C;
  --link-bg: #15202B; --link-ink: #E9EEF2; --link-dim: #9AA6B2;
}
@media (prefers-color-scheme: dark) {
  :root { --panel: #10161C; --face: #19222B; --ink: #E9EEF2; --dim: #8D99A5;
          --rule: #2C3741; --live: #3DBE7E; --lost: #FF5C6C; --trace: #7FB2E5;
          --link-bg: #050A0E; --link-ink: #E9EEF2; --link-dim: #8D99A5; }
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--panel); color: var(--ink);
       font-family: "DejaVu Sans", "Segoe UI", Roboto, Arial, sans-serif; }
header { padding: 0.9rem 1.25rem; border-bottom: 1px solid var(--rule); }
h1 { margin: 0; font-size: 1.1rem; font-weight: 700; line-height: 1.5; }
h1 .sep { color: var(--rule); margin: 0 0.4em; font-weight: 400; }
h1 .credit { font-weight: 400; color: var(--dim); }
.wrap { padding: 1rem 1.25rem; max-width: 72rem; }

/* sensor boxes: fixed 2 columns so PT-1/PT-2 and PT-3/PT-4 pair up by row */
.readings { display: grid; gap: 0.75rem; grid-template-columns: 1fr 1fr; }
.sensor { background: var(--face); border-left: 6px solid var(--rule);
          padding: 0.8rem 1rem 0.6rem; position: relative; }
.sensor.on { border-left-color: var(--trace); }
.row { display: flex; justify-content: space-between; align-items: baseline; gap: 1rem; }
h2 { margin: 0; font-size: 1.25rem; font-weight: 600; white-space: nowrap; }
.val { font-size: clamp(2rem, 5.5vw, 3.4rem); font-weight: 700; line-height: 1.1;
       font-variant-numeric: tabular-nums; text-align: right; }
.unit { font-size: 0.45em; font-weight: 400; color: var(--dim); margin-left: 0.25em; }
.nc { font-size: 1.05rem; color: var(--dim); text-align: right; padding: 0.9rem 0; }
.peak { text-align: right; color: var(--dim); font-size: 0.95rem;
        font-variant-numeric: tabular-nums; }
.peak b { color: var(--ink); font-weight: 600; }
.sensor.peakover .peak b { color: var(--lost); }
canvas { width: 100%; height: 44px; display: block; margin-top: 0.3rem; }
.stale .val { opacity: 0.35; }

/* over-pressure: flashing strip across the top of that PT's box */
.alarm { display: none; margin: -0.8rem -1rem 0.6rem; padding: 0.4rem 1rem;
         background: var(--lost); color: #fff; font-weight: 700; font-size: 1rem; }
.sensor.over { border-left-color: var(--lost); outline: 3px solid var(--lost);
               outline-offset: -3px; }
.sensor.over .alarm { display: block; animation: flash 1s infinite; }
.sensor.over .val { color: var(--lost); }
@keyframes flash { 0%, 49% { opacity: 1; } 50%, 100% { opacity: 0.25; } }
@media (prefers-reduced-motion: reduce) { .sensor.over .alarm { animation: none; } }

/* link strip: same box shape, inverted so it reads as separate from sensor data */
.link { display: grid; grid-template-columns: repeat(3, 1fr); gap: 0.5rem 1.5rem;
        margin-top: 1.25rem; padding: 0.9rem 1rem; background: var(--link-bg);
        color: var(--link-ink); border-left: 6px solid var(--status, var(--lost));
        font-size: 1.1rem; font-variant-numeric: tabular-nums; }
.link span { color: var(--link-dim); }
#status { font-weight: 700; color: var(--status, var(--lost)); }
#status::before { content: ""; display: inline-block; width: 0.7em; height: 0.7em;
                  border-radius: 50%; background: currentColor; margin-right: 0.45em; }

/* data acquisition controls */
.rec { margin-top: 1.25rem; padding: 0.9rem 1rem; background: var(--face);
       border-left: 6px solid var(--rule); }
.rec-row { display: flex; flex-wrap: wrap; align-items: flex-end; gap: 0.75rem 1.25rem; }
.rec-row + .rec-row { margin-top: 0.9rem; padding-top: 0.9rem;
                      border-top: 1px solid var(--rule); }
.btn2 { font: inherit; font-size: 1rem; font-weight: 600; min-height: 2.75rem;
        padding: 0 1.1rem; cursor: pointer; background: var(--panel); color: var(--ink);
        border: 1px solid var(--rule); }
.btn2:disabled { opacity: 0.45; cursor: not-allowed; }
.btn2:focus-visible { outline: 3px solid var(--trace); outline-offset: 2px; }
#events { list-style: none; margin: 0.6rem 0 0; padding: 0;
          font-variant-numeric: tabular-nums; }
#events li { padding: 0.15rem 0; }
#events li span { color: var(--dim); display: inline-block; min-width: 4.5rem; }
.rec.on { border-left-color: var(--lost); }
.rec label { display: flex; flex-direction: column; gap: 0.25rem; color: var(--dim);
             font-size: 0.9rem; }
.rec input { font: inherit; font-size: 1rem; padding: 0.55rem 0.65rem; min-width: 14rem;
             border: 1px solid var(--rule); background: var(--panel); color: var(--ink); }
#recbtn { font: inherit; font-size: 1.1rem; font-weight: 700; min-height: 3rem;
          padding: 0 1.4rem; border: 0; cursor: pointer; color: #fff;
          background: var(--live); }
#recbtn.stop { background: var(--lost); }
#recbtn:disabled { opacity: 0.6; cursor: wait; }
#recbtn:focus-visible, .rec input:focus-visible, .rec .btn2:focus-visible, .files a:focus-visible {
  outline: 3px solid var(--trace); outline-offset: 2px; }
#recinfo { align-self: center; font-size: 1.05rem; font-variant-numeric: tabular-nums; }
.rec.on #recinfo { font-weight: 700; color: var(--lost); }
.rec.on #recinfo::before { content: ""; display: inline-block; width: 0.7em; height: 0.7em;
                           border-radius: 50%; background: currentColor; margin-right: 0.45em; }
.files { margin-top: 1.25rem; }
.files h3 { margin: 0 0 0.4rem; font-size: 1rem; font-weight: 600; color: var(--dim); }
.files ul { list-style: none; margin: 0; padding: 0; }
.files li { padding: 0.3rem 0; border-bottom: 1px solid var(--rule);
            display: flex; justify-content: space-between; gap: 1rem; }
.files a { color: var(--trace); }
.files span { color: var(--dim); font-variant-numeric: tabular-nums; white-space: nowrap; }

@media (max-width: 560px) {
  .readings { grid-template-columns: 1fr; }
  .link { grid-template-columns: 1fr; }
  .rec input { min-width: 0; width: 100%; }
  .rec label { width: 100%; }
}
</style></head>
<body>
<header>
  <h1>Project Aurelian<span class="sep">|</span>DAQ Live Data<span class="sep">|</span><span class="credit">Created and Produced by Scott Meeson</span></h1>
</header>
<div class="wrap">
  <main class="readings" id="grid"></main>
  <section class="link" aria-label="Radio link">
    <div><span>Status:</span> <b id="status">Waiting for first packet</b></div>
    <div><span>Signal:</span> <b id="rssi">–</b></div>
    <div><span>Packets lost:</span> <b id="loss">–</b></div>
  </section>
  <section class="rec" id="rec" aria-label="Data acquisition">
    <div class="rec-row">
      <label>Test name (optional)
        <input id="runname" maxlength="40" placeholder="e.g. tank hydro 1"></label>
      <button id="recbtn" type="button">Start Data Acquisition</button>
      <div id="recinfo" aria-live="polite">Not recording</div>
    </div>
    <div class="rec-row">
      <label>Event label
        <input id="evlabel" maxlength="60" placeholder="e.g. valves open"></label>
      <button id="evbtn" class="btn2" type="button" disabled>Mark Event</button>
      <button id="peakbtn" class="btn2" type="button">Reset Peaks</button>
    </div>
    <ul id="events"></ul>
  </section>
  <section class="files" aria-label="Recordings">
    <h3>Recordings (newest first, click to download)</h3>
    <ul id="files"><li><span>None yet</span></li></ul>
  </section>
</div>
<script>
const SENSORS = __SENSORS__;   // [key, label, unit, limit]
const HISTORY = 300;           // 30 s at 10 Hz
const hist = {};
const grid = document.getElementById("grid");
for (const [k, label, unit, limit] of SENSORS) {
  hist[k] = [];
  const alarm = limit === null ? "" :
    `<div class="alarm" role="alert">Over pressure: above ${limit} ${unit}</div>`;
  grid.insertAdjacentHTML("beforeend",
    `<section class="sensor" id="s-${k}">${alarm}
       <div class="row"><h2>${label}</h2><div class="val" id="v-${k}">–</div></div>
       <div class="peak">Peak <b id="p-${k}">–</b></div>
       <canvas id="c-${k}"></canvas></section>`);
}
const css = () => getComputedStyle(document.documentElement);
function draw(k, over) {
  const c = document.getElementById("c-" + k), h = hist[k];
  const w = c.clientWidth, ht = c.clientHeight, dpr = devicePixelRatio || 1;
  c.width = w * dpr; c.height = ht * dpr;
  const g = c.getContext("2d"); g.scale(dpr, dpr);
  const pts = h.filter(v => v !== null);
  if (pts.length < 2) return;
  let lo = Math.min(...pts), hi = Math.max(...pts);
  if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
  g.strokeStyle = css().getPropertyValue(over ? "--lost" : "--trace");
  g.lineWidth = 1.5; g.beginPath();
  let pen = false;
  h.forEach((v, i) => {
    if (v === null) { pen = false; return; }
    const x = (i / (HISTORY - 1)) * w, y = ht - 3 - ((v - lo) / (hi - lo)) * (ht - 6);
    pen ? g.lineTo(x, y) : g.moveTo(x, y); pen = true;
  });
  g.stroke();
}
let recording = false, filesKey = "";
const recbtn = document.getElementById("recbtn");
const mmss = s => `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
function showRec(r) {
  recording = r.active;
  document.getElementById("rec").classList.toggle("on", r.active);
  recbtn.textContent = r.active ? "Stop Data Acquisition" : "Start Data Acquisition";
  recbtn.classList.toggle("stop", r.active);
  document.getElementById("runname").disabled = r.active;
  document.getElementById("recinfo").textContent = r.active
    ? `Recording ${mmss(r.elapsed)}, ${r.rows} rows, ${r.file}`
    : (r.file ? `Saved ${r.file} (${r.rows} rows)` : "Not recording");
  document.getElementById("evbtn").disabled = !r.active;
  document.getElementById("evlabel").disabled = !r.active;
  const evKey = JSON.stringify(r.events);
  if (evKey !== showRec.evKey) {
    showRec.evKey = evKey;
    document.getElementById("events").innerHTML = r.events.map(e =>
      `<li><span>${mmss(e.t)}</span> ${e.label.replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]))}</li>`).join("");
  }
  const key = JSON.stringify(r.files) + r.active;
  if (key !== filesKey) {
    filesKey = key;
    document.getElementById("files").innerHTML = r.files.length ? r.files.map(f =>
      `<li><a href="/recordings/${encodeURIComponent(f.name)}" download>${f.name}</a>
       <span>${r.active && f.name === r.file ? "recording" : f.kb + " KB"}</span></li>`).join("")
      : "<li><span>None yet</span></li>";
  }
}
recbtn.addEventListener("click", async () => {
  if (recording && !confirm("Stop data acquisition? The file will be closed.")) return;
  recbtn.disabled = true;
  try {
    const res = await fetch(recording ? "/record/stop" : "/record/start", {
      method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({name: document.getElementById("runname").value,
                            client_ms: Date.now()})});
    showRec(await res.json());
  } catch (e) {
    document.getElementById("recinfo").textContent = "Could not reach the Pi. Try again.";
  }
  recbtn.disabled = false;
});
async function post(url, body) {
  const res = await fetch(url, {method: "POST", headers: {"Content-Type": "application/json"},
                                body: JSON.stringify(body || {})});
  return res.json();
}
async function markEvent() {
  const input = document.getElementById("evlabel");
  try { showRec(await post("/record/event", {label: input.value})); input.value = ""; }
  catch (e) { document.getElementById("recinfo").textContent = "Could not reach the Pi. Try again."; }
}
document.getElementById("evbtn").addEventListener("click", markEvent);
document.getElementById("evlabel").addEventListener("keydown", e => {
  if (e.key === "Enter" && recording) markEvent(); });
document.getElementById("peakbtn").addEventListener("click", () => {
  if (confirm("Reset all peak values?")) post("/peaks/reset").catch(() => {}); });
function setStatus(text, ok) {
  document.getElementById("status").textContent = text;
  document.documentElement.style.setProperty("--status", ok ? "var(--live)" : "var(--lost)");
}
async function tick() {
  try {
    const d = await (await fetch("/data")).json();
    if (d.age === null) setStatus("Waiting for first packet", false);
    else if (d.stale) setStatus(`No signal (${d.age.toFixed(1)} s)`, false);
    else setStatus("Receiving", true);
    document.getElementById("rssi").textContent = d.rssi === null ? "–" : `${d.rssi} dBm`;
    document.getElementById("loss").textContent =
      d.age === null ? "–" : `${d.loss_pct.toFixed(1)}%`;
    document.body.classList.toggle("stale", d.stale);
    showRec(d.rec);

    for (const [k, , unit, limit] of SENSORS) {
      const v = d.values[k], el = document.getElementById("v-" + k);
      const sec = document.getElementById("s-" + k);
      const num = typeof v === "number";
      if (v === null) { el.className = "val"; el.textContent = "–"; }
      else if (v === "NC") { el.className = "nc"; el.textContent = "Sensor not hooked up"; }
      else { el.className = "val";
             el.innerHTML = `${v.toFixed(k === "LC1" ? 2 : 1)}<span class="unit">${unit}</span>`; }
      const over = num && limit !== null && v > limit;
      const pk = d.peaks[k];
      document.getElementById("p-" + k).textContent =
        pk === null ? "–" : `${pk.toFixed(k === "LC1" ? 2 : 1)} ${unit}`;
      sec.classList.toggle("peakover", pk !== null && limit !== null && pk > limit);
      sec.classList.toggle("on", num);
      sec.classList.toggle("over", over);
      if (!d.stale) { hist[k].push(num ? v : null);
                      if (hist[k].length > HISTORY) hist[k].shift(); }
      draw(k, over);
    }
  } catch (e) {
    setStatus("Dashboard lost contact with the Pi", false);
  }
}
setInterval(tick, 100); tick();
</script>
</body></html>"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sim", action="store_true", help="fake data, no radio")
    args = ap.parse_args()
    threading.Thread(target=sim_loop if args.sim else radio_loop, daemon=True).start()
    print(f"Dashboard: http://localhost:{WEB_PORT}")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True)


if __name__ == "__main__":
    main()
