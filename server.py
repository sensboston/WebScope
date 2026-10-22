"""Browser front panel for Rigol DS1000E/D oscilloscopes (DS1052E, DS1102E, ...) over USB.

Runs next to the scope (VISA must see it) and serves a single page:
    python server.py [--port 8765] [--host 127.0.0.1] [--resource USB0::...::INSTR]
"""
import argparse
import threading
import time
from pathlib import Path

import pyvisa
import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# UltraScope waits 0.12 s between all commands. In practice the DS1000E only needs it before reading a
# waveform right after a setting changed (otherwise it can hang until power cycled); other commands and
# back-to-back waveform reads are fine with a tiny gap. --gap overrides the small one.
CMD_GAP_S = 0.01
WAVE_AFTER_SET_S = 0.12
STATUS_EVERY = 3        # ask for the trigger status only every few frames
WAVE_POINTS = 600       # "normal" points mode returns the 600 on-screen samples
SCREEN_DIVS_X = 12

# Settings the page may change: name -> (SCPI write template, SCPI query, type).
# The order matters when applying a preset: probe before scale, trigger source before level.
SETTINGS = {
    "ch1.display":  (":CHAN1:DISP {}", ":CHAN1:DISP?", "onoff"),
    "ch1.coupling": (":CHAN1:COUP {}", ":CHAN1:COUP?", "str"),
    "ch1.probe":    (":CHAN1:PROB {}", ":CHAN1:PROB?", "float"),
    "ch1.scale":    (":CHAN1:SCAL {}", ":CHAN1:SCAL?", "float"),
    "ch1.offset":   (":CHAN1:OFFS {}", ":CHAN1:OFFS?", "float"),
    "ch2.display":  (":CHAN2:DISP {}", ":CHAN2:DISP?", "onoff"),
    "ch2.coupling": (":CHAN2:COUP {}", ":CHAN2:COUP?", "str"),
    "ch2.probe":    (":CHAN2:PROB {}", ":CHAN2:PROB?", "float"),
    "ch2.scale":    (":CHAN2:SCAL {}", ":CHAN2:SCAL?", "float"),
    "ch2.offset":   (":CHAN2:OFFS {}", ":CHAN2:OFFS?", "float"),
    "time.scale":   (":TIM:SCAL {}", ":TIM:SCAL?", "float"),
    "time.offset":  (":TIM:OFFS {}", ":TIM:OFFS?", "float"),
    "trig.source":  (":TRIG:EDGE:SOUR {}", ":TRIG:EDGE:SOUR?", "str"),
    "trig.slope":   (":TRIG:EDGE:SLOP {}", ":TRIG:EDGE:SLOP?", "str"),
    "trig.sweep":   (":TRIG:EDGE:SWE {}", ":TRIG:EDGE:SWE?", "str"),
    "trig.level":   (":TRIG:EDGE:LEV {}", ":TRIG:EDGE:LEV?", "float"),
    "acq.type":     (":ACQ:TYPE {}", ":ACQ:TYPE?", "str"),
}

# Shown only in the page's Advanced mode. Read on page load and with presets, not on the periodic refresh.
ADV_SETTINGS = {
    "ch1.invert":   (":CHAN1:INV {}", ":CHAN1:INV?", "onoff"),
    "ch1.bwlimit":  (":CHAN1:BWLIMIT {}", ":CHAN1:BWLIMIT?", "onoff"),
    "ch1.vernier":  (":CHAN1:VERN {}", ":CHAN1:VERN?", "onoff"),
    "ch2.invert":   (":CHAN2:INV {}", ":CHAN2:INV?", "onoff"),
    "ch2.bwlimit":  (":CHAN2:BWLIMIT {}", ":CHAN2:BWLIMIT?", "onoff"),
    "ch2.vernier":  (":CHAN2:VERN {}", ":CHAN2:VERN?", "onoff"),
    "time.format":  (":TIM:FORM {}", ":TIM:FORM?", "str"),
    "trig.coupling": (":TRIG:EDGE:COUP {}", ":TRIG:EDGE:COUP?", "str"),
    "trig.holdoff": (":TRIG:HOLDOFF {}", ":TRIG:HOLDOFF?", "float"),
    "trig.sensitivity": (":TRIG:EDGE:SENS {}", ":TRIG:EDGE:SENS?", "float"),
    "acq.averages": (":ACQ:AVER {}", ":ACQ:AVER?", "int"),
    "acq.memdepth": (":ACQ:MEMDEPTH {}", ":ACQ:MEMDEPTH?", "str"),
}
ALL_SETTINGS = {**SETTINGS, **ADV_SETTINGS}

ACTIONS = {
    "run":    [":RUN"],
    "stop":   [":STOP"],
    "single": [":TRIG:EDGE:SWE SING", ":RUN"],
    "force":  [":FORCETRIG"],
    "auto":   [":AUTO"],
    "level50": [":TRIG%50"],
    "local":  [":KEY:FORC"],     # give the front panel back
}

# Last known settings. Every query costs CMD_GAP_S, so the waveform loop reads scales from here
# instead of asking the scope each frame; /api/state refreshes it (also catches front panel changes).
cache = {}

# Automatic measurements the scope can make; the page picks which ones to show.
MEASURES = {
    "vpp": "VPP", "vmax": "VMAX", "vmin": "VMIN", "vamp": "VAMP", "vtop": "VTOP", "vbase": "VBAS",
    "vavg": "VAV", "vrms": "VRMS", "over": "OVER", "pres": "PRES", "freq": "FREQ", "per": "PER",
    "rise": "RIS", "fall": "FALL", "pwid": "PWID", "nwid": "NWID", "pdut": "PDUT", "ndut": "NDUT",
    "pdel": "PDEL", "ndel": "NDEL",
}


class ScopeOffline(Exception):
    pass


class Scope:
    """Serialised SCPI access with the minimum gap the DS1000E needs between commands.

    Survives the scope being off or unplugged: any VISA error drops the session, and the next
    call tries to reopen it (at most every RECONNECT_S), raising ScopeOffline until it is back.
    """

    RECONNECT_S = 2.0
    IDLE_RELEASE_S = 5.0    # nobody asked for this long (page closed or in Local mode): unlock the front panel

    def __init__(self, resource=None):
        self.rm = pyvisa.ResourceManager()
        self.resource = resource
        self.dev = None
        self.idn = ""
        self.lock = threading.Lock()
        self.last = 0.0
        self.next_try = 0.0
        self.locked = False     # True after any command, until the front panel is given back
        self.conn_id = 0        # bumped on every (re)connect, so the page can tell the scope was power cycled
        self.changed = 0.0      # time of the last command that may have changed a setting
        threading.Thread(target=self._idle_watch, daemon=True).start()

    def _idle_watch(self):
        while True:
            time.sleep(1.0)
            if self.locked and time.monotonic() - self.last > self.IDLE_RELEASE_S:
                self.release()

    def _connect(self):
        if self.dev is not None:
            return
        if time.monotonic() < self.next_try:
            raise ScopeOffline("scope offline")
        self.next_try = time.monotonic() + self.RECONNECT_S
        try:
            name = self.resource
            if name is None:
                found = [r for r in self.rm.list_resources() if "0X1AB1" in r.upper()]   # Rigol USB vendor id
                if not found:
                    raise ScopeOffline("no Rigol scope found")
                name = found[0]
            dev = self.rm.open_resource(name)
            dev.timeout = 3000
            self.idn = dev.query("*IDN?").strip()
            dev.write(":WAV:POIN:MODE NOR")
            self.dev = dev
            self.conn_id += 1
            self.last = time.monotonic()
            print(f"Connected: {self.idn}", flush=True)
            cache.clear()                      # settings may have changed while we were away
        except pyvisa.Error as e:
            raise ScopeOffline(f"scope offline ({e})")

    def _drop(self):
        print("Scope lost", flush=True)
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None

    def _gap(self):
        wait = CMD_GAP_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)

    def _io(self, fn):
        with self.lock:
            self._connect()
            self._gap()
            try:
                self.locked = True
                return fn(self.dev)
            except pyvisa.Error as e:
                self._drop()
                raise ScopeOffline(f"scope offline ({e})")
            finally:
                self.last = time.monotonic()

    def write(self, cmd):
        self._io(lambda d: d.write(cmd))
        self.changed = time.monotonic()

    def query(self, cmd):
        return self._io(lambda d: d.query(cmd).strip())

    def query_raw(self, cmd):
        wait = WAVE_AFTER_SET_S - (time.monotonic() - self.changed)
        if wait > 0:
            time.sleep(wait)

        def ask(d):
            d.write(cmd)
            return d.read_raw()
        return self._io(ask)

    def release(self):
        """Unlock the front panel if the scope is there; never raises."""
        try:
            self.write(":KEY:FORC")
        except ScopeOffline:
            pass
        self.locked = False


def parse_value(kind, text):
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            return None
    if kind == "onoff":
        return text.upper() in ("1", "ON")
    if kind == "int":
        try:
            return int(float(text))
        except ValueError:
            return None
    return text


def format_value(kind, value):
    if kind == "onoff":
        return "ON" if value in (True, 1, "1", "ON", "on") else "OFF"
    if kind == "float":
        return f"{float(value):g}"
    if kind == "int":
        return str(int(float(value)))
    return str(value).upper()


def decode_block(raw):
    """IEEE 488.2 definite block '#<n><len><data>' -> data bytes."""
    if raw[:1] != b"#":
        return raw
    n = int(raw[1:2])
    length = int(raw[2:2 + n])
    return raw[2 + n:2 + n + length]


def read_measure(text):
    # the scope prefixes "<" or ">" when it can only bound the value and returns 9.9e37 when it can't measure
    t = text.lstrip("<>")
    try:
        v = float(t)
    except ValueError:
        return None
    return None if v > 1e37 else v


app = FastAPI()


@app.exception_handler(ScopeOffline)
def offline_handler(request, exc):
    return JSONResponse(status_code=503, content={"offline": True, "detail": str(exc)})
scope: Scope = None
HERE = Path(__file__).parent


class SetReq(BaseModel):
    name: str
    value: object


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


@app.get("/api/state")
def state(full: bool = True):
    for name, (_, q, kind) in (ALL_SETTINGS if full else SETTINGS).items():
        cache[name] = parse_value(kind, scope.query(q))
    return {"idn": scope.idn, **cache, "trig.status": scope.query(":TRIG:STAT?")}


@app.post("/api/set")
def set_value(req: SetReq):
    if req.name not in ALL_SETTINGS:
        raise HTTPException(400, "unknown setting")
    w, q, kind = ALL_SETTINGS[req.name]
    scope.write(w.format(format_value(kind, req.value)))
    cache[req.name] = parse_value(kind, scope.query(q))
    return {req.name: cache[req.name]}


@app.post("/api/apply")
def apply(values: dict):
    """Apply a saved preset: write every known setting in ALL_SETTINGS order, then read the state back."""
    for name, (w, _, kind) in ALL_SETTINGS.items():
        if name in values and values[name] is not None:
            scope.write(w.format(format_value(kind, values[name])))
    return state()


@app.post("/api/action/{name}")
def action(name: str):
    if name not in ACTIONS:
        raise HTTPException(400, "unknown action")
    for cmd in ACTIONS[name]:
        scope.write(cmd)
    return {"ok": True}


frame_no = 0
last_status = "-"


@app.get("/api/wave")
def wave():
    global frame_no, last_status
    frame_no += 1
    if frame_no % STATUS_EVERY == 1 or not cache:
        last_status = scope.query(":TRIG:STAT?")   # also where a reconnect (which clears the cache) happens
    status = last_status
    if not cache:
        state()
    tscale, toffs = cache["time.scale"], cache["time.offset"]
    out = {"tscale": tscale, "toffset": toffs, "status": status, "conn": scope.conn_id, "ch": {}}
    for ch in (1, 2):
        if not cache[f"ch{ch}.display"]:
            continue
        scale, offs = cache[f"ch{ch}.scale"], cache[f"ch{ch}.offset"]
        data = decode_block(scope.query_raw(f":WAV:DATA? CHAN{ch}"))[:WAVE_POINTS]
        # DS1000E byte -> volts (programming guide formula)
        volts = [round((240 - b) * (scale / 25) - (offs + scale * 4.6), 5) for b in data]
        out["ch"][ch] = {"scale": scale, "offset": offs, "v": volts}
    out["dt"] = tscale * SCREEN_DIVS_X / WAVE_POINTS
    return out


@app.get("/api/measure")
def measure(keys: str = "vpp,vavg,freq"):
    wanted = [k for k in keys.split(",") if k in MEASURES]
    out = {}
    for ch in (1, 2):
        if not cache.get(f"ch{ch}.display", True):
            continue
        out[ch] = {k: read_measure(scope.query(f":MEAS:{MEASURES[k]}? CHAN{ch}")) for k in wanted}
    return out


def main():
    global CMD_GAP_S
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--resource", default=None, help="VISA resource name (default: first Rigol found)")
    ap.add_argument("--gap", type=float, default=None, help=f"seconds between commands (default {CMD_GAP_S})")
    args = ap.parse_args()
    if args.gap is not None:
        CMD_GAP_S = args.gap
    global scope
    scope = Scope(args.resource)
    print(f"Serving on http://{args.host}:{args.port}/", flush=True)
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        scope.release()


if __name__ == "__main__":
    main()
