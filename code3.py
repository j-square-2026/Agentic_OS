import os
import socket
from datetime import datetime

import psutil


def get_processes():
    processes = []

    for process in psutil.process_iter(
        ["pid", "name", "cpu_percent", "memory_percent"]
    ):
        try:
            info = process.info

            processes.append({
                "pid": info["pid"],
                "name": info["name"],
                "cpu": round(info["cpu_percent"], 2),
                "memory": round(info["memory_percent"], 2)
            })

        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    processes.sort(key=lambda x: x["cpu"], reverse=True)

    return processes


def get_network():
    connections = psutil.net_connections()

    active = 0

    for connection in connections:
        if connection.status == "ESTABLISHED":
            active += 1

    return {
        "hostname": socket.gethostname(),
        "active_connections": active
    }


def get_battery():
    battery = psutil.sensors_battery()

    if battery is None:
        return {
            "available": False
        }

    return {
        "available": True,
        "percent": battery.percent,
        "charging": battery.power_plugged,
        "seconds_left": battery.secsleft
    }


def get_temperatures():
    try:
        temperatures = psutil.sensors_temperatures()

        result = {}

        for name, entries in temperatures.items():
            result[name] = []

            for entry in entries:
                result[name].append({
                    "label": entry.label,
                    "temperature": entry.current
                })

        return result

    except AttributeError:
        return {}


def show_processes(processes):
    print("\n--- TOP RUNNING PROCESSES ---")

    for process in processes[:15]:
        print(
            f"PID: {process['pid']:>6} | "
            f"CPU: {process['cpu']:>6}% | "
            f"RAM: {process['memory']:>6}% | "
            f"{process['name']}"
        )


def show_network(network):
    print("\n--- NETWORK ---")
    print(f"Hostname          : {network['hostname']}")
    print(f"Active connections: {network['active_connections']}")


def show_battery(battery):
    print("\n--- BATTERY ---")

    if not battery["available"]:
        print("Battery information unavailable.")
        return

    print(f"Battery           : {battery['percent']}%")

    if battery["charging"]:
        print("Power             : Charging")
    else:
        print("Power             : Battery")


def show_temperatures(temperatures):
    print("\n--- TEMPERATURES ---")

    if not temperatures:
        print("Temperature information unavailable.")
        return

    for sensor, entries in temperatures.items():
        for entry in entries:
            label = entry["label"] or "Sensor"
            print(f"{sensor} / {label}: {entry['temperature']}°C")


def main():
    print("\n================================")
    print("       JARVIS SYSTEM SCAN")
    print("================================")

    print(f"Time: {datetime.now()}")

    processes = get_processes()
    network = get_network()
    battery = get_battery()
    temperatures = get_temperatures()

    show_processes(processes)
    show_network(network)
    show_battery(battery)
    show_temperatures(temperatures)

    print("\n================================")
    print("       SCAN COMPLETE")
    print("================================")


if __name__ == "__main__":
    main()