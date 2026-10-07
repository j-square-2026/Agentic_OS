import json
import os
from datetime import datetime


STATE_FILE = "system_state.json"


def load_system_state():
    if not os.path.exists(STATE_FILE):
        print("system_state.json not found.")
        print("Run: python code4.py")
        return None

    with open(STATE_FILE, "r", encoding="utf-8") as file:
        return json.load(file)


def analyze_cpu(state):
    usage = state["cpu"]["usage_percent"]

    if usage >= 90:
        return {
            "problem": "HIGH_CPU_USAGE",
            "severity": "HIGH",
            "message": f"CPU usage is very high: {usage}%",
            "recommendation": "Inspect CPU-heavy processes."
        }

    if usage >= 70:
        return {
            "problem": "ELEVATED_CPU_USAGE",
            "severity": "MEDIUM",
            "message": f"CPU usage is elevated: {usage}%",
            "recommendation": "Monitor CPU-heavy processes."
        }

    return None


def analyze_memory(state):
    usage = state["memory"]["usage_percent"]

    if usage >= 90:
        return {
            "problem": "CRITICAL_MEMORY_USAGE",
            "severity": "HIGH",
            "message": f"Memory usage is critical: {usage}%",
            "recommendation": "Find memory-heavy processes."
        }

    if usage >= 80:
        return {
            "problem": "HIGH_MEMORY_USAGE",
            "severity": "MEDIUM",
            "message": f"Memory usage is high: {usage}%",
            "recommendation": "Inspect memory-heavy processes."
        }

    return None


def analyze_disk(state):
    usage = state["disk"]["usage_percent"]

    if usage >= 95:
        return {
            "problem": "CRITICAL_DISK_USAGE",
            "severity": "HIGH",
            "message": f"Disk usage is critical: {usage}%",
            "recommendation": "Free disk space immediately."
        }

    if usage >= 85:
        return {
            "problem": "HIGH_DISK_USAGE",
            "severity": "MEDIUM",
            "message": f"Disk usage is high: {usage}%",
            "recommendation": "Consider cleaning unnecessary files."
        }

    return None


def analyze_battery(state):
    battery = state["battery"]

    if not battery["available"]:
        return None

    if battery["percent"] <= 10 and not battery["charging"]:
        return {
            "problem": "CRITICAL_BATTERY",
            "severity": "HIGH",
            "message": f"Battery is critically low: {battery['percent']}%",
            "recommendation": "Connect the charger."
        }

    if battery["percent"] <= 20 and not battery["charging"]:
        return {
            "problem": "LOW_BATTERY",
            "severity": "MEDIUM",
            "message": f"Battery is low: {battery['percent']}%",
            "recommendation": "Consider connecting the charger."
        }

    return None


def analyze_processes(state):
    processes = state["processes"]

    problems = []

    for process in processes:
        if process["cpu_percent"] >= 80:
            problems.append({
                "problem": "CPU_HEAVY_PROCESS",
                "severity": "MEDIUM",
                "message": (
                    f"{process['name']} "
                    f"(PID {process['pid']}) is using "
                    f"{process['cpu_percent']}% CPU."
                ),
                "recommendation": "Inspect this process before taking action."
            })

        if process["memory_percent"] >= 20:
            problems.append({
                "problem": "MEMORY_HEAVY_PROCESS",
                "severity": "MEDIUM",
                "message": (
                    f"{process['name']} "
                    f"(PID {process['pid']}) is using "
                    f"{process['memory_percent']}% memory."
                ),
                "recommendation": "Inspect this process before taking action."
            })

    return problems


def analyze_system(state):
    problems = []

    cpu_problem = analyze_cpu(state)
    if cpu_problem:
        problems.append(cpu_problem)

    memory_problem = analyze_memory(state)
    if memory_problem:
        problems.append(memory_problem)

    disk_problem = analyze_disk(state)
    if disk_problem:
        problems.append(disk_problem)

    battery_problem = analyze_battery(state)
    if battery_problem:
        problems.append(battery_problem)

    problems.extend(analyze_processes(state))

    return problems


def create_action_plan(problems):
    actions = []

    for problem in problems:

        if problem["problem"] == "HIGH_CPU_USAGE":
            actions.append({
                "action": "INSPECT_CPU_PROCESSES",
                "risk": "LOW",
                "execute": False
            })

        elif problem == "HIGH_MEMORY_USAGE":
            actions.append({
                "action": "INSPECT_MEMORY_PROCESSES",
                "risk": "LOW",
                "execute": False
            })

        elif problem["problem"] == "HIGH_DISK_USAGE":
            actions.append({
                "action": "ANALYZE_DISK_USAGE",
                "risk": "LOW",
                "execute": False
            })

        elif problem["problem"] == "LOW_BATTERY":
            actions.append({
                "action": "NOTIFY_LOW_BATTERY",
                "risk": "LOW",
                "execute": False
            })

        else:
            actions.append({
                "action": "OBSERVE_AND_REPORT",
                "risk": "LOW",
                "execute": False
            })

    return actions


def save_analysis(analysis):
    with open("agent_analysis.json", "w", encoding="utf-8") as file:
        json.dump(analysis, file, indent=4)


def show_analysis(analysis):
    print("\n================================")
    print("        JARVIS ANALYSIS")
    print("================================")

    print(f"Time: {analysis['timestamp']}")

    problems = analysis["problems"]

    if not problems:
        print("\nSYSTEM STATUS: HEALTHY")
        print("No important problems detected.")
    else:
        print(f"\nProblems detected: {len(problems)}")

        for number, problem in enumerate(problems, 1):
            print(f"\n[{number}] {problem['problem']}")
            print(f"Severity      : {problem['severity']}")
            print(f"Message       : {problem['message']}")
            print(f"Recommendation: {problem['recommendation']}")

    print("\n--- ACTION PLAN ---")

    if not analysis["actions"]:
        print("No actions required.")
    else:
        for action in analysis["actions"]:
            print(f"Action : {action['action']}")
            print(f"Risk   : {action['risk']}")
            print(f"Execute: {action['execute']}")
            print()


def main():
    state = load_system_state()

    if state is None:
        return

    problems = analyze_system(state)
    actions = create_action_plan(problems)

    analysis = {
        "timestamp": datetime.now().isoformat(),
        "problems": problems,
        "actions": actions
    }

    save_analysis(analysis)
    show_analysis(analysis)


if __name__ == "__main__":
    main()