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
from fastapi.responses import FileResponse
from pydantic import BaseModel

CMD_GAP_S = 0.12        # the DS1000E drops commands sent closer than this (UltraScope uses the same gap)
WAVE_POINTS = 600       # "normal" points mode returns the 600 on-screen samples
SCREEN_DIVS_X = 12

# Settings the page may change: name -> (SCPI write template, SCPI query, type)
SETTINGS = {
    "ch1.display":  (":CHAN1:DISP {}", ":CHAN1:DISP?", "onoff"),
    "ch1.coupling": (":CHAN1:COUP {}", ":CHAN1:COUP?", "str"),
    "ch1.scale":    (":CHAN1:SCAL {}", ":CHAN1:SCAL?", "float"),
    "ch1.offset":   (":CHAN1:OFFS {}", ":CHAN1:OFFS?", "float"),
    "ch1.probe":    (":CHAN1:PROB {}", ":CHAN1:PROB?", "float"),
    "ch2.display":  (":CHAN2:DISP {}", ":CHAN2:DISP?", "onoff"),
    "ch2.coupling": (":CHAN2:COUP {}", ":CHAN2:COUP?", "str"),
    "ch2.scale":    (":CHAN2:SCAL {}", ":CHAN2:SCAL?", "float"),
    "ch2.offset":   (":CHAN2:OFFS {}", ":CHAN2:OFFS?", "float"),
    "ch2.probe":    (":CHAN2:PROB {}", ":CHAN2:PROB?", "float"),
    "time.scale":   (":TIM:SCAL {}", ":TIM:SCAL?", "float"),
    "time.offset":  (":TIM:OFFS {}", ":TIM:OFFS?", "float"),
    "trig.source":  (":TRIG:EDGE:SOUR {}", ":TRIG:EDGE:SOUR?", "str"),
    "trig.slope":   (":TRIG:EDGE:SLOP {}", ":TRIG:EDGE:SLOP?", "str"),
    "trig.sweep":   (":TRIG:EDGE:SWE {}", ":TRIG:EDGE:SWE?", "str"),
    "trig.level":   (":TRIG:EDGE:LEV {}", ":TRIG:EDGE:LEV?", "float"),
    "acq.type":     (":ACQ:TYPE {}", ":ACQ:TYPE?", "str"),
}

ACTIONS = {
    "run":    [":RUN"],
    "stop":   [":STOP"],
    "single": [":TRIG:EDGE:SWE SING", ":RUN"],
    "force":  [":FORCETRIG"],
    "auto":   [":AUTO"],
    "level50": [":TRIG%50"],
    "local":  [":KEY:FORC"],     # give the front panel back
}

MEASURES = {"vpp": ":MEAS:VPP? CHAN{}", "vavg": ":MEAS:VAV? CHAN{}", "freq": ":MEAS:FREQ? CHAN{}"}


class Scope:
    """Serialised SCPI access with the minimum gap the DS1000E needs between commands."""

    def __init__(self, resource=None):
        self.rm = pyvisa.ResourceManager()
        if resource is None:
            found = [r for r in self.rm.list_resources() if "0X1AB1" in r.upper()]   # Rigol USB vendor id
            if not found:
                raise SystemExit("No Rigol scope found by VISA")
            resource = found[0]
        self.dev = self.rm.open_resource(resource)
        self.dev.timeout = 3000
        self.lock = threading.Lock()
        self.last = 0.0
        self.idn = self.query("*IDN?")

    def _gap(self):
        wait = CMD_GAP_S - (time.monotonic() - self.last)
        if wait > 0:
            time.sleep(wait)

    def write(self, cmd):
        with self.lock:
            self._gap()
            self.dev.write(cmd)
            self.last = time.monotonic()

    def query(self, cmd):
        with self.lock:
            self._gap()
            reply = self.dev.query(cmd).strip()
            self.last = time.monotonic()
            return reply

    def query_raw(self, cmd):
        with self.lock:
            self._gap()
            self.dev.write(cmd)
            data = self.dev.read_raw()
            self.last = time.monotonic()
            return data


def parse_value(kind, text):
    if kind == "float":
        try:
            return float(text)
        except ValueError:
            return None
    if kind == "onoff":
        return text.upper() in ("1", "ON")
    return text


def format_value(kind, value):
    if kind == "onoff":
        return "ON" if value in (True, 1, "1", "ON", "on") else "OFF"
    if kind == "float":
        return f"{float(value):g}"
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
scope: Scope = None
HERE = Path(__file__).parent


class SetReq(BaseModel):
    name: str
    value: object


@app.get("/")
def index():
    return FileResponse(HERE / "index.html")


# Last known settings. Every query costs CMD_GAP_S, so the waveform loop reads scales from here
# instead of asking the scope each frame; /api/state refreshes it (also catches front panel changes).
cache = {}


@app.get("/api/state")
def state():
    for name, (_, q, kind) in SETTINGS.items():
        cache[name] = parse_value(kind, scope.query(q))
    return {"idn": scope.idn, **cache, "trig.status": scope.query(":TRIG:STAT?")}


@app.post("/api/set")
def set_value(req: SetReq):
    if req.name not in SETTINGS:
        raise HTTPException(400, "unknown setting")
    w, q, kind = SETTINGS[req.name]
    scope.write(w.format(format_value(kind, req.value)))
    cache[req.name] = parse_value(kind, scope.query(q))
    return {req.name: cache[req.name]}


@app.post("/api/action/{name}")
def action(name: str):
    if name not in ACTIONS:
        raise HTTPException(400, "unknown action")
    for cmd in ACTIONS[name]:
        scope.write(cmd)
    return {"ok": True}


@app.get("/api/wave")
def wave():
    if not cache:
        state()
    tscale, toffs = cache["time.scale"], cache["time.offset"]
    out = {"tscale": tscale, "toffset": toffs, "status": scope.query(":TRIG:STAT?"), "ch": {}}
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
def measure():
    out = {}
    for ch in (1, 2):
        if not cache.get(f"ch{ch}.display", True):
            continue
        out[ch] = {k: read_measure(scope.query(q.format(ch))) for k, q in MEASURES.items()}
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--resource", default=None, help="VISA resource name (default: first Rigol found)")
    args = ap.parse_args()
    global scope
    scope = Scope(args.resource)
    print(f"Connected: {scope.idn}")
    scope.write(":WAV:POIN:MODE NOR")
    try:
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    finally:
        scope.write(":KEY:FORC")


if __name__ == "__main__":
    main()
