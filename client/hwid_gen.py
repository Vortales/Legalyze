import hashlib
import platform
import subprocess


def _run_hidden_subprocess(cmd: list[str]) -> subprocess.CompletedProcess:
    creationflags = 0
    startupinfo = None
    if platform.system() == "Windows":
        creationflags = 0x08000000  # CREATE_NO_WINDOW
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 0  # SW_HIDE
    return subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=5,
        creationflags=creationflags,
        startupinfo=startupinfo,
    )


def _get_cpu_id() -> str:
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(["wmic", "cpu", "get", "ProcessorId"])
            lines = result.stdout.strip().split("\n")
            for line in lines:
                line = line.strip()
                if line and line != "ProcessorId":
                    return line
    except Exception:
        pass
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(
                ["powershell", "-Command", "(Get-WmiObject Win32_Processor).ProcessorId"]
            )
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown_cpu"


def _get_gpu_id() -> str:
    try:
        if platform.system() == "Windows":
            result = _run_hidden_subprocess(
                ["wmic", "path", "win32_videocontroller", "get", "PNPDeviceID"]
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
            result = _run_hidden_subprocess(
                ["powershell", "-Command", "(Get-WmiObject Win32_VideoController).PNPDeviceID"]
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
