"""
JARVIS - code9.py
Terminal-first Agentic OS controller.

Key features:
  - Persistent memory (SQLite)
  - Multi-step planning (up to 8 tool cycles)
  - Start Menu + registry app discovery
  - Media player discovery (VLC, ffplay, mpv, PotPlayer, ...)
  - Terminal streaming: yt-dlp | ffplay  (needs Deno for YouTube JS challenge)
  - Playback control: stop / pause / next
  - Safety layer (blocked / confirm-before-run)
"""

import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
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
MAX_TOOL_CYCLES = 8
COMMAND_TIMEOUT = 30


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
# DISCOVERY: Start Menu shortcuts
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


# ============================================================
# DISCOVERY: Installed programs (registry)
# ============================================================

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
        return [
            {
                "name": d.get("DisplayName"),
                "version": d.get("DisplayVersion"),
                "publisher": d.get("Publisher"),
                "location": d.get("InstallLocation"),
            }
            for d in data if d.get("DisplayName")
        ]
    except Exception:
        return []


# ============================================================
# DISCOVERY: Deno (JS runtime for yt-dlp)
# ============================================================

def find_deno():
    p = shutil.which("deno")
    if p and os.path.isfile(p):
        return p
    winget_deno = os.path.expandvars(
        r"%LOCALAPPDATA%\Microsoft\WinGet\Packages"
        r"\DenoLand.Deno_Microsoft.Winget.Source_8wekyb3d8bbwe\deno.exe"
    )
    if os.path.isfile(winget_deno):
        return winget_deno
    # try any DenoLand.* folder
    root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(root):
        for d in os.listdir(root):
            if d.lower().startswith("denoland.deno"):
                for sub in os.listdir(os.path.join(root, d)):
                    cand = os.path.join(root, d, sub, "deno.exe")
                    if os.path.isfile(cand):
                        return cand
    return None


# ============================================================
# DISCOVERY: Media players
# ============================================================

MEDIA_PLAYER_CANDIDATES = [
    ("mpv",       "mpv.exe",             []),
    ("vlc",       "vlc.exe",             [r"C:\Program Files\VideoLAN\VLC",
                                          r"C:\Program Files (x86)\VideoLAN\VLC"]),
    ("ffplay",    "ffplay.exe",          []),
    ("potplayer", "PotPlayerMini64.exe", [r"C:\Program Files\DAUM\PotPlayer",
                                          r"C:\Program Files\PotPlayer",
                                          r"C:\Program Files (x86)\DAUM\PotPlayer"]),
    ("wmplayer",  "wmplayer.exe",        [r"C:\Program Files\Windows Media Player",
                                          r"C:\Program Files (x86)\Windows Media Player"]),
    ("mpc-hc",    "mpc-hc64.exe",        [r"C:\Program Files\MPC-HC",
                                          r"C:\Program Files (x86)\MPC-HC"]),
    ("foobar",    "foobar2000.exe",      [r"C:\Program Files\foobar2000",
                                          r"C:\Program Files (x86)\foobar2000"]),
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
            found[name] = path
    return found


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
if not MEDIA_PLAYERS:
    print("[JARVIS]   (none found)")

print("[JARVIS] Locating Deno (YouTube JS runtime) ...")
DENO_PATH = find_deno()
print(f"[JARVIS]   {DENO_PATH or '(not found — YouTube may refuse to serve streams)'}")


# ============================================================
# PLAYER HANDLE (for stop/pause/next)
# ============================================================

class PlaybackManager:
    def __init__(self):
        self.current_ytdlp = None
        self.current_player = None
        self.current_query = None

    def start(self, yt_dlp_proc, player_proc, query):
        self.stop(silent=True)
        self.current_ytdlp = yt_dlp_proc
        self.current_player = player_proc
        self.current_query = query

    def stop(self, silent=False):
        killed = []
        for proc in (self.current_ytdlp, self.current_player):
            if proc and proc.poll() is None:
                try:
                    proc.terminate()
                    killed.append(proc.pid)
                except Exception:
                    pass
        self.current_ytdlp = None
        self.current_player = None
        prev = self.current_query
        self.current_query = None
        if silent:
            return {"success": True}
        return {
            "success": bool(killed),
            "message": f"Stopped playback of '{prev}'." if killed else "Nothing was playing.",
            "killed_pids": killed,
        }

    def is_playing(self):
        return self.current_player is not None and self.current_player.poll() is None

    def status(self):
        if not self.is_playing():
            return {"playing": False}
        return {
            "playing": True,
            "query": self.current_query,
            "yt_dlp_pid": self.current_ytdlp.pid if self.current_ytdlp else None,
            "player_pid": self.current_player.pid if self.current_player else None,
        }


PLAYBACK = PlaybackManager()


# ============================================================
# TERMINAL
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
        return {
            "success": r.returncode == 0, "status": "completed",
            "return_code": r.returncode,
            "output": (r.stdout or "").strip()[-6000:],
            "error":  (r.stderr or "").strip()[-3000:],
        }
    except subprocess.TimeoutExpired:
        return {"success": False, "status": "timeout",
                "message": f"Command exceeded {COMMAND_TIMEOUT}s."}
    except Exception as e:
        return {"success": False, "status": "error", "message": str(e)}


# ============================================================
# APP CONTROL
# ============================================================

COMMON_ALIASES = {
    "notepad": "notepad.exe", "calculator": "calc.exe", "calc": "calc.exe",
    "paint": "mspaint.exe", "explorer": "explorer.exe",
    "file explorer": "explorer.exe", "cmd": "cmd.exe",
    "command prompt": "cmd.exe", "powershell": "powershell.exe",
    "terminal": "wt.exe", "windows terminal": "wt.exe",
    "task manager": "taskmgr.exe", "chrome": "chrome",
    "google chrome": "chrome", "edge": "msedge", "microsoft edge": "msedge",
    "firefox": "firefox", "spotify": "spotify",
    "vscode": "code", "vs code": "code", "visual studio code": "code",
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
            cwd=WORKDIR,
        )
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
# WEB
# ============================================================

def web_search(query: str):
    try:
        r = subprocess.run(["ddgr", "--np", "-n", "5", query],
                           capture_output=True, text=True, timeout=20)
        if r.returncode == 0 and r.stdout.strip():
            return {"success": True, "message": "Terminal search results:",
                    "results": r.stdout.strip()[:4000]}
    except (FileNotFoundError, Exception):
        pass
    url = "https://www.google.com/search?q=" + query.replace(" ", "+")
    webbrowser.open(url)
    return {"success": True, "message": f"Searching the web for: {query}", "url": url}


def image_search(query: str):
    url = "https://www.google.com/search?tbm=isch&q=" + query.replace(" ", "+")
    webbrowser.open(url)
    return {"success": True, "message": f"Searching images for: {query}", "url": url}


# ============================================================
# MEDIA STREAMING  (yt-dlp | ffplay, with Deno)
# ============================================================

def _yt_dlp_base_cmd():
    cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "--no-update",
    ]
    if DENO_PATH:
        cmd += ["--js-runtimes", f"deno:{DENO_PATH}"]
    return cmd


def play_media(query: str):
    """
    Stream YouTube audio through the terminal pipe: yt-dlp -> ffplay.
    Uses Deno (if found) to solve YouTube's JS challenge.
    Nothing saved to disk. No browser. No window.
    """
    if "ffplay" not in MEDIA_PLAYERS:
        # No ffplay: try mpv (which handles YouTube natively via its own ytdl hook)
        if "mpv" in MEDIA_PLAYERS:
            return _play_with_mpv(query)
        return {
            "success": False,
            "message": "Streaming needs 'ffplay' (from FFmpeg) or 'mpv'. "
                       "Neither found on disk.",
        }

    ffplay_path = MEDIA_PLAYERS["ffplay"]

    yt_dlp_cmd = _yt_dlp_base_cmd() + ["-o", "-", f"ytsearch1:{query}"]
    ffplay_cmd = [
        ffplay_path,
        "-nodisp",
        "-autoexit",
        "-loglevel", "error",
        "-i", "pipe:0",
    ]

    try:
        ffplay_proc = subprocess.Popen(
            ffplay_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        yt_dlp_proc = subprocess.Popen(
            yt_dlp_cmd,
            stdout=ffplay_proc.stdin,
            stderr=subprocess.DEVNULL,
        )
        ffplay_proc.stdin.close()

        PLAYBACK.start(yt_dlp_proc, ffplay_proc, query)

        note = ""
        if not DENO_PATH:
            note = ("  NOTE: Deno (JS runtime) not found — YouTube may refuse "
                    "the stream. Install with: winget install --id=DenoLand.Deno")

        return {
            "success": True,
            "message": (f"Streaming '{query}' through the terminal pipe "
                        f"(yt-dlp -> ffplay). No download, no browser.") + note,
            "pipeline": "yt-dlp | ffplay",
            "pids": {"yt_dlp": yt_dlp_proc.pid, "ffplay": ffplay_proc.pid},
            "deno": DENO_PATH or "MISSING",
        }
    except Exception as e:
        return {"success": False, "message": str(e)}


def _play_with_mpv(query: str):
    mpv_path = MEDIA_PLAYERS.get("mpv")
    if not mpv_path:
        return {"success": False, "message": "mpv not available."}
    try:
        proc = subprocess.Popen(
            [mpv_path, "--no-video", "--really-quiet",
             f"ytdl://ytsearch1:{query}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        PLAYBACK.start(None, proc, query)
        return {"success": True,
                "message": f"Streaming '{query}' via mpv (native ytdl hook).",
                "pipeline": "mpv (ytdl hook)"}
    except Exception as e:
        return {"success": False, "message": str(e)}


def stop_media():
    return PLAYBACK.stop(silent=False)


def media_status():
    return PLAYBACK.status()


# ============================================================
# SYSTEM / INVENTORY
# ============================================================

def system_info():
    m = psutil.virtual_memory()
    d = psutil.disk_usage("C:\\")
    return {
        "cpu_percent": psutil.cpu_percent(interval=1),
        "cpu_cores": psutil.cpu_count(),
        "ram_percent": m.percent,
        "ram_used_gb": round(m.used / (1024 ** 3), 2),
        "ram_total_gb": round(m.total / (1024 ** 3), 2),
        "disk_percent": d.percent,
        "disk_free_gb": round(d.free / (1024 ** 3), 2),
    }


def list_installed_apps(filter: str = None):
    items = INSTALLED_PROGRAMS
    if filter:
        f = filter.lower()
        items = [p for p in items if f in (p["name"] or "").lower()]
    return {"success": True, "count": len(items),
            "apps": [{"name": p["name"], "version": p["version"]}
                     for p in items[:200]]}


def list_media_players():
    return {"success": True, "players": MEDIA_PLAYERS, "deno": DENO_PATH}


def find_app(name: str):
    return {"success": True, "query": name, "resolved_to": _resolve_app(name)}


# ============================================================
# MEMORY TOOLS
# ============================================================

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
        "description": "Run a Windows PowerShell command. Do NOT try to modify $env:PATH in subprocesses — changes won't persist.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string"}}, "required": ["command"]}}},

    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open an installed application by name (uses Start Menu + registry inventory).",
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
        "description": "Search the web (ddgr if available, otherwise browser).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},

    {"type": "function", "function": {
        "name": "image_search",
        "description": "Search Google Images.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},

    {"type": "function", "function": {
        "name": "play_media",
        "description": "Search YouTube and STREAM the top result's audio through the terminal (yt-dlp piped into ffplay). No download, no browser.",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},

    {"type": "function", "function": {
        "name": "stop_media",
        "description": "Stop the currently playing media stream.",
        "parameters": {"type": "object", "properties": {}}}},

    {"type": "function", "function": {
        "name": "media_status",
        "description": "Report whether media is currently playing and what.",
        "parameters": {"type": "object", "properties": {}}}},

    {"type": "function", "function": {
        "name": "system_info",
        "description": "Get CPU, RAM, disk info.",
        "parameters": {"type": "object", "properties": {}}}},

    {"type": "function", "function": {
        "name": "list_installed_apps",
        "description": "List installed programs (optional substring filter).",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string"}}}}},

    {"type": "function", "function": {
        "name": "list_media_players",
        "description": "List discovered media players and the Deno path (if any).",
        "parameters": {"type": "object", "properties": {}}}},

    {"type": "function", "function": {
        "name": "find_app",
        "description": "Resolve a user-supplied app name to a concrete executable or shortcut.",
        "parameters": {"type": "object", "properties": {
            "name": {"type": "string"}}, "required": ["name"]}}},

    {"type": "function", "function": {
        "name": "remember",
        "description": "Save a fact/preference to long-term memory.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "value": {"type": "string"},
            "category": {"type": "string"}}, "required": ["key", "value"]}}},

    {"type": "function", "function": {
        "name": "recall",
        "description": "Look up a memory by key or search query.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}, "query": {"type": "string"}}}}},

    {"type": "function", "function": {
        "name": "forget",
        "description": "Delete a memory entry.",
        "parameters": {"type": "object", "properties": {
            "key": {"type": "string"}}, "required": ["key"]}}},

    {"type": "function", "function": {
        "name": "list_memories",
        "description": "List all stored memories.",
        "parameters": {"type": "object", "properties": {}}}},
]


FUNCTIONS = {
    "run_command": run_command,
    "open_app": open_app,
    "close_app": close_app,
    "open_path": open_path,
    "web_search": web_search,
    "image_search": image_search,
    "play_media": play_media,
    "stop_media": stop_media,
    "media_status": media_status,
    "system_info": system_info,
    "list_installed_apps": list_installed_apps,
    "list_media_players": list_media_players,
    "find_app": find_app,
    "remember": remember,
    "recall": recall,
    "forget": forget,
    "list_memories": list_memories,
}


# ============================================================
# SYSTEM PROMPT
# ============================================================

BASE_SYSTEM_PROMPT = """
You are JARVIS, a local Windows computer assistant in a terminal-only Agentic OS.

You operate the computer through tools. You are the brain; Python executes actions.

CAPABILITIES:
- run_command: PowerShell commands
- open_app / close_app: launch or close applications
- open_path: open files/folders
- web_search / image_search
- play_media: STREAM YouTube audio through the terminal (yt-dlp -> ffplay). No browser.
- stop_media / media_status: stop or check the current playback
- system_info: CPU/RAM/disk
- list_installed_apps: list all installed programs (optional filter)
- list_media_players: show discovered players and Deno path
- find_app: resolve a name to a concrete exe
- remember / recall / forget / list_memories: persistent memory

PLANNING RULES:
1. Think step by step. Many requests need MULTIPLE tool calls in sequence.
2. Do not explain an action you can perform — perform it.
3. For music/video -> play_media. If it says "no player found", call list_media_players.
4. For "stop" / "stop the song" / "quiet" -> stop_media.
5. For "what am I listening to" -> media_status.
6. Never invent file paths or version numbers.
7. Do NOT try to persist environment changes ($env:PATH += ...) — they won't survive.
8. If the user says "remember ..." -> call remember.
9. If a query references something stored earlier -> call recall first.

SAFETY:
- Never attempt destructive commands (delete system files, format, modify security,
  create users, bypass permissions, shutdown/restart).
- Do not claim success unless the tool returned success.

After all tool calls, report the final result briefly. Be concise and calm.
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
        parts.append("MEDIA PLAYERS AVAILABLE:\n" +
                     "\n".join(f"- {n}: {p}" for n, p in MEDIA_PLAYERS.items()))
    if DENO_PATH:
        parts.append(f"DENO (JS RUNTIME): {DENO_PATH}")
    else:
        parts.append("DENO: NOT FOUND — YouTube streaming may fail.")
    return "\n\n".join(parts)


# ============================================================
# JARVIS LOOP
# ============================================================

def ask_jarvis(user_text: str) -> str:
    messages = [
        {"role": "system", "content": build_system_prompt()},
        {"role": "user", "content": user_text},
    ]
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

def main():
    print("=" * 60)
    print(" JARVIS - Agentic OS Prototype (code9)")
    print("=" * 60)
    print(f" Model:             {MODEL}")
    print(f" Working directory: {WORKDIR}")
    print(f" Memory DB:         {DB_PATH}")
    print(f" Start Menu apps:   {len(DISCOVERED_APPS)}")
    print(f" Installed progs:   {len(INSTALLED_PROGRAMS)}")
    print(f" Media players:     {list(MEDIA_PLAYERS) or 'NONE'}")
    print(f" Deno (JS runtime): {DENO_PATH or 'MISSING'}")
    print(" Status:            ONLINE")
    print()
    print("Try:")
    print("  play stay by justin bieber")
    print("  stop the music")
    print("  what am I listening to?")
    print("  list my media players")
    print("  open vlc")
    print("  what is my CPU usage?")
    print()
    print("Type 'exit' to quit.")
    print("=" * 60)

    while True:
        try:
            user_input = input("\nYou > ").strip()
        except (KeyboardInterrupt, EOFError):
            print("\nJARVIS > Shutting down.")
            break
        if not user_input:
            continue
        if user_input.lower() in {"exit", "quit", "bye"}:
            PLAYBACK.stop(silent=True)
            print("JARVIS > Systems standing by.")
            break
        print("\nJARVIS > Processing...")
        try:
            print(f"\nJARVIS > {ask_jarvis(user_input)}")
        except Exception as e:
            print(f"\nJARVIS ERROR > {e}")


if __name__ == "__main__":
    main()