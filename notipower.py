#!/usr/bin/env python3
"""NotiPower - plays a sound when the device is plugged in / unplugged.
Made for SteamOS (Steam Frame). Pure Python standard library: no tkinter, no pip.

  python3 notipower.py --install     add NotiPower to the app list (Steam "+" > Add a Non-Steam Game)
  python3 notipower.py               open the window (a small local web page in your browser)
  python3 notipower.py --background  no window, just watch power (used by "launch when headset starts")
"""
import array, base64, fcntl, glob, json, os, re, shutil, subprocess, sys, tempfile, threading, time, wave
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote
import secrets, urllib.request

HOME = os.path.expanduser("~")
CFG_DIR = os.path.join(HOME, ".config/notipower")
CFG = os.path.join(CFG_DIR, "settings.json")
LOCK = os.path.join(CFG_DIR, "bg.lock")
SOUNDS = os.path.join(CFG_DIR, "sounds")
BUILTIN_DIR = os.path.join(CFG_DIR, "builtin")
CACHE_DIR = os.path.join(HOME, ".cache/notipower")
APP_DIR = os.path.join(HOME, ".local/share/notipower")
APP_PATH = os.path.join(APP_DIR, "notipower.py")
DESKTOP = os.path.join(HOME, ".local/share/applications/notipower.desktop")
ICON = os.path.join(HOME, ".local/share/icons/notipower.svg")
UNIT = os.path.join(HOME, ".config/systemd/user/notipower.service")
PORT = 47865
VERSION = "1.2.0"            # bump this whenever you publish a new version to GitHub
DEFAULT_REPO = "YOUR_USERNAME/notipower"
REPO_FILE = os.path.join(CFG_DIR, "repo")      # install.sh writes "user/repo@branch" here
RAW_BASE = os.environ.get("NOTIPOWER_RAW_BASE", "https://raw.githubusercontent.com")
RATE = 44100

# ---------- built-in sounds + settings ----------
def clamp_vol(v):
    try: return max(0, min(100, int(v)))
    except (TypeError, ValueError): return 100

def make_defaults():
    """Write the built-in sounds (from the lip-roll recording) to the home folder."""
    os.makedirs(BUILTIN_DIR, exist_ok=True); os.makedirs(SOUNDS, exist_ok=True)
    paths = []
    for k in ("charging", "not_charging"):
        p = os.path.join(BUILTIN_DIR, k + ".wav"); data = base64.b64decode(BUILTIN[k])
        if not os.path.exists(p) or os.path.getsize(p) != len(data):
            open(p, "wb").write(data)
        paths.append(p)
    return paths

def load_cfg():
    plug, unplug = make_defaults()
    cfg = {"charging_on": True, "not_charging_on": True,
           "charging_file": plug, "not_charging_file": unplug,
           "charging_vol": 100, "not_charging_vol": 100}
    try: cfg.update(json.load(open(CFG)))
    except Exception: pass
    old = {os.path.join(CFG_DIR, "charging.wav"), os.path.join(CFG_DIR, "not_charging.wav")}
    for k, new in (("charging", plug), ("not_charging", unplug)):   # move off the old beeps
        if cfg[k + "_file"] in old: cfg[k + "_file"] = new
        cfg[k + "_vol"] = clamp_vol(cfg[k + "_vol"])
    return cfg

def save_cfg(cfg):
    os.makedirs(CFG_DIR, exist_ok=True); json.dump(cfg, open(CFG, "w"))

# ---------- playback (with volume) ----------
def scaled_wav(path, g):
    """Copy of a 16-bit wav scaled by g (0..1) in the home cache folder, or None."""
    try:
        with wave.open(path, "rb") as w:
            if w.getsampwidth() != 2: return None
            params, frames = w.getparams(), w.readframes(w.getnframes())
        a = array.array("h"); a.frombytes(frames)
        if sys.byteorder == "big": a.byteswap()
        a = array.array("h", [int(v * g) for v in a])
        if sys.byteorder == "big": a.byteswap()
        os.makedirs(CACHE_DIR, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=CACHE_DIR); os.close(fd)
        with wave.open(tmp, "wb") as o: o.setparams(params); o.writeframes(a.tobytes())
        return tmp
    except Exception:
        return None

def volume_args(player, g):
    """Fallback for non-wav files: ask the player itself to play quieter."""
    return {"pw-play": ["--volume", f"{g:.3f}"],
            "paplay": [f"--volume={int(65536 * g ** (1 / 3))}"],
            "ffplay": ["-volume", str(int(100 * g))],
            "mpg123": ["-f", str(int(32768 * g))],
            "mpv": [f"--af=lavfi=[volume={g:.3f}]"]}.get(player, [])

def _cleanup(proc, tmp):
    try: proc.wait(timeout=60)
    except Exception: pass
    try: os.remove(tmp)
    except OSError: pass

def play(path, volume=100):
    """volume is the slider percent (0-100). 50% is about half as loud (-12 dB)."""
    if not path or not os.path.exists(path): return
    g = (clamp_vol(volume) / 100) ** 2
    if g <= 0: return
    mp3 = path.lower().endswith(".mp3")
    order = (["ffplay", "mpg123", "mpv", "pw-play", "paplay"] if mp3
             else ["pw-play", "paplay", "ffplay", "mpg123", "mpv", "aplay"])
    base = {"ffplay": ["-nodisp", "-autoexit", "-loglevel", "quiet"],
            "mpg123": ["-q"], "mpv": ["--no-video", "--really-quiet"]}
    player = next((p for p in order if shutil.which(p)), None)
    if not player: return
    target, tmp, extra = path, None, []
    if g < 0.999:
        tmp = scaled_wav(path, g)
        if tmp: target = tmp
        else: extra = volume_args(player, g)
    proc = subprocess.Popen([player, *base.get(player, []), *extra, target],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if tmp: threading.Thread(target=_cleanup, args=(proc, tmp), daemon=True).start()

# ---------- power detection ----------
def is_plugged_in():
    plugged = None
    for d in glob.glob("/sys/class/power_supply/*"):
        def rd(n):
            try: return open(os.path.join(d, n)).read().strip()
            except OSError: return None
        if rd("type") == "Battery":
            st = rd("status")
            if st in ("Charging", "Full"): return True
            if st == "Discharging" and plugged is None: plugged = False
        elif rd("online") == "1":
            return True
        elif rd("online") == "0" and plugged is None:
            plugged = False
    return plugged

class PowerWatcher:
    def __init__(self): self.state = is_plugged_in()
    def check(self, cfg, mute=False):
        now = is_plugged_in()
        if now is not None and self.state is not None and now != self.state and not mute:
            k = "charging" if now else "not_charging"
            if cfg[k + "_on"]: play(cfg[k + "_file"], cfg[k + "_vol"])
        if now is not None: self.state = now

# ---------- background mode ----------
_lock_handle = None
def bg_running():
    """True if a --background copy is holding the lock."""
    os.makedirs(CFG_DIR, exist_ok=True)
    f = open(LOCK, "a")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(f, fcntl.LOCK_UN); f.close(); return False
    except OSError:
        f.close(); return True

def run_background():
    global _lock_handle
    os.makedirs(CFG_DIR, exist_ok=True)
    _lock_handle = open(LOCK, "a")
    try: fcntl.flock(_lock_handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError: sys.exit(0)           # already running
    w = PowerWatcher()
    while True:
        w.check(load_cfg())               # re-read so window changes apply live
        time.sleep(1)

# ---------- install / launch-at-start ----------
ICON_SVG = ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
            '<rect width="64" height="64" rx="12" fill="#0e0f12"/>'
            '<path d="M14 26l12-5v22l-12-5z" fill="none" stroke="#e8e8ea" stroke-width="3" stroke-linejoin="round"/>'
            '<path d="M34 24a10 10 0 0 1 0 16M40 19a17 17 0 0 1 0 26" fill="none" stroke="#22c55e" stroke-width="3" stroke-linecap="round"/>'
            '</svg>')

def install():
    for p in (APP_DIR, os.path.dirname(DESKTOP), os.path.dirname(ICON)):
        os.makedirs(p, exist_ok=True)
    if os.path.abspath(__file__) != APP_PATH:
        shutil.copy(os.path.abspath(__file__), APP_PATH)
    os.chmod(APP_PATH, 0o755)
    open(ICON, "w").write(ICON_SVG)
    py = shutil.which("python3") or "/usr/bin/python3"
    open(DESKTOP, "w").write(
        "[Desktop Entry]\nType=Application\nName=NotiPower\n"
        "Comment=Plays a sound when power is connected or disconnected\n"
        f"Exec={py} {APP_PATH}\nIcon={ICON}\nTerminal=false\n"
        "Categories=Utility;\n")
    os.chmod(DESKTOP, 0o755)
    if shutil.which("update-desktop-database"):
        subprocess.run(["update-desktop-database", os.path.dirname(DESKTOP)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    print("Installed. In Steam: + (Add a Game) > Add a Non-Steam Game > NotiPower.")

def systemctl(*a):
    return subprocess.run(["systemctl", "--user", *a], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode

def autostart_enabled(): return systemctl("is-enabled", "notipower.service") == 0

def set_autostart(on):
    if on:
        install()
        os.makedirs(os.path.dirname(UNIT), exist_ok=True)
        py = shutil.which("python3") or "/usr/bin/python3"
        open(UNIT, "w").write(
            "[Unit]\nDescription=NotiPower\n\n[Service]\n"
            f"ExecStart={py} {APP_PATH} --background\nRestart=on-failure\n\n"
            "[Install]\nWantedBy=default.target\n")
        systemctl("daemon-reload"); systemctl("enable", "--now", "notipower.service")
    else:
        systemctl("disable", "--now", "notipower.service")

# ---------- self-update (from your GitHub repo) ----------
def vtuple(v):
    try: return tuple(int(x) for x in v.split("."))
    except ValueError: return (0,)

def repo_and_branch():
    try: r = open(REPO_FILE).read().strip()
    except OSError: r = DEFAULT_REPO
    if not r or "YOUR_USERNAME" in r: return None, None
    repo, _, branch = r.partition("@")
    return repo, branch or "main"

def fetch_latest():
    """Returns (source_text, version_string) of the newest notipower.py, or None."""
    repo, branch = repo_and_branch()
    if not repo: return None
    try:
        req = urllib.request.Request(f"{RAW_BASE}/{repo}/{branch}/notipower.py",
                                     headers={"User-Agent": "NotiPower"})
        text = urllib.request.urlopen(req, timeout=8).read(1_000_000).decode("utf-8")
        m = re.search(r'^VERSION = "([\d.]+)"', text, re.M)
        compile(text, "notipower.py", "exec")          # make sure it's valid Python
        return (text, m.group(1)) if m else None
    except Exception:
        return None

def check_update():
    got = fetch_latest()
    if got and vtuple(got[1]) > vtuple(VERSION):
        UI.latest = got[1]; UI.update_state = "available"

def do_update():
    UI.update_state = "updating"
    got = fetch_latest()
    if not got or vtuple(got[1]) <= vtuple(VERSION):   # nothing newer / download problem
        UI.update_state = "failed"; return
    try:
        os.makedirs(APP_DIR, exist_ok=True)
        tmp = APP_PATH + ".new"
        open(tmp, "w").write(got[0]); os.chmod(tmp, 0o755)
        os.replace(tmp, APP_PATH)                      # swap in the new version
        if autostart_enabled(): systemctl("restart", "notipower.service")
        UI.update_state = "done"
    except Exception:
        UI.update_state = "failed"

# ---------- native file picker (KDE's own dialog, same look as Dolphin) ----------
AUDIO_EXT = (".mp3", ".wav", ".ogg", ".flac")

def choose_file_dialog():
    """Returns the chosen path, "" if cancelled, or None if no picker is installed."""
    if shutil.which("kdialog"):
        cmd = ["kdialog", "--title", "Choose a sound", "--getopenfilename", HOME,
               "*.mp3 *.wav *.ogg *.flac|Audio files\n*|All files"]
    elif shutil.which("zenity"):
        cmd = ["zenity", "--file-selection", "--title=Choose a sound", f"--filename={HOME}/",
               "--file-filter=Audio files | *.mp3 *.wav *.ogg *.flac", "--file-filter=All files | *"]
    else:
        return None
    try: r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except Exception: return None
    if r.returncode not in (0, 1): return None
    return r.stdout.strip()

# ---------- the window (local web page) ----------
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>NotiPower</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0e0f12;--fg:#e8e8ea;--dim:#6b6e76;--green:#22c55e;--line:#2a2c33}
*{box-sizing:border-box}html,body{height:100%;margin:0}
body{background:var(--bg);color:var(--fg);font-family:system-ui,"DejaVu Sans",sans-serif;
display:flex;flex-direction:column;padding:26px 36px;user-select:none}
h1{font-weight:400;font-size:22px;margin:0}
header{display:flex;justify-content:space-between;align-items:center;min-height:38px}
#upd{background:none;color:var(--fg);border:1.5px solid var(--green);border-radius:999px;padding:7px 18px;
font:inherit;font-size:14px;cursor:pointer}
#upd:hover{background:rgba(34,197,94,.1)}
#upd.updating{border-color:var(--line);color:var(--dim);cursor:default}
#upd.done{color:var(--green);cursor:default}
#upd.failed{border-color:#e5484d;color:#e5484d}
main{flex:1;display:flex;justify-content:space-around;align-items:center;gap:20px;flex-wrap:wrap}
.col{display:flex;flex-direction:column;align-items:center}
.tg{width:106px;height:30px;background:none;border:2px solid var(--line);color:var(--dim);
display:flex;align-items:center;gap:0;padding:0 8px;font-size:13px;cursor:pointer;font-family:inherit}
.tg i{width:14px;height:14px;border-radius:50%;background:var(--dim);display:block}
.tg b{flex:1;font-weight:400}
.tg.on{border-color:var(--green);color:var(--green)}.tg.on i{background:var(--green)}
.col h2{font-weight:400;font-size:28px;margin:22px 0 22px}
.sp{cursor:pointer;background:none;border:0;padding:0}
.sp svg{width:150px;height:110px;fill:none;stroke:var(--fg);stroke-width:2;stroke-linejoin:round}
.sp:active svg{stroke:var(--green)}
.file{margin-top:16px;min-width:210px;padding:8px 12px;border:1px solid var(--line);background:none;
color:var(--fg);font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-size:12px;cursor:pointer;
white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:260px}
.file:hover{border-color:var(--dim)}
.ctl{display:flex;align-items:center;gap:14px;margin-top:14px;flex-wrap:wrap;justify-content:center}
.test{display:flex;align-items:center;gap:7px;background:none;border:1px solid var(--line);color:var(--fg);
padding:6px 14px;font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-size:12px;cursor:pointer}
.test svg{width:10px;height:10px;fill:var(--fg)}
.test:hover{border-color:var(--dim)}.test:active{border-color:var(--green)}.test:active svg{fill:var(--green)}
.vw{display:flex;align-items:center;gap:10px}
.pct{font-family:ui-monospace,"DejaVu Sans Mono",monospace;font-size:12px;color:var(--dim);min-width:38px}
.vol{-webkit-appearance:none;appearance:none;width:130px;height:20px;background:none;margin:0;cursor:pointer;--p:100%}
.vol::-webkit-slider-runnable-track{height:2px;background:linear-gradient(to right,var(--green) var(--p),var(--line) var(--p))}
.vol::-moz-range-track{height:2px;background:linear-gradient(to right,var(--green) var(--p),var(--line) var(--p))}
.vol::-webkit-slider-thumb{-webkit-appearance:none;width:14px;height:14px;border-radius:50%;background:var(--green);border:0;margin-top:-6px}
.vol::-moz-range-thumb{width:14px;height:14px;border-radius:50%;background:var(--green);border:0}
footer{border-top:1px solid var(--line);padding-top:18px;display:flex;flex-direction:column;align-items:center;gap:8px;
margin:0 auto;width:min(700px,100%)}
footer span{font-size:16px}
</style></head><body>
<header><h1>NotiPower</h1><button id="upd" hidden></button></header>
<main>
<div class="col" data-k="charging"><button class="tg" data-t="charging_on"><i></i><b>on</b></button>
<h2>charging</h2>
<button class="sp" title="Play"><svg viewBox="0 0 150 110"><polygon points="30,45 62,35 62,85 30,75"/>
<path d="M75.8 48.4A18 18 0 0 1 75.8 71.6M86.5 39.4A32 32 0 0 1 86.5 80.6M97.2 30.4A46 46 0 0 1 97.2 89.6"/></svg></button>
<button class="file" title="Choose a sound file"></button>
<div class="ctl"><button class="test" title="Play this sound"><svg viewBox="0 0 10 10"><polygon points="1,0 10,5 1,10"/></svg>test</button>
<div class="vw"><input type="range" class="vol" min="0" max="100" value="100" title="Volume"><span class="pct">100%</span></div></div></div>
<div class="col" data-k="not_charging"><button class="tg" data-t="not_charging_on"><i></i><b>on</b></button>
<h2>not charging</h2>
<button class="sp" title="Play"><svg viewBox="0 0 150 110"><polygon points="30,45 62,35 62,85 30,75"/>
<path d="M75.8 48.4A18 18 0 0 1 75.8 71.6M86.5 39.4A32 32 0 0 1 86.5 80.6M97.2 30.4A46 46 0 0 1 97.2 89.6"/></svg></button>
<button class="file" title="Choose a sound file"></button>
<div class="ctl"><button class="test" title="Play this sound"><svg viewBox="0 0 10 10"><polygon points="1,0 10,5 1,10"/></svg>test</button>
<div class="vw"><input type="range" class="vol" min="0" max="100" value="100" title="Volume"><span class="pct">100%</span></div></div></div>
</main>
<footer><button class="tg" id="auto"><i></i><b>on</b></button><span>launch when headset starts</span></footer>
<input type="file" id="pick" accept=".mp3,.wav,.ogg,.flac,audio/*" hidden>
<script>
const T="__TOKEN__";let st={},pickKey=null;
const H=(x)=>Object.assign({"X-Token":T},x||{});
const post=(p,o)=>fetch(p,{method:"POST",headers:H({"Content-Type":"application/json"}),body:JSON.stringify(o||{})});
function setTg(el,on){el.classList.toggle("on",!!on);el.querySelector("b").textContent=on?"on":"off"}
function paintVol(c){const v=c.querySelector(".vol");v.style.setProperty("--p",v.value+"%");c.querySelector(".pct").textContent=v.value+"%"}
function draw(){
 document.querySelectorAll("[data-t]").forEach(e=>setTg(e,st[e.dataset.t]));
 setTg(document.getElementById("auto"),st.autostart);
 document.querySelectorAll(".col").forEach(c=>{const k=c.dataset.k,v=c.querySelector(".vol");
  c.querySelector(".file").textContent=st[k+"_name"]||"";
  if(document.activeElement!==v&&st[k+"_vol"]!==undefined){v.value=st[k+"_vol"];paintVol(c)}});
 const u=document.getElementById("upd"),M={available:"Update available :3",updating:"Updating\u2026",
  done:"Updated! Restart the app :3",failed:"Update failed :( tap to retry"};
 u.hidden=!M[st.update];u.textContent=M[st.update]||"";u.className=st.update||""}
async function load(){st=await (await fetch("/api/state",{headers:H()})).json();draw()}
document.querySelectorAll("[data-t]").forEach(e=>e.onclick=async()=>{
 st[e.dataset.t]=!st[e.dataset.t];draw();await post("/api/set",{key:e.dataset.t,value:st[e.dataset.t]})});
document.getElementById("auto").onclick=async()=>{st.autostart=!st.autostart;draw();
 await post("/api/autostart",{on:st.autostart});load()};
document.querySelectorAll(".col").forEach(c=>{
 c.querySelector(".sp").onclick=()=>post("/api/play",{key:c.dataset.k});
 const k=c.dataset.k,v=c.querySelector(".vol");
 v.oninput=()=>{paintVol(c);st[k+"_vol"]=+v.value;clearTimeout(v._t);
  v._t=setTimeout(()=>post("/api/volume",{key:k,value:+v.value}),150)};
 c.querySelector(".test").onclick=()=>post("/api/play",{key:k,volume:+v.value});
 c.querySelector(".file").onclick=async()=>{const k=c.dataset.k;
  const r=await (await post("/api/choose",{key:k})).json();
  if(r.fallback){pickKey=k;document.getElementById("pick").click()}else load()}});
document.getElementById("pick").onchange=async e=>{const f=e.target.files[0];if(!f)return;
 await fetch("/api/upload?key="+pickKey,{method:"POST",headers:H({"X-Filename":encodeURIComponent(f.name)}),body:f});
 e.target.value="";load()};
document.getElementById("upd").onclick=async()=>{
 if(st.update!=="available"&&st.update!=="failed")return;
 st.update="updating";draw();await post("/api/update");load()};
setInterval(()=>post("/api/ping"),3000);setInterval(load,6000);load();post("/api/ping");
</script></body></html>"""

class UI:
    token = secrets.token_urlsafe(16)
    last_ping = None
    update_state = "none"   # none | available | updating | done | failed
    latest = None
    started = time.time()

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def _send(self, code=200, body=b"", ctype="application/json"):
        if isinstance(body, (dict, list)): body = json.dumps(body).encode()
        elif isinstance(body, str): body = body.encode()
        self.send_response(code); self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

    def _host_ok(self):   # blocks DNS-rebinding tricks from other websites
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok(): return self._send(403)
        if self.path == "/":
            return self._send(body=PAGE.replace("__TOKEN__", UI.token), ctype="text/html; charset=utf-8")
        if self.path == "/api/state" and self.headers.get("X-Token") == UI.token:
            c = load_cfg()
            return self._send(body={
                "charging_on": c["charging_on"], "not_charging_on": c["not_charging_on"],
                "charging_name": os.path.basename(c["charging_file"]),
                "not_charging_name": os.path.basename(c["not_charging_file"]),
                "charging_vol": c["charging_vol"], "not_charging_vol": c["not_charging_vol"],
                "autostart": autostart_enabled(),
                "update": UI.update_state, "version": VERSION, "latest": UI.latest})
        self._send(404)

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-Token") != UI.token: return self._send(403)
        path, _, query = self.path.partition("?")
        n = int(self.headers.get("Content-Length") or 0)
        if n > 25_000_000: return self._send(413)
        raw = self.rfile.read(n)
        cfg = load_cfg()
        if path == "/api/ping":
            UI.last_ping = time.time()
        elif path == "/api/set":
            d = json.loads(raw or b"{}")
            if d.get("key") in ("charging_on", "not_charging_on"):
                cfg[d["key"]] = bool(d.get("value")); save_cfg(cfg)
        elif path == "/api/autostart":
            set_autostart(bool(json.loads(raw or b"{}").get("on")))
        elif path == "/api/play":
            d = json.loads(raw or b"{}"); k = d.get("key")
            if k in ("charging", "not_charging"):
                v = clamp_vol(d.get("volume", cfg[k + "_vol"]))
                if "volume" in d: cfg[k + "_vol"] = v; save_cfg(cfg)
                play(cfg[k + "_file"], v)
        elif path == "/api/volume":
            d = json.loads(raw or b"{}"); k = d.get("key")
            if k in ("charging", "not_charging"):
                cfg[k + "_vol"] = clamp_vol(d.get("value", 100)); save_cfg(cfg)
        elif path == "/api/update":
            do_update()
            return self._send(body={"ok": UI.update_state == "done"})
        elif path == "/api/choose":
            k = json.loads(raw or b"{}").get("key")
            if k not in ("charging", "not_charging"): return self._send(400)
            p = choose_file_dialog()
            if p is None: return self._send(body={"fallback": True})
            if p and os.path.isfile(p) and p.lower().endswith(AUDIO_EXT):
                os.makedirs(SOUNDS, exist_ok=True)
                dest = os.path.join(SOUNDS, os.path.basename(p))
                if os.path.abspath(p) != os.path.abspath(dest): shutil.copy2(p, dest)
                cfg = load_cfg(); cfg[k + "_file"] = dest; save_cfg(cfg); play(dest, cfg[k + "_vol"])
        elif path == "/api/upload":
            k = query.replace("key=", "")
            name = re.sub(r"[^\w.\- ]", "_", os.path.basename(unquote(self.headers.get("X-Filename", "sound.wav"))))
            if k not in ("charging", "not_charging") or not name.lower().endswith((".mp3", ".wav", ".ogg", ".flac")):
                return self._send(400)
            os.makedirs(SOUNDS, exist_ok=True)
            dest = os.path.join(SOUNDS, name); open(dest, "wb").write(raw)
            cfg[k + "_file"] = dest; save_cfg(cfg); play(dest, cfg[k + "_vol"])
        else:
            return self._send(404)
        self._send(body={"ok": True})

CHROMIUMS = ["chromium", "chromium-browser", "google-chrome", "google-chrome-stable",
             "brave-browser", "microsoft-edge"]
FLATPAKS = [("org.chromium.Chromium", True), ("com.google.Chrome", True),
            ("com.brave.Browser", True), ("org.mozilla.firefox", False)]

def open_window(url):
    """Open the page in an app-style browser window. Tries what SteamOS usually has."""
    print("NotiPower is at", url)
    dev = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for b in CHROMIUMS:
        if shutil.which(b):
            subprocess.Popen([b, f"--app={url}", "--window-size=900,700"], **dev); return
    if shutil.which("flatpak"):
        for fid, chromium_like in FLATPAKS:
            if subprocess.run(["flatpak", "info", fid], **dev).returncode == 0:
                extra = [f"--app={url}"] if chromium_like else ["--new-window", url]
                subprocess.Popen(["flatpak", "run", fid, *extra], **dev); return
    if shutil.which("firefox"):
        subprocess.Popen(["firefox", "--new-window", url], **dev); return
    if shutil.which("xdg-open"):
        subprocess.Popen(["xdg-open", url], **dev); return
    print("No browser found - open that address in any browser.")

def run_gui(open_browser=True):
    url = f"http://127.0.0.1:{PORT}/"
    try: srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:                       # already running: just show the window again
        if open_browser: open_window(url)
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    install()
    threading.Thread(target=check_update, daemon=True).start()
    if open_browser: open_window(url)
    watch = PowerWatcher()
    while True:                           # quit shortly after the window is closed
        time.sleep(1)
        watch.check(load_cfg(), mute=bg_running())   # background service plays instead, if on
        if UI.last_ping is None:
            if time.time() - UI.started > 90: break
        elif time.time() - UI.last_ping > 20: break

def main():
    if "--install" in sys.argv: install()
    elif "--background" in sys.argv: run_background()
    else: run_gui(open_browser="--no-browser" not in sys.argv)

# ---------- built-in sounds (cut from your lip-roll recording, 22.05 kHz mono wav) ----------
BUILTIN = {
"charging": """
UklGRj5QAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YRpQAAAAAAAAAAAAAAAAAAABAAAAAAABAAEAAAABAAEAAAAA
AAAAAQABAAAAAAAAAAAA/v////7//P/+//b/+v/7/wIA//8CAAQA+f8EAPj/AQDz/wEA+v/+//3////3/+3/+P/4//3/9/8LAAoA
EgAHAA0ABgALAAsADQASABIAEgAKAA0ADAASAA4AGAAXABIADgANAA4ACAALAA4AEwARABMAEwAWABIADgAOAA4ACAACAP7/AgAE
AAkABAAEAAUABQANAAsADgABAPn/+P///wAABAAOAA4AEQAGAAgA+//3//j//f/6/+//8P/q/+L/4v/j/+f/4//h/9//5//z//L/
9P/4/wIA+v/o/+v/5v/6/9f/8//d//L/9//z//7/8P8MAPL//v/y/wAA6f/n//D/9//o/+3/+//8//j/7P/x//P/BwADABQAFAAR
ABMABwD9/wIA+f/h/+n/5//1//f//v8AAAAADQAfABMA/v/9/wQAHQAIAO3/7/8GAB4AGgASAAgAGQArAAAA4f/X//z/GQDs/ycA
CgAUAOP/9/8LAAIA9P/j/+L/AADz/+D/pP+7/+b/4v/e/8D/0v/2//L/+v/c/83/7v/2/ygA6v8AAOz/CgD6/9P//f/p/xsA6P/6
/xkACADw/+r/CgAQAC0ABAARAO3/HwATABEABQAOAAIA///8/wAAEgDg/8j/GQDg//j/4/+x/6T/5P8BAAkA5v/T/9D/y/+u//j/
3P/M/+f/iv+i/6T/tf+r/5//j/+t/3j/gv9w/6H/SP/b/hf/Wf+T/3f/GP9O/0H/Hf/v/gL/Bf8A/+j+of7Q/tD+mP5r/k7+AP42
/lL+Cf4X/qP91v0N/s395/2t/Yj9ff1w/aH9SP1A/ev87/wi/Q799/wD/a384vyu/Jn8tfxH/Cb85Pvh+wn8AfyC/Bz85/se/Gb6
t/o58irhSKIvirmEqNfZxQ3JELVcnxnDQ8Zh5OTUc8Ky3FvrQDWDNtBSVEWCVpFjP1IgQepTAkPLSuguYlMEZrdZHFCLUl1v0FvQ
SdtQD1QNUx1FMjy8R+NNG1KJVeRDtEahQtdE1j0MTFVbxFenVvFPtkmnJRHpv96QuNalk6vZumHJN8zJ1ynaItbA04vUydXV1V/M
+8LlxRTIL8U4vca8fcHQxi/B1r3PxLHKYcqWzS7Rp9ND0yPX2NrX2BnZVd3I463m7ege6Y/kP+o97hbtR+3F7lLtEO838R71B+4K
73H1PPTU7NDqGO6C7E/oMeMo4nzoZOnY7FPsDe6F4gjOT5cBrEzd1a6J0gDESsX9vPSqDafZmuKY9J5To7K2MrczumK8U7mfvUO9
sMBmwefRRNBF0zPa/ubG9xsapC/XOaxSTlBxZeJdymh6XV5sPGQXYzFmn2aZZ/pkvV+EXwFdhl1IY+1hKmC9W+1Y31xjWiFYzWEJ
YnpqrGA5WTxRtVbTV7lMU0aCU0hVvFb/Vc5QIlCsQrg33UFZRCZGhUavO1kAm9wu2HjTL8d9zhfGF7ZBt1m1JLg7vdjAxMPavoG8
8bxNvKW6u7ezt8K1Q7U2tza6B73rvGu6R7tSxdTJsczbyn3KWsx6yr3Nu9Qq19XZ+doj2bbdzN784lHh0dsC2z/bHN8S4vviJOV3
47fjHOEE4frfTuLR4GjeV95n3vjfLN+y3bbeaN0a4LPgvOTU5ZHjCOO75W3lbeSXxFOVsYz9lT+egJxUpr6rBbQCuZe+qcCjw5jF
oMbax1rIOskzyu3JcMyezCzPU89k360I6gVvNIZLxFbkXLRna3CEchNy8W8Fb9hxsHCwbolp6mxuYzZfqFUMTl5dNWEsXzVj/mal
ZpBiKGM4a85psWieZh5g/lizWY5Ysla1VSlUV1LaULJNrkymSwRJvkdhRbZEb0LYQLs/Wj1wOjg3njb9NX8z6DBQLeMrHis0KK4n
iyaGI0v/y8fAsbevq6yrsCiu66Sjo86jjKGvmOSYu5nxmpeedZ8QoDafbp4joEifJZ/2oDGgvqDIoFCg46Kqo4inW6wdrzaujrJW
t0649r2jwADBwMFhw+HCf8QNx9XKiM1vzljOjNFM1ivWwtos3M/bOd5h4CDh8du437rfoeVz1R3GuaZ4noPp378I1gzHpKcHp5Oi
ebWMr8m41brGwOTDKceyyMvLGM5Lz0DSDtMq05/VHdbC1lzZ59og3TzfBOKU4trlZOia76IZwC16O2dFID2lTihLqVJSWLlaa2FK
XS9hAmWRZgFmxmXmZyZmYWLvaiNtc2zhaypuwnFockpvFnA0bjJxXXH5bnFuPWsfaVBm0GMAYv9fAl8EXnxcmVqkWZdYO1fzVENT
AFLUT1tO4EusSRRIckUERPFBbD/xPZ072TmON5w1RjNgMRIwVS3VK5gpmieFIXsDOgEYBIkF4gB1ACUMqQ6FCor2aerw6m3hu9uT
0h/KxL31qZKf6JavlX+TWpX7k9qSMZbVkkaUMZFYkL+O4o6sj/KQppPok1OVmpWPlqeXj5jimVma7JtUnX6fCqI7pGmpL62Mr9Cv
97J0t5q6Ob8AwujH/M1J0kLU0NXm2UXdOeDN4Z3jn+al52Xqdu2Q72DxAvO289fz5/P69kD3tPb29/P58fu0+gv7cPlN+ub6Ff0/
/7b9meMiqpKXIrcFtZ2sr7UyuorD08rZ0WDWndp93GLdeN7t3sDfP+Du4FriaONY5OPkPOeT6M3q0esA8zL/UBxHPi9O+F1fZ0Bq
222tcHNwyW4nbj5u22ukaJhm/mMkYxthxlslYMlhPGNnYfpk42oOapRsgnAdcWFygHEvcaFujmvVZzxkkWFjX3Jdy1vaWpxZP1g2
VxFWfVTLUnxR8E/tTStMdEoySCpGskOXQbs+vT1uLtYbaA18BQn8ggJSFLoa5SKGHw8jdyf/Kk4UD/I17u70PPkdAOP5R/P+82r4
Sfcv9E72QwGv+Znpgeyp76v5uft387fs/uxI49fYWdZCztzIXMAUwI3JYNRY4JTdw8wLySPKC8iaw/7EC8EnwF28hsNpwiy0p6ks
pnilf6H1ldmRf5CHlBiZopbylaOWM5WSkw6SppBJktCTwpY0mI2ZlZlUmoGcBp6Un4ShIqLro+ikEaY3qjytXK+3sli1y7XNt/O5
jb9uxufGjcrFz9HSb9VT183YKd2R4XPkkOZB5mXqHO9e8Trz1PIj84H23fld/xUCiwcbCikKzQqdDFgQuBC2EnQVShcjF1YSNeii
tEapVaLUuDfZANID3B3cJ+GL49/mcemB7M3utfDm8WTyUPIe8gzyIvR+9cn2kvaD/XcQgxmpKps+F1IiXbVlzGnlaq5tMG/Jb41w
eHCDbYJo1mdeZaJiRl9cYbdf5FxjW0tebGP/ZiJnJGUjZcll42WYZLlmM2RrYoxd3lgFV4lWQVWaVXdYiVdiWABV7VDHTsNT/1Z1
WKpb6l9HZERkI2GOXPVWrVFOTfdJW0dARcZDNUL7P5k++DwkO2soyyCkJ0glMxzBGBMeEiMiGn0GfvFT6OXlgePs3kbYb9/34ZTT
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
dMZwyBzOIdL700rTCNIU0EXOGs4FzWPMWMlbxijFQ8c1zLzS/drq4UPqD/UsAqAPwBqoIRIlrSiRK6Is9Sr0KZ4pwyxQLw80lDv4
Qp5IqE2sUDRRq1HeT35KUEMrPO4y/SgtIksdFxqsFekRgg5uCbMDLf55+u74j/mH+jX5cPV38Trune0q7I3ppegh5XDiE+L049Pm
aOf+5cPivd6W3+Xiruc97vTzDPiZ/HwB1wJOABX4cvJr8d3yrfQv9/j4B/np9oz35/Wy8sPyqvIk85byafEu8V7xuvHS8cvwIvDr
8MXyo/QX9FDzDfIE8kjzXfTo89DyOvGG63znkucS6bDqi+xL7r/vufE2887zRPSx9GX1h/ZP98L3bvgw+Sf5Yvml+Ub6lftg/HP8
F/0n/nn+2f6x/+EA3QECA+ADegQLBYIFBwbqBr4HmQjaCCwJZgoODLAN1g4GEN0RYRP5EwsUXxTrFAoVnhThE8ETbxTOFGsUxhRX
FT0WEBeNF/AXdhjBGMcYkxhyGI0YohjJF+YWshaEFnQWghaFFjIW8hUxFtgV4RTCE0QSvhArEDgPaQ6gDRoM8gozCgkJYwdoBvAF
tQViBaEEWARLBC4EvgRzBc8FAAb+BfwFHAWkBMEEygXkBcUECAScA18D+gN1BP4DQQS4BG0FygV4BsQGtQZxBvQFigVzBYMFjwWi
BR0G4gUEBg0HHAfhBhEGUQWkAhz/beoPu6OVAIcpnZCuGboMymu256d3tbi2C730v2vEh8ydynDA9MM6z23OnM+E0W7KtMs12dvl
tfIJAm4StCekPC9Ra13aYXtjBGf7ajFukXA3cOpukG2wbBJqqmeIZJ1gIV1tWnlY61b4VY5UQlLvTyNFMDyLNaUnNhTqCFX5K+ca
247RUMiRwg66j7I0s7exOa4arHatbK0/rXGv77BfscSxsLH1tEq5O7waxDLMf9Hc08vXad9L59Pxy/9GD98YCR66JsAviTPVOGlB
BEoQUDRU2Vh/WwleDmATYs9hdV1lV7tQokomRjFDPEEXQB8/xT3TPMk3IylMHK0TGAdl+SDq2diBy9TEVcFXvbi8TbbArDSm3qET
nZmbXZrgmGOX3JfVmO+ayp4zooWkXahFrEix6bhTwfHFzsjyymXKOs7T1NXajeFk6Vr0xgJrFdMntziCR3pRg1nPYKVmE2rAavZo
5GPWW0xS70byO9Etrx7nEMIFq/vF+dz6mPj29q7zYO4n5wzinN9Y2qLTxcdUu/itzaSfn2Sba5ngly2XPJeNmWGdjKO3qgewirZN
v4HIIdJ72j3fb9x12qHXLNd518LYutn63Mrfv+Uk9EYJcBviLMA7cURqTPhT21oXXgde9VkBU9hKrUKRPOo68zWcMAYwbTHKMmY3
vztxOdo0ZTHLLg0rGCmYIesRRPun5ZTWUs0JyInEfMD/vKW47rebu7LApcPRxS7IismOyv7Ni9Fb0B7KmMSjvwG+T8ByxNjIRczC
zWXTtuGa8kADUBd9JXQsNDNRPONCikcUStFJrUpzTElPr1O5VtFXTlbHVOBR/04STvlMKEm8QpY5LSyNIv8bWBThCXgAePal7fPq
Yeng5lLk2N513NTZdtl/2AHWydHAzBLK+8l1zGLOOtKE1UXXSdiM2yLgpOMd5tToA+l866fv9Pf4/58F2wf6C9kQghYsHsoihyQ8
JGQjJx5IGegYzRtkHpof4SAlInAi2yI0IXgbkRVrESsOQA3CC+0I0AWHA8ABOgFRASf+E/xc+VD3gfYM9kj2F/YW9E3x3+jv21PV
O9Ku0y7bhOej9LwARQb9Bf7/1/Xg7I3pK+kc6Ybpl+m96h/tfO0F6QfmEeVX5ffnVOpV7cjwo/KA9Gr1U/Qw9ED1ZfaO95H4TPog
/DL9uv4A/8X+C/+5/0MBQAKcAnkDdQTlBX0HYQjbCDoJIQpVC1wM2A1PD3QQKREoEbEQxxA0EY8RxhH1EIoQphBGEKEP/A4ND1kP
/Q7PDkEPgg/cDowOKw8PD2YOew33DKMN+w06DjQOkQ0aDY0MuQxhDcsNFQ6HDlIOrA6lDikOAA52DeMM3QyyDGgMjgyYDIQMaAxL
DJ4LHQuKCmMKLgqWCg8LHgyXDJ0NDw7IDX4NGA0jDO4L1QuRC70LYwtfCqoJBgmgCA0J1Ag6CWcImwYfBesDZQP/AgMCdAHlAdoB
wgEeAVAA8f8IAPQANADBAF//4ft55MjI4NrbDEAI6vhxAlv9bNoVni6F0ZkAoVuQoZO0lZ6bEKfWvp/aNQ/VLPQ41UPFOk8nZxIP
8/DSI8N4tUavsrQfueDIn+ZHDn04aFrTbDh1I3jzeC15j3jNdQxweWH6RjckJfxB0+27lLTBsnmz1rqq0CX4nR5tNOxGtVI9VUxV
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
QCG6IN4fQh9GHjYd7hyZHJUcmBv9GbkZHxq7GmwbphtfG30bjBsjG1UalRk1GeUYHxnzGIMXbxboFdwVyRX1FV4WuxYvFh8W6RZF
FvAV/xQIFJUTRhM/E5cS1hGgEX8R8BCCEMIPPA8YD/QO1Q7UDj4Ofw2ADLALswv+CnwKoAlsCDcI3QeOB0kH2QYeBkMGbgb3BJ4F
lgV6AZvmhqbcsU7DNtReAer5cQAb8vjrO+UW6AXlz+IC5zLi5N2x29TfPNyf4a3emtqV2sDZpNk33BfhB+DQ4Y7sJPKC75bqYNjw
y/bbAN8v5+jsIfcU/5gKMBGKF3wfSx/nH8QXlRu8HFogDyUqKMowIzc6PglDYkvETcNNVk8vU29YpV8vYOFbUlewV45YBlQqUKBF
xztPMOYpfSnGK1w0VjV7OqhDHkDaQP834jKcLRIl/xgdFO4TZhBrD3kOmg72CQ0Low37D28SqxFxDesIsALpATL/5/7i9yTyOPBL
7lnrpeza8Wz1cu692aHND83P0NnRRtOB1jjP77/QuHe6nLf2seioSqf9p/ikN6Qzp7apAawlrvWuc68wsNCwbLFWspqyRbMAtk63
27Yntxu3Wbnvu32+W8IjxszInMsvztnSmdbq1vLXR9h82i7dyN4z4Cjit+NG43DkTOjw7GvxUPNM9Dj2d/dd9474dPne+aj66PqR
/D7+AP8zABIBcAGuAYYBHgLHA6ME9gVZB+gHpQj8CGgJzAlyCaIIjwjMCIsIegjvCGAJ7gluCmIKewpgC6wMuw3fDhUQGxCrEOYQ
1BDiEV0TSxSsFZIWohegGBQYYheMF/AXwhfCF0sYbhkTGjcbhBzlHPIczx2EHqweHB9sHyMf5h4IH3sfOSAnIHMfqB7BHeEcohxT
HHwc5xy1HG0c5RuYGxgbORppGXMZNRnxF+AWahY5FWQTXxJ9EeYQfBBID54OeQ6eDQINywzlC+AK4QlJCN8GTwYKBsMFPQUCBEMD
YAIgAcYAHwCd/xQAv/+M/j/+Iv7w/dL9/Pw1/Lv7WPtC+2r72vs6/Mb8E/1Z/V39+fzn/Nz8bfwK/OH74fsp/JX8/Pw5/YP9H/6D
/gv/vv8sANb/zv/0/9//mv81/67+Lv4D/hz+D/+D/8z/fgD0AFABlAHhAcYBWQECAdcApAB1APD/tf/j/8L/rP8eAEwAYwAHAAsA
nP1b8X3FGK89xFjgJ/vx/4kA9fyF+ij4Jvfh97H0N/d8+Jv2Xffc+RT9hQGhCdcFZQ+WEDoRIRUiG2IenB/lGdUgiCyHL4QtwRxF
KCsqBSc7HkoUBxfeEn0KmfyK/av9effU9fXy1/gx/A3+cPzS+7j6+Pgj+eH2IfbE9Rj0PPCr7aLt1+5A71PuVO828Jzx1/En8Bnw
wvGH8fzwNvAc8d3z9vP+8mXxdfOn9e31wfYt96T3K/YT9R31zvXP9mb2hvab9+f4u/mx+mr7E/w3/er8wPyZ/BH8BP01/WH9iP7R
/gH+zP0D/k39zfwR/Ir7XPti/BT9Dv6S/ZT8fv3b/XL9fPxt+xb7RPvL+nf6lfoN+sD6cvso/Df9I/14/Pz7wfwj/Zj8TfyC/Nv8
4vyZ/ND89vyH/U3+zf6J/qP+Hv83/67/k/8J/z//OACgAY4CkwIAAgUBiwCGAIUBXgLQAnED9wM7BNEE/QTWBG0EswOIA44D4ANs
BM4E7gQuBXMFvgVKBioGbwX7BE0FhwU9BegE+QSOBVAF6gQRBVsFbQUkBakE6ANGA8cC1ALHAuUC4gKTAjMCOgKhAssCiQKzAUkB
eAG1AdoBsgF8AZMBeAHbAGIAYwAVABQABADk/yIA5v8QAF8AewCjAMIADQHZAO0AWgHjAI0AQgA4AFIAAADZ/9v/4v+k/2b/m/+4
/6T/xP/s/w8AfQABAW0BrwF4ATIBwwDX/3z/Yf88/zr/Wf+r/zoAuQDmAAABUAF2AVQB6QBcAF4ATgA/AHoA5AAGAeIApABIAB0A
DgAkAEMAQwD//8D/cv9J/5L/nv+d/8T/3/+z/7X/pv+n/9n/mP9Z/x7/yP6e/pr+v/6m/uj+QP+A/4X/bv/g/ycAHwAEAP3/oP8r
/4/+IP77/Qf+Ov4i/iX+Lv4y/rD+LP9b/4T/vv/F/5//rv+G/33/Xf83/2f/lP9v/zb/Jv8g/3n/0P8dAKkANgFmARIB5ADbANkA
2gCjAIoAWABTAJoA4gDxALgAhgB3AIQArgDTAH4ATwBuAHIATgAnAAMABAA6AGUAjQC2AOUAKAE2ASYBAwEEASkB+AAJAeEAlwBu
AFEAHwAOAPH/yP/Y/xcAhgDOAIIAMABBAG4ALACh/yP/8f69/oH+m/7W/tH+B/8h/y7/af+c/87/v/9q/wT/5f67/q/+z/62/q/+
0P4k/4n/xf/Y/8D/0//y/yAAPwBhAK8A/AD/AKQAbgBiABEAtv8q/8T+1P5j//L/ZgChAMEA5gDDAMQA/wD3AKIAKAD6//v/vv+N
/1f/nv/T/6H/a/+g/9f/+//k/+H/KQBGAG4AaABKAFQAPgDo/53/lv+g/5f/av9G/2P/mv/E/7j/sf+s/+T/GQAZADQALwADAOn/
2f/r//v/wP98/3v/rf/B/7P/qP/T/wcABQDn/+n/VwDBAMEAmwCZAJMAjQB1ADkAAQD6/+H/x//b/9X/2P+5/6b/nP+5/8j/t/+q
/8j/EQBFAHYAjQC3AKwAewBKACsA5v+k/5X/hP+L/7r//f89AHQAdQBiAHIANADk/7f/qv+q/7L/6f8KAAEA9v/f//n/BgDP/9X/
rv9d/1v/kP/R/w4AFgC2/3n/YP9V/07/R/8s/zL/Zf+b/xcAngCuAKUAWQDu/8j/p/+4/7v/qf+M/6P/hP9U/4r/mf+Z/6X/rP+n
/+L/CgASABAADAANACUA///T/6j/k/+Y/6r/yv/M//D/MAAGAKz/hf9d/2P/pv/3/zMASgBeAJIAowCrALkAgQAoAAkAAAAGAAMA
8P/v/wAACQBZALAAtwC3AJsAggCLAKwAqQCAADkA+v////v//v8BADYAUgA/AC0ACAAyAFEALAD2/+P/BwA7AGcAgwCBAGoALAD8
/w8ACADr//j/4P/S//H/DwAsABQA9f/u//j////2/9j/5P/p/+r/GwA4ADUAPwAWAPX/CgD0////MQAAANP/8//4/wEAGQAAABcA
PwBDAEMAbQCjALUAhABIABYA6f/R/97/AAAJAPT/7v8mAE4AYgB8AHEAewCeAJsAlABwAGwAYwBSAD4ALAAKAPr/GAAaABQALABC
AFcAYABiAHoAhwCAAIEAigB3ADQACAAPAAcA8P/L/7n/yv/s/xoARQBNAEwAWwBrAGEAaQBxAGoAUQAsACcALgArADgATABQAEUA
RwBeAFwAQgBOAF4AYABXAEYAQwA6ADQALAApADAALgADAOX/0v/I/9X/8P8LADIANQAUABIAIgA5ADwAIgAMAAAA9v/t/+//CwAX
AB4AGgAIABUANABLAD8AEAD4/+T/3P/g/+D/8v/5/+X/6P/t//n/FAAJAPv/5//P/8f/z//b/+X/2//M/8L/vf/G/8P/uf/B/9D/
yP/I/9L/6f/7//T/4f/P/8n/t/+j/6L/mP+Y/5z/pf+4/87/4v/u//T/7//m/9H/rv+Q/47/oP+o/6v/sP++/8T/pv+b/5z/l/+O
/4X/hP+I/53/tP/b/+j/3//P/8L/uv+1/6v/p/+t/7P/t/+u/57/hv90/4T/l/+d/5j/m/+q/8P/1v/k/+v/5P/L/8X/xf+//6v/
kP+G/4b/hf+M/5//q/+v/73/1//d/+H/6v/3/wEAAAAAAAQA9//s/+X/2P/W/9r/2P/p//P/8f/y//T/AAAZADQANwAyACoAHAAF
APf/7P/a/9D/2f/o/+X/8P/7//3/BAAIAAYABQABAAgAFQAfACQAIQAdABwAHQATAAYA+//0//X/9f/1//H/8f/y/+3/9v8AABEA
IAApACkAIQAbAA4ABgAHAAMA9f/i/9j/0v/W/9v/4//p/+v/8P/8/wAACQAQABAADwAOABIADwAXACYAKwA0ADQAKgAnACUAHwAi
AC4AMQA7AD4APwA/AD0AQABDAEgASwBGADsANQA2AD4ASABNAE8ATABKAEgAQAAyACcAIgAfABwAEAAKAA0AEgAVABQAEAAQABEA
EAALAAUAAgAAAAAAAgAAAAMABQAFAAUABQABAAAA+//1//L/9v/4//r//f/2//L/8P/0//P/+P/1//H/9P/w//j/AAAHAAsACgAI
AAUABAAAAP7//f/+//z/+//7//r/+v/5//3/AgAHAAoACwAPAA0ACwAFAAAA/P/8//3//P/7//v///8AAAAAAgAFAAYABgAGAAUA
BAAEAAIAAAACAAIAAwACAAAAAAACAAMAAwADAAcABwAHAAgACAAJAAcABwAIAAcABAADAAIAAwAFAAYABwAHAAkACQAIAAgACAAI
AAcABgAGAAcABwAHAAYABgAIAAkACQAIAAcACAAJAAkACAAHAAYABgAFAAQABAADAAQAAwADAAQAAwAEAAQAAwAEAAMABAADAAMA
""",
"not_charging": """
UklGRibsAABXQVZFZm10IBAAAAABAAEAIlYAAESsAAACABAAZGF0YQLsAAAAAPv/8f/p/+D/1v/Q/8j/wP/A/6z/uv+R/6r/b//b
/6P/if/I9wXq3eq05tryT/Yc+zD7AfsW/rD7l/0v+i/7Mfq5+cf3dfh29tn3PvcT+Ij3oPp+/akAjQGXAM8CMQTICokNahEwETsO
7QqKDLgPEhPeElARsw1lDHIOJhZIGPQYihonF3cZDReXFwQZYhloHQgeah8WHH8etB39IeQl8CK7JlokeSCiIJga5RYaGp8dYyAL
INoaSBVIGaYayxmaHs4arB0TI18fOSciKm0sNh0wC9T0TOVj6QfsyeKx2UPgLOAx36jMSrpluP+8z8P6ysvPH9HU0UPSBc9uzRTP
us+U0inSTM+rz2PQyNK91X7Xjdiz2jbcFeA25Pnll+ex6DXrnuzr7Q3tr+yc7hLuGu2m7OrttO+y877xvu2l72DxsPFF72nsaeuN
6urtI+y46v/sgOuC7HrsH+7N7h3xFvL58zD1IvJ58JLwEfQG9vL3U/nR94P1VvZv9PP2UvmN/tMATACf61/Zu/PMzK3D7c7wtDS8
aqXQr4PGUgobBSMIqAi+AWAAkPwi+Mz15fbG8NXybvFk7gny+f5ABewDOhDHGEQgaCgsKWQtUzP/MZI1OTY+OM81uzCTLo4yADfa
OPc5CD45QQtDvkdvSNdPvVNfVO5QQE8WS1VGFD5ELhgr5ykMKgEtuDGAPcNNR1KCTklMjEyNRsw2iSveHXcPOAVkA34PxBArDh0Y
rCD7J0ElDyFGGx0dchS2CREMYQ6cGREmICtfLaUuPTTdMk8ykTAWKkEcvg/iEOgXeBdHGQwVwgnY/qf++vo8AOz3BO685ajsAAAz
C08Ltfgh88Lt9erC8Nj3+Pwl+o7y8fMo+nf2d/Cm8YvzUPNN+hAHdQ2hDa0TQREHDqP+otncu/27L8Q6wSCxtrKzvVjB877Mq5Se
IqAppF2tnbA9tfu1HrhRt/22Mrewtv23b7kJuoS5Mrwzv3LBr8MMxwPIU8rRzdfQtdQd1JfSoNW+2+fgNOF04E/gv+SF5hPkTOXG
543uMPCS7brtCvST+ND36/cS9ef12/bf+A38RPw//zr/NAHVAqEDswhUCB8IYgv7C9AO6Q22C1IOTQ3oDdAPTA48DocNXA/xEcgR
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
JKC0pjCxbcPr1yfvxgZ0G0UlDCGJE7MBNOtU2MbLm8LlvOC7sMIYz+ffd/laFwwwpkA3TKVQqE9KS5tB6SxkD97wHdlVx3K9MLr0
uZ+8W8WB0sPguvAm/sYEHASl/i35ofEY6QfkGtyO0gTOvs0T0rra8uKA57/o2ef35mPoRuyi8yn85P8BAwIHKgrpChAK6gYkArb/
bwG4Bl4NjhFWEyESKQ8yC4II8gfjBqwD0AG4AQUBlwBL/T77NPit7yLj8N9C55XyDPoC+kT4i/VP8eDxSvS37xne0NKB0VjOX83M
0DfUwteQ3Ljg0uav7NvvUfKC8+Px8fD48M3vvO4G7q3seO2I8D7zm/U49374ePpk/Jb+AQIsBS8GuQYVB+gH0woXDesNOg7GDtMO
mw89EmoUfBW3Fe4VNRcOGeMZ2xqIG4oaQBppGhEaLxq3GToYMBcoF7EWlxYyF38WthUHFUgUehMIE4ESTRE9ELYOYA23C9gKxApp
CnsKtAqiC1sMCA3UDEoMfAsFCZIHSwfIBs0Ggga4BT8GsQY9B8gI6wnTCssLFQxvDKcMVAwAC1wKXAtdC3gKbAlVCf4I7QfTBhwH
EQgfCI0IcgktCnkKnwqxC2AMQwzsC8kLows/C90KWgoZCq8JTgqPC5MMGw5bD64PYRCDEN8P/w6JDl0NpgsQC9gJHQmUCIEILAh0
Bu0FPgZ1Bh8G0QRwBDwE2wPkAm8A+QGTAb79Zd2Nn2+NH7JE4Trv7/vu/gv6TvrF9RfzvPlB6PzeVOIHy6iyQa+ArKK+ocmd0ubp
RRN7NeBGqlIJVEdTSE12R+c4fx9d/krqreFA5MLqgO4g/tsVIishOwhKlVRUWrhYalLKQqs0DSmXD6T0YdvGzO/FccY/zfPWeOWw
86sGYBUEHq0hox8XGekOmgHe9kLx2eik4nfkcuW1543xG/wPC+QWqR/jKTIwdzMSMdEuHivsJdohRhqSGQkbmB4UJBwlTCkqLuos
XShUIdkVvwTi86Lj/NUJ0ZXQ5M9B0jLbXubp7B/wrPHj8gXx2OwX6GrkFd6O2XLWpNMK0s3RitV82yfjlOp+9fwEbBEbHk0s3zej
PopD1UXIQ4A8Cy9CHxoQiQIF+sX2fvXt8Dvuzepm5ori/uAl4VPg0t813CXU5suLw6+92bmnuJ27qr+vw4zKqNKA297ju+3L9Nb5
FPuo+2T41/P18QnwKe1U7aHxwvcCBBsS5CF7LVA0zThFOmA3Ly7SH/kK2Pib6mjcq9AUyfzE+8Oix7HQG+Hb75783gTeBhkDmP+/
/OD1zO1B5Z/gJt5v30fkMOZs5Tjjd+Po48/nwO3/8ir4r/xRAlgHug3+FN0ZYRyKHdQfXCGlIfogTCAZHtMZORcEFdMSHxDlDKEI
jgNUAHL/VP34+xP7d/r7+aT6wP0zAWMEDQReAbP+E/5pAd4D1/ku6y/nHu7Z+QkDbQcLBIMBKAJRA436ded03iHbS9TNzhXR0dJc
1o/YqttZ3priC+aM6dvrpetG62TrxOtU7bDugO6X7m/vBfIh9sH5N/zG/i4B4gIkBB4FWwb3B4UIswgRCl8Mdg+kEV8SMRIvE+EU
HxalFhMWKBWfFKMToxITE4ITgxObEy8UZBXbFuAXHhhRGEEYJxgAGGUX8BYmF10X1BcRGLUXahf3FxUXhhUWFN0Rzg98DkUN1AtE
Cl4IJgePBgsGCwXrBJ8EuAN/AswBnAG8AAoA3f8VAB4AGgCVADUBlwEjARUAEgAdAOP/WQAQAYcBrQEPAjkCbQKNA2UDAQTtBH4G
RwcyCFEJ1QkYCpwJugpIDGYN3g3ODfgNdw6jDmcP1g58DsMOFw6zDjMOww10DssNHg3zDDkMJgwhDOsK4QlwCU0JtQp/CQIK7Qj4
B1MH7gUbBfgD0QMuAwcDpANnBKwErwRgBCEExwO6AhcDXAEEADb/gv4t/tr97fyw++b6C/rv+GT4F/it+Fb5Hvnf+KL40vfE9Rf1
dPWk+FT3y+sWt3CQSJY2sYXMj+L89o3wVtjYrle1LrubwSzEN8AY1XPeu/0SG8w1PT94O/oyAiehGZgOYQWRACwFOAryHVUxN0Dw
SCpQsFYqXWZg7mBOW3tS10FLKIYPy/d56xzZzciOv9jAos242vvm8PYWBHsNNRYsGyUZTA6G/inqqdyh09LQadQl19fgD+wh9x4L
8RyfKa8yvTUxNe0xXyynJlYgzhlTFw4awR/MIyUrEzW9O7Q+Uj9SPSs6mTOKKzUdUA6tB+v+PvRA6ZXfHNiOzNfCRLswtBKz8rI6
scq0s7xGw3PH2cmjzTTQaNJn1zjhZO3195sE3g/xGF8nmjM3PhdFoEpyUHBTzlWMVlBWJ1OES+pAKDQBJawSy/096UzaG8+fxunA
bL2Hum+2trOqsLCuAq+tsBG1T7q7xGDSLeFI8Lb41vxX+Yb0TO8h6Ari7Nwq2wDaeNtK43LwiP/PEpAl9TQiQgFLxlDsUWBNYUPN
MOsWM/qQ4G7Ldr11t7y1oLk0w03TIecc+RkHow67EEQOZAgY/qHtZNxdzW7BJriCshivt65Ksz6+4M3B4m36Hw+KHcUmOyxlLQYp
WSEiGnUSqgpCBWcEDgcSC54SNxx+J10yTTrKPts9mziVLgUhqRKuBVv6GPBe6TXm1ePO4lTkY+fG6J/rs++W9Kz3Tvko+k/6RPkI
+YD6KPzq/nsDGgcXCjsOzROPFeAWfxq3FHILCAUgCCESnhqUHmsdUxoHE6QL8gI+9nvqiuTh4Yzk7+fn5tzmyuo06MDiR9w+2MfZ
7dqT3WngDuOd49XkKOW25LHkfOU955TqRe7z8HLzzfWR93/5qvoe+7X7RPuK+jH6MfqU+qr71fwG/rD/YgELBJoHEgpnC5AMtA3T
DdgNTA6NDkkPqg9VECASKBTIFf8WoRelF4oYjRnUGX4ZuhjfF9IWrhXrFGgUShMbEkERyBDaELAQSxCpDwAPLQ4YDYcL/QnwCG4H
qgVbBGYDxQKtAtoC7AIGA+QDgQXNB3wJPAqqCqUKLwrECUcJRAiVB1IHaAfMBxkIBAg0CKQIgAjbCP4JgArtCToKiAocCnsJIAni
CHAI0QflBg8GjAXvBJoE+wNvAy8E3QQoBXMFPwZJB64H3wdyCMAIXAiRBzYH4wY1BpgGsAcUCCcIbQmcCswKzQoVC08KjwkYCaMI
SwihBykGoQRtBI0EJAO9ANj/7P8QAcYAKf8g/+H+Pv1X+0j60fkW+RD4A/eI9rb2bfan9ZT2Pvjc+P/3XvY79+v2CvU89fb18fTY
8/X01PVD9o/3vPiu+mv6fPke+mv6qPrZ+J/63vgL8rrEUJJRjpWh0aFnmDSiMsyq/R0gRUFsVl1gDFkuUdRCnhOQ5FzLi7RWp/uj
k6fBtAjW3QKdLd5OsWNQbAFv43BlcNdrhV+NTTMuaQNn2Pa7dbNftHu7R87Z83kmw0ezUa1XlFvRXX5WGj9bHyz8UtRhszakyZ66
nemf/acSvN/ggQqWLxRIqlIsVzJUnktbOUoa+PXP0Le2Wao7pUyji6j9tcvJ/ulnEBMwfEOFTrVSGVBWSMU7fit/DvT3bOze3Rnc
ueRU88cI3CLlNotFoU8AVG9RAUn+Onof+PtW4TPKorecrX+rTa3EsQa5HMkB3gPuIgG3FBEhACijK44nBhekARft0NxNz8zEUMJN
xAfM+NtN+W0giUM5WW9jQmioaU1nsl6aTfkvOv7lzgay9aO8nNaaCp2nol2wkMfh6gYQgis3PJNB5jxxMXwaHPT5zb2z+aHJmCmV
YJXpl4GdSKrmwfHjnwylLrNCxEhQR28+TSx/ENrvR84kt9KshquxroW6ENHM9FocUz+EVQphTGa0Zlpj6lg4RHcl6P0b3F3ENrUS
rQislLDwtzHECtiY7ugB+hTLJJQr+CkzH74Kqu7o03zB/7V6rxSv/bMyvs3PB+vVCFchjzPCPZBAnT0pOFIxXSZQG1oTaQ1tCwMQ
0BefHikjGSeLKEcntiRvII0ZFxPtDYMIigN//aj2be6Z6L3mM+fI6rLw4/XL+qb74vv8/H77Gf1hAngJ8A4HEuMRSRClDPwIhgQA
ATj7YPZl9bb11PjL/ncEFgXvBEcCcPhG6lvkaOOn2hHTH9HP0PvShtR52c/eG+VF6dLtp/DS8S7z1PMx86DxKvDT7p7uY+717Trv
g/Op95n6pf6DAhAFtwZnCBUKCwtDCnkIIAfMBpMGlAZQB9EIHwo5C3wMNg7jD7oRdBNwFBoVxBQEFP0T+BM3Ez4TaxPOEkQSIRN1
E3ET9RNeFMgUgxXKFZ4VhhUOFfETNxLhEEkP0Q2ADF8LYwo7CbYIjAg4CYEJDwlxCH4H2ga5BX4EjANBAu0AXADR/wn/qP9PAGEA
FwE5AgYDhwMzBGQEbgTRBAsGmQc2CCsJHQq2Co8LKgzADFMMvwt+C+sKKAo/Cf8HhAfZBqcGBgeeBwUJWApzC88LcAv8ChEKNAl8
COQGvQW2BcQFcgXYBFsEMwT6AwUE+ANBBDMEoAMFBJcEagW+BXMFFwWqBMEDUAPDAx0EnQQhBDgD2QM+BNADOAMAAtUAOgC5/7r+
1P2Z/Sn9Wvym+xT7xPo7+uX4zvfO9ob1hPWz9Wv1C/az9pf2v/bW9xf4EPcH9mf1jPTh8xTznPK78ubxRvIQ88P1ifJE6Rq3kZXm
mJqPq5PAnNKvQ/PiKZI+eUrvWjxallokU1w1uhTC6qa9NaruoJ2dI6L/rRbGSvpzM5xW6Whlbt9xEnKycSptOWOeSFsVVduHsTKj
v5tmmbmbIqUsuRDmahvqQ0haQ2LlZFBg0FiLRMwf8/OTz1q6mrDIr0+0scFd3ywJkzQwVdVjBGuKbMtrKGicXXpInSHP7l/Ca6nu
nMKYPZlLnKKlgLYS0ZT3nh8sPE1KF07PRUAy8gxN4Pe+QqXwmgmXoJTlltef07UB6PUpCFZWaRtwH3KycqFwJGzAYOxFmhlF6K3H
Q7YusE+yzb6007n3KCOCQ2JXvl4RX3tWr0XpI/PvAsCEoXaUCI/ejFGMmYyLjnqS0JtKsJDX/Q0yO2dVn2ELZ7xnEWXgXptPHDMp
DsTuN9mZ0ZPX4Oj9AxMkUj8EU0te72S/Z6NlCF4jSgAe3OJes0+axZBTjUmMOYxDjWaQx5fQpoXC6+hED6QuTELgSk5HjTcPGUnt
xMbrr16jCZ3nm2Kf9qm6wuDxdisKU6pmn26JcSRy5XClbB1g2UKzFSfk/cCLrfij36D8ofep0bnz0SjxqQ81Jh409jlNNxItSRtu
ApnmH9ArwGW2MrGDr5yxhLcvxVzZ4vIgDjIi+i5MMn8taSXzF6YHWvrc8QLuLO4l8cn1xPxECRsXLiRxLn40NjeCNg42UDNmLUQl
qBvtEjUK9QWFBYgDdgS2BE8Aw/sM+DH4j/hX+I/65/vQ/Fz/r//H/nj8+/q2/FH9jv/RAagAsv+F/8f/4AAtBKwJhwwCC7QI7AiY
C0QKwf+D+D72pOZg0bHKn8v1z5bWENrP3lvlaOm/7K7v/e8s7/Pur+w56j7qNOou69rtke9b8hj33/p0/Vf/HQEJA10ETAUBBkUG
UAUqAyECoANXBhcImAiFCe4KiwtzC7gL1wyfDfUM+QqdCt0LLQziC/0L+At/DOwNSQ99EKESIxSuFOcVPxdaGCwZ4RjDF1IWTRS4
EmUR7A+FDokNpwwxDLEMcgy3CwQLnwnlCM4I+gctBx8G6QS8A+ECUALEAUcBPwEcAaUAPgDf/8z/vv9+AMQBsgIFBEcFHAbvBioH
fgf7B7QIJgkxCa8I3QeoByAHpgb/Bs4Hvwg4CkELVwymDNYMDw10DJcL4QoICqgIsQelBqwFagUhBQIFjwVJBq4GVwcLCCQJygkl
CaMIIwgKB+4FbQVXBTEFpgTMBI8FUwVRBeEFHAb5BWYFqgR9BJME7AMPA/UCRwJ4AecAlABxAHgA9f/p/oT+S/5B/Sj8i/vW+mn6
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
s+7B7k7vJPL39ar6dv+qAfYAXgBNAdIEDQrWDn4R4Q2nCJ8FPgTXAvn8Ofj29qH5FP3O/sz/Of9D/GL5d+6g6H7pKOs27cPtfe5v
7XvtM+0G7Zjs3Oza7WrvEvEr81j08vTn9TH3NPmM+5P9Hv/0/20AUwDJ/8L/ggAOAZIAWgBTAagCQwTlBLUEzARrBCUE9gNtBLkF
VgZxBsUGMAdcCP8JYAtqDEUNmA4ZEAoR3RF+Ej4TvxM2FPkUixVkFaMUZRP6EfkPJw4uDZMM2AzmDNMMwQyiDL0MhgzAC9cKFgrf
CYwJ4ghQCKYHnQZRBU8ExAP6ApgCHgK3AYcBSwEBAcYA4gBtAd4BUgLZAgED1QKCAqoB8ADHAO4AdgEoAq8CjgOWBB8FaAVtBQcF
pgT8AxwDhgJaAjACTQKhArkCQgPvAzEEjQRMBZcFJgZ4BnIGdQZUBs0FdQWXBYsFWwXPBJcEHgRmAxkDRQOLA/EDmARCBeMFDQaW
Bg4HigYcBs4F5gRQBDwEWAM6AnEB5QC8AMAAgwD0AIMB4gHWAZgBsADK//D+PP4l/Xn7MPrH+LH3G/e69ZT0QPSI8yTz//Kg8pDy
/fIB8w/zkvOY88fzpfQ79Wz14vXe9pH3TPgO+aX5K/rG+mv7Y/zc/Hf8XPwP/Ff85PxO/kz7kfIFw0qvLsbfvPWeLqBotSfcPvXr
3XbPhdgg5VrzNg6HEmsPcQky+Onkptom1I3T8N0d6eL6pBP6L5lGIVaCXzhmPWnmakhrxWp+Zz9fZ1JUQHspbQ6U7tXZT9Yq2Rng
Aeyl93oF+wvCBMv7vfzz9f3i1s2hucmq06H1mwOayJsJoJGpzbtO1ov53R0SNZ1CmEufT01QiVDZTbJJsUWgQ01CH0OIRmFITEs8
T1RSpFYEVzBUvU8YSKs6vSazDS7vN9Skv/euRKTKnmSb9prNnCGf+KIvqI6uKLYOwEPNENg64hbpTu0i8ZjwcfAL8ob5XwcWG9U0
MErXWHZjr2g6a+1rP2mYY8JbC1OIS6xFlDeKIh8Jh+6y2Y/NXsY+wi3DLsMIvxu8W7mZtZSw86n+pOygZp5HnV+evKARpdWtJrpb
y+ngNfmOD0oeyitiNSU7xT7rQBI/ozsVOPA3czuxP89GeE1kUddRL1D2TVxHJD0XLroXAvyz5PjTmsipwP64FrJWqwSnS6QNozei
sqIppP6lCam5rSu2gMAhzHfYS+FZ6Gvve/d3/kAF7QzsFUghYy1AO4hI1VPWWlRfFWFUYK5d1VnXU4JM3kJzNL0hkAsC9b7i99V8
zh7KVMeTw5i+rLmXtdCyZ7KDs5G0XLe2uRe8TL8Rw5PIT9B52UHlpPJeATcPCRlFHiwg/x/IIt0nHC/yN4o/FEUnSclKuEkuRsU/
lTbpKwciDxivDT0CzPX66H7eJtd11EfVydfq20rhCuWv52HqEOw+7QDwJvNd9qz2EfUX8wHwdu0/6/Hqm+5e9tgADAzyFJscByI3
JaQnxyiaKUspNie1IhAdgBbvDoAInQMBAagAzgBZAeQAiP01+Zz1lPIx8gT1N/kc/T4AMQHF/7z84/sn/18DCAMv/yv9A/1k/iD/
hv5r/Wf7t/o3857rg+pD6/ntT/Bo8s3ycvTg9P3znPMW8wnz+fKe8kzyEfJx8X7x2PJu9Af2Nfe59wX4zfgk+pj7d/2M/44BUANK
BAYFAwZ1B2IIigjXCBwJbwkMCrAKogvPDH0Nsw3iDUAO5g5BD+MOiw49DsUNXA0oDTANug0bDo4OPA8fEBURAhJaEjASvRENESIQ
6w5QDUgLdgk/CFkHrgZ4BkYGJQZkBhYH0weVCIgJFwq3CbsISwfVBVoE/QLCAWUANP+C/lf+df7l/nP/IgBLAXkCOwOeA8EDyAOt
A3wDKQP7AqMCTQLWAZUB2QE/AoMCYwJEApMCLAO6AyoEYgQ9BCgE5AOjA5cD0gMGBCQEBgTAA2YD8QKHAlMCNQK6AaMB5QEjAlsC
aAJpAogCzgIWAyAD6wKfArQC1ALEAtACQwOKA54D/QN1BJ0E0QQXBcoEAQRgA4UCfAGJAJb/MP7l/Or7//qg+m/6jvof++774vw2
/pj+0v5N//3+r/4N/lv90/z2+1H6iPnt+Oj3b/cE+HD4cvjy+Mj5nfpE+7b7S/zS/HP8Z/xE/WH91vzO/L781Pu7+pz6v/oL+nn5
wvl5+t369Prr+gr7U/sK+4D63frc+4f8W/zu/FD8nP0c+3zzp8UIpUHVN9FAoYqfgrbbwSLGStJw2t3o5e1NAEYafCYgJoopwSHL
D8gLfgcyA50InQzADMEVfiT8MYA/3ErsUM1TC1PfUbZNTUN8MHgaywf69YvlZ9QqyoXNPdAW0EzWnN5g6Ub20vXC8fP47fj48iTy
Z+4s6iLsmu+I9tkGjxk3KE84TkbkUOVX/lrnWGxTG0vJP4MxAx8gDekBZ/j47VrqP+3/7rHtru5e8c72XPiD8rnnTN3e0WfF4Lx1
tEWwtrJqtPy16rvpxLvODdxL7fcEuB0nNJNGDFPIXMxg5mCYXjJZD1B+QeAyOCodJoogBRyLGoEZMRhkFWYQwwk6AUb0d+Rg1MnD
z7V3rEKmtKGnnzWglKHXpBurNbaYxvbV4uSJ9ksFzBAeHVMrkjYDQORIqU5DUelRck99SlJD+jnbMPkpKSVkJHQjMh3DFI4O3gIY
79fbr8z0vf6xJqv/p6KmQamerTSxhbZFvZbE78v60cTZz97u3/zgHOLi4bLgUeGr5qruJPt7C48b2ivMOXRD8EjBTAhPwE5qS5VE
RDkdKTMUqv7V7sDjit3r3ADd0N3O3+nhQePG5ZvoNevh6rrp9Ohd5qDhjtoQ06DMXMhPx8LJrc9Y2Lzh6Oqu9DX/oAlnEsQZxCGz
J3ErSC4dLlorRidTI9ogUB8OH1kfGB5dG6MVCw9LCPMAJPx++Xf3hvWh9Obz1PLp7tvsaOyt7ZDwxvTZ+SP9Rv+0AP0BuwLvAq8D
wASVBR0GrwMMALP9Gf0S/aP7q/iY9zX2qvam+ZL7qfp09AHsmeY+5FfiuuPc5YDn/Oji6VDq9Orf67Psxu1x7kbv5/A08w31Jvb8
9iL41Pn/+ln75vuA/MX8jf1M/1YBPwPaBGoGRwiMCtkMng54ELURTBKhEvQSlhMgFEAUUBRnFGgUkBRkFCIUDhQ1EwQSnhEmEVoQ
IxDdD0gPFg8fDzYPnA+yD0YPTw9hD9YOVw4HDm0NdAxXC4sK/glZCZUI3wcqB1EGwwWkBdcFIQZIBhIG0QXWBcQF8gVwBqcGtgYN
B+kGawbtBRwFbgSeA00CYQExAWEBsAHHAcgB6gE9AucCyQOTBEgF3gWaBl8H1AfGB2oHUgciB+EGvwasBqMGtga2BnsGTgamBlsH
3QdRCIcIOwhPCHMI/QdbBwwHCAcgB1kHeQeSB24HGwcHB7QGJAbeBYkFMwU1BR4FugReBOcDlANXA9gC5QIGA9kCeQLXAc4A2P/h
/mX9mfsl+hH5Q/ik95X3Pfi6+Av5t/mZ+sH7D/3x/YX+Bv+N/1D/6/5F/oz9b/wo+1H6Q/lz+EX4KviE+Of4f/mH+lP7hPsX/L78
l/xj/Gz88PsC+zr6b/l/+Kv3x/Zk9lb2mvYl95D3tPif+fv57vr6++r8wf2r/hX/b/+D/2T/gP/R//j/VgAQALr/Nf+n/s/+FP/M
/sn+y/5T/gH+ov07/XH9o/3u/c79m/1g/l7+wf0m/f38Qf3//Or8cfwn/M/7PPuC+2/7k/un+xr8Hv32/Z7+DP+t/3kAwQBuADYB
DwGKAGMAMAAjAP7+X/76/W398fyb/Hn8ZfwL/BX8IfwI/Ij7wfuu+0b8m/xg/Nf8PP1L/bT9Lv4c/uP9Nv0f/TD9VPy3/Hr84v1f
+T7zccI2sAeyMK6E1bLrKwhrBjQJqvQ74CzXadRb4MboAvLV82P2SvIY+D77BwAMCMMOsBZYHq0jtCb4JxsqAy4GL2Asayc1IBka
/RQXEpAP8wvnCiMJzwjZC8cQ9hIJGaoYYBgYGvkYfhrkGUsZFxXAEy0PKwteCJcILwmxDIgRPxU1HAsiVCi0Kikp3iZ9JC4frxoE
FNMMrgNfANL93PxU/bX+dQFzA+YHuAoCDAUJ8AQ0AY399vuk9+n1UvI58S7yQu8O72vzY/maASsLjxNnGccbmxtUGYwTTBEBEG8N
aAxMCjAHYQJn/xX+tvq6+Hb2w/bu9h72kvbo+M340PeV9/n5wPzO/g0Ay/8W/+X8LfnN8mbtjOxe8FzxWesS4l3bv9sw4hHlBuSJ
5abm1d7HzQbKDMoMzGDOH9Ia14/Z0Nt13EXdb9x73cjeSd8+4EHhxOHm4IHhU+Pj5bPpuOx17jLwFPJ+85/0Gvah95n4Lfj996b4
2fmR+9z7dPvM+6j9xP4s/gT/wwCBAvcCJQKIA00FGAY+BpAGGwe9B/QI+gjxCAYJawkbCjoK0wrlCkELtgunC9wLGAzDDGoMnwv/
Cn4K/wnQCFAIEggKCKAHVwcEBzEHcAfBBxsIzwdYBx0GuQTOAwwD4gK4AvgCugNEBFQEYQQwBC8E/QQnBVsFswWLBogGKAZTBmYG
xwbaBh4HFweoBkcGHgZlBqgGAQdOB5YH3gdlCAAJeQmvCccJAwoTCukJcgkWCSUJHwlWCU4JPgl4CYQJdwnCCXgKpwqCClUKJwrI
CWcJRAnoCHcIQQgLCLUHqQcvCIQImAhJCOMHggdGB0wHkwfFB6sHKQdbBpsFEQUlBKEDqAM3A/4C8gLmAqICeQJHAucBgwHvAMwA
lQAPAHD/Z/++/9r//f9t/9/+eP47/i/+4f3f/fT95v22/Yf9gv3U/Rf+VP4P/hT+H/4B/g/+dP6v/tX+qf5g/iz+Cv7N/f79Zv4f
/4H/1f+I/w//9P55//H/EQB8/x3/j/6W/pH+7f4o/5f/+f/c/9v/uf8pAGkAgwCFADsAmQCUAKsAzP9+/+P+c/6t/tb+lP+3/9j/
sf8KAPv/+v9V/+f+2/6F/ln+Jv7Y/cf9rP2f/Zr9lv0Y/ab8TfwT/Lr8BP17/cj89fzc/Kr9X/3M/KH8bPye/Cf8AfyS+2H7NvxK
/EX8xPyZ/RL+Zv0Y/DD8Y/yc+5r64fnS9Evglq8EoBqpDszF7s3vyO549nT3n/Tp8cXyAfcd/XoA2P9aA6QFHgm/DEYPuw0VE5Qg
bCS1LSYlJywnLwo3ejtnQUc2fSuzJtAjUCqvKD0pXiEoISwgjR88HushJiRxI7odPBxKHTkkrCN3IEYZ6RPYDtYI0gmbDLEJCfvu
9Cr6rfx58m7lIdtw2zDbLuDZ4z7mYuUy4rzg/d6K35HfgeCs3rLcztr/3JLfqeG148/j+eOM42rk/OWB5oDnq+ga6V3rKO0e72Tw
2/F580b0zvVp9tH3bfjZ+Z348vli/ckAygF7AO7/OQAOACAApQBjAqwD4wT4BP4E1wPIBGMFVQX0BAkEWQTXBEsEgANWArsBkgBe
AGgBZQLpAev/bf/K/5QAFQDT/5T/Kv8u/gb9CP1A/Uv+w/78/p/+Yv///8b/+v6E/S/8K/xx/Tf/MQAZAOz+9P3J/Ej8C/3I/UH/
OwAuANn/GgB0AGUApv9M/u79SP56/9wAWwHhAAUBaQJUA+sDkgNVA+sDtQSIBQkGRQZ1BsYGfwasBhAHEQdLB6UH/AZ2BpwGcwfK
B0QHYgblBbIFGgZ6Bo8GhQYwBrEFiwU+BdsECgTwAsIC2QK6As0CFAMPAxMDKQMtA5ID+AOCA84CxwH4AI4ApwC1ABcBWwEUAcEA
gAAqAPz/5/+y/8b/ov+m/zH/uv6M/iT+D/6v/Tf9yfyz/Oj8J/1r/e79Wv6a/hD+Pf5T/ob+gv6k/gT/UP85/0b/u//g/3EA3QBn
Aa0B1AHlAV8CsALAAnICBwL0AZsBpgEkApMC4ALlAhADCwMAA98CxgL1AuwCrQKEAkkCSAIsAiECDgI0Aj0CWgICApgBeQGVAbYB
ywHrAZgBLAHmAOgA8wC2AE4A9f+1/3D/Uv9R/x7/Df/4/i7/WP82/wf/BP8M/xX/vP4q/g3+CP4D/vD94f3G/bn91P3l/Qb+yf3P
/dH9tv1//UH9PP0//T79Tv1G/Vv9jv2f/fz9Q/6A/nL+ef7Q/hz/RP9T/2n/nP/h/+D/yv+u//b/JwBRAI0A3wBCAXwBegHAAdYB
5QG+AXMB/gBdAAsAGwBVAFgAXwAuAF8AuQD0AO4A1QCmAIoAjwB1AFIA+P+X/3P/ZP9D/0r/k//K/8D/gv9Q/0L/Xf+T/4X/XP9I
/0D/P/8K/9r+iP5X/iH+F/79/YL9VP1o/ZT9/P0f/iX+Gv4v/j/+UP5W/ln+af46/if+TP6i/vD+YP+R/53/kP+Z/5//qP+//y0A
ngDjADIBLQFCATYB7ACqAJYApABuADEAMQB4APAAEgEyAVEBPgERAfoA0AADATEBDAGTAEEAXQCLAJMATAA7ABQA3f/B/+X/UwBw
AHoAVgBOAFsAQAAIAPX/9v/s/9T/2v/9/w4AAgDL/6X/oP+7/93/xv+P/4r/af9s/2D/Xv+M/6j/ff9r/1v/cP+N/6j/l/+f/7n/
pP+o/8X/AAAdABIAEwAuAIkAwgCoAGAAMQAoAEAAawCYAJYAlACFAJ4ApwCYAJEAhwBqACEA2f8BAE8ATwBUAEcAQAAPANz/yv/M
/9z/+P9SAJIAxQDAAI8ATQA1ACAABgAHAN7/zf8XAIEA+gAsASwBOQE7ARcB8ACDACoAAwAWADgAcgCvANwAvQBIAM//of9u/4r/
yf8EAPL/v/9m/3X/if9F/wX/CP9Z/43/if+p/6z/lf9o/3f/jv9p/0X/T/9B/3f/vf+8/8D/tf+V/3X/Pv9V/7z/5v+3/6L/sP/G
/9r/uP+o/+P/BwD+/9H/7f8HADQABgDV/7j/rf+0/87/2/8KAG0A6QBSAWEBOgEKAeoAvwCaAIMAgABTAEsAWACBAJwAqwDNAPQA
IAEZAQABygCwAKQAuAC/ALwAogBHAAQA4f/q/+b/1f+1/7v/qf+z/6H/mf+S/3z/bv9a/2f/c/9f/1//gv+G/8H/8f8JAOv/3f/V
//z/IgA9ACcA+f+z/5r/g/9i/3T/t//Z/8n/zP/s/zoALQDC/2n/O/9I/3X/p//N/7L/af9n/7X//f8FABAA7//8/wwAIwA6AFQA
XAB4AJQAmACJAFQALAAQAAYAAAA5AD4APwAdAOv/3P/P/9z/BwAgAA8A9f/t/wAAGAAHAOL/5//9/w0AJgBOAFQAIADt/7D/rv+m
/7D/ov+I/1z/c/+Q/7H/mP+w/83/0f/Q/9z/y/+t/5z/i/+b/4b/i/+E/5n/if9p/4X/1P/+/wkA5v++/9j/5P8GACoATQAvABIA
+//s/+r/z//K/8H/o/+c/8f/+f8VAAgAAwDy//r/AAAMAB4AFwD1/97/yP/F/wsANQBBADAAHQAuADoALAAxADwANgAvAEsATABJ
AFQAVAAoABEAHgAsAD0AHgAFAPz/BgAFAPX/2P/c/+z/AAAUACQAAQDT/6z/i/+R/5f/qv/a/wAADAAcAD4AVQBJADcALQAnACoA
UwBxAIwAngCdAJ0AhAA1AP7/+P/i/+3//P8ZAFMAWgBmAHoAcgBbAEAAOQAOANb/z//k/yAANgAqABIA+v/s/+r/BwAKAAIA4/8F
AEoAnACeAHwAVwBLAEoAUQBeAGIAUQAxABsABwArAFgAagBeAE8ANAA9AC4AHQD4/9r/m/+C/7H/4P///woAAwDm/97/6P/z//3/
9f/w/+X/3f/w/w4ARQBCADoAPQAwAEAANQAsAAAA7f/w/zQAegCZAIAAbgBZADkADQDh/7H/sP+9/8//7v8AABMAJwAyACYAEgAG
ABEAAQDt/9b/rv+l/6P/t//F/8z/1v/g/wUAHAArAD0AQwBgAFwASQApABsAEwAWAAoAAQAYABsA///o/+j/6//x//v/6//W/6n/
nP+a/6j/rv+y/6f/pv+N/4v/pf+8/8f/2////xAADgD8/wAAEwAiAAkABQAtAEEALgAbABgAGgAqAEEATQBPADgAEAD//+r/2v/W
/+f/7v/q/+b/3v/v/wYABwACAPv/BAACAP7/7v/n//L/+P8AAAQABwALAAcABAD5/wQAHQAhAC4AKwApADgARwBMAD0AJwAbABQA
EgAeAB8AFQDv/9j/6P/8/wgAAAD//wAA8v/J/5f/j/+k/6H/pv+x/77/yP/E/7H/p/+r/63/zf/j//P/7P/d/9v/5//1/wkABgAK
AAMAEwAHAPn/9v/z//3/AQD0/+b/6P/i/8n/rP+V/5z/rv+z/7D/q/+0/9P/8v8HAAAA+P/b/8z/3v/e/9v/uv+p/6r/rv+l/5r/
tP/H/+P/2f/V/9L/zv/e//3/FQAXABEACgARABQA+f/V/9f/2f/R/9b/3P/s//T//v8AAAAABwARABgAIwATABIABwACAOb/x/+8
/8f/yP/F/+D///8SACQAJgA1AEEANAAfABwAIAAjABsAEwAPAAQA6//h/9b/2P/i/+z/4f/l/+3//P8bAC8AMAAnABgA+v/d/9H/
1f/L/7z/u/+6/8r/3v/w//n/+f/r////DQAfACgAHwAgADAANgAuACsAJAAUAAQA9f/7/xwAMgAyACoAGAAZACgANABLAFsAQAAi
ACMAHQAPAPX/4//p/+X/8/8CABoAKQAqADcAJQAGAPb/+/8NABYACwACAAcA7v/h/9//3//r/wAAEQAiACIAGQAZAA8AHAAjABAA
+//8//D/5f/c/+n/8f8BAAoACgAVABgABQAFAPb/+/8IABUAFAAJAAcA9//W/9P/6v8BABMAHQA1ADQAPAA6ADMALQAdAA8AAADv
//j//f/0//T/AAABAAUAAgARADgAPgA7ACsADQD4/+//5f/R/9D/xv/G/9D/2//o//b/AQACAAUADQAYACoAKQA6ADYALQAMAP3/
//8VABYAHAAzAEMAVwBWAGAAXgBVAEkAQwBRAFMAXgBpAHAAawBkAGQATwA7ADEAEAD8////BAALAAwAAgARAC4AMgAiAAUABQAO
ABYAHgAhADIAPwBGAEcANgApAB0AIwAuACMAKABOAHsAigCEAHIAWABHAD0AJQAIAPr/8v/l//D/7//u/+L/y//H/8b/xv/D/8b/
wP/Q/9P/0P/Q/9T/1f/X/9n/4f/b/+P/5f/l/97/1f/V/9f/2v/Z/9j/0P/e/+L/6v/2//j/3P/L/8v/wP+8/7n/vv/P/+j/5//o
/+j/6f/b/9r/2f/V/9v/3f/b/+f/4//b/+f/8f/2////AgD9/wQABQAGAP7/9P/v//v/9P/o/9//0P/J/8H/s/+r/63/vf/S/9z/
5f/Z/9//5f/p/+X/2v/S/8v/wf+0/6b/m/+c/7j/x//g/9v/4P/t////AQACAAIAEAASAA0ADQASAAgABgAQABwAHQAYABAACwAE
AAAAAAAFAA0ADwD8//D/6P/j/+f/1f/K/8v/6v///wgACwAJAAAABQAMABIAGAAPAAkACQARAAUABAAMABUAAwADAAQA/f8MABcA
GgAhACUALwA/AEQARAAzACoALAAdABAABwD0//X/9/////3/+/8HABEAKgA2ADMAMgAgABUACwD9/wAA/f/+//v/AwD3//X/7P/k
/+//AAALABEAHQAfACcAKgAgABsAGAAkACkAJgAiACIAHgAWAAoA+//v/+n/6P/y//v/AwADAAAA/v/x//b/+//6//T//v8CAAIA
+f/2//D/6//4//T//f8JABMAFwAHAP7/+v/x//X/6//n/+7/8/8CAAIAAAAAAAEAAAAAAPf/7f/k/9T/z//C/9H/zv/Q/9H/z//O
/97/5f/n/+n/7/8EACIAMQA4ADoARABIAEIAQgBGAEwATABSAFcAXABkAG4AeABpAHAAZQBfAF0ASwBKAEAANAAxADAANAA2ADcA
LAAnACIALAAwAC4AJAAcAB0AFQD+/+n/4v/V/9v/2v/y/wAABgAMAA0ACwAOAAAADAAPAAAA+v/0//T/9P/3/wAA+v/8////AAD8
/+H/2P/X/+X/9v/9//3/+v/z/+b/2P/P/8v/w//E/77/u//H/9D/1f/U/9D/vv+x/6//sf+z/7//yf/H/8z/2//k/+T/2f/E/8T/
yf/G/8z/2P/l//H/BwAQAA4AAADk/9X/2P/V/9L/y//W/97/5P/j/9n/0f/L/8T/uf/B/8P/wv/I/9D/z//V/9P/0v/f/+L/5f/r
//X/9//+/wMACQAGAP3/9f/0/+r/7P/p/+X/5f/m/+H/3P/S/9H/zP++/7r/u//L/9r/6v/2//z/BQACAPX/8f/j/8//zv/A/8D/
xf/A/87/0//p//b//f8FAAsAGQAgAC0AMgA8AEkASABQAEkAPQA+ADoAMwA4ADMAMgAsADQAQAA0ADQAKwApACkAIAAgACsAMQA1
ADAAMAA8ADcAMwAnACEAGAAVAA8ABQADAPv/AAARABcAFwAdABcAIAAkACYAMgBAAEIAOAAxADEANgAwACgAHQARAAcADAAVABgA
FQAZAAcA//8AAAgAEgAVABwAGwAgACIALAA9AFMAVgBPAEoANQAnABwADAAHAP7///8PABEAHAAaAA0ACQALABIAFQARAAcACQAA
AAYADAANABYAHAAYABkAGgAPAAoADAAIAAgAEQAeACEAHQAiABQAEwARAAgACQAJABYAFgAGAAQA+//2//T/8v/y//b/9v8EAAwA
BwACAAAABQADAAAAAwD4//P/7//k/+L/4v/s/9z/1f/I/9D/0v/S/93/4f/m/9X/4P/c/9r/3v/b/9X/2P/f/+X/9//8//r//v/3
/+n/4f/W/9D/0f/E/8r/yv/a/+X/7P/0//D/7P/p//P/9v/y/+b/2//S/9T/0f/f/+L/5f/h/9z/2f/R/93/3f/U/9f/4P/r//b/
6f/q/97/2v/d/9b/1//a/9r/2P/X/+L/6P/u/+r/6f/q/+r/7P/v//T/+f8LABcAJAAoAB4AGgARAAQAAQD7//T/AQAKAAwADwAV
ACIAGQAUAAwACQAXABMAEwAVAA8ABgD9////AAD9/wEAAwAJAAgACwANAAwACQAYACAAGAAQABwAHgAjABgAEAAXABIADgADAPn/
6v/s//T//////wAA/f8FAAwAEAASABkAHQAnACIAGgAUAAkA+/8AAPT/+P/0//n/9v/v/+r/7v/0/wEADAAZAB0AHwAtACcAKQAe
ABkAFQAIAAAA8v/8/wIADwASAB0AGgASAAsABgASABYAFgAWABAAEQAJAPj/8P/p/+X/8f/x//P/7//v//n/+P8HABYAIAAcABcA
DgAOAA0AEAAPABMACAAMABIAHAAXAAgAAAD3//X/9P/0//P/AwADAAMAAwD4//D/7//p/+T/5v/n/+f/3P/R/9X/z//Q/9X/2P/f
/+T/3P/S/97/4P/d/+L/5//h/9n/3f/l/+3/8v/s//D/5v/c/+T/7v/v//H/5//c/93/1f/a/93/2//V/+b/9P/+////AAAEAA4A
CgAZABkAHgASAA8ACgAGAAkAEAAXABIAFAAUABEAFAAjAC0APAA1ADsAPQBBAD4AQgA1ADsAPwBHAFMAUwBQAEkASABJAEEAOAA2
ADYARwBSAFUAZgBkAGgAYgBWAEkAPgA7ADoARAA5ADUANwBDAEkASwBIAD0AMgAjACEAHQAcABcAGQAgABYAFwAUAAwAAQD6//H/
7f/t//P/9//5//f/8//0//j/+f/w/+z/6f/q/+7//f/4//D/8//n/9f/z//a/9D/0f/X/+L/4P/V/9X/yv/B/8P/v//D/8f/0P/X
/9H/v/+1/7P/sP/D/8v/yf/O/77/vP++/63/qv+u/7H/wf/C/8T/xP/F/7n/uv+2/8L/u/+8/7v/u/++/7r/t/+//8D/vv++/7//
v//I/9j/4f/l/+n/9v/1//r/7//m/+r/5v/n/+3/6f/r/+3/9f/e/9j/3//l/+v/9f8CAAIAAgATABYAJwAgABAACQAJAAYA/P/6
//f/+f/z/////f8BAA0ADgASABcAGAAbABsAJAAjAB4AIAAeAB4AHgAkACMAHwAdABsAFQAQAAcACAAIAAoAFwATABIAEgAZACgA
LQAwADkAQwA9ADMAJAAfAB8AHwAcAB4AEgAXAB0AGwAcABEAEQAPABIADgAVAB8AIwAfABkADgD//+//4//n/+z/6P/s/+7/9f/8
/wUAAgD5//r/+P/s//r/BAAMAB0AKgAqABwADAARABUACwAFAPv/+//6//3/CwAOABMA/v/8/wEA+f8AAAkACQAaAC0AJQAqACwA
IAAjAB0AEAAUAA0ABQD///z/+//5//X/8//4/+T/3f/t/+//9P8BAAoABQAHAAwACgAMAA4AGwAnABQAEAAJAAgACgASAA4ABwAO
AAoACwALAAAAAQAJABEACwAKADn/CgABADwAhv/MAPL/sP+lAHj/DgD2/13/owDn/87/DABqAL7/+f9OAP7/BAA7AP7/VgDo/wAA
8v8wAKz/EwAPAAMA//8/AOH/JQAjAP3/8v8bAPX/6f/U/9z/4f/9/87/+f8GAOr/7v8fAP3///////r/CgAMAAoAHAAKAAUADAAO
APv/EgACAAAAAAABABcACwAOAB4AIgATAAUAAwD+/wIA+f/3//L/7//3//T/9P/n//r/8f/s/+v/7f/s//H/+v/8//T/+f/6////
AAD//wMA9v/0/+z/7P/p/93/3//W/9f/2P/R/9P/1//i//L//P8QAA8AGwAUAAEA9P/h/9v/2f/k/97/3//j/+f/6f/q/+//9//9
/wcABAAJABEACwAPABkAGAARABAADgAIAAUACAAEAAIA/v/v/+j/7f/v/+7/5//m/+r/3//m/+P/9v8KAA4AHwAdABkAFgAGAPj/
7v/s/+n///8LABkAIAAYAAwACAD4//j/9v/t/+7/9v/u/+z/7P/7/wIAAwANABQACwAMABMACAATABUAGAATAAoAEwAKAP3/+//q
/+P/5f/6/wEACgAOABEAEgARAA4AEgAVABYAFAAZABgAFwAjABsAEwAPAAoAAgAHAAIAAgAIAA4ADQAUABoAGwAfABUADAAUABQA
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
fAJIAq8Azv/F/tH8pPzv+6r4cPUy8+bwW/Ax8T7xK/E98Qvwz+4071fvRu+Q8A3y7fJ59ML1kve69yf2RfSM8sbyNvOa9Wb4yvrk
/Mn8x/uk+rT5tfii9+P4JvlY+Cb2OPY391z1jvPe8QHxifCQ8bDyTfJA8c7wY/E+8onzjfV99h726fUd9s31ivS38t/wc/Bt8Wfy
OfSz9tL4Ufpt+xb8LP3G/ggABAHwAUYCnQKTA2kEAgULBsoG+AZ4Bz8IFQkFCmkKfgooC7oLAgxJDIoMpAy+DP4MRg2wDfkN/w2s
DVUNEg3IDKQMiQwtDKILNwvECksK9wlzCe0IUQjaB3oHkgYyBk0FtQRRBB0ENATbA7ADJgPxAvsCsgKhAnICKQIfArEBpwGvAcgB
FwJ6As0CZgPtA5AEEgV3BcEF0QVpBr4G/AaGB7wHHAj6B0gIiAgPCQ0KYwovC6ELlQu9C7YLtwv8C7ALkAvyC+kLIwzPDKIMlwx6
DGcMPAxkDGAMxQsWCygKgQnNCFsIaAcFB4IGpgUoBYQEogOuAvsBLwGNAAAAp/82/1H+TP0m/Gj8Kfnj8tbCzrqBtYDINOrL7Ebu
4eOx5LPc298E20TZ/dox28HdrN4t4KPcK9jIxV3BccmayLnZd/BCBoUUYR92ISokTiaYI1cgThvEGIoZex6lHG8cAhshGhIeCSH8
JVQoeCwoKu0nSyuSLtAxJzCyLDIohCVWIX4cSBi7EtYNkgZ1AG/7k/hr9Vbx4u6k6wnt2u5e73Xw7vGf8zf0ufY1+ef7Wv4Q/0n/
HP9UAbsB2gJTA6QERQRyBE8GTwUqB5UHbQj5CdIKyw2CD9URxBLCExsUfxSDFNkTEBOiEOwOCQ4iDuoNzgxHC1AHmgQPA/cBVgMW
B1cLdQxzDjcQyA+HEh4XzBeeF5MYrxWYEk8T7RBgD8kO8AhnA3MA2vzz+Yf7lvwh/Cn9Y/t8+UH5sPlm+f35sfnf9i/zZe+T6kHm
NOLo3UjZ79QS0cfP1s/gzwrS1tJ901bV7tbF2Zre7uIH5hDpyukD6vfqNOtM61HpBOYl4/LhheFZ4jHkDubo5xTqEO6l8034C/0N
AyoImQuNDkYRghNuFKAUexO/ECYNhQt0C8ULGw0TDr0N1wwnDIUMmQ0qD4QQvBD1EAYR+hA9D/YMlwo8BtMCYwCH/V/6Sfg29Y/x
iO0m6iDpKeor68briOz36+Tp8ujz6IzocejR6ETo7+aR5SfkN+Pm45XjWOMr5KjkTuap6ZrtE+9o8YvzzvR09pP4n/qc/JP+Av9T
AAcBgwDlAEQCKQMSA7sD9ANKBaEHVAl+CqQKhArJCqsL8wx5DkQPYw8xD+sOXA/dEOISiBU8F90XaBefFlIWRhb8FWkVixRHEoIQ
sw/8Dl4OFg36CvQIWAfMBq0HAwlnCnkK8AizByoHsQYxBskEfAFA/m37ufgP+Q/7Fvwh/Yz9E/0T/Tr93fyN/K78bvwI/AD88Pv5
+0X8cPsr+4X6dfnH+FT5q/oI/OL9I/4b/4v+2vmR9EjwRe8y8O3yIfTF8ynzmfOU88rzN/PM72bj9tPMzwXTt9iy3WDiTuW35v7n
n+go6nLrvezZ7Tfuce7B74rx8/J39KL1zPZ++EP6zfuQ/UH/gwAZAl4E0QYPCQQKKgr1CpwMoQ5sELMRgBJFE84THBQKFWEWMBc7
F1sWABblFtQX+helF4EXhBd0F1EXIBfLFj4WhxWrFP8TkRNrEw8TOhJGEXEQ9A+SDxUPgA6sDb8M4gsnC8EKIApaCT8IDgdhBiQG
1QWbBSEFdAS/AwADWgIpAkgCDQKVAXkBsAE5ArgC6AL/AhQDMwNAA10D1gNeBGYEiQQfBc0FLAY1BoUG9QbjBpYG8AadBw0IhQih
CMIIUwnTCWQK7QrTCnkKdgpvCmgKnwqiCpYKWQo9ClgKfAo9CsMJpgmGCR4J1gjDCF4ItAcKB4IGKAYpBRcEhgPpAvgBOgHtANMA
JQA9/z3+s/0v/ZH8n/w5/DP7X/qD+fv48Pj5+I349/fV9x34xfeE93r3CPdq9uP1w/X39YX21Pap9nn2IfaL9gz3a/eN9033xffk
94/3LPcw93v3xvd79w33xPY69vPpC7tgl7KWLqRLrkTClsp70JHglfWeB/IV3xpEFj4TDwtd/y/9BPzQ9Ivyte7g6tLtq/G/9Yf6
IgC1BOIO1xsBKRoxpzL3M/815Tr2PqdA+DziOAMzYSrRI9Ifnxz9FKIKl/wp9Dn3OPjR83LyGfI09AL6Yv/FBJ8L3BDtFD8Zgx0r
Im4mrCinKQoqnSl2Kj0qGCi/JAYgSxw1GZoX9BaUF1kXiBWoFYMXCRqgHHgfmyCIIZMgVB2nG9cXgxQ4EXUM9wjGBMwAF/z89Rbv
t+j543LfkdvQ2kbbDNtZ3kHl2uoY7+LyffeA+2D9Rf+tAT0CE/+b+/f39/Mc8H7tiOvY6K/mCuVQ55XrHvC09UX64fw4/zkC4AXQ
BroDpv4u+xT4HvR68snxze8m7cXrCuwa7zvyH/XD92j6j/2s/zsBvQHeAN7/q/1F+8n5lvi+91n2nvVa9Uf1J/VA9Q325PbR9tD0
bPSw9D70Z/J/8MDu+uyO7ALt6+x/7PfrhOvO7CHvSO/p7p7uQu+c76nvOu9h7m7u0O7a7wTxBvI68tjye/Og86jzqfQz9ZP1KPWi
9Y32CfcY9uvw2uxJ6oTqEuwA79DyUPX29aHz2vAq7o3sy+wk7rzu6/BR8hrxLPER8Rvyz/FW8Z3vPe/k7mLsDOvd6d7pT+o06jfq
x+o07FruJe7/7lzvPfC18F3x8vL19CX3ifjP+e76D/xc/Xb+VP9mAOQBHANhBMQFCQdHCJIJ7wr8C/4M7w2IDjcPZRBeEf4RWhKL
EgUTsRMfFFoUYRRzFFwUPBSQFAcVRRWJFWgVdRWdFZAVOhXVFCIUaRPMEuMRURHCED8Q0w/JD+8PABCiD+QOeQ5IDvwNvw1yDRIN
zwyhDHIMZQxnDCIMqAseC7wKVQoOCgAKwAloCVgJlgmxCYAJSAk2CWgJqwnVCdEJ5QnUCd8JPwqdCvQKdAvHC/kLOww0DOULzwu5
C3oLHwvpCqkKjAqgCqkKyQrICsQK0QrNCpsKVAoiCggKMwpSCkkKLAr9CZkJGAmICNYHJwecBjoG2AWuBWEFAwWGBBYE2gORAx8D
tgIbAoMBGwHgAKAASQBEABcAvf9z/9H+D/5k/fr8pPxQ/Lv7Bftg+v/5xfmo+bX55/kg+iD6Cvr0+fH58/kQ+gb6qvlu+ZP5sfmo
+Y35gflu+WP5efmQ+bL5rPnt+V/63vp3++77PPxi/Kn8Cv2I/en9Sf6K/oT+fP6l/gL/Rf8O/4b+/f3D/a79n/2P/bP9H/5m/qH+
7/5E/4L/pP+p/5T/iP+Y/5P/k/+s/6j/pP+o/9H/tP8o/6j+a/5P/nn+ZP5D/vr9of2v/fv92v1o/WH9Df3P/Of84Pz4/MH8bfxZ
/DT8EPwJ/OX7C/xQ/En8NPwh/AT8FvwW/Mf7rfuP+5n7y/sp/Bv8T/yb/Ln86/xf/dz9T/5P/kb+Q/7U/gP/kf2G72/Iudjy5i3r
4fIs9WT+yfkg+in0bvZf9DLypvO58+DyF/VZ93f1qfdR91L3OPhF+kz64vvt/A/9LP4/AGsDJwS7Ax4CXANKBVkHEQfWBlAISgjK
BxEHxgjbCXgJLgnjBooGOgorChEI2AcdB88H1gigCZ4KwgpaC8wKBQzdDI8NQQ5qD3QQwBICFZIXPhoBGrwZxRiZGWkZKhqtGeYY
Bhj9FjkXdRsCHm4g/yE0IEAewh54HxIfjB8THegbkBX2FdAXvxtiH1MejRtDFW4UgRUvGZAcOxy5EFX/GfMp7iH1Vvom/kL5xvFL
67vpp+nh5FnjId8F3aLfbt6O3trfqN5T27LXatGAzzHRrdNx1YbW49aw2L/a49si3NPbYNvJ3BffBOHz4ULiFeL+4izluOZe6azq
t+u16yvsnO2g77Ty5fOr9H/0aPUr9uD3PPnw+WD6PPkd+kn7Nvxr/vj/AQCI/5v/0f+aACQAVwCKALAAPgGLAH0AYv80/8n/pP+x
/+L+2P8j/0b/vP5D/ur+yv8nAMf+b/3T/KH9b//C/6b/o/4F/v/9Yv54/+H/Pf9Q/m//uQApAmgCQgLDAl4DzAOQA14DTAM3BJUF
EAfcB/YH7QeCCCUJEgpaCnwKqgo7C6QLAAz5CzcM1wxTDRcOqw26DSAPHhAcEXUQVxAfEPAPeBDcEFoQFBDBD80P+g9+DwUPkQ6e
DloN5wxgDLkMTA0VDUQNUgygCx0LuwqoCRwJwQiaCKYIvgdzBgMFVARSA5wD6AN/A4QCwAHJAAgAf/9G/2X/KP8J/6v+zv0U/Rv9
3vyI/NX7+fua+8X7hPxr/Fv8tvtB+1/78/sR/fn8Gfxg+2n72vvh/OL8Ov31/JP85vye/YL+/P4z/+7+wf4B/lT+6f5w/4P/dv95
/9X/ZABpAJcAegBOABoARQC+AJsB5QFeAmcCmwGCAa0BQwKqAjUCDwHmABEBfwGvAekBmgEHAREB7wC8ALMAgQBJAPz/+v/J/53/
dP+H/zD/9P7P/pP+Mv7y/Yj97Pwy/Bb8q/wT/dL9/v1v/b78tPyh/Lb8nPxo/Hn8yvwH/d78sPwa/DL8dfzV/O78ofy7/Mr8C/0Q
/Wn9Vv3C/ez95v3G/ez99P00/kX+HP75/f39ov4a/3X/s/+Q/5r/5/9JAEsAWwCEAIkAxgDjABEBLQE7AVYBcAF3AZoBpgGZAZMB
aAFeAWQBaAG9AdwBuQGPAYgBpQGpAVsBwQB2AGUAgwCQAJIAhACdAJUAbwBrAGAADQCk/1D/Qf9C/z3/Rv8+/1D/UP9M/z7/TP9A
//r+1P6P/mX+Uf4y/h3+F/4Y/iz+fP6F/pf+vf6Z/pD+nv6c/pH+jP5s/mP+eP6f/sH+4f4J/yj/Sf9g/4v/m//L/+H/4v/x/93/
4f8gADQAJAAsAB4AOwCJAJIAwwD+ANwA4gD5APkABQEWARwBFQH3AN4AvADBANQArQBrAFEAZgCDAI4AbABsAGgAMQA3ADMAMAAe
APP/z//L/7H/pf+p/5H/ev8y/yr/QP9K/0H/R/9F/xj/6P7C/sv+0f6w/rb+zP7u/gH/DP8q/xr/NP9a/1//cf9T/z7/W/9m/4z/
oP+m/8D/t//g////FQAjAFMAlQCzAMsA1ADGAMsAmwCSAJAAbQBuAIIAtgDSAPMACwEbAQoB2ADTAOsA9gDnAOYA1QC4AKwAlQCG
AIAATQArAGUAkADXAOgA1ADpAPYAzwCTAHYAdQBcACYAAgAhAC0AMgA5AAkA/v8lACAAAgAQAAAA1f+//63/of+A/0f/Pv86/0r/
VP9U/0X/Q/80/zr/Nf9P/0//Rf9U/2T/WP9K/zH/Ff8t/zz/V/9y/5z/qv+g/6X/uf+2/7//v/+0/5H/c/+c/7z/xv/C/7n/yf/O
/+H/9P/u/+v/8f8BACcALgA2ADAAKQAfAAQADwAtAFEAaABvAGwAXwCDALAAsACPAIYAgACAAHQAeQChAKMAfwBYAFQAXwBcABQA
BAD6//v/AAD0/wMADADt/7P/v//U/8H/zf/M/7P/q/+d/5//pP+f/7H/oP+E/4n/iP90/23/U/9I/0D/df+R/57/oP+N/6X/y//s
/xMAFwAHABgAJgBHAEgARABBAEYAOgAmACYAMQA4AE4AYwBsAGEAbwCWAKoAlQCKAKgApwCUAJAArgDKAM0A5wDiAM4AtwCtAK4A
rwCkALAAugCsAKYAnwCwAMgAuQCkAJgAlgCuALIApgCkAI8AVwBAADoAHwAnAC4AQABnAGEAawB8AHsAhAByAH0ASAAUAPf/3P/a
/+z//P8KAAIA9v8RADEAMwA1ADsAKAAmACEAGgAZABAAIQAiABYACgAQAAYA/f8gADEAHQAZABAAGgAmAC0AMgAxACIAHAAgABwA
KAAaADYAVwBKADYAKwAiAB8APABTAE0ATgBZAEgAPAA4ADoAPAAqACsAKQAgABwALAAtABIA7P/v//f/+//+//r/DQD+/+r/4f/S
/9H/3v/v/wEA8//o/9j/yP/Z/9z/2P/s/+L/0v/W/7n/m/+F/5T/rP+9/9L/y//W/+7/9v8JAP3/1//Q/8f/u/+//8v/0//N/8b/
wv/V/9f/5//v//z/4//M/7T/q//C/8j/r/+c/5n/pf+l/6v/q/+i/63/v//B/9T/1P/N/9L/v/+u/43/jf+e/53/nP93/4f/kf+y
/8X/2v/r//f/+v/v/+X/4//k/+z/9f/8//L/2f/J/8H/vP/E/8j/wP/f/93/1//R/83/yv/M/9f/y//B/8b/2f/x/wEACwD2//H/
+v/8/wIADgAhAC0AEAAAAPr/CQALAAsACgD6//X/7P8AAA4ABgD4/93/uv+q/67/vf/B/7z/q/+o/7z/w//P//P/8P/a/8L/uP/Q
/+L/5f/h/9X/4v/z//X/+P/9//L/4v/e/+v/5f/U/9r/3//q/+3/6f/9//P/5v/i/9r/w//K/83/0P/E/73/r/+r/7H/tP+z/7L/
rv+8/9H/zv/D/63/rP+6/7n/sf+l/6j/vv/J/9T/6v/u/+v/7v8EACcAQQA9ACUAEwASAAwAEgAAAAEAHwAlADcAPAAvADoANgA9
ADoANwA5AD4APQBHAEYALwAiABYAHwAmADYAKwAIAOf/1v/W/93/9P///wAABAD5//z///8TAAYAEQAbACcAQAA5ACkAKgAmABwA
CwDv/+z/7f/k/+T/3v/c/+f/8P/y/+j/6v/h/+z/AwAdACAAEgATABwAOAA4AC4AHQAGAPv/+P8AAAAA//8EAAAABgABAAEACwAQ
AAUACgAYADQAUgBMAEoAQQBHAE0ARwBNAE4AOQAyACQAHgAXACIANQBAACwAIwAoACkAKAAgACwALwApACIAJgAeABEAEAAMAA0A
EwAXAA8AAAACAAgADwAPAAcAEQAJAAgACAAFAAgA//8JAAwACgAjAB0AFgAUAAQA8v/p/+r/8/8JACAAEgAWABAACwAAAOX/3//d
/+n/8f/8//7//v/9/wAACwAfACYALwAlABcA//8LABcAKgAmACAAKQAqADIANAA4ACgAIgANAAMAEAAWABwAFAAVAAAA9v/0//z/
9v/3//3//v/z//P/9f/1//P/6//y//f/7//1////CwAZABwAGwAbACEAJAAdABwAFwALAAUAAgD4//f/AwACAA8AFAANABUAGwAU
ABoAKAArAC8ALgAtADAALwA0ADcALwAyACkAHwAJAAoADwD6//v/+v/7//H/8f/2//b/8P/3//r/CwAcABoAGgAVAAgADAD+//b/
7P/f/9r/0//U/87/3P/j/+T/4P/o/woAFQAZAA0AAwD5/+7/8P/u/9T/wv/U/+b/7f8AAAwADAAAAPz/9P/4//r//P8EAAoADQAK
AA0AGAAcAA0ACgADAAwADgAZACYAJwAoACUAKwAeAAQA8P/V/8//1//c/+j/7f/s//X/8P/u/+r/4f/l/+f/6v/s/+r/5//v//H/
6P/p//H/6v/k/9r/2v/i//T//v8CAP7/+f/p/97/1P/W/9n/2//i//L/9P/t/+3/7v///w8AHAAZABoAFgAMAAYA9v/4//n/7f/v
//T/5//s//P/AAABAAIAAgANAAoAEAAWABkAGAARAAwA//8AAAYABAD9//T/9v/p/9z/1f/S/+L/7f/9//z/8P/q//f/DAAVAAgA
EgAUACIAGAALAAIA//////3/CwAdACsANQA3AC8AKAAlACcALgAvACkAHgAYAB0AFwAdACkALAArADUAOQA5AEIAPwBEAEIAPgA5
ADsAOAA1ACgAIgAQAAwABAD3//f/7f/u/+r/3//U/8n/wf/C/9D/5P/v//T//v/3/wAA+//9/wAA/f/+/wMA8f/y//f/9f/y/9r/
z//X/9D/yv/I/73/tP+q/6b/pv+k/6v/uv+6/7r/rP+d/6H/pP+d/6D/p/+q/7L/uf++/7v/w//M/9n/4f/m/97/2//U/+3/7P/1
//P/6v/y//T//f/6/+//7f/s/+//6//w//L/8//r/+H/6f/q/wIAEQAoAC0AJAAiAB8AJwArACsAHQATAAwADQAbACgAMQA2AC4A
IgAVAAMA8//g/9P/0f/j/+3///8JAA4AEAAQAAoABAD//wIAAQARAA4ADgAOAAUADwALABEADAAZACcALgAxADIALgAjACYAKAAt
AC4ANgA4ADkAOQAxADEAIwAfACMAIAAhACcALwAyAEAARwBGAE4ATwBGAEAAJwARAAkACQAGAA4AGwAYABUADQAOAAkAEwAaABQA
GwAhACoALgA2ADQAMQA3AC0AJwAdABgAGQALAAYAAAD8/wEACQAVABgAEwAZAA4ABAD5//H/9P/5//b/+P/4/+f/5v/e/+T/6v/o
/+7/9P/u//L/6f/T/87/zP/C/9D/0P/i/+r/6//5//b/+f8BAAoAAgAAAP//+f/6//j/AAALABIAGQAaABsAEQAUABkAHgAdACcA
MABCAEwATgBXAFcAVwBPAEwATwBCAEoARABHAEkAPwA2ACkAKAAgABMAEAALAAoACwARABYAGwATABQAFwANAAUA+f/i/9n/0v/U
/9H/zf/K/77/s/+x/7f/sv+x/7H/t/+7/7j/vv/E/8X/vv+2/8L/sP+n/6j/sv+9/7f/uP+//7//xv+9/7v/uv+y/7b/uf/G/8X/
zv/a/+z/8P/u//D/4P/f/9//4v/h/+f/6f/2//z/9f/w//H/8v/s/+T/5P/j/97/5P/o/+T/4//m/+7/6P/e/+f/9f/z/+v/5v/Q
/7//r/+u/63/of+p/6r/qv+y/6r/s/+//8n/1P/T/9j/5P/r/+n/7P/g/9n/5P/j//H/9P/8//3/9/8AAAIAAwAPABYAEAAKAAkA
CAAKAA0AGwAcACUAHwAlACoANQA1AC4AKwAbABIAAgAAAAAAAQANAA0AEQAaAB8AHQAdACEAIwArADIAMAArACQAJQAbACMAIAAM
ABAAEQAMAAUADQANABsAHAAeABIABQD8//r/AAD8/wEADQAaABQAIgAlABoAHQAkACQAKgAtADIAPAA5AEIAPwA1ACoAJwAlACMA
GAASABkAIAAjACMAJAApACQAIgArADQAMgA8AC4AIwAcABoAFwAOABIADAAKAAoABgABAAsACgAFAA0AEwAQABUADQAMAAYADAAS
AB0AIgA0ADsALwAcABkAHQAbACUAJAAiAB0AGgAcAA0ADwALAA0AHQAoACgAHQAYABAA/v/3//j/AAACABAAFgAVAAsAAQD1//H/
5//o/+v/4P/m/9//5f/1//z/AQD+/wAA7f/m/+H/2v/d/+P/8P/u/+//6//w/+j/1f/W/9n/4P/c/93/4//g/+n/4//i/9b/1P/Z
/9H/1f/M/8v/yv/M/87/z//E/8j/0f/V/9j/4P/q//7/BAADAAcAAgADAP7/9f/x/+z/7f/2/wIAAwAMABEADQAKAAMABwD+//f/
+//5//r/7v/w//r//v8DAAUABAAIAAIA/f/2//n//f/+/wgABQAGAAUADwAIAAcACwD+//j/4v/g/9v/2v/e/97/3v/N/9H/0v/V
/97/7f/u//P/8v/t/+//8v8AAAUACwAEAAcABgAFAAUABwAHAAwABwAKABAACAALABYAJgArADIAOwBAAEgAQQA/ADYAKgArACAA
GgAgABkAFwAVAAoAAwAIAAYA/f/8/wAA/v8QAAkAEgAdABsAIAAkABsAHwAOABIACgAEAAkABQATABYAGAATAA0AAADu/+H/5//1
//b/+P/1//z/BgAJAAYAAAABAP7/CwAUABYAHAARAAwAAAD+/wEA7v/w/+r/9v/9/wIADQASAAkAAAAEAAEAAQD//wIABQAAAO3/
6v/v/+7//P//////+//5/wAAAQADAAIA/v8HAPz/7P/j/97/3f/Y/9D/3v/e/+v/+v/7//L/5f/v//X/BwALAAQAAgABAAkAAgAA
AAYADwAIAPv/8//v/+T/7//7/wgADwAIAAoAEQARABMADwABAAEA//8BAPf/8//t/+7/8//0//P/8f/y/+v/6//m/+L/4P/j/+n/
6f/0/+z/7//w//v//f///wAA9v8AAPz//v/y/+n/6P/j/93/4//t//z/CwALABEAFAAVABkAEgAYAB0AEgAJAP//AQDz//H/7v/u
//f/8P/3/+//+/8IAAkADgAbAB4AJAAdABAADAAGABMAHQAgABgADAAGAPz/7f/q/+r/+P/9//3/BAD8//b/9P/9/wIA+f8AAP//
AAAAAAcA+f/3//j/8v/u//X/+v8AAAoADwAYABIAKAAlACUAIAAUABEAFQAbAB4AHQAVABcADAAAAPv/+f/4//r/9f8AAAIACwAL
ABMAFQAOABAADQAHAAIA/f8DAP//+f/5//j/AAACAP7/9v/0//z/AgAAAAMACAAWABIAFQAUAAMAAQD1/+3/5P/d/+D/5f/x//j/
9//u//L/8//y/+//7//o/+3/8f/v//b/9P///woADgAJAAAAAQAAAPb/+f/3/+//9f///wUABgACAAYABQAMABAAEAAJABUAFgAL
ABIABAAFAAMAAAAGAAEAAgAJAAAA/v////b/6P/r////BgAXABkAGAASABUAHQAXAAcA/f/t/+z/9v/5//r/+v8GAAUACwAUABoA
FwAVABYAFgAaABEAHQAaABUAGAANAP//+f/w//L/9//3/wcABAABAAMAAQAEAAIA6v/r/+v/9f////v/BAAHABIAEgAgACsAKwAi
ABwAEgAMAA4ACgAHAAAABAAGAAUA+//0/+//9f/3//z/CAAOABUACAD8//j/8v/k/9//4f/Z/8//0f/Y/+z/7P/w/wAAAAD9//r/
8//2//L//v8CAAYADgAPAA0AEgAMAAYAAwAEAPz/9v/5//z//v8CAAAA+v////H/6f/u/+//+f/1//P/9//2//j/9f/r/+L/2//d
/+X/8v/5//n/AAAFAAUAAwAEAPj/7f/o/+r/5f/d/+T/6//r/+7/+//5//L/5v/i/+L/3v/j/+v/7v/l/+v/7//p/+f/1//Y/+X/
6f/v//f/9f////X/+f/+/w8AFAAOAA4ADgAXACEAKgAqAB4AGQAWABEABgAAAP//9v/4//X/9v/1/+//6P/q/+n/4f/i/+z/7v/t
/+7/+v/8/wMA+//3//v/9v/1/wEAAgANAA4ACQD9//r/7f/s//D/+//4//D//P8AAAcACwALAAkABQAAAAMA+P/8//3//P8AAAAA
DAAbACsAHgAdABMABwDz//X/AgADAA4AGwAfACEAJgApADEALgAiACoAFgAQAA8ADAAGAA4AEQAZABgAEgAKAP3/8P/s//D/8/8A
AA4AEQATABQADwAQABoAIQAeACEAGgAUABMACAATAB4AFAAXABcAEAASAA0ACwAFAP//9f/2/wAA/P8IAAcABgAJAAsABwAAAPz/
9//9/wIABAAIAAAABgAGAAsAFAARABwAGgAXAAwAAAAAAP//BgAEAAoACAAEAAQABwADAAIAAQABAAkA/v/3//P/7v/0//L/+/8A
AAgACAAAAAcACAAIABMAGQAcACAAFQAMAAAA9f/f/+z/8f/4//7/AwAAAPP/+//3//n//v/5//P/8//s/+T/6P/v//b/+f8CABAA
GgAhACIAFAAFAP3//f8GABgAIgAZABwAGQAcAA0AEAAIAPv/9v/r/+r/6//v//j/9v8FAAgAHQAYABUACwAFAP3/+P/5/wAA//8D
AAMA9//1/+v/6//y//H/+P/2//3//P/1//v//v8EABQAEgAOAAwABAD2/+r/6f/h/9//3v/f/9//5P/u/+f/7//2/wAADwAXABoA
FQALAAQA8//0//j/+f/9////AgAAAAEAAAD6//v/7f/l/+f/5f/0//r//f/+/wAAAgADAAEA/v8AAAAABwAGAAUA/v8AAPr/9P/g
/93/4//i/+D/6P/v/wAAEQAfACMAIgAXAA0ABwAAAAAA+f/8//j/9v/1//D/6v/s//H/9P/3/wAABAADABMAGQAbAA4ADQAHAAsA
AAD8//n/9v/6//L/9//3//D/+f8BAAYAAwAGAAoACwADAA0ADQAWAB0AFgASABgAHAAYABUACwARABIAFQALAAEABwD9/wIAAAD5
//D/9v/y//P/9f/t//H/5//n//D/5v/i//D/9P/z//H/6//3/wAADQAKABMAHAAQABcAGwANABEAEAAUAAgAEwAVABcAGAAWAA4A
AAAAAP3///8DAAUABwACAAcACAAOAAIA/P8AAPf/8P/p/+X/6P/y/+3/8P/u/+3/7v/n/+7/8f/8/wMABwAQAA8ACwARAAIAAAD3
//H/7v/w/+//+P/8//b/9f/6//X/+f/7//r/+v8AAPn/+P/z/+r/7P/s//L/4//h/+T/3f/j//L/6//o/9j/z//O/9P/1v/d//L/
9f8AAAkACgAKABEAEgASABEACgD+//3/+P/3//j/7//3//P/9f/z/+v/3//e/+z/9/8HAP7/BwANABAADgAQABkAHQAeABcADQAL
APz/+v/q/+v/7f/y//L/+//+/wsACQALAAsADAAWABgAGgATABEADQD+//v/7v/w//3/AAD//wwADAD+//7////8/+v/9v/2/wEA
DwAeACoAKgAlACMAIwAgABgAFwAQAAYABAALABAADgAaACUAIAAYABMABgD7//v/9//9//7/+/8NAA0ADAAFAPr//P/x//v/AgAB
AAYADgAaACgAHQATAA0ACQAYAAsADwAVAAwABAD6//P/6v/l//H/8P/z/+7/5P/o/+X/6v/5//z/AQAOABEACAAGAAQACAAIAAgA
DQAEAAUACAAAAP7//f/8//7//f///wAA+f/6//7//f8EAAMACgADAAQADAAQABYAGAASAAwA+f/1/+H/2P/U/9r/1f/i/+//8//3
//7////3//X/9v/9/wMA/f///wIAAwD6//r/+P/4///////x/+X/6f/1//3/AQAFAAcACwAIAAcACAD///r/9v/9////AQD1//f/
/v8RACEAIAAnAB4AGAAXAAQA9v/t/+//+f/9//D/6//o/+z/2//X/9n/3v/k/+z/8v/3//r/AAABAAUACwALAA0AEAAQAAMA+P/v
//j/AAABAAMAAQD///r//v8AAAEA+f/+/wMABwACAAUAEAAaABkAEgAXAAYACgAHAAQAAgAAAAAABgADAAIACQARABgAFQAUAA4A
EQAFAPL/+P/0/+//7v/7/wUABQACAAgACQAIAAIA/v///wYACwAKAP7//P/0/+7/8v/p/+7/+v8AAP3/AAD+/wEACAAXAB8AGQAc
AA8ACwAJAP7////8//n//f8CAAcACgABAAMA/P/2//b/6f/1//T/8f/q/+L/5P/e/+D/5P/2//f/8/8CABEAEgAfAB8AFgAUAAwA
DwAMAAEABAD6//T/9//3//X/9f/1//3/BwAGABsAHQAXACQAIQAkACIAJgAdABwAHwAZABQAAAD9/wEABwAHABIADgAJAAcABAAQ
ABgAHQAgAB4AFwAZAA0ACwABAP3/+P/4//X/+v/3//z/AAD8////AAAPABMAEAAYAA8AFAAKAAIAAgAEAAsAEAAXABgAIQAbAA4A
CAAGAAUA+////xMAFQAeABMACgAIAAkABQAKACAAKwAxAC0ALQAbAAoA/f/2//T/6//q/9z/1v/U/9P/2f/k/+z/9//3//T/8v/r
/+b/+v8BABAAFwALAAoA+P/z//f/8P/2//z/9//r/+j/6v/u/+j/6f/x//b/8//2//j/8v/z//j/+f///wUAEQARAAoAAgD3//n/
7v/q//L/9v8GAAsABwALAAoA/P/y/+//7v/5//z//P8BAPz//v/0//H/9v/+////8P/q/+P/7//4//r/AAD7//T/6v/e/+H/1f/R
/87/yP/P/9b/5v/m/+z/7f/u/+r/8//1//3/AgARAAgACgAXAA4ACwAAAPr/+/8FAAgACwAJAAEAAwD7//n/8v/q/+r/7//x/+//
6//o/+z/7P/3//j/6//1//L/7v/s/+3/9//w//D/8f/3////+f/x/+7/+f/5//3/AAAIAAcA+P/0//j//v/9//z/BAAHAAUABwAI
AAsACQALAAYAAgD5//T/8P/1/wAADQAVAB4AGAARABAADwAAAPr////7/wMACQAHAA0AAAAAAAEAEQAVABgAGgAdABsAEgD7//b/
9v/8/wkABwAAAAUA///0/wAAAwAOABcAGwAeABAABQD5//D/7f/u//v/+v/9/wIACwAAAP///v8AAAQACgALAAcA/v/1//X/8//7
//v/AAADAAYABgD9/wkABAAHAAgACAAQAA0ADwAFAP///v/8//3/9//0/+7/5P/r//r/AAAEAP//BwAKAAoABwARABAAEgAPAAkA
AQD8//P/+f/6/wEACAAFAAoAAgANABYAGAASAAQADAALAAsAEQALAAMABwAOABYAGQAVAAwA///4/+f/5//w//H/+//3/+3/8P/6
/wMABAAAAAAA/P/9/wYABQAPABcALAAtADcAOQAlABUABAD///z/AAAPACUAJgAnACMAJAAWABQAHAAPABAABgADAPv/+P/o/+z/
5P/a/+X/4v/r//b/AgAAAAUACgALABUAHAAhABUAFQAQAPz/7v/t/+D/3v/f/+T/7P/z//z/AAAAAAAAAAACAAsAGQAfAB8AHQAZ
ABAA7//8/wYAAQDd/wcA+f/k//v/6P/s/wAA//8PABEA///6/xIACADu//j/+v/v/wAA8v/U/8n/zv/X/+j/6f/r//D/8f/l//H/
2//a/+3/+f///wwAEwAFAPv/8P/p/9//z//i/+f/2//W/9n/5f/q/+//8//y/+7/6f/k/9r/3v/P/9P/3P/o////AwARABAAAQAJ
AAwADAAOABEADQAKAAcADwAKABMAIAAjAB4AFwAJAAAA/P/w/+j/7P/v//T/8//u//n/9P/7/wMAAAADAAgAEQAQAAQADAAPAAUA
BQALAAgADgACAAIA+//2//P/+//9//v/AAAAAAMAAAACAAAA/f/2//7/AgD9/wEABQD9//X/7//y/+n/7f/v//P/9P/y//b//f8A
APr//P8QAAwAFQAWAA8AEgAEAAAA///9/wAA9/8AAPD/8P/w//z/AAAAAAIA//8IAAAA/v8CAAAAAAAKAA0ABwAMAAAA+f/y/+z/
5v/3/wYAEQAZABkAEwAKABMAFwAjACEAGQASAAwAAwAAAAAA+v8AAAEAAwD1//j/+P/1//L/6//m/+H/7P/8/wAACAAEAAsADgAO
ABEAFQAWABoAHAAdABoACQAKAA0AAgAAAP3/BAAFAAsABgAEAAYADAABAAYABQD9/+z/5f/t/+//BAAQABAADwAQAAYADwAHAA0A
FwAeACcAJwAlACIAGAAaABUADQADAP//AgABAPj/AwAGAAUABwAJAAoABQAHAAgABQABAPr/6v/p//H/BQAHAAoAAwD6//r/+v/3
//T///8IABkAIQAVAAgACwAHAAsABwAJABAAEAAbACgAKAAbABEAEgAJAA0ACQAFAPf/+v/t/+n/9//t/+3/+P/9/wYABQAIAAwA
CgAMAA4ADwAHAA8ACwAKAAIABQD2/wYA+/8DAP3/AwABAAMA/f8EAP7//v8EAAIA/v/8//3/+/8KAPP/6//r/93/4//a//T/6/8B
APz/BAAGAAAABAAWAAAACwAfAB0A+f8GABYA6P/s/93/3v/h/83/zf/q/zEA/v/o/xgADAAOAA0A5f8RAAYA8P8RANb/8/8qAPD/
7f8RAEMA3v+8/00Ayv/p/+//AAC+/8z/NAD2//b/NQD4/wgAHADS/wMA9f/u/2MAtv+4/zoAGgDr/9P/9f/F/67/kf+M/w0A7v+4
/9//YQAzAOL/MwD6/97/qf/O/57/EgCT/48AjP/9/wQAoP+e/9X/YQCM/8f/jf9XALn/8//K/zIAy/+W/5v/ef+X/6L/CAATAO//
4P8qALT/qf/t/6v/7v/n/27/rP+c/xIAuf9a/6j/lf+I/0b/zf/G/8v/qv+a/+b/yf+a/3T/4f9f/4X/o/9M/5H/OP9Q/y7/c/9N
/yz/Uv/X/iz/vf7N/gr/z/6R/uL+qP7f/uH+rP5x/pP+EP7w/ZP9rv8SAp74X8/JinK2Id6H3y/uot8O5XLPgNvyzWfbq9oG2M3d
kdnh1f7dVuYp3nnkouPj4T/pye/d7w3tLfY6/GQCVw8VGYMchRi7DusTHxzWIc0ish/VIWEgCiB3HekhxCsOLPYsmSN/KAk3vD1M
O849RUHmQg1G9ke4TaRMm08ETehLt0sfR1VAvDeXM342tzNWM4QtvS6pMQ06zzrXPbJA90MEQw827iqTE2IJqgGk+v39/ftZCn4Y
lh4fHYEbHiHSJlMgBR5lBuD4EPwv+XoIgRSNFKsSwhL5DsYXUBvuHLAktR+MHM4I4PRS7RDl8Nz0yqnRGOnC76/a8MS1zM7dmuGN
0Ra8UbzPwoTFi8ekwjzDi8LZw4a8hKrwpaWkm6a5q4GtpbDes/S3erlzu3C9ib/8wDTBbcNExQnGtMWByEnMrcyNzd3M3c4a0u3V
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
cQZGBOUCsQRNCOcKcQxdESoUyRVOFeUMFv8X8Dvf/9Bixuy9nrqjulq8lMDTxLbIjM4f2J/ibelX7brs1uhy5oLlt+IW4xLnku3f
+FgFKxN7IPwo+CqIKtsl+x/eG3IXhxGuCkEEDfuC82/uROnw5OTiiuPB48fkR+k/7cvv7vLj9F/2MvdT94D1OfJa72ztdezq6sTq
Te3w77XzU/nP/z0JVBKAGVwfKiBvH6MdABhKE0MQggxOCVcHhQUQBEsBB/z+9tnz2PHD86r3SPvB/Ez7pPqP+WX20/Dj7lnwRfNX
9e/1ffZV9+b1GPMy8Dvrgulw5/fjSeNo5WTpE+1G7S7t+u1u7mXv5+sw7M7un/EN9If1//ZY+Jj5dvnK+Tv6Ifry+Sn6Z/pd+/v8
9v0N/4gB6QSHB6IIhAlXC/wMYw3/DSUPlg+vD1oPKQ9lD8UP4RDjEf8SzBRNFvcXMxnWGd0aARzxHDod8hv8GjoaDBm2F94WcRaK
FmgXqBdfF3UX4xZUFuoVqBRnE78SHxGnD5YOOw1CC6wJ0AhwCGoIVAhSCCUIEwg3CMYHVgYJBegFrwanBswGuwa9Bt8GWQZSBjgG
HAVdBJIEzwTfBBsFaQUbBkMGgAYqBxMHtgbRBUYEsAPoAxAEQgS6BN0E5gQUBhsHOQfOBwgI6Ad9CKAIhQdnBhoGUQXlBAMFxwRE
BLsEPgVIBS8F9wQBBUAFCgU9BMwEmwSXA/0C9wHBAUECgwLvAmcDvgOVA6MDJgSyBF8FugS3A30D+wJCA9EDcAI2AaEA2P75/HX8
lPtT+6X7OfpZ+eb6nfpn+lbl1LQZlL+Oo5A35P8Y0fnNAivrvaZpsoSuuLiy4B71RgiLGLkXxg7vHr8QEwMSA6D7V/3pEycZZSHp
M7k7uUuOV9heq182Xl9ZflTjUblL8D37JWwT5/pw5P3baNbC3SPdY9ck157cYeslA+QEwfo8AeP8NPP27aDmteDE5T/u6/n0Cj0X
uiYlL/svUzWxPG1DL0klSAFFGUCVNZknZxo7C+/7K/CT5PvgZ+Ae5Q/oBesV7ozt3evF6XPqXugZ6A3fpc8exkzAd7hntGC3u71z
yXLdZPwMH2s911FKW1VftGBGXMNTTEYoM6Id0gZp8JnjH+ED37bhxueH7tH3GwTXDmUVWRdWES4K2v5t7zrbZcfit6urjKSOoFCe
LZ5XoSiozLWFzBvq+w5GLpND3lKXWyZfmF4yWlJPEz8fKGcN2vIp39XTUtGo1x7p4gAlFkgmQi5HMKosWiLnFDcFD+z50Oi786pK
obadC5wMnFue7aJ1rCW8r9JS7XsCDBEVHa4jriO2IZ0cZBbFD2cIYAEI/br6RP3JAy8MKRloJ3A1Sj4TQow/8zYrJyETL/255qvW
CcoIwRS72bfGt6i7s8MizZLb2+r/+D0FuA0ZDgwHhvrh6UvaO88Qyj3M+9Mp4mv2vAzpIac0HUEWSBFLdEvpSF9D4jl+LOoafAd4
9gHrB+Jo3t/eOeHb4yLoo+z872X1FfuF/6MBbgKFAVX+y/lu87Ls4uRS4DLfYeEG55rvivoKA6wHAgwNEpEYJB5KI8YmFSe7JU4k
ByDeGS4UwA2LCI4EUAJFAVQA6/8q/0X9oPva+r/7zvzE/Rz/rf4j/an92P4H/vL7lfsr/Yz+l/9f/1ABCQHu+1z4lPkr/iMBCfuE
8JzsZusQ6MLpM+558eHvifBT8lX0DPZB9oL3L/e49j/25/XM9Fj0FvX29Kv0BfX59TH3jPiR+YP6D/yO/hEBFgIzAicDTAT0BM0F
fAaHBrQGwAZwBksHbQljCxkNFA4RD5UQpxHREsMUMRZtFg0W7BVNFqQWWBZOFYQUqBMHEyATCBNnEg0SqxEsEZUQzg/kDwQQSA/5
Dc4MzguWC60LtQqzCacIOgddBuUFYgX9BP0EUwXUBQ4G1wWZBS0FcgW7BRQGMQYXBgoGlgUpBUUE3gMDBDUEgQQkBIADqQPjA5YD
4QLVARcBMwH0AL8AHgHxAR8CIQKyAggEDAQaA5kCJAN5AwIDFQOHAp8BnQDI/03/Jv9IAEEAzv9VAEgBFQIRAggCfgI8A7YDfwPH
AzcF2QT+AxsD8AKBA2UEBQWEBDsEBwRyBOEERwQ7A5kCVQJ7ARMBGgE6AaoAi/4Z/fv8Av33+8D7KPx1+076rfr9+x78Lfuc+tj5
vPlk+iP6xPlP+VX4A/js+Qf4AOSTqPGKc5aEnymXPZnftobtByURRb9FmE3nRSIvkR61AdTXlcuevzSymLURuIrBReGJBrMi7UGd
VCVfwmbraZxquWcCYuhVqkKKLPESQvMg3x/cI9+p5qL1Sg9rLz5Cnk22Uo9V5VkHWf5KEjmSIkEAqtjQt/OmxJ1ymsCbvZ+dp0W8
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
bjaDNVg0WjGBLYspTiakIhkigh8LGZkTaw9fCUsEPQED/sL5+vQk8FXsCOnk56Hobeok6t7ovOdp52LnN+f86DrpTuh26jfvpfWd
/n4JkxJmHDIiciXKJxUnoyReIxkhBx3rGUcWwhFrDzoN8wp9CWwIsQZrBRgDt//7/Mv4YPe69eL1qPam9134/vlm/J39AQCIAMX/
9v5q/qD9tfvo+Bv15vMk9ND17/g0+8/9uP0p+kb1TvOh8Yju++qi6ZXqV+y67D7s5uwa7m7v6+/S75vvbe8x75TuE+4u7jjvXvCc
8Rbzo/T09XX32vg5+n37h/wv/fn9Tv/uAP0BdQJLA7oEhgY2CKIJEQsRDPIMeg0cDpAOBg8MD2oOTw4QDxQQPxGxERoSxBIJE+oS
uxJeErkRDRGPELAP7Q5rDhQOrA0cDYoMAwzCCzQLygo9CjQJtwgICMAHlweAB1gHQweFB0MHQQeNBm4FDQVlBGMEXAQ4BMgDaANL
A2EDjAOVA8UDYgMIA78CnAILAmsBswEqAQ8B3wBJAHEAMQBQAKn/mv8VAPP///+5AMMApACCABEAFgAPAA0AcwBRAFMAewCPAJ0A
5wDyAKEAOgAdAFAAtQAtAUkBxQEpAtYCPQP0AxEEGQQnBBwEeASvBKEEmARiBLQDowO9Ay4EFwQbBNcDdgPHAgACbAH5ACYADP9p
/hf+Cv5z/VP+KP4T/X3xCc8P0HS3ybRB2Q/m5uLo2pnQLM5vyxrCmsbGu/W4NMMO4+r+ThJQHdIdARqkEfwI/QWhBloEEAYXB0ML
TBI0HCknpC3fMus3YT0zQjBFvkRqQc86CjUULyYmlhnXDv3/l/Ff63fm5uMA6MDp8+kj7qTxdvpYAc4GYA2AE7AbFSSFKqYv6Swb
JhkjxxyCEuQPag3wB08I7gqoDEoRphYTFyEavxoEF8oVshNFDJgCLfnR6RPdBNUIz9zLUcrxxwDH48arxafI8s+O1U/ZjN454+Dl
iO10+m0IEhaIJFIwJjoGQJhD8kXMRidFuz/wObg0qi/PKzAqHCXPHrwZVxN8DCkH+AEe+nP06vAO7FDjddm30K/H/8BlvLq5Jriv
tsW117RRs9uyeLX+uSzA08s/29Dr+fx3ER0n9DfHQ9lMiVCET3lMAkkgQ6g7kTOaKvAgVBdiExcU8RVtGAkd/R0eGbcQbAfO+ovt
lOFb1qTJnb7Ntzqwy6sbqCGmL6WOpjSpSa5mteq8y8Wn0ITa++cW+SsL3x4wL5A6eUCsQa09fzqnNMgtZyoPJ1kj1yKEIdghhiYr
KnUr7y0OKxkiqxoYD9H/6fAC467SjcZ8vZS2kbNgsY+wRrGFs/60d7oIwYfGjsxZ1FzZY+CW6evz6QG+DhgbxCVNLcYxvDaLOCo6
NDxcOwc61DhON+szUzS/MiEx7S5OKmcjkRsUFOEIhAII+ADsCuHX16/N8sfkxVnC4MDUv+y+pL/zwZzDFMcDynXMr9DG2Jbg1evB
91wFShR3INgrkDWGOgA6dDtaOnI4iDcgNgIyyy3VJ7Mi5yDQHpMcCBx8Ga4SmQ7/CWYDt/zQ9/3vBepL5fPh694028nW99MB0vbQ
bNNV11XcDOA85fLqCvEd9oL7VQL8CCQSIxzmJN8qli4hLl8sCCqkJqUl0yRBItIelBsfFh4Sgw/zChwHSQOI/iz71vkS+OL2EPby
9Rv2Zvaa9iX3rvad9aD0evPh8+jzTfUP9rL2BPf793z62/5yBBML+xAfFKgUlRQLExoR1RCKD7UPog9oD0IPnA4ODHMJCwc6A9IA
G/9h+3T2u/Kk8Brxi/GO8nT0WvaW9kD3Y/dP9sD0gfKo8DnwmPET8gvw1uw/6+3qNezn7Q/vwe8f8Lnw1PH+8jr0nPVY9lP2OfYx
9pX2gPdx+IP5oPpv+1v8iP2B/if/nP+S/wsAGQE7AmcD8ANOBDgFSAZtB6gIdAniCeIJmQmhCU4K+ApsC6oLtwstDAgNxQ2EDgYP
Bw8FD+YO5w4fDzkPIQ8qDyYPAw/4DhwPBg/KDmkO+Q22Df8MTAz3C0kLpwoXCuoJ6gl/Cb0ICghOB4oGIwYJBuwFegXdBGwE+gNm
Aw0D1QKiAnkCQALvAewB0wFuAfsAcACx/zT/rf6Q/p3+aP47/kb+Tf5e/pb+q/6//qX+1/6T/2YA5gBFAa4BqQF5AZwB0wHhAewB
kwFNATEBDQEpAScB8AC4ABMBdQGAAZIBiwFeATEBRgHJASMC8wH0AR0C/wEwAg4C3gHcAa8BwAFfASwBDwG6AJUASQD//+P/OwAL
AfcBMQJBAi8CwwFgASIB/QDwAJcASAAnAIz/VP8e/hH+tfz0+8buKtN+16/OSbpPxRLPBsUkq7+vEcAPyKjPb9Fg6RD7LwVgCNkF
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
8PQU9tv2mPXp84/tbOh05rXmjumg6zfumu6I7+jvavA98KrugO/379DwCPLf82r1ivZ499f3ufij+cv6Y/yv/X7+tP88AY4CLQSr
BWMGvwYkB48HTghlCVMKwAr6CiQLpQt9DHANag5VD9oPZBAbEYUR4xHTEb0R3hEKEhMSWhKnEvESPhMeE+ASyBK1EqQSchIoEqMR
WRE8EeAQfxAsEMoPbQ8kD+gOow5GDhoO8A3QDacNjw2PDXYNIA2ODDEM+AuJCzML2gpYCs0JnglwCWAJfAlJCfEIxgjHCIIIJQiw
Bz0H8waDBgkGhAUbBdAEWgTzA6kDgANeA0QDyAIxAqEBdgGMAYABdAFNAVMBWQF7AY8BuwHsAQACCwJJAoECgQJIAvcByAG7AW8B
WgF9AY8BiAF1AaIBzAHyAfwB+QEmAl4ChgJxAiMCpwFUATUBPQH7AMYAzgDHAG8Ayv9g/xD/yf5j/gz+8v0E/ib+YP7D/i3/If/+
/hH/9P7D/mX+H/4i/hz+Av6Y/XP9Ef3C/F78OPxF/CP8D/wt/ID80vw9/Z793f0F/uP9wv27/Zb9hf0d/X38bvwF/Hv7Z/t2+z77
1/qz+gb7UfsW+zD7n/vT+yn8Vvza/CP9//yi/Ib8l/zt+yz7Ifsn+/r6Xfu1+/P7UfzU/BX9bP34/RH+av5U/g/+E/6x/Sv+o/0w
/3n9P/tf2wPAjdvi3RHmn+fI7aXtbeN6wvS+TcpmtJKwZb4h2BzvrwMeBXYELQXc+uD1WfM78FnvHfJ88LT01v2FB+MR7xkbHzUj
9yk4L6QyHTRoNh41fDISMBcuLi2IKOciGh82HVEcmSBHILsg8SNGJBMktiUaJYIk5yPTGq8XORdqEhEN4AihBPX/cP5B+0/84f1Y
/l0ABAPxB+QIEw2BD0wThBTpFfcUyhPaEwoTnRD6DO8LrQnbByEHDQnXCg8MQQ3zD1cR6xGVEL8NOQjjASL7yfYz88bvJ++f8Kf0
Ovi++4r+rv7o/ML64vbe88zyFvF+7tXuUvEB9Er07vSm94j5Jfoj+tD7iv3W/qX/7P5n/Dj8mvu5+hb5ufYv9nr2MPdr9hv2Z/ax
9DD0ovIb8ULyLPLJ8lPx5fCM8Hjwiepf4u/eBd3v3lrhQOMg40niTOFq4j/jKt/63r7dsdnD11jcqt8n4Cngut9W4L3eRt443Wbb
OdvT22jdkt4q4YnjCeWw5QnmiefY6OPq6exu7oPvwvA78hD0efYM+MH4K/kF+nz7wv2A/wwB/wF3A40F5QZGCJ0JbQqvCgULsQqD
C9kMSA1tDaUNFw7JDrcPxg/FD5wPMw8QDywPKQ8IDyIPqg5bDuQNoQ23DUcOqg4JD0gPBw+wDkEOVw26DIsMOgwyDGEMsAwvDWkN
RQ0HDV4MswuoC5ULfgtlC2QLrQvDC1gLrgriCfcIcgjcB1oHZAe/B9sHugeLB4cHoQdjB5gHwQdjB2kHwwdKCL4I3gh/CBUIlAcN
By8HFQfDBoIGVwbABlIHvQcoCJkI0gg1CYQJ0Qn4CasJLgnLCFEI3AcxB24GmQUFBc0EfwSLBK4EvgSFBJcE5gT3BP8EtgR5BE8E
CAS7A4ADVwP2An4C/AGcAWIBOAEHAegAuwC9ABABSAGUAbYBtQG2AZ0BYAEKAcsAQQDU/2f/5f68/o/+O/75/fT92/0K/kr+df6S
/nj+bf52/lj+Mf7p/Z79hP2O/Zv9sP3J/dP9yP3Y/Rz+RP6T/uj+C/9a/wYAIgBoAH8AVAA6AC8A0f+//7f/ff/X////DwCdAPMA
6QAbAf8AvgCxAFAANQCRAKIAmQBaANv/2//A/zz/+f4G/63+4P4I/x3/Cv/O/jf/GP9Z/4f//P+t/33/Fv+e/mT+6P31/Zf9Nv0t
/R39iP2w/S3+lP1t/gb9KgC6+9L9Ednrzcv5I+yNALPzV/249R74AfUz8j73nPFh9mz1kPLX8tX2EvX49Qn5DPcz+Nf5qPqe+iP8
JfzG/En+JQH3ASEANP4x/Qb/qQBhAWUBUQNzAwIE1ASwA5oHogkBB5kItwbaCEMMkQ2tDTcM7w1XDIr4DP7bBPcOMRM8GPcczBpG
HBoZfhybG5YcohvpGqwXYxWfFVwThRePHfgf5yB+HAIY8RbBFbkSQBAzEWoSZQ5sCSUE2QHLBBsG5gsRD0EM3AlFAsMERQNg+Ubx
TeuS55nncenq6dTmEOXc46/ghuDE4VzjQuX/5NLkguQV5arlvObn5UPlfOU95tTmJuhu6MzoYeil6RzrFe3j7pvvL/Bh8Q/yn/PW
9Ez1A/YW9sX2Tfe0+BH6qvkg+hz63frK+5f80v3c/WP/6v8bAYwBIQKpARUBIAG2AM8AdAEcAp0CoAIuAtQBxAJyAoICWwISAswB
sQEXARkBDAFwAHAACgA/AHAAs//H/77/VgBPABYArP99/7j/dP/9/sj+3f42/2j/tP/Z/w4AAABOAJMAlACsAE4BqwFhAnECwQLO
AowCkwJ+AooCoQJPA8IDMAR3BKsElATBBEMFbwXUBaYFewWABYEFsAU1Bv8F3AUPBvwFKgZOBlQGAQY1BrsGIAdjBxoHxwZpBj0G
RQYxBtUFdgVXBQYF7gTOBKAEmQRkBCYEzgOPA4YDZgMTA7YCQALWAaAB1QHZAb8BRQECAdEAogCPAIcAZQAOABYA3P/j/7v/eP+N
/2j/ff94/5v/kf9h//L+sf6c/nr+hv7M/iT/Wv8U/zT/e/+K/3r/Nv8m/yD/Nv88/0//YP+J/9H/+P/0/97/6v/4////MQByAIUA
pQDNAMsA8gAFAeMAxgDJAPQA4gDkAMgAuwDKANMA/ADnAAYB8QAcAUMBLQEaAQIB/ADQAL8AmgCYAIAAXgBNAE0AQQAOAPb/FQAa
AO7/5//4/9v/2P+l/57/pf+E/3b/Sv8q/x//BP8I/+P+0P7O/t7+3v7h/gb/QP9t/4D/if99/3X/YP9K/zr/Ov8d/w3/AP8E///+
3f7q/vX+Ev8y/1H/hf+r/6//tf/F/8P/w/+7/7L/pv+c/37/c/+H/5H/nv+//97/+f8eADwAQABUAFoAVQBpAEwAQQA9AEQAOAA0
AC8ANQBDADUALwAZAAYA8v/o/+X/9P/4/+7/8P/y//n//P/z/+3/6v/d/7v/qP+0/7T/r/+r/6P/rP+v/6X/nv+R/4z/hv92/3f/
g/+I/5j/pP+m/6P/pv+S/3r/cf9k/17/bv9t/33/i/+M/5n/nP+k/6D/lf+I/4L/fP+H/5P/qP/H/9L/zf/M/8v/xf/J/8z/yf/X
/9v/3v/s/wAADQAVAAsAEgAMAA8ACQD8//v/9f///wAABQAKABMADwAHAAMABwAAAAQAAAD6//b/9f/5//n/AQAAAP7//f/4//D/
/v/2/+T/3f/T/9z/6P/y/+7/9//3/+z/7f/q/+v/5v/f/97/5P/t//T/8f/v/+3/3f/k/+f/3v/n/+f/5v/e/93/4v/t//H/9v8A
APr/+v/7/////f/1//j/+f/3//j/+P8GABQAFgAeAB8AIAAVABIAEwAPAAsACQARABcADQAJAAYABAAMAAkAEQAOAAwADQAVABMA
DwALABAAEwAKAAUADQAQAAkADgASABEAFQASABUAFwATAA8ADwASABAAFAAUAA8AEgASABIAEQAPAA4ADQAOAA4ADQAKAAkABAAD
AAcACAAGAAYACAAJAAsACQAHAAcABAAEAAYABwAFAAcABwAIAAgABwAHAAcACAAIAAgACQAKAAwADQAMAAwADAANAAwADQAMAAwA
DAAKAAsACgALAAgA
""",
}

if __name__ == "__main__":
    main()
