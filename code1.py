import platform
import os
import shutil
from datetime import datetime


def main():
    print("================================")
    print("       AGENTIC OS - CODE 1")
    print("================================")
    print()

    print("System Information")
    print("------------------")

    print(f"Operating System : {platform.system()}")
    print(f"OS Version       : {platform.version()}")
    print(f"Machine          : {platform.machine()}")
    print(f"Processor        : {platform.processor()}")
    print(f"Python Version   : {platform.python_version()}")

    print()
    print("Computer Resources")
    print("------------------")

    total_disk, used_disk, free_disk = shutil.disk_usage("/")

    print(f"Disk Total       : {total_disk / (1024**3):.2f} GB")
    print(f"Disk Used        : {used_disk / (1024**3):.2f} GB")
    print(f"Disk Free        : {free_disk / (1024**3):.2f} GB")

    print()
    print(f"CPU Cores        : {os.cpu_count()}")

    print()
    print(f"Current Time     : {datetime.now()}")

    print()
    print("Agentic OS is running.")
    print("================================")


if __name__ == "__main__":
    main()