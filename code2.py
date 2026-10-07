import os
import platform
import shutil
import socket
import time
from datetime import datetime

try:
    import psutil
except ImportError:
    print("psutil is not installed.")
    print("Run: pip install psutil")
    exit()


def get_system_info():
    memory = psutil.virtual_memory()
    disk = shutil.disk_usage(os.getcwd())

    return {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "os": platform.system(),
        "os_version": platform.version(),
        "machine": platform.machine(),
        "processor": platform.processor(),
        "hostname": socket.gethostname(),

        "cpu_percent": psutil.cpu_percent(interval=1),
        "cpu_cores": psutil.cpu_count(logical=True),

        "ram_total_gb": round(memory.total / (1024 ** 3), 2),
        "ram_used_gb": round(memory.used / (1024 ** 3), 2),
        "ram_percent": memory.percent,

        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "disk_used_gb": round(disk.used / (1024 ** 3), 2),
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),

        "boot_time": datetime.fromtimestamp(
            psutil.boot_time()
        ).strftime("%Y-%m-%d %H:%M:%S")
    }


def show_system_info(info):
    print("\n==============================")
    print("        JARVIS SYSTEM")
    print("==============================")

    print(f"Time          : {info['time']}")
    print(f"OS            : {info['os']}")
    print(f"OS Version    : {info['os_version']}")
    print(f"Machine       : {info['machine']}")
    print(f"Processor     : {info['processor']}")
    print(f"Hostname      : {info['hostname']}")

    print("\n--- CPU ---")
    print(f"CPU Usage     : {info['cpu_percent']}%")
    print(f"CPU Cores     : {info['cpu_cores']}")

    print("\n--- RAM ---")
    print(f"RAM Total     : {info['ram_total_gb']} GB")
    print(f"RAM Used      : {info['ram_used_gb']} GB")
    print(f"RAM Usage     : {info['ram_percent']}%")

    print("\n--- Disk ---")
    print(f"Disk Total    : {info['disk_total_gb']} GB")
    print(f"Disk Used     : {info['disk_used_gb']} GB")
    print(f"Disk Free     : {info['disk_free_gb']} GB")

    print("\n--- System ---")
    print(f"Boot Time     : {info['boot_time']}")

    print("\n==============================")
    print("JARVIS SYSTEM SCAN COMPLETE")
    print("==============================")


def main():
    info = get_system_info()
    show_system_info(info)


if __name__ == "__main__":
    main()