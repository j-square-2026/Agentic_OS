import json
import os
import platform
import socket
from datetime import datetime

import psutil


def get_cpu_state():
    return {
        "usage_percent": psutil.cpu_percent(interval=1),
        "cores_logical": psutil.cpu_count(logical=True),
        "cores_physical": psutil.cpu_count(logical=False),
        "frequency_mhz": (
            round(psutil.cpu_freq().current, 2)
            if psutil.cpu_freq()
            else None
        )
    }


def get_memory_state():
    memory = psutil.virtual_memory()

    return {
        "total_gb": round(memory.total / (1024 ** 3), 2),
        "used_gb": round(memory.used / (1024 ** 3), 2),
        "free_gb": round(memory.available / (1024 ** 3), 2),
        "usage_percent": memory.percent
    }


def get_disk_state():
    disk = psutil.disk_usage(os.getcwd())

    return {
        "total_gb": round(disk.total / (1024 ** 3), 2),
        "used_gb": round(disk.used / (1024 ** 3), 2),
        "free_gb": round(disk.free / (1024 ** 3), 2),
        "usage_percent": disk.percent
    }


def get_process_state():
    processes = []

    for process in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_percent"]
    ):
        try:
            info = process.info

            processes.append({
                "pid": info["pid"],
                "name": info["name"],
                "cpu_percent": round(info["cpu_percent"], 2),
                "memory_percent": round(info["memory_percent"], 2)
            })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    processes.sort(
        key=lambda process: process["cpu_percent"],
        reverse=True
    )

    return processes[:20]


def get_network_state():
    connections = psutil.net_connections()

    established = 0

    for connection in connections:
        if connection.status == "ESTABLISHED":
            established += 1

    return {
        "hostname": socket.gethostname(),
        "active_connections": established
    }


def get_battery_state():
    battery = psutil.sensors_battery()

    if battery is None:
        return {
            "available": False
        }

    return {
        "available": True,
        "percent": battery.percent,
        "charging": battery.power_plugged
    }


def get_system_state():
    return {
        "timestamp": datetime.now().isoformat(),

        "system": {
            "os": platform.system(),
            "os_version": platform.version(),
            "machine": platform.machine(),
            "processor": platform.processor(),
            "hostname": socket.gethostname()
        },

        "cpu": get_cpu_state(),

        "memory": get_memory_state(),

        "disk": get_disk_state(),

        "processes": get_process_state(),

        "network": get_network_state(),

        "battery": get_battery_state()
    }


def save_state(state):
    with open("system_state.json", "w", encoding="utf-8") as file:
        json.dump(state, file, indent=4)


def print_summary(state):
    print("\n================================")
    print("       JARVIS SYSTEM STATE")
    print("================================")

    print(f"Time       : {state['timestamp']}")
    print(f"OS         : {state['system']['os']}")
    print(f"Hostname   : {state['system']['hostname']}")

    print("\nCPU")
    print(f"Usage      : {state['cpu']['usage_percent']}%")
    print(f"Cores      : {state['cpu']['cores_logical']}")

    print("\nMemory")
    print(f"Usage      : {state['memory']['usage_percent']}%")
    print(f"Used       : {state['memory']['used_gb']} GB")
    print(f"Free       : {state['memory']['free_gb']} GB")

    print("\nDisk")
    print(f"Usage      : {state['disk']['usage_percent']}%")
    print(f"Free       : {state['disk']['free_gb']} GB")

    print("\nProcesses")
    print(f"Tracked    : {len(state['processes'])}")

    print("\nNetwork")
    print(f"Connections: {state['network']['active_connections']}")

    print("\nBattery")

    if state["battery"]["available"]:
        print(f"Level      : {state['battery']['percent']}%")
        print(f"Charging   : {state['battery']['charging']}")
    else:
        print("Unavailable")

    print("\n================================")
    print("State saved to system_state.json")
    print("================================")


def main():
    state = get_system_state()

    save_state(state)
    print_summary(state)


if __name__ == "__main__":
    main()