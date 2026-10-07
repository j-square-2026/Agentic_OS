"""
JARVIS - code7.py
Terminal-first Agentic OS controller.

New in code7:
  - Persistent memory (SQLite)
  - Multi-step planning (up to 8 tool cycles)
  - Auto application discovery (scans Start Menu)
  - Terminal-native media playback (yt-dlp + mpv/ffplay)
  - Keeps the safety layer from code6
"""

import json
import os
import re
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
# SECURITY
# ============================================================

BLOCKED_PATTERNS = [
    r"\bformat\b",
    r"\bdiskpart\b",
    r"\bshutdown\b",
    r"\brestart-computer\b",
    r"\bstop-computer\b",
    r"\bremove-item\b.*-recurse",
    r"\bdel\b.*[c-z]:\\",
    r"\brmdir\b.*[c-z]:\\",
    r"\brd\b.*[c-z]:\\",
    r"\breg\s+(delete|add)\b",
    r"\bnet\s+user\b",
    r"\bnet\s+localgroup\b",
    r"\bicacls\b.*\/grant",
    r"\btakeown\b",
    r"\bcipher\b.*\/w",
    r"\bwbadmin\b",
    r"\bmanage-bde\b",
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
    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, cmd_lower, re.IGNORECASE):
            return "BLOCKED"
    for pattern in CONFIRM_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return "CONFIRM"
    return "ALLOW"


# ============================================================
# PERSISTENT MEMORY (SQLite)
# ============================================================

class Memory:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS memories (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    key TEXT UNIQUE,
                    value TEXT NOT NULL,
                    category TEXT DEFAULT 'general',
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS interactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_input TEXT,
                    jarvis_response TEXT,
                    timestamp TEXT
                )
                """
            )

    def remember(self, key: str, value: str, category: str = "general"):
        now = datetime.now().isoformat()
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO memories (key, value, category, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    value=excluded.value,
                    category=excluded.category,
                    updated_at=excluded.updated_at
                """,
                (key, value, category, now, now),
            )
        return {"success": True, "message": f"Remembered: {key} = {value}"}

    def recall(self, key: str = None, query: str = None):
        with self._conn() as c:
            if key:
                row = c.execute(
                    "SELECT key, value, category, updated_at FROM memories WHERE key=?",
                    (key,),
                ).fetchone()
                if row:
                    return {"success": True, "memory": dict(row)}
                return {"success": False, "message": f"No memory found for key '{key}'."}
            if query:
                rows = c.execute(
                    """
                    SELECT key, value, category, updated_at FROM memories
                    WHERE key LIKE ? OR value LIKE ?
                    ORDER BY updated_at DESC LIMIT 20
                    """,
                    (f"%{query}%", f"%{query}%"),
                ).fetchall()
                return {"success": True, "memories": [dict(r) for r in rows]}
            rows = c.execute(
                "SELECT key, value, category, updated_at FROM memories ORDER BY updated_at DESC LIMIT 50"
            ).fetchall()
            return {"success": True, "memories": [dict(r) for r in rows]}

    def forget(self, key: str):
        with self._conn() as c:
            c.execute("DELETE FROM memories WHERE key=?", (key,))
        return {"success": True, "message": f"Forgot: {key}"}

    def log_interaction(self, user_input: str, response: str):
        with self._conn() as c:
            c.execute(
                "INSERT INTO interactions (user_input, jarvis_response, timestamp) VALUES (?, ?, ?)",
                (user_input, response, datetime.now().isoformat()),
            )

    def recent_interactions(self, n: int = 4):
        with self._conn() as c:
            rows = c.execute(
                "SELECT user_input, jarvis_response FROM interactions ORDER BY id DESC LIMIT ?",
                (n,),
            ).fetchall()
            return [dict(r) for r in reversed(rows)]

    def snapshot(self, limit: int = 30):
        with self._conn() as c:
            rows = c.execute(
                "SELECT key, value, category FROM memories ORDER BY updated_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]


MEM = Memory(DB_PATH)


# ============================================================
# APPLICATION DISCOVERY (scan Start Menu)
# ============================================================

def discover_apps():
    """Scan Windows Start Menu for installed app shortcuts."""
    apps = {}
    search_roots = [
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs",
        os.path.expandvars(r"%APPDATA%\Microsoft\Windows\Start Menu\Programs"),
    ]
    for base in search_roots:
        if not os.path.isdir(base):
            continue
        for root, _dirs, files in os.walk(base):
            for f in files:
                if f.lower().endswith(".lnk"):
                    name = f[:-4].strip().lower()
                    apps.setdefault(name, os.path.join(root, f))
    return apps


print("[JARVIS] Discovering installed applications...")
DISCOVERED_APPS = discover_apps()
print(f"[JARVIS] Found {len(DISCOVERED_APPS)} applications.")


# ============================================================
# TERMINAL
# ============================================================

def run_command(command: str):
    security = security_check(command)

    if security == "BLOCKED":
        return {
            "success": False,
            "status": "blocked",
            "message": "This command is blocked by JARVIS safety policy.",
        }

    if security == "CONFIRM":
        print("\n!! JARVIS wants to run:")
        print(f"    {command}")
        answer = input("Allow this command? [y/N]: ").strip().lower()
        if answer != "y":
            return {
                "success": False,
                "status": "cancelled",
                "message": "User denied the command.",
            }

    try:
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                command,
            ],
            cwd=WORKDIR,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT,
        )
        return {
            "success": result.returncode == 0,
            "status": "completed",
            "return_code": result.returncode,
            "output": (result.stdout or "").strip()[-6000:],
            "error": (result.stderr or "").strip()[-3000:],
        }
    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "status": "timeout",
            "message": f"Command exceeded the {COMMAND_TIMEOUT}s limit.",
        }
    except Exception as e:
        return {"success": False, "status": "error", "message": str(e)}


# ============================================================
# APPLICATION CONTROL
# ============================================================

COMMON_ALIASES = {
    "notepad": "notepad.exe",
    "calculator": "calc.exe",
    "calc": "calc.exe",
    "paint": "mspaint.exe",
    "explorer": "explorer.exe",
    "file explorer": "explorer.exe",
    "cmd": "cmd.exe",
    "command prompt": "cmd.exe",
    "powershell": "powershell.exe",
    "terminal": "wt.exe",
    "windows terminal": "wt.exe",
    "task manager": "taskmgr.exe",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "msedge",
    "microsoft edge": "msedge",
    "firefox": "firefox",
    "spotify": "spotify",
    "vscode": "code",
    "vs code": "code",
    "visual studio code": "code",
}


def open_app(app: str):
    name = app.lower().strip()

    # 1. Known alias
    if name in COMMON_ALIASES:
        target = COMMON_ALIASES[name]
    # 2. Discovered Start Menu shortcut
    elif name in DISCOVERED_APPS:
        target = DISCOVERED_APPS[name]
    else:
        # 3. Fuzzy match on discovered apps
        match = next((k for k in DISCOVERED_APPS if name in k), None)
        target = DISCOVERED_APPS[match] if match else app

    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{target}'"],
            cwd=WORKDIR,
        )
        return {"success": True, "message": f"Opened {app}."}
    except Exception as e:
        return {"success": False, "message": str(e)}


def close_app(app: str):
    process_name = app.lower().strip()
    if not process_name.endswith(".exe"):
        process_name += ".exe"

    protected = {
        "system.exe", "winlogon.exe", "csrss.exe", "smss.exe",
        "services.exe", "lsass.exe", "explorer.exe", "dwm.exe",
    }
    if process_name in protected:
        return {
            "success": False,
            "message": f"{process_name} is protected and cannot be closed by JARVIS.",
        }

    closed = []
    for process in psutil.process_iter(["pid", "name"]):
        try:
            if process.info["name"] and process.info["name"].lower() == process_name:
                process.terminate()
                closed.append(process.info["pid"])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    if closed:
        return {"success": True, "message": f"Closed {app}.", "pids": closed}
    return {"success": False, "message": f"{app} was not found running."}


# ============================================================
# FILE / FOLDER
# ============================================================

def open_path(path: str):
    try:
        target = os.path.expandvars(os.path.expanduser(path))
        if not os.path.exists(target):
            return {"success": False, "message": f"Path does not exist: {target}"}
        os.startfile(target)
        return {"success": True, "message": f"Opened {target}."}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============================================================
# WEB
# ============================================================

def web_search(query: str):
    """
    Try ddgr (DuckDuckGo CLI) first; fall back to browser.
    """
    try:
        result = subprocess.run(
            ["ddgr", "--np", "-n", "5", query],
            capture_output=True, text=True, timeout=20,
        )
        if result.returncode == 0 and result.stdout.strip():
            return {
                "success": True,
                "message": "Terminal search results:",
                "results": result.stdout.strip()[:4000],
            }
    except FileNotFoundError:
        pass
    except Exception:
        pass

    url = "https://www.google.com/search?q=" + query.replace(" ", "+")
    webbrowser.open(url)
    return {
        "success": True,
        "message": f"Searching the web for: {query}",
        "url": url,
    }


def image_search(query: str):
    url = "https://www.google.com/search?tbm=isch&q=" + query.replace(" ", "+")
    webbrowser.open(url)
    return {"success": True, "message": f"Searching images for: {query}", "url": url}


# ============================================================
# TERMINAL MEDIA (yt-dlp + mpv/ffplay)
# ============================================================

def play_media(query: str):
    """
    Search YouTube via yt-dlp and play the top result in the terminal.
    No browser, no GUI window.
    """
    try:
        result = subprocess.run(
            [
                sys.executable, "-m", "yt_dlp",
                "-g",
                "-f", "bestaudio/best",
                "--no-playlist",
                "--no-warnings",
                f"ytsearch1:{query}",
            ],
            capture_output=True,
            text=True,
            timeout=45,
        )

        if result.returncode != 0:
            return {
                "success": False,
                "message": (result.stderr or "yt-dlp failed").strip()[-500:],
            }

        urls = [u.strip() for u in result.stdout.splitlines() if u.startswith("http")]
        if not urls:
            return {"success": False, "message": "No stream URL found."}

        stream_url = urls[0]

        player = None
        candidates = [
            ["mpv", "--no-video", "--really-quiet", stream_url],
            ["ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", stream_url],
        ]
        for cmd in candidates:
            try:
                subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                player = cmd[0]
                break
            except FileNotFoundError:
                continue

        if player is None:
            return {
                "success": False,
                "message": "Neither 'mpv' nor 'ffplay' found in PATH. Install one.",
            }

        return {
            "success": True,
            "message": f"Playing '{query}' via {player} (terminal only, no browser).",
        }

    except subprocess.TimeoutExpired:
        return {"success": False, "message": "yt-dlp search timed out."}
    except Exception as e:
        return {"success": False, "message": str(e)}


# ============================================================
# SYSTEM INFO
# ============================================================

def system_info():
    memory = psutil.virtual_memory()
    disk = psutil.disk_usage("C:\\")
    return {
        "cpu_percent": psutil.cpu_percent(interval=1),
        "cpu_cores": psutil.cpu_count(),
        "ram_percent": memory.percent,
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "disk_percent": disk.percent,
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
    }


# ============================================================
# MEMORY TOOLS (wrappers for the model)
# ============================================================

def remember(key: str, value: str, category: str = "general"):
    return MEM.remember(key, value, category)


def recall(key: str = None, query: str = None):
    return MEM.recall(key=key, query=query)


def forget(key: str):
    return MEM.forget(key)


def list_memories():
    return {"success": True, "memories": MEM.snapshot(50)}


# ============================================================
# TOOL DEFINITIONS
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Run a Windows PowerShell command. For terminal tasks, file listing, CLI programs, system queries.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "PowerShell command to execute."}
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open a Windows application by name. Scans Start Menu and known aliases.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {"type": "string", "description": "Application name, e.g. Chrome, Notepad, VS Code."}
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "Close a Windows application by process name.",
            "parameters": {
                "type": "object",
                "properties": {"app": {"type": "string", "description": "Process or app name."}},
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_path",
            "description": "Open a file or folder on Windows.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File or folder path."}},
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web. Uses ddgr (terminal) if installed, otherwise browser.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Search query."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_search",
            "description": "Search Google Images and open results in browser.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Image search query."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_media",
            "description": "Search YouTube and play media directly in the terminal (no browser). Uses yt-dlp + mpv/ffplay.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "Song, video, or media to play."}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Get current CPU, RAM, and disk information.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": "Save a fact, preference, or detail to long-term memory. Use when the user says 'remember that...' or shares something worth retaining.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short identifier, e.g. 'favorite_color'."},
                    "value": {"type": "string", "description": "The value to remember."},
                    "category": {"type": "string", "description": "Optional category (e.g. 'preference', 'fact', 'user')."},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "recall",
            "description": "Look up something previously stored in memory by key or search query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Exact key to fetch."},
                    "query": {"type": "string", "description": "Search term across keys and values."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Delete a memory entry by key.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "Memory key to remove."}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_memories",
            "description": "List all stored memories.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]


# ============================================================
# TOOL EXECUTION MAP
# ============================================================

FUNCTIONS = {
    "run_command": run_command,
    "open_app": open_app,
    "close_app": close_app,
    "open_path": open_path,
    "web_search": web_search,
    "image_search": image_search,
    "play_media": play_media,
    "system_info": system_info,
    "remember": remember,
    "recall": recall,
    "forget": forget,
    "list_memories": list_memories,
}


# ============================================================
# SYSTEM PROMPT
# ============================================================

BASE_SYSTEM_PROMPT = """
You are JARVIS, a local Windows computer assistant running inside a terminal-only Agentic OS.

You operate the computer through tools. You are the brain; Python executes the actions.

CAPABILITIES:
- run_command: run normal PowerShell commands
- open_app / close_app: launch or close applications
- open_path: open files/folders
- web_search / image_search: search the internet
- play_media: play YouTube audio directly in the terminal (no browser)
- system_info: CPU / RAM / disk status
- remember / recall / forget / list_memories: persistent long-term memory

PLANNING RULES:
1. Think step by step. Some requests need MULTIPLE tools in sequence.
   Example: "open notepad and write my name" -> open_app, then run_command.
2. Choose the correct tool yourself. Do not explain an action you can perform.
3. For "open"/"launch"/"start" -> open_app. For files/folders -> open_path.
4. For music/video -> play_media (terminal, no browser).
5. For pictures -> image_search. For general web queries -> web_search.
6. For system status -> system_info.
7. Use run_command for legitimate terminal tasks.
8. If the user says "remember ..." -> call remember.
9. If a query refers to something stored earlier -> call recall first.

SAFETY RULES:
- Never attempt destructive commands (delete system files, format drives, modify
  security settings, create users, bypass permissions, shutdown/restart).
- If a task requires a dangerous operation, explain it is blocked.
- Do not claim an action succeeded unless the tool returned success.

After all tool calls are done, briefly report the final result to the user.
Be concise, calm, and efficient.
"""


def build_system_prompt():
    """Assemble a system prompt with memory context."""
    memories = MEM.snapshot(30)
    recent = MEM.recent_interactions(3)

    parts = [BASE_SYSTEM_PROMPT.strip()]

    if memories:
        lines = [f"- {m['key']}: {m['value']}" for m in memories]
        parts.append("\nLONG-TERM MEMORY:\n" + "\n".join(lines))

    if recent:
        lines = [f"User: {r['user_input']}\nJARVIS: {r['jarvis_response']}" for r in recent]
        parts.append("\nRECENT CONVERSATION:\n" + "\n---\n".join(lines))

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

    for cycle in range(MAX_TOOL_CYCLES):
        response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )
        message = response["message"]
        messages.append(message)

        tool_calls = message.get("tool_calls", [])
        if not tool_calls:
            final_text = message.get("content", "Done.")
            break

        for call in tool_calls:
            function_name = call["function"]["name"]
            arguments = call["function"].get("arguments", {}) or {}

            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except Exception:
                    arguments = {}

            func = FUNCTIONS.get(function_name)
            if func is None:
                result = {"success": False, "message": f"Unknown tool: {function_name}"}
            else:
                try:
                    result = func(**arguments)
                except Exception as e:
                    result = {"success": False, "message": str(e)}

            print(f"\n[Tool] {function_name}({arguments})")
            print(f"[Result] {result}")

            messages.append(
                {
                    "role": "tool",
                    "tool_name": function_name,
                    "content": json.dumps(result, default=str),
                }
            )
    else:
        final_text = "I reached the maximum number of actions for this request."

    MEM.log_interaction(user_text, final_text)
    return final_text


# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 60)
    print(" JARVIS - Agentic OS Prototype (code7)")
    print("=" * 60)
    print(f" Model:             {MODEL}")
    print(f" Working directory: {WORKDIR}")
    print(f" Memory DB:         {DB_PATH}")
    print(f" Apps discovered:   {len(DISCOVERED_APPS)}")
    print(" Status:            ONLINE")
    print()
    print("Try:")
    print("  play Stay by Justin Bieber")
    print("  remember my favorite color is blue")
    print("  what is my favorite color?")
    print("  open notepad")
    print("  list the files in E:\\Agentic_OS")
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
            answer = ask_jarvis(user_input)
            print(f"\nJARVIS > {answer}")
        except Exception as e:
            print(f"\nJARVIS ERROR > {e}")


if __name__ == "__main__":
    main()