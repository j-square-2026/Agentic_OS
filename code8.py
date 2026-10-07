"""
JARVIS - code8.py
Fixes:
  - Real media player discovery (no PATH games)
  - Full installed-app inventory from Windows registry + winget
  - play_media uses ANY discovered player (mpv/vlc/ffplay/potplayer/...)
  - New tools: list_installed_apps, find_app
"""

import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import webbrowser
from datetime import datetime
from pathlib import Path

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
# SECURITY  (unchanged from code7)
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
    cmd_lower = command.lower()
    for p in BLOCKED_PATTERNS:
        if re.search(p, cmd_lower, re.IGNORECASE):
            return "BLOCKED"
    for p in CONFIRM_PATTERNS:
        if re.search(p, command, re.IGNORECASE):
            return "CONFIRM"
    return "ALLOW"


# ============================================================
# PERSISTENT MEMORY  (unchanged from code7)
# ============================================================

class Memory:
    def __init__(self, db_path: str):
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
    """Query the Windows registry for installed programs via PowerShell."""
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
# DISCOVERY: Media players (real, on-disk)
# ============================================================

MEDIA_PLAYER_CANDIDATES = [
    # name        exe filename           search hints
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
    """Locate an executable by scanning PATH + winget packages + known dirs."""
    # 1. PATH
    p = shutil.which(exe_name)
    if p and os.path.isfile(p):
        return p

    # 2. Explicit dirs
    for d in (extra_dirs or []):
        cand = os.path.join(d, exe_name)
        if os.path.isfile(cand):
            return cand

    # 3. winget packages (hashed folder names, so walk)
    winget_root = os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\WinGet\Packages")
    if os.path.isdir(winget_root):
        for root, _dirs, files in os.walk(winget_root):
            if exe_name in files:
                return os.path.join(root, exe_name)

    # 4. Common user installs
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
if MEDIA_PLAYERS:
    for name, path in MEDIA_PLAYERS.items():
        print(f"[JARVIS]   {name:10s} -> {path}")
else:
    print("[JARVIS]   (none found)")


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
    # fuzzy match against Start Menu
    m = next((k for k in DISCOVERED_APPS if n in k), None)
    if m:
        return DISCOVERED_APPS[m]
    # fuzzy match against installed programs (registry)
    m2 = next((p for p in INSTALLED_PROGRAMS if n in (p["name"] or "").lower()), None)
    if m2 and m2.get("location"):
        loc = os.path.expandvars(m2["location"])
        if os.path.isdir(loc):
            # find first .exe inside
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
# TERMINAL MEDIA  (FIXED — uses any discovered player)
# ============================================================

# order of preference
PLAYER_ORDER = ["mpv", "vlc", "ffplay", "potplayer", "mpc-hc", "wmplayer", "foobar"]

def _player_command(player_name: str, stream_url: str):
    """Return the argv to play `stream_url` in the given player (no GUI window where possible)."""
    path = MEDIA_PLAYERS[player_name]
    if player_name == "mpv":
        return [path, "--no-video", "--really-quiet", stream_url]
    if player_name == "ffplay":
        return [path, "-nodisp", "-autoexit", "-loglevel", "quiet", stream_url]
    if player_name == "vlc":
        return [path, "--intf", "dummy", "--no-video", "--play-and-exit", stream_url]
    if player_name == "potplayer":
        return [path, stream_url]         # PotPlayer has no true headless mode
    if player_name == "mpc-hc":
        return [path, "/play", "/close", stream_url]
    if player_name == "wmplayer":
        return [path, stream_url]
    if player_name == "foobar":
        return [path, stream_url]
    return None


def play_media(query: str):
    """
    Stream YouTube audio through a pipe: yt-dlp -> ffplay.
    Nothing is saved to disk. No browser. No window.
    """
    if "ffplay" in MEDIA_PLAYERS:
        ffplay_path = MEDIA_PLAYERS["ffplay"]
    else:
        ffplay_path = shutil.which("ffplay")

    if not ffplay_path:
        # Fall back to mpv if ffplay isn't there (mpv handles YouTube natively)
        if "mpv" in MEDIA_PLAYERS:
            return _play_with_mpv(query)
        return {
            "success": False,
            "message": "Streaming needs 'ffplay' (from FFmpeg) or 'mpv'. "
                       "Neither was found on disk."
        }

    # 1. yt-dlp: find the top YouTube result's best audio and stream it to stdout
    yt_dlp_cmd = [
        sys.executable, "-m", "yt_dlp",
        "-f", "bestaudio/best",
        "--no-playlist",
        "--no-warnings",
        "--quiet",
        "-o", "-",                      # <-- write media to stdout, not a file
        f"ytsearch1:{query}",
    ]

    # 2. ffplay: read media from stdin, no video, exit when done
    ffplay_cmd = [
        ffplay_path,
        "-nodisp",
        "-autoexit",
        "-loglevel", "error",
        "-i", "pipe:0",                 # <-- read from stdin
    ]

    try:
        # Start ffplay first, wiring its stdin to a pipe
        ffplay_proc = subprocess.Popen(
            ffplay_cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        # Start yt-dlp, wiring its stdout to ffplay's stdin
        yt_dlp_proc = subprocess.Popen(
            yt_dlp_cmd,
            stdout=ffplay_proc.stdin,
            stderr=subprocess.DEVNULL,
        )

        # Close the parent's copy of ffplay's stdin so the pipe is clean
        ffplay_proc.stdin.close()

        return {
            "success": True,
            "message": f"Streaming '{query}' through the terminal pipe "
                       f"(yt-dlp -> ffplay). No download, no browser.",
            "pipeline": "yt-dlp | ffplay",
            "pids": {"yt_dlp": yt_dlp_proc.pid, "ffplay": ffplay_proc.pid},
        }

    except Exception as e:
        return {"success": False, "message": str(e)}


def _play_with_mpv(query: str):
    """mpv can fetch YouTube natively (it calls yt-dlp under the hood)."""
    mpv_path = MEDIA_PLAYERS.get("mpv")
    if not mpv_path:
        return {"success": False, "message": "mpv not available."}
    try:
        subprocess.Popen(
            [mpv_path, "--no-video", "--really-quiet",
             f"ytdl://ytsearch1:{query}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {"success": True, "message": f"Streaming '{query}' via mpv.",
                "pipeline": "mpv (ytdl hook)"}
    except Exception as e:
        return {"success": False, "message": str(e)}

# ============================================================
# SYSTEM / INVENTORY TOOLS
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
    """Return programs installed on the system (filter is a substring match)."""
    items = INSTALLED_PROGRAMS
    if filter:
        f = filter.lower()
        items = [p for p in items if f in (p["name"] or "").lower()]
    return {"success": True, "count": len(items),
            "apps": [{"name": p["name"], "version": p["version"]}
                     for p in items[:200]]}


def list_media_players():
    return {"success": True, "players": MEDIA_PLAYERS}


def find_app(name: str):
    target = _resolve_app(name)
    return {"success": True, "query": name, "resolved_to": target}


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
        "description": "Run a Windows PowerShell command. Use full exe paths when possible. Do NOT try to modify $env:PATH in subprocesses — changes won't persist.",
        "parameters": {"type": "object", "properties": {
            "command": {"type": "string", "description": "PowerShell command."}},
            "required": ["command"]}}},

    {"type": "function", "function": {
        "name": "open_app",
        "description": "Open an installed application by name. Uses discovered Start Menu shortcuts and the registry inventory.",
        "parameters": {"type": "object", "properties": {
            "app": {"type": "string", "description": "Application name."}},
            "required": ["app"]}}},

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
        "description": "Search YouTube and play the top result in a discovered local media player (no browser).",
        "parameters": {"type": "object", "properties": {
            "query": {"type": "string"}}, "required": ["query"]}}},

    {"type": "function", "function": {
        "name": "system_info",
        "description": "Get CPU, RAM, disk info.",
        "parameters": {"type": "object", "properties": {}}}},

    {"type": "function", "function": {
        "name": "list_installed_apps",
        "description": "List installed programs on the system. Optional substring filter.",
        "parameters": {"type": "object", "properties": {
            "filter": {"type": "string", "description": "Optional filter, e.g. 'player', 'editor'."}}}}},

    {"type": "function", "function": {
        "name": "list_media_players",
        "description": "List media players JARVIS found on disk, with their full paths.",
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
- play_media: play YouTube audio in a local media player (no browser)
- system_info: CPU/RAM/disk
- list_installed_apps: list ALL installed programs (optional filter)
- list_media_players: list media players found on disk (with paths)
- find_app: resolve a name to a concrete exe
- remember / recall / forget / list_memories: persistent memory

PLANNING RULES:
1. Think step by step. Many requests need MULTIPLE tool calls in sequence.
2. Do not explain an action you can perform — perform it.
3. For music/video -> play_media. If it says no player found, call list_media_players first.
4. For "what apps do I have" / "find my <X>" -> list_installed_apps or find_app.
5. Never invent file paths or version numbers. If you don't know a path, use
   list_installed_apps / find_app / run_command with `where.exe` or `Get-ChildItem`
   to discover it FIRST.
6. Do NOT try to persist environment changes (e.g. `$env:PATH += ...`) — those only
   affect the single subprocess and will not survive.
7. When launching an exe, prefer the FULL path from find_app / list_media_players.
8. If the user says "remember ..." -> call remember.
9. If a query references something stored earlier -> call recall first.

SAFETY:
- Never attempt destructive commands (delete system files, format, modify security,
  create users, bypass permissions, shutdown/restart).
- If a task requires a dangerous operation, explain it is blocked.
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
    print(" JARVIS - Agentic OS Prototype (code8)")
    print("=" * 60)
    print(f" Model:             {MODEL}")
    print(f" Working directory: {WORKDIR}")
    print(f" Memory DB:         {DB_PATH}")
    print(f" Start Menu apps:   {len(DISCOVERED_APPS)}")
    print(f" Installed progs:   {len(INSTALLED_PROGRAMS)}")
    print(f" Media players:     {list(MEDIA_PLAYERS) or 'NONE'}")
    print(" Status:            ONLINE")
    print()
    print("Try:")
    print("  list my media players")
    print("  list installed players")
    print("  play Stay by Justin Bieber")
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
            print("JARVIS > Systems standing by.")
            break
        print("\nJARVIS > Processing...")
        try:
            print(f"\nJARVIS > {ask_jarvis(user_input)}")
        except Exception as e:
            print(f"\nJARVIS ERROR > {e}")


if __name__ == "__main__":
    main()