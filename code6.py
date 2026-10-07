import json
import os
import re
import subprocess
import webbrowser
from pathlib import Path

import ollama
import psutil


MODEL = "qwen3.5:4b"
WORKDIR = r"E:\Agentic_OS"


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


def security_check(command):
    """Check a command before allowing Windows execution."""

    command_lower = command.lower()

    for pattern in BLOCKED_PATTERNS:
        if re.search(pattern, command_lower, re.IGNORECASE):
            return "BLOCKED"

    for pattern in CONFIRM_PATTERNS:
        if re.search(pattern, command, re.IGNORECASE):
            return "CONFIRM"

    return "ALLOW"


# ============================================================
# TERMINAL
# ============================================================

def run_command(command):
    """
    Execute a Windows PowerShell command with safety checks.
    """

    security = security_check(command)

    if security == "BLOCKED":
        return {
            "success": False,
            "status": "blocked",
            "message": "This command is blocked by JARVIS safety policy.",
        }

    if security == "CONFIRM":
        print("\n⚠️  JARVIS wants to run:")
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
            timeout=30,
        )

        output = result.stdout.strip()
        error = result.stderr.strip()

        return {
            "success": result.returncode == 0,
            "status": "completed",
            "return_code": result.returncode,
            "output": output[-6000:],
            "error": error[-3000:],
        }

    except subprocess.TimeoutExpired:
        return {
            "success": False,
            "status": "timeout",
            "message": "Command exceeded the 30 second limit.",
        }

    except Exception as e:
        return {
            "success": False,
            "status": "error",
            "message": str(e),
        }


# ============================================================
# APPLICATION CONTROL
# ============================================================

def open_app(app):
    """
    Open a Windows application.
    """

    common_apps = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "paint": "mspaint.exe",
        "explorer": "explorer.exe",
        "file explorer": "explorer.exe",
        "cmd": "cmd.exe",
        "powershell": "powershell.exe",
        "terminal": "wt.exe",
        "vscode": "code",
        "vs code": "code",
        "chrome": "chrome",
        "google chrome": "chrome",
        "edge": "msedge",
        "microsoft edge": "msedge",
        "spotify": "spotify",
    }

    target = common_apps.get(app.lower().strip(), app)

    try:
        subprocess.Popen(
            ["powershell.exe", "-NoProfile", "-Command", f"Start-Process '{target}'"],
            cwd=WORKDIR,
        )

        return {
            "success": True,
            "message": f"Opened {app}.",
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e),
        }


def close_app(app):
    """
    Close an application by process name.
    """

    process_name = app.lower().strip()

    if not process_name.endswith(".exe"):
        process_name += ".exe"

    protected = {
        "system.exe",
        "winlogon.exe",
        "csrss.exe",
        "smss.exe",
        "services.exe",
        "lsass.exe",
        "explorer.exe",
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
        return {
            "success": True,
            "message": f"Closed {app}.",
            "pids": closed,
        }

    return {
        "success": False,
        "message": f"{app} was not found running.",
    }


# ============================================================
# FILE / FOLDER CONTROL
# ============================================================

def open_path(path):
    """
    Open a file or folder using Windows Explorer/default application.
    """

    try:
        target = os.path.expandvars(os.path.expanduser(path))

        if not os.path.exists(target):
            return {
                "success": False,
                "message": f"Path does not exist: {target}",
            }

        os.startfile(target)

        return {
            "success": True,
            "message": f"Opened {target}.",
        }

    except Exception as e:
        return {
            "success": False,
            "message": str(e),
        }


# ============================================================
# WEB
# ============================================================

def web_search(query):
    """
    Open a Google search in the default browser.
    """

    url = "https://www.google.com/search?q=" + query.replace(" ", "+")
    webbrowser.open(url)

    return {
        "success": True,
        "message": f"Searching the web for: {query}",
        "url": url,
    }


def image_search(query):
    """
    Open Google Images.
    """

    url = "https://www.google.com/search?tbm=isch&q=" + query.replace(" ", "+")
    webbrowser.open(url)

    return {
        "success": True,
        "message": f"Searching images for: {query}",
        "url": url,
    }


def play_media(query):
    """
    Open YouTube search.
    """

    url = "https://www.youtube.com/results?search_query=" + query.replace(" ", "+")
    webbrowser.open(url)

    return {
        "success": True,
        "message": f"Searching YouTube for: {query}",
        "url": url,
    }


# ============================================================
# SYSTEM INFORMATION
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
# TOOL DEFINITIONS
# ============================================================

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": (
                "Run a normal Windows PowerShell command. "
                "Use for terminal tasks, checking files, listing folders, "
                "running CLI programs, querying system information, etc. "
                "Do not use for destructive operations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The PowerShell command to execute.",
                    }
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "open_app",
            "description": "Open a Windows application.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Application name, such as Chrome, Notepad, VS Code, Spotify.",
                    }
                },
                "required": ["app"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "close_app",
            "description": "Close a normal Windows application by process name.",
            "parameters": {
                "type": "object",
                "properties": {
                    "app": {
                        "type": "string",
                        "description": "Application/process name.",
                    }
                },
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
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "File or folder path.",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web using the default browser.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "image_search",
            "description": "Search Google Images and open the results.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Image search query.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "play_media",
            "description": "Search YouTube for a song, video, movie, or other media.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Media to search for.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "system_info",
            "description": "Get current CPU, RAM and disk information.",
            "parameters": {
                "type": "object",
                "properties": {},
            },
        },
    },
]


# ============================================================
# TOOL EXECUTION
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
}


# ============================================================
# JARVIS
# ============================================================

SYSTEM_PROMPT = """
You are JARVIS, a local Windows computer assistant.

Your job is to understand natural-language requests and use the available
tools to operate the computer.

You can:
- run normal terminal commands
- open applications
- close applications
- open files and folders
- search the web
- search images
- search/play media through YouTube
- inspect CPU, RAM and disk

IMPORTANT:
1. Choose the correct tool yourself.
2. Do not explain how to perform an action when you can perform it.
3. For "open", "launch", "start" requests, actually use open_app/open_path.
4. For songs/videos, use play_media.
5. For pictures/photos/images, use image_search.
6. For web searches, use web_search.
7. For system status, use system_info.
8. Use run_command for legitimate terminal tasks.
9. Never attempt destructive commands.
10. Never attempt to delete system files, format drives, modify security settings,
    create users, bypass permissions, or shut down/restart the computer.
11. If a task requires a dangerous operation, explain that it is blocked.
12. After performing a tool action, briefly report the result.
13. Do not claim an action succeeded unless the tool returned success.

Be concise, calm and efficient.
"""


def ask_jarvis(user_text):

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT,
        },
        {
            "role": "user",
            "content": user_text,
        },
    ]

    # Allow several tool/action cycles.
    for _ in range(5):

        response = ollama.chat(
            model=MODEL,
            messages=messages,
            tools=TOOLS,
        )

        message = response["message"]

        messages.append(message)

        tool_calls = message.get("tool_calls", [])

        if not tool_calls:
            return message.get("content", "Done.")

        for call in tool_calls:

            function_name = call["function"]["name"]
            arguments = call["function"].get("arguments", {})

            function = FUNCTIONS.get(function_name)

            if function is None:
                result = {
                    "success": False,
                    "message": f"Unknown tool: {function_name}",
                }

            else:
                try:
                    result = function(**arguments)
                except Exception as e:
                    result = {
                        "success": False,
                        "message": str(e),
                    }

            print(f"\n[Tool] {function_name}")
            print(f"[Result] {result}")

            messages.append(
                {
                    "role": "tool",
                    "tool_name": function_name,
                    "content": json.dumps(result),
                }
            )

    return "I reached the maximum number of actions for this request."


# ============================================================
# MAIN LOOP
# ============================================================

def main():

    print("=" * 60)
    print(" JARVIS - Local Agentic OS Prototype")
    print("=" * 60)
    print(f" Model: {MODEL}")
    print(f" Working directory: {WORKDIR}")
    print(" Status: ONLINE")
    print()
    print("Examples:")
    print("  play Stay by Justin Bieber")
    print("  show me a photo of a cow")
    print("  open Chrome")
    print("  open my Agentic OS folder")
    print("  search the web for latest Python news")
    print("  what is my CPU usage?")
    print("  list the files in this folder")
    print()
    print("Type 'exit' to quit.")
    print("=" * 60)

    while True:

        try:
            user_input = input("\nYou > ").strip()

        except KeyboardInterrupt:
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