import hashlib
import subprocess
import platform


def _get_cpu_id() -> str:
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "cpu", "get", "ProcessorId"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and line != "ProcessorId":
                    return line
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject Win32_Processor).ProcessorId"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown_cpu"


def _get_gpu_id() -> str:
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["wmic", "path", "win32_videocontroller", "get", "PNPDeviceID"],
                capture_output=True, text=True, timeout=5
            )
            lines = result.stdout.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and line != "PNPDeviceID":
                    return line
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            result = subprocess.run(
                ["powershell", "-Command",
                 "(Get-WmiObject Win32_VideoController).PNPDeviceID"],
                capture_output=True, text=True, timeout=5
            )
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown_gpu"


def generate_hwid() -> str:
    cpu = _get_cpu_id()
    gpu = _get_gpu_id()
    raw = f"CPU:{cpu}|GPU:{gpu}"
    return hashlib.sha256(raw.encode()).hexdigest()
