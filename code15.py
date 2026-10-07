"""
JARVIS - code15.py
Terminal-first Agentic OS — daily use, interruptible, safe.

Playback: uses mpv.exe (GUI binary) so windows actually appear.
Failures are logged to mpv_playback.log and reported to the user.
"""

import ast
import json
import operator
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import webbrowser
from datetime import datetime, timedelta

import ollama
import psutil


# ============================================================
# CONFIG
# ============================================================

MODEL = "qwen3.5:4b"
WORKDIR = r"E:\Agentic_OS"
DB_PATH = os.path.join(WORKDIR, "jarvis_memory.db")
CACHE_PATH = os.path.join(WORKDIR, "jarvis_cache.json")
MPV_LOG = os.path.join(WORKDIR, "mpv_playback.log")
CACHE_TTL_HOURS = 24
MAX_TOOL_CYCLES = 6
COMMAND_TIMEOUT = 30
EMAIL_TIMEOUT = 30
FAST_PATH = True

# Voice
VOICE_SAMPLE_RATE = 16000
VOICE_FRAME_MS = 30
VOICE_SILENCE_SEC = 0.9
VOICE_MAX_SEC = 20
VOICE_INITIAL_TIMEOUT = 5
VOICE_MIN_SPEECH_FRAMES = 8
VOICE_BOOST = 1.8
VOICE_VAD_LEVEL = 2
VOICE_LANGUAGE = "en"
WHISPER_MODEL = "base.en"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE = "int8"
WHISPER_INITIAL_PROMPT = (
    "JARVIS commands: play music, play video, open chrome, check email, "
    "tell me about, search for, set timer, take screenshot, weather, "
    "battery, volume up, volume down, mute, notes, remind me, calculate."
)
MIC_DEVICE = None
SHOW_MIC_LIST = False

# TTS
SPEAK_RESPONSES = False
TTS_RATE = 185

# Display
TYPE_EFFECT = False
TYPE_DELAY = 0.012


# ============================================================
# INTERRUPT
# ============================================================

CANCEL = threading.Event()
_SHUTDOWN = threading.Event()
_ESC_WATCHING = False


def _start_esc_watch():
    global _ESC_WATCHING
    if _ESC_WATCHING or os.name != "nt":
        return
    _ESC_WATCHING = True

    def _loop():
        try:
            import msvcrt
        except ImportError:
            return
        while not _SHUTDOWN.is_set():
            try:
                if msvcrt.kbhit():
                    ch = msvcrt.getch()
                    if ch == b"\x1b":
                        CANCEL.set()
                        print("\n[INTERRUPT] ESC — cancelling...", flush=True)
            except Exception:
                pass
            time.sleep(0.04)

    threading.Thread(target=_loop, daemon=True).start()


def _reset_cancel():
    CANCEL.clear()


def _is_cancelled():
    return CANCEL.is_set()


def _cancel_check(msg="Cancelled."):
    if CANCEL.is_set():
        return {"success": False, "status": "cancelled", "message": msg}
    return None


# ------------------------------------------------------------
# TASK REGISTRY
# ------------------------------------------------------------

TASKS = {}
_TASK_ID = [0]
_TASK_LOCK = threading.Lock()


def _register_task(kind, label, handle=None):
    with _TASK_LOCK:
        _TASK_ID[0] += 1
        tid = _TASK_ID[0]
        TASKS[tid] = {"id": tid, "kind": kind, "label": label,
                      "handle": handle, "started": datetime.now().isoformat()}
    return tid


def _unregister_task(tid):
    with _TASK_LOCK:
        TASKS.pop(tid, None)


def _prune_tasks():
    dead = []
    with _TASK_LOCK:
        for tid, info in TASKS.items():
            h = info.get("handle")
            if h is None:
                continue
            if hasattr(h, "poll"):
                if h.poll() is not None:
                    dead.append(tid)
            elif hasattr(h, "is_alive"):
                if not h.is_alive():
                    dead.append(tid)
        for tid in dead:
            TASKS.pop(tid, None)


def list_tasks():
    _prune_tasks()
    with _TASK_LOCK:
        snapshot = [dict(info) for info in TASKS.values()]
    print("\n" + "=" * 60)
    if not snapshot:
        print("TASKS: (none running)")
    else:
        print(f"TASKS ({len(snapshot)})")
        print("=" * 60)
        for t in snapshot:
            h = t["handle"]
            desc = ""
            if hasattr(h, "pid"):
                desc = f"pid={h.pid}"
            elif isinstance(h, threading.Thread):
                desc = "thread"
            ts = t["started"][11:19]
            print(f"  [{t['id']:>3}] {t['kind']:<10} {t['label'][:40]:<42} {desc}  ({ts})")
    print("=" * 60 + "\n")
    return {"success": True, "count": len(snapshot), "tasks": snapshot}


def kill_task(tid):
    _prune_tasks()
    try:
        tid = int(tid)
    except (TypeError, ValueError):
        return {"success": False, "message": "task id must be integer"}
    with _TASK_LOCK:
        info = TASKS.get(tid)
    if not info:
        return {"success": False, "message": f"No task {tid}."}
    h = info.get("handle")
    try:
        if h is None:
            pass
        elif hasattr(h, "terminate"):
            h.terminate()
            try:
                h.wait(timeout=2)
            except Exception:
                h.kill()
        elif hasattr(h, "cancel"):
            h.cancel()
    except Exception as e:
        return {"success": False, "message": f"kill failed: {e}"}
    _unregister_task(tid)
    return {"success": True, "message": f"Killed task {tid} ({info['kind']}: {info['label']})."}


def kill_all_tasks():
    _prune_tasks()
    with _TASK_LOCK:
        ids = list(TASKS.keys())
    killed = []
    for tid in ids:
        r = kill_task(tid)
        if r.get("success"):
            killed.append(tid)
    return {"success": True, "killed": killed,
            "message": f"Killed {len(killed)} task(s)."}


# ============================================================
# SAFETY
# ============================================================

BLOCKED_PATTERNS = [
    r"\bformat\b", r"\bdiskpart\b", r"\bclean\s+all\b",
    r"\brm\s+-rf\b", r"\brm\s+-r\b", r"\brm\s+-f\b",
    r"\bdel\s+/[fsq]\b", r"\berase\b", r"\bdd\s+if=",
    r"\bmkfs\b", r"\bfdisk\b", r"\bparted\b",
    r"\bshutdown\b", r"\brestart-computer\b", r"\bstop-computer\b",
    r"\bhalt\b", r"\bpoweroff\b", r"\bsleep\b",
    r"\bremove-item\b.*-(recurse|force)\b",
    r"\bdel\b.*[a-z]:\\\\?", r"\brmdir\b.*[a-z]:\\\\?",
    r"\brd\b.*[a-z]:\\\\?", r"\brm\b.*[a-z]:\\\\?",
    r"\bnet\s+user\b", r"\bnet\s+localgroup\b",
    r"\bnew-localuser\b", r"\badd-localuser\b", r"\bremove-localuser\b",
    r"\breg\s+(add|delete|import)\b", r"\bregedit\b",
    r"\bset-itemproperty\b.*hklm:", r"\bremove-itemproperty\b",
    r"\bicacls\b", r"\btakeown\b", r"\bcacls\b",
    r"\bset-executionpolicy\b.*(unrestricted|bypass)",
    r"\bdisable-.*(defender|firewall|uac)\b",
    r"\bstop-service\b.*(defender|firewall|winmgmt|eventlog)\b",
    r"\bbcdedit\b", r"\bbootrec\b", r"\breagentc\b",
    r"\bwbadmin\b", r"\bmanage-bde\b", r"\bcipher\b.*\/w\b",
    r"\btaskkill\b.*\/f\b.*\/im\b.*\*", r"\bstop-process\b.*-force\b",
    r"\bsc\s+(delete|stop|config)\b",
    r"\bcurl\b.*\|\s*(sh|bash|powershell|cmd)\b",
    r"\biwr\b.*\|\s*iex\b", r"\binvoke-expression\b.*downloadstring\b",
    r"\bpowershell\b.*-enc", r"\bpowershell\b.*-encodedcommand\b",
    r"\bfrombase64string\b",
]

CONFIRM_PATTERNS = [
    r"\bwinget\s+(install|uninstall|upgrade)\b",
    r"\bchoco\s+(install|uninstall|upgrade)\b",
    r"\bpip\s+install\b",
    r"\bnpm\s+(install|uninstall)\b",
    r"\bstart-process\b.*-verb\s+runas\b",
    r"\bset-executionpolicy\b",
    r"\bset-itemproperty\b",
    r"\bmove-item\b", r"\brename-item\b",
    r"\bcopy-item\b.*-recurse\b",
]

DESTRUCTIVE_VERBS = [
    r"\bdel\b", r"\berase\b", r"\brmdir\b", r"\brd\b", r"\brm\b",
    r"\bremove-item\b", r"\bunlink\b", r"\btruncate\b",
    r"\bformat\b", r"\bwipe\b", r"\bcipher\b", r"\bshred\b",
]

PROTECTED_PATH_HINTS = [
    r"c:\\windows\b", r"c:\\program files\b", r"c:\\program files \(x86\)\b",
    r"c:\\users\\.*\\appdata\b", r"\\system32\b", r"\\syswow64\b",
    r"\\programdata\b",
]


def _has_protected_path(cmd):
    low = cmd.lower()
    for pat in PROTECTED_PATH_HINTS:
        if re.search(pat, low):
            return True
    return False


def _has_destructive_verb(cmd):
    low = cmd.lower()
    for pat in DESTRUCTIVE_VERBS:
        if re.search(pat, low):
            return True
    return False


def security_check(cmd):
    low = cmd.lower()
    for pat in BLOCKED_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return "BLOCKED"
    if _has_destructive_verb(cmd) and _has_protected_path(cmd):
        return "BLOCKED"
    if _has_destructive_verb(cmd):
        drive_paths = re.findall(r"[a-z]:\\\\?[^\"'\s|<>]*", low)
        for p in drive_paths:
            if not p.startswith(WORKDIR.lower()):
                return "BLOCKED"
        if re.search(r"\s/(?:$|\s)", low):
            return "BLOCKED"
    for pat in CONFIRM_PATTERNS:
        if re.search(pat, low, re.IGNORECASE):
            return "CONFIRM"
    return "ALLOW"


# ============================================================
# MEMORY
# ============================================================

class Memory:
    def __init__(self, db_path):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        c = sqlite3.connect(self.db_path)
        c.row_factory = sqlite3.Row
        return c

    def _init_db(self):
        with self._conn() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                key TEXT UNIQUE, value TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                created_at TEXT, updated_at TEXT)""")
            c.execute("""CREATE TABLE IF NOT EXISTS interactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_input TEXT, jarvis_response TEXT, timestamp TEXT)""")

    def remember(self, key, value, category="general"):
        now = datetime.now().isoformat()
        with self._conn() as c:
            c.execute("""INSERT INTO memories (key,value,category,created_at,updated_at)
                VALUES (?,?,?,?,?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value,
                category=excluded.category, updated_at=excluded.updated_at""",
                (key, value, category, now, now))
        return {"success": True, "message": f"Remembered: {key} = {value}"}

    def recall(self, key=None, query=None):
        with self._conn() as c:
            if key:
                r = c.execute("SELECT key,value,category,updated_at FROM memories WHERE key=?",
                              (key,)).fetchone()
                return {"success": True, "memory": dict(r)} if r else \
                       {"success": False, "message": f"No memory for '{key}'."}
            if query:
                rows = c.execute("""SELECT key,value,category,updated_at FROM memories
                    WHERE key LIKE ? OR value LIKE ?
                    ORDER BY updated_at DESC LIMIT 20""",
                    (f"%{query}%", f"%{query}%")).fetchall()
                return {"success": True, "memories": [dict(r) for r in rows]}
            rows = c.execute("""SELECT key,value,category,updated_at FROM memories
                ORDER BY updated_at DESC LIMIT 50""").fetchall()
            return {"success": True, "memories": [dict(r) for r in rows]}

    def forget(self, key):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE key=?", (key,))
        return {"success": True, "message": f"Forgot: {key}"}

    def log(self, u, r):
        with self._conn() as c:
            c.execute("INSERT INTO interactions (user_input,jarvis_response,timestamp) VALUES (?,?,?)",
                      (u, r, datetime.now().isoformat()))

    def recent(self, n=2):
        with self._conn() as c:
            rows = c.execute("SELECT user_input,jarvis_response FROM interactions ORDER BY id DESC LIMIT ?",
                             (n,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def snapshot(self, limit=8):
        with self._conn() as c:
            rows = c.execute("SELECT key,value,category FROM memories ORDER BY updated_at DESC LIMIT ?",
                             (limit,)).fetchall()
            return [dict(r) for r in rows]

    def add_note(self, text):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        return self.remember(f"note_{ts}", text, category="note")

    def show_notes(self, limit=30):
        with self._conn() as c:
            rows = c.execute("""SELECT key,value,created_at FROM memories
                WHERE category='note' ORDER BY created_at DESC LIMIT ?""",
                (limit,)).fetchall()
        return [dict(r) for r in rows]

    def clear_notes(self):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE category='note'")
        return {"success": True, "message": "All notes cleared."}


MEM = Memory(DB_PATH)


# ============================================================
# DISCOVERY
# ============================================================

def _scan_start_menu():
    apps = {}
    for base in [r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
                 os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs")]:
        if not os.path.isdir(base):
            continue
        for root, _d, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".lnk"):
                    apps.setdefault(f[:-4].strip().lower(), os.path.join(root, f))
    return apps


def _scan_installed_programs():
    ps = r"""
$paths=@('HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*')
Get-ItemProperty $paths -ErrorAction SilentlyContinue |
  Where-Object { $_.DisplayName } |
  Select-Object DisplayName, DisplayVersion, InstallLocation, Publisher |
  ConvertTo-Json -Compress
"""
    try:
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
                           capture_output=True, text=True, timeout=30)
        if r.returncode != 0 or not r.stdout.strip():
            return []
        data = json.loads(r.stdout)
        if isinstance(data, dict):
            data = [data]
        return [{"name": d.get("DisplayName"), "version": d.get("DisplayVersion"),
                 "publisher": d.get("Publisher"), "location": d.get("InstallLocation")}
                for d in data if d.get("DisplayName")]
    except Exception:
        return []


def _find_exe(exe_name, extra_dirs=None):
    p = shutil.which(exe_name)
    if p and os.path.isfile(p):
        return p
    for d in (extra_dirs or []):
        cand = os.path.join(d, exe_name)
        if os.path.isfile(cand):
            return cand
    for root_base in [os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"),
                      os.path.expandvars(r"%LOCALAPPDATA%\Programs")]:
        if os.path.isdir(root_base):
            for root, _dirs, files in os.walk(root_base):
                if exe_name in files:
                    return os.path.join(root, exe_name)
    return None


def _scan_media_players():
    """
    Return dict with 'mpv_exe' and 'mpv_com' keys where available,
    plus 'vlc' and 'ffplay'.
    """
    found = {}
    mpv_exe = _find_exe("mpv.exe", [r"C:\Program Files\MPV Player"])
    mpv_com = _find_exe("mpv.com", [r"C:\Program Files\MPV Player"])
    if mpv_exe:
        found["mpv_exe"] = mpv_exe
        # Prefer mpv.com only for silent/background usage; keep both
        if mpv_com:
            found["mpv_com"] = mpv_com
        found["mpv"] = mpv_exe  # default = GUI binary (window opens)
    vlc = _find_exe("vlc.exe", [r"C:\Program Files\VideoLAN\VLC"])
    if vlc:
        found["vlc"] = vlc
    ffplay = _find_exe("ffplay.exe")
    if ffplay:
        found["ffplay"] = ffplay
    return found


def _scan_terminal_tools():
    tools = {}
    for name in ["lynx", "w3m", "chafa", "timg", "himalaya", "ddgr", "yt-dlp"]:
        p = shutil.which(name)
        if p and os.path.isfile(p):
            tools[name] = p
    return tools


def _scan_deno():
    p = shutil.which("deno")
    if p and os.path.isfile(p):
        return p
    root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(root):
        for d in os.listdir(root):
            if d.lower().startswith("denoland.deno"):
                for sub in os.listdir(os.path.join(root, d)):
                    cand = os.path.join(root, d, sub, "deno.exe")
                    if os.path.isfile(cand):
                        return cand
    return None


def load_cache():
    if not os.path.isfile(CACHE_PATH):
        return None
    try:
        with open(CACHE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        ts = datetime.fromisoformat(data.get("timestamp", "1970-01-01"))
        if (datetime.now() - ts).total_seconds() / 3600 > CACHE_TTL_HOURS:
            return None
        return data
    except Exception:
        return None


def save_cache(apps, progs, players, tools, deno):
    try:
        with open(CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump({"timestamp": datetime.now().isoformat(),
                       "discovered_apps": apps, "installed_programs": progs,
                       "media_players": players, "terminal_tools": tools,
                       "deno": deno}, f, indent=2)
    except Exception:
        pass


def do_full_scan():
    print("[JARVIS] Full scan...")
    apps = _scan_start_menu();  print(f"[JARVIS]   Start Menu:      {len(apps)}")
    progs = _scan_installed_programs(); print(f"[JARVIS]   Installed progs: {len(progs)}")
    players = _scan_media_players(); print(f"[JARVIS]   Media players:   {list(players) or 'NONE'}")
    tools = _scan_terminal_tools(); print(f"[JARVIS]   Terminal tools:  {list(tools) or 'NONE'}")
    deno = _scan_deno(); print(f"[JARVIS]   Deno:            {deno or 'MISSING'}")
    save_cache(apps, progs, players, tools, deno)
    return apps, progs, players, tools, deno


_cached = load_cache()
if _cached:
    DISCOVERED_APPS = _cached.get("discovered_apps", {})
    INSTALLED_PROGRAMS = _cached.get("installed_programs", [])
    MEDIA_PLAYERS = _cached.get("media_players", {})
    TERMINAL_TOOLS = _cached.get("terminal_tools", {})
    DENO_PATH = _cached.get("deno")
    _age = (datetime.now() - datetime.fromisoformat(_cached["timestamp"])).total_seconds() / 60
    print(f"[JARVIS] Loaded cache ({_age:.1f} min old). 'refresh' to rescan.")
else:
    DISCOVERED_APPS, INSTALLED_PROGRAMS, MEDIA_PLAYERS, TERMINAL_TOOLS, DENO_PATH = do_full_scan()


# ============================================================
# VOICE
# ============================================================

def _check_voice_deps():
    missing = []
    for pkg in ["faster_whisper", "sounddevice", "webrtcvad", "numpy"]:
        try:
            __import__(pkg)
        except ImportError:
            missing.append(pkg)
    return missing


VOICE_MISSING = _check_voice_deps()
VOICE_AVAILABLE = not VOICE_MISSING
VOICE_IN = None
VOICE_OUT = None


def _type_out(text, delay=None):
    d = TYPE_DELAY if delay is None else delay
    if not TYPE_EFFECT:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch); sys.stdout.flush()
        if ch in ".!?\n":
            time.sleep(d * 8)
        elif ch == ",":
            time.sleep(d * 4)
        else:
            time.sleep(d)
    print()


def list_microphones():
    import sounddevice as sd
    devs = sd.query_devices()
    print("\nAvailable input devices:")
    print("-" * 60)
    inputs = []
    for i, d in enumerate(devs):
        if d.get("max_input_channels", 0) > 0:
            inputs.append(i)
            default = ""
            try:
                if i == sd.default.device[0]:
                    default = "  [DEFAULT]"
            except Exception:
                pass
            print(f"  [{i}] {d['name']}{default}")
    print("-" * 60 + "\n")
    return inputs


class VoiceInput:
    def __init__(self, device=None):
        self.device = device
        self._model = None
        self._vad = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return
        import webrtcvad
        from faster_whisper import WhisperModel
        print(f"[VOICE] Loading Whisper '{WHISPER_MODEL}'...")
        self._model = WhisperModel(WHISPER_MODEL, device=WHISPER_DEVICE,
                                   compute_type=WHISPER_COMPUTE)
        self._vad = webrtcvad.Vad(VOICE_VAD_LEVEL)
        print("[VOICE] Ready.")

    def record(self):
        import numpy as np, sounddevice as sd
        self._load()
        chunk = int(VOICE_SAMPLE_RATE * VOICE_FRAME_MS / 1000)
        max_chunks = int(VOICE_MAX_SEC * 1000 / VOICE_FRAME_MS)
        silence_need = int(VOICE_SILENCE_SEC * 1000 / VOICE_FRAME_MS)
        initial = int(VOICE_INITIAL_TIMEOUT * 1000 / VOICE_FRAME_MS)
        frames, sil, started = [], 0, False
        dev = self.device if self.device is not None else MIC_DEVICE
        print(f"[VOICE] Listening{' on ['+str(dev)+']' if dev is not None else ''}... (speak, ESC to cancel)")
        try:
            with sd.InputStream(samplerate=VOICE_SAMPLE_RATE, channels=1,
                                dtype="int16", blocksize=chunk, device=dev) as stream:
                for i in range(max_chunks):
                    if CANCEL.is_set():
                        print("[VOICE] Cancelled.")
                        return None
                    data, _ = stream.read(chunk)
                    try:
                        speech = self._vad.is_speech(data.tobytes(), VOICE_SAMPLE_RATE)
                    except Exception:
                        speech = False
                    if speech:
                        if not started:
                            print("[VOICE] Recording...")
                            started = True
                        sil = 0
                        frames.append(data.copy())
                    elif started:
                        sil += 1
                        frames.append(data.copy())
                        if sil >= silence_need:
                            break
                    elif i > initial:
                        print("[VOICE] No speech detected.")
                        return None
        except Exception as e:
            print(f"[VOICE] Mic error: {e}")
            return None
        if not started or len(frames) < VOICE_MIN_SPEECH_FRAMES:
            print("[VOICE] Too short / no speech.")
            return None
        return np.concatenate(frames, axis=0).flatten()

    def transcribe(self, audio, live=True):
        if audio is None:
            return ""
        import numpy as np
        self._load()
        with self._lock:
            audio_f = audio.astype(np.float32) / 32768.0
            audio_f = np.clip(audio_f * VOICE_BOOST, -1.0, 1.0)
            segs, _ = self._model.transcribe(
                audio_f, language=VOICE_LANGUAGE, beam_size=5,
                vad_filter=False, initial_prompt=WHISPER_INITIAL_PROMPT,
                condition_on_previous_text=False,
            )
            pieces = []
            if live:
                print("\n" + "=" * 60)
                print("  YOU SAID:")
                print("=" * 60, flush=True)
            for seg in segs:
                p = seg.text.strip()
                if not p:
                    continue
                pieces.append(p)
                if live:
                    sys.stdout.write(p + " "); sys.stdout.flush()
            if live:
                sys.stdout.write("\n")
                print("=" * 60 + "\n", flush=True)
        return " ".join(pieces).strip()


class VoiceOutput:
    def __init__(self):
        self._engine = None
        self._lock = threading.Lock()

    def _load(self):
        if self._engine is not None:
            return
        import pyttsx3
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", TTS_RATE)

    def speak(self, text, block=False):
        if not text or len(text.strip()) < 2:
            return
        try:
            self._load()
        except Exception:
            return

        def _run():
            with self._lock:
                try:
                    self._engine.say(text)
                    self._engine.runAndWait()
                except Exception:
                    pass

        if block:
            _run()
        else:
            threading.Thread(target=_run, daemon=True).start()


def _ensure_voice():
    global VOICE_IN, VOICE_OUT
    if not VOICE_AVAILABLE:
        return False
    if VOICE_IN is None:
        VOICE_IN = VoiceInput()
    if VOICE_OUT is None:
        VOICE_OUT = VoiceOutput()
    return True


def _capture_voice():
    if not _ensure_voice():
        print("JARVIS > Voice unavailable. Missing: " + ", ".join(VOICE_MISSING))
        print("        pip install faster-whisper sounddevice webrtcvad-wheels pyttsx3 numpy")
        return None
    _reset_cancel()
    _start_esc_watch()
    audio = VOICE_IN.record()
    if audio is None:
        return None
    print("[VOICE] Transcribing...\n")
    text = VOICE_IN.transcribe(audio, live=True)
    if not text:
        return None
    print(f"You (voice) > {text}")
    return text


# ============================================================
# PLAYBACK  (mpv.exe = GUI binary, opens window)
# ============================================================

class PlaybackManager:
    def __init__(self):
        self.proc = None
        self.query = None
        self.task_id = None

    def start(self, proc, query):
        self.stop(silent=True)
        self.proc = proc
        self.query = query
        self.task_id = _register_task("media", query, proc)

    def stop(self, silent=False):
        killed = []
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate(); killed.append(self.proc.pid)
            except Exception:
                pass
        prev = self.query
        self.proc = None
        self.query = None
        if self.task_id is not None:
            _unregister_task(self.task_id)
            self.task_id = None
        if silent:
            return {"success": True}
        return {"success": bool(killed),
                "message": f"Stopped '{prev}'." if killed else "Nothing playing.",
                "killed_pids": killed}

    def is_playing(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        if not self.is_playing():
            return {"playing": False}
        return {"playing": True, "query": self.query, "pid": self.proc.pid}


PLAYBACK = PlaybackManager()


def _mpv_launch(args, label):
    """
    Launch mpv with given args, wait briefly, verify it's alive.
    Returns (proc, error_string_or_None).
    """
    mpv = MEDIA_PLAYERS.get("mpv_exe") or MEDIA_PLAYERS.get("mpv")
    if not mpv:
        return None, "mpv.exe not found. Install: winget install shinchiro.mpv"

    # Clear previous log
    try:
        with open(MPV_LOG, "w", encoding="utf-8") as f:
            f.write("")
    except Exception:
        pass

    log_f = open(MPV_LOG, "w", encoding="utf-8")
    try:
        proc = subprocess.Popen(
            [mpv] + args,
            stdout=log_f,
            stderr=subprocess.STDOUT,
            cwd=WORKDIR,
            creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        )
    except Exception as e:
        try:
            log_f.close()
        except Exception:
            pass
        return None, f"failed to launch mpv: {e}"

    # Give mpv 1.5 seconds to either start playing or die
    time.sleep(1.5)
    if proc.poll() is not None:
        # It exited early — read the log
        try:
            log_f.close()
        except Exception:
            pass
        try:
            with open(MPV_LOG, "r", encoding="utf-8", errors="ignore") as f:
                err = f.read().strip()[-800:]
        except Exception:
            err = "(no log)"
        return None, f"mpv exited immediately. Log:\n{err}"

    return proc, None


def play_media(query: str):
    """Stream YouTube AUDIO, force an mpv window open."""
    args = [
        "--force-window=yes",
        "--keep-open=no",
        "--ytdl-format=bestaudio/best",
        "--cache=yes",
        "--cache-secs=30",
        f"ytdl://ytsearch1:{query}",
    ]
    proc, err = _mpv_launch(args, f"audio:{query}")
    if err:
        return {"success": False, "message": err}
    PLAYBACK.start(proc, query)
    return {"success": True, "message": f"Playing audio in window: {query}",
            "pid": proc.pid}


def play_video(query: str):
    """Stream YouTube VIDEO in an mpv window."""
    args = [
        "--force-window=yes",
        "--keep-open=no",
        "--ytdl-format=bestvideo[height<=?720]+bestaudio/best",
        "--cache=yes",
        "--cache-secs=30",
        f"ytdl://ytsearch1:{query}",
    ]
    proc, err = _mpv_launch(args, f"video:{query}")
    if err:
        return {"success": False, "message": err}
    PLAYBACK.start(proc, query)
    return {"success": True, "message": f"Playing video in window: {query}",
            "pid": proc.pid}


def stop_media():
    return PLAYBACK.stop(silent=False)


def media_status():
    return PLAYBACK.status()


def mpv_debug():
    """Diagnose mpv playback — run a 3-second local test tone."""
    mpv = MEDIA_PLAYERS.get("mpv_exe") or MEDIA_PLAYERS.get("mpv")
    if not mpv:
        return {"success": False, "message": "mpv.exe not found."}
    try:
        r = subprocess.run(
            [mpv, "--force-window=yes", "--length=3",
             "av://lavfi:sine=frequency=440:duration=3"],
            capture_output=True, text=True, timeout=15)
        print("\n--- mpv test result ---")
        print(f"return code: {r.returncode}")
        print(f"stderr:\n{r.stderr[-800:]}")
        print("-----------------------\n")
        return {"success": r.returncode == 0,
                "message": "mpv test tone finished (you should have heard a beep + seen a window)."}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============================================================
# TIMERS
# ============================================================

TIMERS = {}
_TIMER_ID = [0]
_TIMER_LOCK = threading.Lock()


def set_timer(seconds, label=""):
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return {"success": False, "message": "seconds must be integer"}
    if seconds <= 0:
        return {"success": False, "message": "seconds must be > 0"}

    with _TIMER_LOCK:
        _TIMER_ID[0] += 1
        tid = _TIMER_ID[0]

    task_ref = [None]

    def fire():
        print(f"\n\n*** TIMER {tid} FIRED: {label or f'{seconds}s'} ***\n")
        if VOICE_AVAILABLE and _ensure_voice():
            VOICE_OUT.speak(f"Timer: {label or 'time is up'}")
        if task_ref[0] is not None:
            _unregister_task(task_ref[0])
        with _TIMER_LOCK:
            TIMERS.pop(tid, None)

    t = threading.Timer(seconds, fire)
    t.daemon = True
    t.start()
    task_ref[0] = _register_task("timer", f"{seconds}s {label}".strip(), t)

    with _TIMER_LOCK:
        TIMERS[tid] = {"timer": t, "label": label, "seconds": seconds,
                       "started": datetime.now().isoformat()}
    return {"success": True, "id": tid, "seconds": seconds,
            "message": f"Timer {tid} set for {seconds}s ({label or 'no label'})."}


def list_timers():
    with _TIMER_LOCK:
        items = [{"id": k, "label": v["label"], "seconds": v["seconds"]}
                 for k, v in TIMERS.items() if v["timer"].is_alive()]
    return {"success": True, "count": len(items), "timers": items}


def cancel_timer(timer_id):
    try:
        tid = int(timer_id)
    except (TypeError, ValueError):
        return {"success": False, "message": "timer_id must be integer"}
    with _TIMER_LOCK:
        v = TIMERS.pop(tid, None)
    if not v:
        return {"success": False, "message": f"No timer {tid}."}
    v["timer"].cancel()
    return {"success": True, "message": f"Timer {tid} cancelled."}


# ============================================================
# CORE TOOLS
# ============================================================

def run_command(command: str):
    security = security_check(command)
    if security == "BLOCKED":
        return {"success": False, "status": "blocked",
                "message": "BLOCKED by safety policy — this could harm the system."}
    if security == "CONFIRM":
        print(f"\n!! JARVIS wants to run:\n    {command}")
        if input("Allow? [y/N]: ").strip().lower() != "y":
            return {"success": False, "status": "cancelled", "message": "User denied."}
    try:
        proc = subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=WORKDIR, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        t0 = time.time()
        while proc.poll() is None:
            if CANCEL.is_set():
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                return {"success": False, "status": "cancelled",
                        "message": "Cancelled by user (ESC)."}
            if time.time() - t0 > COMMAND_TIMEOUT:
                proc.terminate()
                return {"success": False, "status": "timeout",
                        "message": f"Exceeded {COMMAND_TIMEOUT}s."}
            time.sleep(0.05)
        out, err = proc.communicate()
        return {"success": proc.returncode == 0, "status": "completed",
                "return_code": proc.returncode,
                "output": (out or "").strip()[-5000:],
                "error": (err or "").strip()[-2000:]}
    except Exception as e:
        return {"success": False, "status": "error", "message": str(e)}


COMMON_ALIASES = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "explorer": "explorer.exe",
    "cmd": "cmd.exe", "powershell": "powershell.exe",
    "terminal": "wt.exe", "task manager": "taskmgr.exe",
    "chrome": "chrome", "edge": "msedge", "firefox": "firefox",
    "spotify": "spotify", "vscode": "code", "vs code": "code",
}


def _resolve_app(name: str):
    n = name.lower().strip()
    if n in COMMON_ALIASES:
        return COMMON_ALIASES[n]
    if n in MEDIA_PLAYERS:
        return MEDIA_PLAYERS[n]
    if n in DISCOVERED_APPS:
        return DISCOVERED_APPS[n]
    m = next((k for k in DISCOVERED_APPS if n in k), None)
    if m:
        return DISCOVERED_APPS[m]
    m2 = next((p for p in INSTALLED_PROGRAMS if n in (p["name"] or "").lower()), None)
    if m2 and m2.get("location"):
        loc = os.path.expandvars(m2["location"])
        if os.path.isdir(loc):
            for f in os.listdir(loc):
                if f.lower().endswith(".exe"):
                    return os.path.join(loc, f)
    return name


def open_app(app: str):
    target = _resolve_app(app)
    try:
        subprocess.Popen(["powershell.exe", "-NoProfile", "-Command",
                          f"Start-Process '{target}'"], cwd=WORKDIR)
        return {"success": True, "message": f"Opened {app}.", "resolved_to": target}
    except Exception as e:
        return {"success": False, "message": str(e)}


def close_app(app: str):
    proc = app.lower().strip()
    if not proc.endswith(".exe"):
        proc += ".exe"
    protected = {"system.exe", "winlogon.exe", "csrss.exe", "smss.exe",
                 "services.exe", "lsass.exe", "explorer.exe", "dwm.exe"}
    if proc in protected:
        return {"success": False, "message": f"{proc} is protected."}
    closed = []
    for p in psutil.process_iter(["pid", "name"]):
        try:
            if p.info["name"] and p.info["name"].lower() == proc:
                p.terminate(); closed.append(p.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return ({"success": True, "message": f"Closed {app}.", "pids": closed}
            if closed else
            {"success": False, "message": f"{app} was not running."})


def open_path(path: str):
    try:
        t = os.path.expandvars(os.path.expanduser(path))
        if not os.path.exists(t):
            return {"success": False, "message": f"Path does not exist: {t}"}
        os.startfile(t)
        return {"success": True, "message": f"Opened {t}."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def _ddg(query, n=8):
    import urllib.parse, urllib.request
    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36"})
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    except Exception as e:
        return [], str(e)
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL)
    out = []
    for href, title, snippet in blocks[:n]:
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            href = urllib.parse.unquote(m.group(1))
        out.append({"title": title, "url": href, "snippet": snippet})
    return out, None


def web_search(query: str):
    _reset_cancel(); _start_esc_watch()
    results, err = _ddg(query, 8)
    if not results:
        return {"success": False, "message": f"No results ({err or 'empty'})."}
    print("\n" + "=" * 60)
    print(f"SEARCH: {query}")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r['title']}")
        print(f"    {r['url']}")
        if r["snippet"]:
            print(f"    {r['snippet']}")
    print("\n" + "=" * 60 + "\n")
    return {"success": True, "count": len(results), "results": results}


def research_topic(query: str):
    _reset_cancel(); _start_esc_watch()
    results, _ = _ddg(query, 6)
    if _is_cancelled():
        return _cancel_check()
    if not results:
        return {"success": False, "message": "Search returned no results."}
    try:
        from newspaper import Article
    except ImportError:
        return {"success": False, "message": "newspaper3k not installed."}

    fetched = []
    for r in results[:3]:
        if _is_cancelled():
            return _cancel_check()
        try:
            a = Article(r["url"]); a.download(); a.parse()
            text = (a.text or "").strip()
            if len(text) < 300:
                continue
            fetched.append({"title": a.title or r["title"],
                            "url": r["url"], "text": text[:3500]})
        except Exception:
            continue
    if _is_cancelled():
        return _cancel_check()
    if not fetched:
        snippets = "\n\n".join(f"[{r['title']}] {r['url']}\n{r['snippet']}" for r in results)
        fetched = [{"title": "Snippets", "url": "multiple", "text": snippets[:7000]}]

    context = "\n\n".join(f"SOURCE: {f['title']} ({f['url']})\n{f['text']}" for f in fetched)
    prompt = (f'The user asked: "{query}"\n\n'
              "Using ONLY the sources below, write a clear, well-organized answer "
              "for a terminal user. Plain text, no markdown headers, no bullet "
              "symbols beyond simple dashes. 200-350 words.\n\n"
              f"SOURCES:\n{context}\n\nANSWER:")

    holder = {}

    def _worker():
        try:
            resp = ollama.chat(model=MODEL,
                               messages=[{"role": "user", "content": prompt}])
            holder["answer"] = resp["message"]["content"].strip()
        except Exception as e:
            holder["error"] = str(e)

    t = threading.Thread(target=_worker, daemon=True)
    t.start()
    while t.is_alive():
        if CANCEL.is_set():
            return {"success": False, "status": "cancelled",
                    "message": "Research cancelled (ESC)."}
        time.sleep(0.05)

    if "error" in holder:
        return {"success": False, "message": holder["error"]}
    answer = holder.get("answer", "")
    print("\n" + "=" * 60)
    print(f"RESEARCH: {query}")
    print("=" * 60)
    print(answer)
    print("\n" + "-" * 60)
    print("Sources:")
    for f in fetched:
        print(f"  - {f['title']}\n    {f['url']}")
    print("=" * 60 + "\n")
    return {"success": True, "answer": answer,
            "sources": [f["url"] for f in fetched]}


def read_article(url: str):
    _reset_cancel(); _start_esc_watch()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        from newspaper import Article
        a = Article(url); a.download(); a.parse()
        if _is_cancelled():
            return _cancel_check()
        text = a.text or ""
        if not text.strip():
            return {"success": False, "message": "No article text."}
        MAX = 7000
        print("\n" + "=" * 60)
        print(f"ARTICLE: {a.title}")
        print("=" * 60)
        if a.authors:
            print(f"Authors: {', '.join(a.authors)}")
        if a.publish_date:
            print(f"Date:    {a.publish_date}")
        print("-" * 60)
        print(text[:MAX])
        if len(text) > MAX:
            print(f"\n... [truncated at {MAX} chars]")
        print("=" * 60 + "\n")
        return {"success": True, "title": a.title, "length": len(text)}
    except Exception as e:
        return {"success": False, "message": f"Failed: {e}"}


def browse_url(url: str):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    if "lynx" in TERMINAL_TOOLS:
        try:
            subprocess.run([TERMINAL_TOOLS["lynx"], "-accept_all_cookies", url],
                           cwd=WORKDIR)
            return {"success": True, "message": "Closed lynx."}
        except Exception as e:
            return {"success": False, "message": str(e)}
    return {"success": False, "message": "lynx not installed."}


def view_image(url_or_path: str):
    if "chafa" not in TERMINAL_TOOLS:
        return {"success": False, "message": "chafa not installed."}
    local = url_or_path
    if url_or_path.startswith(("http://", "https://")):
        try:
            import urllib.request
            ext = os.path.splitext(url_or_path.split("?")[0])[1] or ".png"
            fd, tmp = tempfile.mkstemp(suffix=ext, dir=WORKDIR)
            os.close(fd)
            urllib.request.urlretrieve(url_or_path, tmp)
            local = tmp
        except Exception as e:
            return {"success": False, "message": f"Download: {e}"}
    if not os.path.isfile(local):
        return {"success": False, "message": f"Not a file: {local}"}
    try:
        r = subprocess.run([TERMINAL_TOOLS["chafa"], "--size=60x30", local],
                           capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print("\n" + r.stdout + "\n")
            return {"success": True, "message": "Displayed."}
        return {"success": False, "message": r.stderr[:300]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def image_search(query: str):
    try:
        import urllib.request, urllib.parse
        url = "https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote(query)
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        m = re.findall(r'https://[^"]+?\.(?:jpg|jpeg|png|webp)', html)
        if m:
            return view_image(m[0])
        return {"success": False, "message": "No image found."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def list_emails(folder="INBOX", limit=10):
    if "himalaya" not in TERMINAL_TOOLS:
        return {"success": False, "message": "himalaya not installed."}
    try:
        r = subprocess.run([TERMINAL_TOOLS["himalaya"], "envelope", "list",
                            "-f", folder, "-s", str(limit)],
                           capture_output=True, text=True, timeout=EMAIL_TIMEOUT)
        if r.returncode == 0:
            print("\n" + r.stdout + "\n")
            return {"success": True, "output": r.stdout[:3500]}
        return {"success": False, "message": r.stderr[:400]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def read_email(email_id, folder="INBOX"):
    if "himalaya" not in TERMINAL_TOOLS:
        return {"success": False, "message": "himalaya not installed."}
    try:
        r = subprocess.run([TERMINAL_TOOLS["himalaya"], "message", "read",
                            str(email_id), "-f", folder],
                           capture_output=True, text=True, timeout=EMAIL_TIMEOUT)
        if r.returncode == 0:
            print("\n" + r.stdout + "\n")
            return {"success": True, "output": r.stdout[:5000]}
        return {"success": False, "message": r.stderr[:400]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def send_email(to, subject, body):
    if "himalaya" not in TERMINAL_TOOLS:
        return {"success": False, "message": "himalaya not installed."}
    try:
        mml = f"To: {to}\nSubject: {subject}\n\n{body}"
        r = subprocess.run([TERMINAL_TOOLS["himalaya"], "message", "send"],
                           input=mml, capture_output=True, text=True,
                           timeout=EMAIL_TIMEOUT)
        if r.returncode == 0:
            return {"success": True, "message": f"Sent to {to}."}
        return {"success": False, "message": r.stderr[:400]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def system_info():
    m = psutil.virtual_memory()
    d = psutil.disk_usage("C:\\")
    return {"cpu_percent": psutil.cpu_percent(interval=0.5),
            "cpu_cores": psutil.cpu_count(),
            "ram_percent": m.percent,
            "ram_used_gb": round(m.used / (1024 ** 3), 2),
            "ram_total_gb": round(m.total / (1024 ** 3), 2),
            "disk_percent": d.percent,
            "disk_free_gb": round(d.free / (1024 ** 3), 2)}


def battery_status():
    try:
        b = psutil.sensors_battery()
        if not b:
            return {"success": False, "message": "No battery."}
        left = None
        if b.secsleft and b.secsleft > 0 and not b.power_plugged:
            left = str(timedelta(seconds=int(b.secsleft)))
        return {"success": True, "percent": round(b.percent, 1),
                "plugged_in": b.power_plugged, "time_left": left}
    except Exception as e:
        return {"success": False, "message": str(e)}


def calc(expression: str):
    ops = {ast.Add: operator.add, ast.Sub: operator.sub,
           ast.Mult: operator.mul, ast.Div: operator.truediv,
           ast.Pow: operator.pow, ast.Mod: operator.mod,
           ast.USub: operator.neg, ast.UAdd: operator.pos,
           ast.FloorDiv: operator.floordiv}

    def _ev(node):
        if isinstance(node, ast.Constant):
            if isinstance(node.value, (int, float)):
                return node.value
            raise ValueError("only numbers")
        if isinstance(node, ast.BinOp):
            return ops[type(node.op)](_ev(node.left), _ev(node.right))
        if isinstance(node, ast.UnaryOp):
            return ops[type(node.op)](_ev(node.operand))
        raise ValueError("unsupported")

    try:
        tree = ast.parse(expression, mode="eval")
        result = _ev(tree.body)
        return {"success": True, "expression": expression, "result": result}
    except Exception as e:
        return {"success": False, "message": f"calc error: {e}"}


def volume(action: str):
    action = action.lower()
    if action in ("up", "down"):
        code = 175 if action == "up" else 174
        cmd = ("1..5 | ForEach-Object { "
               "(New-Object -ComObject WScript.Shell).SendKeys([char]%d) }" % code)
    elif action in ("mute", "unmute"):
        cmd = "(New-Object -ComObject WScript.Shell).SendKeys([char]173)"
    else:
        return {"success": False, "message": "action must be up/down/mute/unmute"}
    try:
        subprocess.run(["powershell.exe", "-NoProfile", "-Command", cmd],
                       capture_output=True, text=True, timeout=10)
        return {"success": True, "message": f"Volume {action}."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def screenshot(path: str = ""):
    try:
        from PIL import ImageGrab
    except ImportError:
        return {"success": False, "message": "Pillow not installed."}
    try:
        if not path:
            path = os.path.join(WORKDIR,
                                f"screenshot_{datetime.now():%Y%m%d_%H%M%S}.png")
        img = ImageGrab.grab()
        img.save(path)
        return {"success": True, "message": f"Screenshot: {path}", "path": path}
    except Exception as e:
        return {"success": False, "message": str(e)}


def find_file(name: str, root: str = None):
    if not root:
        root = os.path.expanduser("~")
    try:
        cmd = (f'Get-ChildItem -Path "{root}" -Recurse -Filter "*{name}*" '
               f'-ErrorAction SilentlyContinue | Select -First 20 -Expand FullName')
        r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive",
                            "-Command", cmd],
                           capture_output=True, text=True, timeout=45)
        out = (r.stdout or "").strip()
        if not out:
            return {"success": False, "message": f"No match for '{name}'."}
        print("\n" + out + "\n")
        return {"success": True, "results": out.splitlines()}
    except Exception as e:
        return {"success": False, "message": str(e)}


def weather(city: str = ""):
    import urllib.request, urllib.parse
    q = urllib.parse.quote(city) if city else ""
    url = f"https://wttr.in/{q}?format=%l:+%c+%t+%w+%h"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
        line = urllib.request.urlopen(req, timeout=10).read().decode("utf-8").strip()
        print(f"\nWEATHER: {line}\n")
        return {"success": True, "report": line}
    except Exception as e:
        return {"success": False, "message": str(e)}


def define(word: str):
    import urllib.request, urllib.parse
    url = f"https://api.dictionaryapi.dev/api/v2/entries/en/{urllib.parse.quote(word)}"
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=10).read().decode("utf-8"))
        entry = data[0]
        lines = [f"WORD: {entry.get('word', word)}"]
        for meaning in entry.get("meanings", [])[:2]:
            pos = meaning.get("partOfSpeech", "")
            for d in meaning.get("definitions", [])[:2]:
                lines.append(f"  ({pos}) {d.get('definition', '')}")
        text = "\n".join(lines)
        print("\n" + text + "\n")
        return {"success": True, "text": text}
    except Exception as e:
        return {"success": False, "message": f"define failed: {e}"}


def add_note(text: str):
    r = MEM.add_note(text)
    print(f"\nNote saved: {text}\n")
    return r


def show_notes():
    rows = MEM.show_notes()
    if not rows:
        print("\n(no notes)\n")
        return {"success": True, "count": 0}
    print("\n" + "=" * 60)
    print(f"NOTES ({len(rows)})")
    print("=" * 60)
    for r in rows:
        ts = (r.get("created_at") or "")[:16].replace("T", " ")
        print(f"  [{ts}] {r['value']}")
    print("=" * 60 + "\n")
    return {"success": True, "count": len(rows), "notes": rows}


def clear_notes():
    return MEM.clear_notes()


def list_installed_apps(filter: str = None):
    items = INSTALLED_PROGRAMS
    if filter:
        f = filter.lower()
        items = [p for p in items if f in (p["name"] or "").lower()]
    return {"success": True, "count": len(items),
            "apps": [{"name": p["name"], "version": p["version"]} for p in items[:150]]}


def list_media_players():
    return {"success": True, "players": MEDIA_PLAYERS,
            "terminal_tools": TERMINAL_TOOLS, "deno": DENO_PATH}


def find_app(name: str):
    return {"success": True, "query": name, "resolved_to": _resolve_app(name)}


def remember(key, value, category="general"):
    return MEM.remember(key, value, category)


def recall(key=None, query=None):
    return MEM.recall(key=key, query=query)


def forget(key):
    return MEM.forget(key)


def list_memories():
    return {"success": True, "memories": MEM.snapshot(50)}


def speak_text(text: str):
    if not _ensure_voice():
        return {"success": False, "message": "Voice output unavailable."}
    VOICE_OUT.speak(text, block=False)
    return {"success": True, "message": f"Speaking: {text[:80]}"}


def listen_once():
    text = _capture_voice()
    if not text:
        return {"success": False, "message": "No speech detected."}
    return {"success": True, "text": text}


# ============================================================
# TOOL DEFINITIONS
# ============================================================

TOOLS = [
    {"type": "function", "function": {"name": "run_command",
        "description": "Run a PowerShell command (safety-checked).",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {"name": "open_app",
        "description": "Open an installed application.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {"name": "close_app",
        "description": "Close an application by process name.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {"name": "open_path",
        "description": "Open a file or folder.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {"name": "web_search",
        "description": "Search the web, print results in terminal.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "research_topic",
        "description": "Research a topic and write the answer. Use for 'tell me about X'.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "read_article",
        "description": "Extract and print article text from a URL.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "browse_url",
        "description": "Open a URL in lynx (terminal browser).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {"name": "image_search",
        "description": "Search images and show the first in terminal.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "view_image",
        "description": "Display an image (URL or path).",
        "parameters": {"type": "object", "properties": {
            "url_or_path": {"type": "string"}}, "required": ["url_or_path"]}}},
    {"type": "function", "function": {"name": "play_media",
        "description": "Stream YouTube AUDIO (opens an mpv window).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "play_video",
        "description": "Stream YouTube VIDEO (opens an mpv window).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {"name": "stop_media",
        "description": "Stop current media playback.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "media_status",
        "description": "Report what's playing.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "list_emails",
        "description": "List recent emails.",
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {"name": "read_email",
        "description": "Read an email by ID.",
        "parameters": {"type": "object", "properties": {
            "email_id": {"type": "string"}, "folder": {"type": "string"}},
            "required": ["email_id"]}}},
    {"type": "function", "function": {"name": "send_email",
        "description": "Send an email.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}}, "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {"name": "system_info",
        "description": "CPU / RAM / disk status.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "battery_status",
        "description": "Battery percent and time remaining.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "calc",
        "description": "Evaluate arithmetic.",
        "parameters": {"type": "object", "properties": {
            "expression": {"type": "string"}}, "required": ["expression"]}}},
    {"type": "function", "function": {"name": "volume",
        "description": "Control volume: up, down, mute, unmute.",
        "parameters": {"type": "object", "properties": {
            "action": {"type": "string"}}, "required": ["action"]}}},
    {"type": "function", "function": {"name": "screenshot",
        "description": "Save a screenshot.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "find_file",
        "description": "Find files by name.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}, "root": {"type": "string"}},
            "required": ["name"]}}},
    {"type": "function", "function": {"name": "weather",
        "description": "Get current weather.",
        "parameters": {"type": "object", "properties": {
            "city": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "define",
        "description": "Look up a word's definition.",
        "parameters": {"type": "object", "properties": {
            "word": {"type": "string"}}, "required": ["word"]}}},
    {"type": "function", "function": {"name": "add_note",
        "description": "Save a note.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "show_notes",
        "description": "Show all notes.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "clear_notes",
        "description": "Delete all notes.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "set_timer",
        "description": "Set a timer in seconds.",
        "parameters": {"type": "object", "properties": {
            "seconds": {"type": "integer"}, "label": {"type": "string"}},
            "required": ["seconds"]}}},
    {"type": "function", "function": {"name": "list_timers",
        "description": "List active timers.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "cancel_timer",
        "description": "Cancel a timer by ID.",
        "parameters": {"type": "object", "properties": {
            "timer_id": {"type": "integer"}}, "required": ["timer_id"]}}},
    {"type": "function", "function": {"name": "list_installed_apps",
        "description": "List installed programs (optional filter).",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "list_media_players",
        "description": "Show discovered media players and terminal tools.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "find_app",
        "description": "Resolve an app name to its executable.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {"name": "remember",
        "description": "Save a fact or preference.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "value": {"type": "string"},
            "category": {"type": "string"}}, "required": ["key", "value"]}}},
    {"type": "function", "function": {"name": "recall",
        "description": "Look up a memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "query": {"type": "string"}}}}},
    {"type": "function", "function": {"name": "forget",
        "description": "Delete a memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}}, "required": ["key"]}}},
    {"type": "function", "function": {"name": "list_memories",
        "description": "List all memories.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {"name": "speak_text",
        "description": "Speak text aloud.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {"name": "listen_once",
        "description": "Capture and transcribe one mic utterance.",
        "parameters": {"type": "object", "properties": {}}}},
]


FUNCTIONS = {
    "run_command": run_command, "open_app": open_app, "close_app": close_app,
    "open_path": open_path, "web_search": web_search,
    "research_topic": research_topic, "read_article": read_article,
    "browse_url": browse_url, "image_search": image_search, "view_image": view_image,
    "play_media": play_media, "play_video": play_video,
    "stop_media": stop_media, "media_status": media_status,
    "list_emails": list_emails, "read_email": read_email, "send_email": send_email,
    "system_info": system_info, "battery_status": battery_status,
    "calc": calc, "volume": volume, "screenshot": screenshot,
    "find_file": find_file, "weather": weather, "define": define,
    "add_note": add_note, "show_notes": show_notes, "clear_notes": clear_notes,
    "set_timer": set_timer, "list_timers": list_timers, "cancel_timer": cancel_timer,
    "list_installed_apps": list_installed_apps,
    "list_media_players": list_media_players, "find_app": find_app,
    "remember": remember, "recall": recall, "forget": forget,
    "list_memories": list_memories,
    "speak_text": speak_text, "listen_once": listen_once,
    "list_tasks": list_tasks, "kill_task": kill_task,
    "kill_all_tasks": kill_all_tasks, "mpv_debug": mpv_debug,
}


# ============================================================
# FAST-PATH
# ============================================================

_UNIT_SECONDS = {
    "s": 1, "sec": 1, "secs": 1, "second": 1, "seconds": 1,
    "m": 60, "min": 60, "mins": 60, "minute": 60, "minutes": 60,
    "h": 3600, "hr": 3600, "hrs": 3600, "hour": 3600, "hours": 3600,
}


def try_fast_path(text):
    if not FAST_PATH:
        return None
    tl = text.strip().lower()

    if tl in ("tasks", "list tasks", "ps", "jobs"):
        return "list_tasks", {}
    if tl in ("stop all", "kill all", "stop everything", "killall"):
        return "kill_all_tasks", {}
    m = re.match(r"^kill\s+(\d+)$", tl)
    if m:
        return "kill_task", {"tid": int(m.group(1))}
    if tl in ("mpv debug", "diagnose mpv", "test mpv"):
        return "mpv_debug", {}

    m = re.match(r"^play\s+(.+?)\s+video$", tl)
    if m:
        return "play_video", {"query": m.group(1)}
    m = re.match(r"^play\s+(.+)$", tl)
    if m:
        return "play_media", {"query": m.group(1)}

    if tl in ("stop", "stop music", "stop video", "pause", "quiet"):
        return "stop_media", {}
    if tl in ("what's playing", "whats playing", "media status", "playing"):
        return "media_status", {}

    m = re.match(r"^open\s+(.+)$", tl)
    if m:
        return "open_app", {"app": m.group(1)}

    m = re.match(r"^search\s+(.+)$", tl)
    if m:
        return "web_search", {"query": m.group(1)}

    m = re.match(r"^read\s+(?:article\s+)?(https?://\S+)$", tl)
    if m:
        return "read_article", {"url": m.group(1)}

    m = re.match(r"^(?:note|note:)\s+(.+)$", tl)
    if m:
        return "add_note", {"text": m.group(1).strip()}
    if tl in ("notes", "show notes", "my notes"):
        return "show_notes", {}
    if tl in ("clear notes", "delete notes"):
        return "clear_notes", {}

    m = re.match(r"^calc\s+(.+)$", tl)
    if m:
        return "calc", {"expression": m.group(1)}

    if tl in ("battery", "battery status"):
        return "battery_status", {}
    if tl in ("sysinfo", "system info", "system status", "cpu", "ram"):
        return "system_info", {}

    m = re.match(r"^volume\s+(up|down)$", tl)
    if m:
        return "volume", {"action": m.group(1)}
    if tl in ("mute", "unmute"):
        return "volume", {"action": tl}

    if tl in ("screenshot", "take screenshot", "screen shot"):
        return "screenshot", {}

    m = re.match(r"^weather(?:\s+(.+))?$", tl)
    if m:
        return "weather", {"city": (m.group(1) or "").strip()}

    m = re.match(r"^define\s+(.+)$", tl)
    if m:
        return "define", {"word": m.group(1)}

    m = re.match(r"^timer\s+(\d+)\s*([a-z]*)$", tl)
    if m:
        n = int(m.group(1))
        unit = (m.group(2) or "s").lower()
        secs = n * _UNIT_SECONDS.get(unit, 1)
        return "set_timer", {"seconds": secs, "label": f"{n} {unit}"}
    if tl in ("timers", "list timers"):
        return "list_timers", {}
    m = re.match(r"^(?:cancel\s+timer|timer\s+cancel)\s+(\d+)$", tl)
    if m:
        return "cancel_timer", {"timer_id": int(m.group(1))}

    if tl in ("check email", "check mail", "inbox", "email"):
        return "list_emails", {}
    m = re.match(r"^read\s+email\s+(\d+)$", tl)
    if m:
        return "read_email", {"email_id": m.group(1)}

    return None


# ============================================================
# SYSTEM PROMPT
# ============================================================

BASE_SYSTEM_PROMPT = """
You are JARVIS, a Windows computer assistant in a terminal-only Agentic OS.
You operate the computer through tools. You are the brain; Python executes actions.

CAPABILITIES:
- open_app, close_app, open_path, run_command
- web_search, research_topic, read_article, browse_url
- play_media (audio in window), play_video (video in window), stop_media, media_status
- image_search, view_image
- list_emails, read_email, send_email
- system_info, battery_status, calc, volume, screenshot, find_file, weather, define
- add_note, show_notes, clear_notes, set_timer, list_timers, cancel_timer
- list_installed_apps, list_media_players, find_app
- remember, recall, forget, list_memories, speak_text, listen_once
- list_tasks, kill_task, kill_all_tasks

RULES:
1. Think step by step. Use multiple tools when needed.
2. Do not explain an action you can perform.
3. "Tell me about X" / "What is X" -> research_topic
4. "Search X" / "look up X" -> web_search
5. Audio -> play_media. Video -> play_video. Stop -> stop_media.
6. "Read this article <url>" -> read_article. "Browse <url>" -> browse_url.
7. "Show me a picture of X" -> image_search.
8. "Check my email" -> list_emails. "Send email to X" -> send_email.
9. Never invent file paths.
10. Never open a browser. All output must be terminal (except mpv windows).

SAFETY: never attempt destructive commands. Do not claim success unless the tool returned success.
Be concise.
"""


def build_system_prompt():
    parts = [BASE_SYSTEM_PROMPT.strip()]
    mems = MEM.snapshot(8)
    if mems:
        parts.append("MEMORY:\n" + "\n".join(
            f"- {m['key']}: {m['value']}" for m in mems))
    recent = MEM.recent(2)
    if recent:
        parts.append("RECENT:\n" + "\n---\n".join(
            f"User: {r['user_input']}\nJARVIS: {r['jarvis_response']}"
            for r in recent))
    parts.append(f"DENO: {DENO_PATH or 'MISSING'}")
    return "\n\n".join(parts)


# ============================================================
# JARVIS LOOP
# ============================================================

def _truncate(s, n=1500):
    s = str(s)
    return s if len(s) <= n else s[:n] + "...[truncated]"


def ask_jarvis(user_text: str) -> str:
    _reset_cancel()
    _start_esc_watch()

    task_id = _register_task("llm", user_text[:40],
                             threading.current_thread())

    messages = [{"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": user_text}]
    final_text = ""

    for cycle in range(MAX_TOOL_CYCLES):
        if _is_cancelled():
            _unregister_task(task_id)
            return "[interrupted by user]"

        holder = {}

        def _worker():
            try:
                holder["resp"] = ollama.chat(model=MODEL, messages=messages, tools=TOOLS)
            except Exception as e:
                holder["err"] = str(e)

        t = threading.Thread(target=_worker, daemon=True)
        t.start()
        while t.is_alive():
            if CANCEL.is_set():
                _unregister_task(task_id)
                return "[interrupted by user]"
            time.sleep(0.05)

        if "err" in holder:
            _unregister_task(task_id)
            return f"LLM error: {holder['err']}"
        resp = holder.get("resp", {})
        msg = resp.get("message", {})
        messages.append(msg)

        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            final_text = msg.get("content", "Done.")
            break

        for call in tool_calls:
            if _is_cancelled():
                _unregister_task(task_id)
                return "[interrupted by user]"
            fname = call["function"]["name"]
            args = call["function"].get("arguments", {}) or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: args = {}
            fn = FUNCTIONS.get(fname)
            if fn is None:
                result = {"success": False, "message": f"Unknown tool: {fname}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:
                    result = {"success": False, "message": str(e)}
            print(f"\n[Tool] {fname}({args})")
            print(f"[Result] {_truncate(result, 800)}")
            messages.append({"role": "tool", "tool_name": fname,
                             "content": json.dumps(result, default=str)})
    else:
        final_text = "Reached max actions."

    _unregister_task(task_id)
    MEM.log(user_text, final_text)
    return final_text


# ============================================================
# MAIN
# ============================================================

def _brief_result(tool, result):
    if not isinstance(result, dict):
        return str(result)
    if result.get("message"):
        return result["message"]
    if tool == "calc":
        return f"{result.get('expression')} = {result.get('result')}"
    if tool == "battery_status":
        s = f"{result.get('percent')}%"
        if result.get("plugged_in"):
            s += " (charging)"
        elif result.get("time_left"):
            s += f" ({result['time_left']} left)"
        return s
    if tool == "weather":
        return result.get("report", "ok")
    if tool == "system_info":
        return (f"CPU {result.get('cpu_percent')}% | "
                f"RAM {result.get('ram_percent')}% | "
                f"Disk {result.get('disk_percent')}%")
    if tool == "show_notes":
        return f"{result.get('count', 0)} notes."
    if tool == "list_timers":
        return f"{result.get('count', 0)} active timers."
    if tool == "list_tasks":
        return f"{result.get('count', 0)} tasks."
    if tool == "define":
        return result.get("text", "ok")
    if result.get("success"):
        return "done"
    msg = result.get("message") or result.get("error") or "ok"
    return str(msg)


def main():
    global SPEAK_RESPONSES, FAST_PATH, TYPE_EFFECT, MIC_DEVICE

    print("=" * 60)
    print(" JARVIS - Agentic OS (code15)")
    print("=" * 60)
    print(f" Model:             {MODEL}")
    print(f" Whisper:           {WHISPER_MODEL}")
    print(f" Working dir:       {WORKDIR}")
    print(f" mpv.exe:           {MEDIA_PLAYERS.get('mpv_exe', 'MISSING')}")
    print(f" Terminal tools:    {list(TERMINAL_TOOLS) or 'NONE'}")
    print(f" Deno:              {DENO_PATH or 'MISSING'}")
    print(f" Voice:             {'READY' if VOICE_AVAILABLE else 'MISSING DEPS'}")
    print(f" Fast path:         {'ON' if FAST_PATH else 'OFF'}")
    print(f" mpv log:           {MPV_LOG}")
    print()
    print("Press ESC anytime to cancel the current task.")
    print()
    print("Quick commands:")
    print("  play <song>          play <song> video     stop")
    print("  open <app>           search <query>        check email")
    print("  read <url>           note <text>           notes")
    print("  calc 2+2             weather [city]        battery")
    print("  define <word>        volume up/down/mute   screenshot")
    print("  timer 5 minutes      timers                find <filename>")
    print("  tasks                kill <id>             stop all")
    print("  mpv debug            (diagnose mpv playback)")
    print("  tell me about <topic> (uses the LLM)")
    print()
    print("Special:")
    print("  v          voice loop      mics       list microphones")
    print("  mic <n>    pick microphone")
    print("  speak on/off,  type on/off,  fastpath on/off")
    print("  refresh    rescan,  exit    quit")
    print("=" * 60)

    if SHOW_MIC_LIST and VOICE_AVAILABLE:
        try:
            list_microphones()
        except Exception as e:
            print(f"(mic list failed: {e})")

    while True:
        try:
            user_input = input("\nYou [Enter=speak] > ").rstrip()
        except (KeyboardInterrupt, EOFError):
            print("\nJARVIS > Shutting down.")
            break

        low = user_input.lower().strip()

        if low in {"exit", "quit", "bye"}:
            PLAYBACK.stop(silent=True)
            print("JARVIS > Standing by.")
            break

        if low == "speak on": SPEAK_RESPONSES = True; print("JARVIS > Voice output ON."); continue
        if low == "speak off": SPEAK_RESPONSES = False; print("JARVIS > Voice output OFF."); continue
        if low == "type on": TYPE_EFFECT = True; print("JARVIS > Typewriter ON."); continue
        if low == "type off": TYPE_EFFECT = False; print("JARVIS > Typewriter OFF."); continue
        if low == "fastpath on": FAST_PATH = True; print("JARVIS > Fast path ON."); continue
        if low == "fastpath off": FAST_PATH = False; print("JARVIS > Fast path OFF."); continue

        if low == "mics":
            if not VOICE_AVAILABLE:
                print("JARVIS > Voice deps missing."); continue
            try:
                list_microphones()
            except Exception as e:
                print(f"JARVIS > {e}")
            continue
        m = re.match(r"^mic\s+(\d+)$", low)
        if m:
            MIC_DEVICE = int(m.group(1))
            if VOICE_IN is not None:
                VOICE_IN.device = MIC_DEVICE
            print(f"JARVIS > Microphone set to [{MIC_DEVICE}].")
            continue

        if low == "refresh":
            print("JARVIS > Rescanning...")
            globals()["DISCOVERED_APPS"], globals()["INSTALLED_PROGRAMS"], \
            globals()["MEDIA_PLAYERS"], globals()["TERMINAL_TOOLS"], \
            globals()["DENO_PATH"] = do_full_scan()
            print("JARVIS > Cache refreshed.")
            continue

        if low == "v":
            print("JARVIS > Voice loop. Say 'stop listening' or press ESC/Ctrl+C.")
            _start_esc_watch()
            try:
                while True:
                    _reset_cancel()
                    text = _capture_voice()
                    if CANCEL.is_set():
                        print("JARVIS > Voice loop cancelled.")
                        break
                    if not text:
                        continue
                    if text.lower().strip() in {"stop listening", "stop", "exit voice"}:
                        print("JARVIS > Voice loop ended.")
                        break
                    _dispatch(text)
            except KeyboardInterrupt:
                print("\nJARVIS > Voice loop ended.")
            continue

        if user_input == "":
            _reset_cancel()
            text = _capture_voice()
            if not text:
                continue
            user_input = text

        _dispatch(user_input)


def _dispatch(user_input):
    t0 = time.time()
    _reset_cancel()
    _start_esc_watch()

    fast = try_fast_path(user_input)
    if fast:
        fname, args = fast
        fn = FUNCTIONS.get(fname)
        if fn:
            try:
                result = fn(**args)
            except Exception as e:
                result = {"success": False, "message": str(e)}
            dt = time.time() - t0
            if CANCEL.is_set():
                print(f"JARVIS > cancelled ({dt*1000:.0f} ms)")
                return
            print(f"JARVIS > {_brief_result(fname, result)}  ({dt*1000:.0f} ms)")
            if SPEAK_RESPONSES and _ensure_voice():
                VOICE_OUT.speak(_brief_result(fname, result), block=False)
            return

    print("\nJARVIS > thinking... (ESC to cancel)")
    try:
        answer = ask_jarvis(user_input)
        dt = time.time() - t0
        if CANCEL.is_set() or answer == "[interrupted by user]":
            print(f"JARVIS > cancelled ({dt:.1f}s)")
            return
        print(f"\nJARVIS > ", end="", flush=True)
        _type_out(answer)
        print(f"  ({dt:.1f}s)")
        if SPEAK_RESPONSES and _ensure_voice():
            VOICE_OUT.speak(answer, block=False)
    except Exception as e:
        print(f"\nJARVIS ERROR > {e}")


if __name__ == "__main__":
    main()