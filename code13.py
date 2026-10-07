"""
JARVIS - code13.py
Terminal-first Agentic OS controller with VOICE.

New in code13:
  - Voice input via faster-whisper (offline, local)
  - Live streaming transcript: text appears word-by-word as Whisper decodes
  - Typewriter effect on JARVIS replies (toggle with TYPE_EFFECT)
  - Voice loop ("v"), speak on/off, listen_once, speak_text tools

Keeps everything from code12:
  - research_topic, web_search (terminal, no browser)
  - mpv audio/video, lynx browser, article reader, chafa image viewer
  - himalaya email, memory, app discovery, safety layer
"""

import json
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
from datetime import datetime

import ollama
import psutil


# ============================================================
# CONFIG
# ============================================================

MODEL = "qwen3.5:4b"
WORKDIR = r"E:\Agentic_OS"
DB_PATH = os.path.join(WORKDIR, "jarvis_memory.db")
MAX_TOOL_CYCLES = 10
COMMAND_TIMEOUT = 30
ARTICLE_TIMEOUT = 60
EMAIL_TIMEOUT = 30

# Voice
VOICE_SAMPLE_RATE = 16000
VOICE_FRAME_MS = 30
VOICE_SILENCE_SEC = 1.2
VOICE_MAX_SEC = 20
VOICE_INITIAL_TIMEOUT = 5
WHISPER_MODEL = "base.en"
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE = "int8"

# TTS
SPEAK_RESPONSES = False
TTS_RATE = 185

# Display
TYPE_EFFECT = True
TYPE_DELAY = 0.015


# ============================================================
# SECURITY
# ============================================================

BLOCKED_PATTERNS = [
    r"\bformat\b", r"\bdiskpart\b", r"\bshutdown\b",
    r"\brestart-computer\b", r"\bstop-computer\b",
    r"\bremove-item\b.*-recurse",
    r"\bdel\b.*[c-z]:\\", r"\brmdir\b.*[c-z]:\\", r"\brd\b.*[c-z]:\\",
    r"\breg\s+(delete|add)\b", r"\bnet\s+user\b", r"\bnet\s+localgroup\b",
    r"\bicacls\b.*\/grant", r"\btakeown\b", r"\bcipher\b.*\/w",
    r"\bwbadmin\b", r"\bmanage-bde\b",
]

CONFIRM_PATTERNS = [
    r"\bwinget\s+(install|uninstall|upgrade)\b",
    r"\bchoco\s+(install|uninstall|upgrade)\b",
    r"\bpip\s+install\b",
    r"\bnpm\s+(install|uninstall)\b",
    r"\bStart-Process\b.*-Verb\s+RunAs",
    r"\bSet-ExecutionPolicy\b",
]


def security_check(command: str) -> str:
    low = command.lower()
    for p in BLOCKED_PATTERNS:
        if re.search(p, low, re.IGNORECASE):
            return "BLOCKED"
    for p in CONFIRM_PATTERNS:
        if re.search(p, command, re.IGNORECASE):
            return "CONFIRM"
    return "ALLOW"


# ============================================================
# PERSISTENT MEMORY
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
            c.execute("""INSERT INTO memories (key, value, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value, category=excluded.category,
                    updated_at=excluded.updated_at""",
                (key, value, category, now, now))
        return {"success": True, "message": f"Remembered: {key} = {value}"}

    def recall(self, key=None, query=None):
        with self._conn() as c:
            if key:
                r = c.execute("SELECT key, value, category, updated_at FROM memories WHERE key=?",
                              (key,)).fetchone()
                return {"success": True, "memory": dict(r)} if r else \
                       {"success": False, "message": f"No memory for '{key}'."}
            if query:
                rows = c.execute("""SELECT key, value, category, updated_at FROM memories
                    WHERE key LIKE ? OR value LIKE ?
                    ORDER BY updated_at DESC LIMIT 20""",
                    (f"%{query}%", f"%{query}%")).fetchall()
                return {"success": True, "memories": [dict(r) for r in rows]}
            rows = c.execute("""SELECT key, value, category, updated_at FROM memories
                ORDER BY updated_at DESC LIMIT 50""").fetchall()
            return {"success": True, "memories": [dict(r) for r in rows]}

    def forget(self, key):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE key=?", (key,))
        return {"success": True, "message": f"Forgot: {key}"}

    def log_interaction(self, u, r):
        with self._conn() as c:
            c.execute("INSERT INTO interactions (user_input, jarvis_response, timestamp) VALUES (?, ?, ?)",
                      (u, r, datetime.now().isoformat()))

    def recent_interactions(self, n=3):
        with self._conn() as c:
            rows = c.execute("SELECT user_input, jarvis_response FROM interactions ORDER BY id DESC LIMIT ?",
                             (n,)).fetchall()
            return [dict(r) for r in reversed(rows)]

    def snapshot(self, limit=30):
        with self._conn() as c:
            rows = c.execute("SELECT key, value, category FROM memories ORDER BY updated_at DESC LIMIT ?",
                             (limit,)).fetchall()
            return [dict(r) for r in rows]


MEM = Memory(DB_PATH)


# ============================================================
# DISCOVERY
# ============================================================

def discover_start_menu_apps():
    apps = {}
    roots = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    for base in roots:
        if not os.path.isdir(base):
            continue
        for root, _d, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".lnk"):
                    apps.setdefault(f[:-4].strip().lower(), os.path.join(root, f))
    return apps


def discover_installed_programs():
    ps = r"""
$paths = @(
  'HKLM:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKLM:\Software\Wow6432Node\Microsoft\Windows\CurrentVersion\Uninstall\*',
  'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\*'
)
Get-ItemProperty $paths -ErrorAction SilentlyContinue |
  Where-Object { $_.DisplayName } |
  Select-Object DisplayName, DisplayVersion, InstallLocation, Publisher |
  ConvertTo-Json -Compress
"""
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True, text=True, timeout=30,
        )
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


def find_deno():
    p = shutil.which("deno")
    if p and os.path.isfile(p):
        return p
    winget_deno = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe\deno.exe")
    if os.path.isfile(winget_deno):
        return winget_deno
    root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(root):
        for d in os.listdir(root):
            if d.lower().startswith("denoland.deno"):
                for sub in os.listdir(os.path.join(root, d)):
                    cand = os.path.join(root, d, sub, "deno.exe")
                    if os.path.isfile(cand):
                        return cand
    return None


MEDIA_PLAYER_CANDIDATES = [
    ("mpv",       "mpv.exe",             [r"C:\Program Files\MPV Player"]),
    ("vlc",       "vlc.exe",             [r"C:\Program Files\VideoLAN\VLC"]),
    ("ffplay",    "ffplay.exe",          []),
]


def find_exe_on_disk(exe_name, extra_dirs=None):
    p = shutil.which(exe_name)
    if p and os.path.isfile(p):
        return p
    for d in (extra_dirs or []):
        cand = os.path.join(d, exe_name)
        if os.path.isfile(cand):
            return cand
    winget_root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_root):
        for root, _dirs, files in os.walk(winget_root):
            if exe_name in files:
                return os.path.join(root, exe_name)
    user_programs = os.path.expandvars(r"%LOCALAPPDATA%\Programs")
    if os.path.isdir(user_programs):
        for root, _dirs, files in os.walk(user_programs):
            if exe_name in files:
                return os.path.join(root, exe_name)
    return None


def discover_media_players():
    found = {}
    for name, exe, extra in MEDIA_PLAYER_CANDIDATES:
        path = find_exe_on_disk(exe, extra)
        if path:
            if name == "mpv":
                com_path = os.path.splitext(path)[0] + ".com"
                if os.path.isfile(com_path):
                    path = com_path
            found[name] = path
    return found


def discover_terminal_tools():
    tools = {}
    for name in ["lynx", "w3m", "chafa", "timg", "himalaya", "ddgr", "yt-dlp"]:
        p = shutil.which(name)
        if p and os.path.isfile(p):
            tools[name] = p
    return tools


# ============================================================
# STARTUP SCAN
# ============================================================

print("[JARVIS] Scanning Start Menu ...")
DISCOVERED_APPS = discover_start_menu_apps()
print(f"[JARVIS]   {len(DISCOVERED_APPS)} Start Menu shortcuts.")

print("[JARVIS] Scanning registry for installed programs ...")
INSTALLED_PROGRAMS = discover_installed_programs()
print(f"[JARVIS]   {len(INSTALLED_PROGRAMS)} installed programs.")

print("[JARVIS] Locating media players on disk ...")
MEDIA_PLAYERS = discover_media_players()
for name, path in MEDIA_PLAYERS.items():
    print(f"[JARVIS]   {name:10s} -> {path}")

print("[JARVIS] Locating terminal tools ...")
TERMINAL_TOOLS = discover_terminal_tools()
for name, path in TERMINAL_TOOLS.items():
    print(f"[JARVIS]   {name:10s} -> {path}")

print("[JARVIS] Locating Deno ...")
DENO_PATH = find_deno()
print(f"[JARVIS]   {DENO_PATH or '(not found)'}")


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


def _type_out(text, delay=None):
    """Print text character-by-character for a live-typing feel."""
    d = TYPE_DELAY if delay is None else delay
    if not TYPE_EFFECT:
        print(text)
        return
    for ch in text:
        sys.stdout.write(ch)
        sys.stdout.flush()
        if ch in ".!?\n":
            time.sleep(d * 8)
        elif ch == ",":
            time.sleep(d * 4)
        else:
            time.sleep(d)
    print()


class VoiceInput:
    def __init__(self, model_size=WHISPER_MODEL, device=WHISPER_DEVICE,
                 compute_type=WHISPER_COMPUTE, sample_rate=VOICE_SAMPLE_RATE):
        self.model_size = model_size
        self.device = device
        self.compute_type = compute_type
        self.sample_rate = sample_rate
        self._model = None
        self._vad = None
        self._lock = threading.Lock()

    def _load(self):
        if self._model is not None:
            return
        import webrtcvad
        from faster_whisper import WhisperModel
        print(f"[VOICE] Loading Whisper '{self.model_size}' "
              f"(first run downloads ~140 MB)...")
        self._model = WhisperModel(self.model_size, device=self.device,
                                   compute_type=self.compute_type)
        self._vad = webrtcvad.Vad(2)
        print("[VOICE] Ready.")

    def record_until_silence(self):
        import numpy as np
        import sounddevice as sd

        self._load()

        chunk_size = int(self.sample_rate * VOICE_FRAME_MS / 1000)
        max_chunks = int(VOICE_MAX_SEC * 1000 / VOICE_FRAME_MS)
        silence_chunks_needed = int(VOICE_SILENCE_SEC * 1000 / VOICE_FRAME_MS)
        initial_chunks = int(VOICE_INITIAL_TIMEOUT * 1000 / VOICE_FRAME_MS)

        frames = []
        silence_count = 0
        started = False

        print("[VOICE] Listening... (speak now)")

        try:
            with sd.InputStream(samplerate=self.sample_rate, channels=1,
                                dtype="int16", blocksize=chunk_size) as stream:
                for i in range(max_chunks):
                    data, _overflow = stream.read(chunk_size)
                    raw = data.tobytes()
                    try:
                        is_speech = self._vad.is_speech(raw, self.sample_rate)
                    except Exception:
                        is_speech = False

                    if is_speech:
                        if not started:
                            print("[VOICE] Recording...")
                            started = True
                        silence_count = 0
                        frames.append(data.copy())
                    else:
                        if started:
                            silence_count += 1
                            frames.append(data.copy())
                            if silence_count >= silence_chunks_needed:
                                break
                        else:
                            if i > initial_chunks:
                                print("[VOICE] No speech detected.")
                                return None
        except Exception as e:
            print(f"[VOICE] Microphone error: {e}")
            return None

        if not started or not frames:
            return None
        return np.concatenate(frames, axis=0).flatten()

    def transcribe(self, audio, live=True):
        """
        Transcribe int16 numpy array. Returns text.
        If live=True, streams each Whisper segment as it arrives.
        """
        if audio is None:
            return ""
        import numpy as np

        self._load()
        with self._lock:
            audio_f = audio.astype(np.float32) / 32768.0
            segments, _info = self._model.transcribe(
                audio_f, language="en", beam_size=1, vad_filter=False,
            )

            pieces = []
            if live:
                print("\n" + "=" * 60)
                print("  YOU SAID:")
                print("=" * 60, flush=True)

            for seg in segments:
                piece = seg.text.strip()
                if not piece:
                    continue
                pieces.append(piece)
                if live:
                    if TYPE_EFFECT:
                        for ch in piece + " ":
                            sys.stdout.write(ch)
                            sys.stdout.flush()
                            if ch in ".!?":
                                time.sleep(TYPE_DELAY * 6)
                            elif ch == ",":
                                time.sleep(TYPE_DELAY * 3)
                            else:
                                time.sleep(TYPE_DELAY)
                    else:
                        sys.stdout.write(piece + " ")
                        sys.stdout.flush()

            if live:
                sys.stdout.write("\n")
                print("=" * 60 + "\n", flush=True)

        return " ".join(pieces).strip()


class VoiceOutput:
    def __init__(self, rate=TTS_RATE):
        self.rate = rate
        self._engine = None
        self._lock = threading.Lock()

    def _load(self):
        if self._engine is not None:
            return
        import pyttsx3
        self._engine = pyttsx3.init()
        self._engine.setProperty("rate", self.rate)

    def speak(self, text, block=False):
        if not text or len(text.strip()) < 2:
            return
        try:
            self._load()
        except Exception as e:
            print(f"[VOICE] TTS unavailable: {e}")
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


VOICE_IN = VoiceInput() if VOICE_AVAILABLE else None
VOICE_OUT = VoiceOutput() if VOICE_AVAILABLE else None


# ============================================================
# PLAYBACK MANAGER
# ============================================================

class PlaybackManager:
    def __init__(self):
        self.proc = None
        self.query = None

    def start(self, proc, query):
        self.stop(silent=True)
        self.proc = proc
        self.query = query

    def stop(self, silent=False):
        killed = []
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.terminate()
                killed.append(self.proc.pid)
            except Exception:
                pass
        prev = self.query
        self.proc = None
        self.query = None
        if silent:
            return {"success": True}
        return {"success": bool(killed),
                "message": f"Stopped playback of '{prev}'." if killed else "Nothing was playing.",
                "killed_pids": killed}

    def is_playing(self):
        return self.proc is not None and self.proc.poll() is None

    def status(self):
        if not self.is_playing():
            return {"playing": False}
        return {"playing": True, "query": self.query, "pid": self.proc.pid}


PLAYBACK = PlaybackManager()


# ============================================================
# TERMINAL / APP / FILE
# ============================================================

def run_command(command: str):
    security = security_check(command)
    if security == "BLOCKED":
        return {"success": False, "status": "blocked",
                "message": "This command is blocked by JARVIS safety policy."}
    if security == "CONFIRM":
        print("\n!! JARVIS wants to run:")
        print(f"    {command}")
        if input("Allow this command? [y/N]: ").strip().lower() != "y":
            return {"success": False, "status": "cancelled", "message": "User denied."}
    try:
        r = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=WORKDIR, capture_output=True, text=True, timeout=COMMAND_TIMEOUT,
        )
        return {"success": r.returncode == 0, "status": "completed",
                "return_code": r.returncode,
                "output": (r.stdout or "").strip()[-6000:],
                "error":  (r.stderr or "").strip()[-3000:]}
    except subprocess.TimeoutExpired:
        return {"success": False, "status": "timeout",
                "message": f"Command exceeded {COMMAND_TIMEOUT}s."}
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
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{target}'"],
            cwd=WORKDIR)
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


# ============================================================
# WEB SEARCH + RESEARCH
# ============================================================

def _duckduckgo_search(query: str, max_results: int = 8):
    import urllib.parse
    import urllib.request

    url = "https://html.duckduckgo.com/html/?q=" + urllib.parse.quote(query)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) "
                      "Chrome/120.0 Safari/537.36",
    })
    try:
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
    except Exception as e:
        return [], str(e)

    results = []
    blocks = re.findall(
        r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?'
        r'<a[^>]+class="result__snippet"[^>]*>(.*?)</a>',
        html, re.DOTALL,
    )
    for href, title, snippet in blocks[:max_results]:
        title = re.sub(r"<[^>]+>", "", title).strip()
        snippet = re.sub(r"<[^>]+>", "", snippet).strip()
        m = re.search(r"uddg=([^&]+)", href)
        if m:
            href = urllib.parse.unquote(m.group(1))
        results.append({"title": title, "url": href, "snippet": snippet})
    return results, None


def web_search(query: str):
    results, err = _duckduckgo_search(query, max_results=8)
    if not results:
        return {"success": False,
                "message": f"No results found. ({err or 'empty response'})"}

    print("\n" + "=" * 60)
    print(f"SEARCH: {query}")
    print("=" * 60)
    for i, r in enumerate(results, 1):
        print(f"\n[{i}] {r['title']}")
        print(f"    {r['url']}")
        if r["snippet"]:
            print(f"    {r['snippet']}")
    print("\n" + "=" * 60 + "\n")

    return {"success": True, "engine": "duckduckgo-html",
            "count": len(results), "results": results}


def research_topic(query: str):
    results, _ = _duckduckgo_search(query, max_results=6)
    if not results:
        return {"success": False, "message": "Search returned no results."}

    try:
        from newspaper import Article
    except ImportError:
        return {"success": False,
                "message": "newspaper3k not installed. "
                           "Run: pip install newspaper3k lxml_html_clean"}

    fetched = []
    for r in results[:3]:
        try:
            a = Article(r["url"])
            a.download()
            a.parse()
            text = (a.text or "").strip()
            if len(text) < 300:
                continue
            fetched.append({
                "title": a.title or r["title"],
                "url": r["url"],
                "text": text[:4000],
            })
        except Exception:
            continue

    if not fetched:
        snippets = "\n\n".join(
            f"[{r['title']}] {r['url']}\n{r['snippet']}" for r in results
        )
        fetched = [{"title": "Search snippets", "url": "multiple",
                    "text": snippets[:8000]}]

    context = "\n\n".join(
        f"SOURCE: {f['title']} ({f['url']})\n{f['text']}" for f in fetched
    )
    prompt = (
        f'The user asked: "{query}"\n\n'
        "Using ONLY the sources below, write a clear, well-organized answer "
        "for a terminal user. Plain text, no markdown headers, no bullet "
        "symbols beyond simple dashes. Be informative but concise - "
        "roughly 200-400 words. Do not mention the sources unless relevant.\n\n"
        f"SOURCES:\n{context}\n\nANSWER:"
    )

    try:
        resp = ollama.chat(model=MODEL,
                           messages=[{"role": "user", "content": prompt}])
        answer = resp["message"]["content"].strip()
    except Exception as e:
        return {"success": False, "message": f"LLM summarization failed: {e}"}

    print("\n" + "=" * 60)
    print(f"RESEARCH: {query}")
    print("=" * 60)
    print(answer)
    print("\n" + "-" * 60)
    print("Sources:")
    for f in fetched:
        print(f"  - {f['title']}")
        print(f"    {f['url']}")
    print("=" * 60 + "\n")

    return {
        "success": True,
        "message": f"Researched '{query}' using {len(fetched)} source(s).",
        "sources": [f["url"] for f in fetched],
        "answer": answer,
    }


# ============================================================
# TERMINAL BROWSER / ARTICLE / IMAGE
# ============================================================

def browse_url(url: str):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    browser = None
    if "lynx" in TERMINAL_TOOLS:
        browser = ("lynx", [TERMINAL_TOOLS["lynx"], "-accept_all_cookies", url])
    elif "w3m" in TERMINAL_TOOLS:
        browser = ("w3m", [TERMINAL_TOOLS["w3m"], url])
    if not browser:
        return {"success": False,
                "message": "No terminal browser found. Install lynx: scoop install lynx"}
    name, cmd = browser
    try:
        subprocess.run(cmd, cwd=WORKDIR)
        return {"success": True, "message": f"Closed {name}.", "browser": name}
    except Exception as e:
        return {"success": False, "message": f"{name} failed: {e}"}


def read_article(url: str):
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    try:
        from newspaper import Article
        article = Article(url)
        article.download()
        article.parse()
        text = article.text or ""
        if not text.strip():
            return {"success": False, "message": "No article text extracted."}
        MAX = 8000
        truncated = len(text) > MAX
        print("\n" + "=" * 60)
        print(f"ARTICLE: {article.title}")
        print("=" * 60)
        if article.authors:
            print(f"Authors: {', '.join(article.authors)}")
        if article.publish_date:
            print(f"Date:    {article.publish_date}")
        print("-" * 60)
        print(text[:MAX])
        if truncated:
            print(f"\n... [truncated at {MAX} chars]")
        print("=" * 60 + "\n")
        return {"success": True, "title": article.title, "length": len(text),
                "truncated": truncated,
                "message": f"Article printed: {article.title}"}
    except ImportError:
        return {"success": False, "message": "newspaper3k not installed."}
    except Exception as e:
        return {"success": False, "message": f"Extraction failed: {e}"}


def view_image(url_or_path: str):
    if "chafa" not in TERMINAL_TOOLS and "timg" not in TERMINAL_TOOLS:
        return {"success": False,
                "message": "No terminal image viewer. Install chafa."}
    local_path = url_or_path
    if url_or_path.startswith(("http://", "https://")):
        try:
            import urllib.request
            ext = os.path.splitext(url_or_path.split("?")[0])[1] or ".png"
            fd, tmp = tempfile.mkstemp(suffix=ext, dir=WORKDIR)
            os.close(fd)
            urllib.request.urlretrieve(url_or_path, tmp)
            local_path = tmp
        except Exception as e:
            return {"success": False, "message": f"Download failed: {e}"}
    if not os.path.isfile(local_path):
        return {"success": False, "message": f"Not a file: {local_path}"}
    try:
        if "chafa" in TERMINAL_TOOLS:
            cmd = [TERMINAL_TOOLS["chafa"], "--size=60x30", local_path]
        else:
            cmd = [TERMINAL_TOOLS["timg"], "-g", "60x30", local_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        if r.returncode == 0:
            print("\n" + r.stdout + "\n")
            return {"success": True, "message": f"Displayed: {local_path}"}
        return {"success": False, "message": r.stderr[:500]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def image_search(query: str):
    try:
        import urllib.request, urllib.parse
        search_url = "https://www.google.com/search?tbm=isch&q=" + urllib.parse.quote(query)
        req = urllib.request.Request(search_url, headers={"User-Agent": "Mozilla/5.0"})
        html = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", errors="ignore")
        matches = re.findall(r'https://[^"]+?\.(?:jpg|jpeg|png|webp)', html)
        if matches:
            return view_image(matches[0])
        return {"success": False, "message": "No image URL found."}
    except Exception as e:
        return {"success": False, "message": f"Image search failed: {e}"}


# ============================================================
# EMAIL
# ============================================================

def list_emails(folder: str = "INBOX", limit: int = 10):
    if "himalaya" not in TERMINAL_TOOLS:
        return {"success": False, "message": "himalaya not installed."}
    try:
        r = subprocess.run(
            [TERMINAL_TOOLS["himalaya"], "envelope", "list",
             "-f", folder, "-s", str(limit)],
            capture_output=True, text=True, timeout=EMAIL_TIMEOUT)
        if r.returncode == 0:
            print("\n" + r.stdout + "\n")
            return {"success": True, "folder": folder, "output": r.stdout[:4000]}
        return {"success": False, "message": r.stderr[:500]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def read_email(email_id: str, folder: str = "INBOX"):
    if "himalaya" not in TERMINAL_TOOLS:
        return {"success": False, "message": "himalaya not installed."}
    try:
        r = subprocess.run(
            [TERMINAL_TOOLS["himalaya"], "message", "read", email_id, "-f", folder],
            capture_output=True, text=True, timeout=EMAIL_TIMEOUT)
        if r.returncode == 0:
            print("\n" + r.stdout + "\n")
            return {"success": True, "output": r.stdout[:6000]}
        return {"success": False, "message": r.stderr[:500]}
    except Exception as e:
        return {"success": False, "message": str(e)}


def send_email(to: str, subject: str, body: str):
    if "himalaya" not in TERMINAL_TOOLS:
        return {"success": False, "message": "himalaya not installed."}
    try:
        mml = f"To: {to}\nSubject: {subject}\n\n{body}"
        r = subprocess.run(
            [TERMINAL_TOOLS["himalaya"], "message", "send"],
            input=mml, capture_output=True, text=True, timeout=EMAIL_TIMEOUT)
        if r.returncode == 0:
            return {"success": True, "message": f"Email sent to {to}."}
        return {"success": False, "message": r.stderr[:500]}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============================================================
# MEDIA
# ============================================================

def play_media(query: str):
    if "mpv" not in MEDIA_PLAYERS:
        return {"success": False, "message": "mpv not found."}
    mpv = MEDIA_PLAYERS["mpv"]
    cmd = [mpv, "--no-video", "--really-quiet",
           "--ytdl-format=bestaudio/best", f"ytdl://ytsearch1:{query}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=WORKDIR)
        PLAYBACK.start(proc, query)
        return {"success": True, "engine": "mpv",
                "message": f"Streaming audio '{query}' via mpv.", "pid": proc.pid}
    except Exception as e:
        return {"success": False, "message": str(e)}


def play_video(query: str):
    if "mpv" not in MEDIA_PLAYERS:
        return {"success": False, "message": "mpv not found."}
    mpv = MEDIA_PLAYERS["mpv"]
    cmd = [mpv, "--ytdl-format=bestvideo[height<=?720]+bestaudio/best",
           "--really-quiet", f"ytdl://ytsearch1:{query}"]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, cwd=WORKDIR)
        PLAYBACK.start(proc, query)
        return {"success": True, "engine": "mpv (video)",
                "message": f"Streaming video '{query}' via mpv.",
                "pid": proc.pid}
    except Exception as e:
        return {"success": False, "message": str(e)}


def stop_media():
    return PLAYBACK.stop(silent=False)


def media_status():
    return PLAYBACK.status()


# ============================================================
# VOICE TOOLS
# ============================================================

def speak_text(text: str):
    if not VOICE_AVAILABLE or VOICE_OUT is None:
        return {"success": False, "message": "Voice output unavailable."}
    VOICE_OUT.speak(text, block=False)
    return {"success": True, "message": f"Speaking: {text[:100]}"}


def listen_once():
    if not VOICE_AVAILABLE or VOICE_IN is None:
        return {"success": False, "message": "Voice input unavailable."}
    audio = VOICE_IN.record_until_silence()
    if audio is None:
        return {"success": False, "message": "No speech detected."}
    text = VOICE_IN.transcribe(audio, live=True)
    if not text:
        return {"success": False, "message": "Could not transcribe."}
    return {"success": True, "text": text}


# ============================================================
# SYSTEM / INVENTORY / MEMORY TOOLS
# ============================================================

def system_info():
    m = psutil.virtual_memory()
    d = psutil.disk_usage("C:\\")
    return {"cpu_percent": psutil.cpu_percent(interval=1),
            "cpu_cores": psutil.cpu_count(),
            "ram_percent": m.percent,
            "ram_used_gb": round(m.used / (1024 ** 3), 2),
            "ram_total_gb": round(m.total / (1024 ** 3), 2),
            "disk_percent": d.percent,
            "disk_free_gb": round(d.free / (1024 ** 3), 2)}


def list_installed_apps(filter: str = None):
    items = INSTALLED_PROGRAMS
    if filter:
        f = filter.lower()
        items = [p for p in items if f in (p["name"] or "").lower()]
    return {"success": True, "count": len(items),
            "apps": [{"name": p["name"], "version": p["version"]}
                     for p in items[:200]]}


def list_media_players():
    return {"success": True, "players": MEDIA_PLAYERS,
            "terminal_tools": TERMINAL_TOOLS, "deno": DENO_PATH}


def find_app(name: str):
    return {"success": True, "query": name, "resolved_to": _resolve_app(name)}


def remember(key, value, category="general"): return MEM.remember(key, value, category)
def recall(key=None, query=None): return MEM.recall(key=key, query=query)
def forget(key): return MEM.forget(key)
def list_memories(): return {"success": True, "memories": MEM.snapshot(50)}


# ============================================================
# TOOL DEFINITIONS
# ============================================================

TOOLS = [
    {"type": "function", "function": {
        "name": "run_command",
        "description": "Run a Windows PowerShell command.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},
    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open an installed application by name.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {
        "name": "close_app",
        "description": "Close an application by process name.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string"}}, "required": ["app"]}}},
    {"type": "function", "function": {
        "name": "open_path",
        "description": "Open a file or folder on Windows.",
        "parameters": {"type": "object", "properties": {
            "path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "web_search",
        "description": "Search the web and print results in the terminal. Never opens a browser.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "research_topic",
        "description": ("Research a topic and write the answer in the terminal. "
                        "Use for 'tell me about X', 'what is X', 'explain X'."),
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "play_media",
        "description": "Stream YouTube AUDIO via mpv.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "play_video",
        "description": "Stream YouTube VIDEO via mpv (video window, no browser).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "stop_media", "description": "Stop the current media stream.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "media_status", "description": "Report current media playback status.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "browse_url",
        "description": "Open a URL in a terminal text browser (lynx).",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "read_article",
        "description": "Extract and print the clean article text from a URL.",
        "parameters": {"type": "object", "properties": {
            "url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "image_search",
        "description": "Search Google Images and display the first result via chafa.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},
    {"type": "function", "function": {
        "name": "view_image",
        "description": "Display an image (URL or path) in the terminal.",
        "parameters": {"type": "object", "properties": {
            "url_or_path": {"type": "string"}}, "required": ["url_or_path"]}}},
    {"type": "function", "function": {
        "name": "list_emails",
        "description": "List recent emails via himalaya.",
        "parameters": {"type": "object", "properties": {
            "folder": {"type": "string"}, "limit": {"type": "integer"}}}}},
    {"type": "function", "function": {
        "name": "read_email",
        "description": "Read a specific email by ID.",
        "parameters": {"type": "object", "properties": {
            "email_id": {"type": "string"}, "folder": {"type": "string"}},
            "required": ["email_id"]}}},
    {"type": "function", "function": {
        "name": "send_email", "description": "Send an email via himalaya.",
        "parameters": {"type": "object", "properties": {
            "to": {"type": "string"}, "subject": {"type": "string"},
            "body": {"type": "string"}}, "required": ["to", "subject", "body"]}}},
    {"type": "function", "function": {
        "name": "speak_text",
        "description": "Speak the given text aloud through the speakers.",
        "parameters": {"type": "object", "properties": {
            "text": {"type": "string"}}, "required": ["text"]}}},
    {"type": "function", "function": {
        "name": "listen_once",
        "description": "Capture one utterance from the microphone and transcribe it.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "system_info", "description": "CPU/RAM/disk info.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "list_installed_apps",
        "description": "List installed programs (optional filter).",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "list_media_players",
        "description": "Show discovered media players, terminal tools, and Deno path.",
        "parameters": {"type": "object", "properties": {}}}},
    {"type": "function", "function": {
        "name": "find_app", "description": "Resolve an app name to a concrete executable.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},
    {"type": "function", "function": {
        "name": "remember", "description": "Save a fact/preference to memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "value": {"type": "string"},
            "category": {"type": "string"}}, "required": ["key", "value"]}}},
    {"type": "function", "function": {
        "name": "recall", "description": "Look up a memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "query": {"type": "string"}}}}},
    {"type": "function", "function": {
        "name": "forget", "description": "Delete a memory entry.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}}, "required": ["key"]}}},
    {"type": "function", "function": {
        "name": "list_memories", "description": "List all stored memories.",
        "parameters": {"type": "object", "properties": {}}}},
]


FUNCTIONS = {
    "run_command": run_command, "open_app": open_app, "close_app": close_app,
    "open_path": open_path, "web_search": web_search,
    "research_topic": research_topic,
    "play_media": play_media, "play_video": play_video,
    "stop_media": stop_media, "media_status": media_status,
    "browse_url": browse_url, "read_article": read_article,
    "image_search": image_search, "view_image": view_image,
    "list_emails": list_emails, "read_email": read_email, "send_email": send_email,
    "speak_text": speak_text, "listen_once": listen_once,
    "system_info": system_info, "list_installed_apps": list_installed_apps,
    "list_media_players": list_media_players, "find_app": find_app,
    "remember": remember, "recall": recall, "forget": forget,
    "list_memories": list_memories,
}


# ============================================================
# SYSTEM PROMPT
# ============================================================

BASE_SYSTEM_PROMPT = """
You are JARVIS, a local Windows computer assistant in a terminal-only Agentic OS.
You operate the computer through tools. You are the brain; Python executes actions.

CAPABILITIES:
- run_command, open_app, close_app, open_path
- web_search (terminal results), research_topic (search + summarize)
- play_media / play_video / stop_media / media_status
- browse_url (lynx), read_article, image_search, view_image
- list_emails / read_email / send_email
- speak_text (TTS), listen_once (mic capture)
- system_info, list_installed_apps, list_media_players, find_app
- remember / recall / forget / list_memories

RULES:
1. Think step by step. Use MULTIPLE tool calls when needed.
2. Do not explain an action - perform it.
3. "Tell me about X" / "What is X" / "Explain X" / "Who was X" -> research_topic
4. "Search for X" / "Show me links about X" -> web_search
5. Audio -> play_media. Video -> play_video.
6. "Read this article" -> read_article. "Browse X" -> browse_url.
7. "Show me a picture of X" -> image_search.
8. "Check my email" -> list_emails. "Send email" -> send_email.
9. "Say X" / "speak X" -> speak_text.
10. "Listen" / "take a voice command" -> listen_once.
11. "Remember ..." -> remember. Reference to earlier info -> recall first.
12. Never invent file paths.

SAFETY:
- Never attempt destructive commands.
- Do not claim success unless the tool returned success.

Be concise and calm. After tool calls, report the result briefly.
"""


def build_system_prompt():
    parts = [BASE_SYSTEM_PROMPT.strip()]
    mems = MEM.snapshot(30)
    if mems:
        parts.append("LONG-TERM MEMORY:\n" +
                     "\n".join(f"- {m['key']}: {m['value']}" for m in mems))
    recent = MEM.recent_interactions(3)
    if recent:
        parts.append("RECENT CONVERSATION:\n" +
                     "\n---\n".join(f"User: {r['user_input']}\nJARVIS: {r['jarvis_response']}"
                                    for r in recent))
    if MEDIA_PLAYERS:
        parts.append("MEDIA PLAYERS:\n" +
                     "\n".join(f"- {n}: {p}" for n, p in MEDIA_PLAYERS.items()))
    if TERMINAL_TOOLS:
        parts.append("TERMINAL TOOLS:\n" +
                     "\n".join(f"- {n}: {p}" for n, p in TERMINAL_TOOLS.items()))
    parts.append(f"DENO: {DENO_PATH or 'MISSING'}")
    parts.append(f"VOICE INPUT: {'available' if VOICE_AVAILABLE else 'NOT available'}")
    parts.append(f"VOICE OUTPUT: {'available' if VOICE_AVAILABLE else 'NOT available'}")
    return "\n\n".join(parts)


# ============================================================
# JARVIS LOOP
# ============================================================

def ask_jarvis(user_text: str) -> str:
    messages = [{"role": "system", "content": build_system_prompt()},
                {"role": "user", "content": user_text}]
    final_text = ""
    for _ in range(MAX_TOOL_CYCLES):
        resp = ollama.chat(model=MODEL, messages=messages, tools=TOOLS)
        msg = resp["message"]
        messages.append(msg)
        tool_calls = msg.get("tool_calls", [])
        if not tool_calls:
            final_text = msg.get("content", "Done.")
            break
        for call in tool_calls:
            fname = call["function"]["name"]
            args = call["function"].get("arguments", {}) or {}
            if isinstance(args, str):
                try: args = json.loads(args)
                except Exception: args = {}
            fn = FUNCTIONS.get(fname)
            if fn is None:
                result = {"success": False, "message": f"Unknown tool: {fname}"}
            else:
                try: result = fn(**args)
                except Exception as e: result = {"success": False, "message": str(e)}
            print(f"\n[Tool] {fname}({args})")
            print(f"[Result] {result}")
            messages.append({"role": "tool", "tool_name": fname,
                             "content": json.dumps(result, default=str)})
    else:
        final_text = "I reached the maximum number of actions for this request."
    MEM.log_interaction(user_text, final_text)
    return final_text


# ============================================================
# MAIN
# ============================================================

def _capture_voice():
    """Record + transcribe once. Returns text or None. Shows transcript live."""
    if not VOICE_AVAILABLE:
        print("JARVIS > Voice unavailable. Missing: " + ", ".join(VOICE_MISSING))
        print("        Install with: pip install faster-whisper sounddevice "
              "webrtcvad-wheels pyttsx3 numpy")
        return None

    audio = VOICE_IN.record_until_silence()
    if audio is None:
        print("[VOICE] (no speech detected)")
        return None

    print("[VOICE] Transcribing...\n")
    text = VOICE_IN.transcribe(audio, live=True)

    if not text:
        print("[VOICE] Could not understand.")
        return None

    print(f"You (voice) > {text}")
    return text


def main():
    global SPEAK_RESPONSES

    print("=" * 60)
    print(" JARVIS - Agentic OS Prototype (code13)")
    print("=" * 60)
    print(f" Model:             {MODEL}")
    print(f" Working directory: {WORKDIR}")
    print(f" Start Menu apps:   {len(DISCOVERED_APPS)}")
    print(f" Installed progs:   {len(INSTALLED_PROGRAMS)}")
    print(f" Media players:     {list(MEDIA_PLAYERS) or 'NONE'}")
    print(f" Terminal tools:    {list(TERMINAL_TOOLS) or 'NONE'}")
    print(f" Deno:              {DENO_PATH or 'MISSING'}")
    print(f" Voice input:       {'READY' if VOICE_AVAILABLE else 'MISSING DEPS'}")
    print(f" Voice output:      {'READY' if VOICE_AVAILABLE else 'MISSING DEPS'}")
    if not VOICE_AVAILABLE:
        print(f"   missing:         {', '.join(VOICE_MISSING)}")
        print(f"   install:         pip install faster-whisper sounddevice "
              f"webrtcvad-wheels pyttsx3 numpy")
    print(f" Type effect:       {'ON' if TYPE_EFFECT else 'OFF'}")
    print(" Status:            ONLINE")
    print()
    print("Commands:")
    print("  <type text>       type a request normally")
    print("  (empty Enter)     press Enter to SPEAK a request")
    print("  v                 voice loop (speak, act, repeat)")
    print("  speak on/off      toggle JARVIS speaking its replies")
    print("  type on/off       toggle typewriter effect")
    print("  exit              quit")
    print()
    print("=" * 60)

    while True:
        try:
            user_input = input("\nYou [Enter=speak] > ").rstrip()
        except (KeyboardInterrupt, EOFError):
            print("\nJARVIS > Shutting down.")
            break

        low = user_input.lower().strip()

        if low in {"exit", "quit", "bye"}:
            PLAYBACK.stop(silent=True)
            print("JARVIS > Systems standing by.")
            break

        if low == "speak on":
            SPEAK_RESPONSES = True
            print("JARVIS > Voice output ON.")
            continue
        if low == "speak off":
            SPEAK_RESPONSES = False
            print("JARVIS > Voice output OFF.")
            continue

        if low == "type on":
            globals()["TYPE_EFFECT"] = True
            print("JARVIS > Typewriter effect ON.")
            continue
        if low == "type off":
            globals()["TYPE_EFFECT"] = False
            print("JARVIS > Typewriter effect OFF.")
            continue

        if low == "v":
            print("JARVIS > Voice loop. Say 'stop listening' or press Ctrl+C to exit.")
            try:
                while True:
                    text = _capture_voice()
                    if not text:
                        continue
                    if text.lower().strip() in {"stop listening", "stop", "exit voice"}:
                        print("JARVIS > Exiting voice loop.")
                        break
                    print("\nJARVIS > Processing...")
                    try:
                        answer = ask_jarvis(text)
                        print(f"\nJARVIS > ", end="", flush=True)
                        _type_out(answer)
                        if SPEAK_RESPONSES and VOICE_OUT:
                            VOICE_OUT.speak(answer, block=False)
                    except Exception as e:
                        print(f"\nJARVIS ERROR > {e}")
            except KeyboardInterrupt:
                print("\nJARVIS > Voice loop ended.")
            continue

        if user_input == "":
            text = _capture_voice()
            if not text:
                continue
            user_input = text

        print("\nJARVIS > Processing...")
        try:
            answer = ask_jarvis(user_input)
            print(f"\nJARVIS > ", end="", flush=True)
            _type_out(answer)
            if SPEAK_RESPONSES and VOICE_OUT:
                VOICE_OUT.speak(answer, block=False)
        except Exception as e:
            print(f"\nJARVIS ERROR > {e}")


if __name__ == "__main__":
    main()