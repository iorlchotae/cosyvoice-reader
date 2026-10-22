"""硬件监控：CPU / 内存 / GPU / 显存。

CPU 与内存用 psutil，GPU 用 nvidia-smi 查询。nvidia-smi 不可用时
GPU 项返回占位值。
"""

import shutil
import subprocess
import threading
import time
from typing import Optional

try:
    import psutil
except ImportError:  # pragma: no cover
    psutil = None

_CPU_LOCK = threading.Lock()
_CPU_PREV = None      # (total_jiffies, idle_jiffies, monotonic)


def cpu_percent() -> float:
    """整机 CPU 占用百分比，拿不到基准点时返回 -1（上层显示 N/A）。

    这里不能用 psutil.cpu_percent(interval=None)：它把"上一次采样点"存在
    以**线程 id** 为键的字典里（psutil/__init__.py 里 `_last_cpu_times.get(tid)`），
    而 WebUI 用的是 ThreadingHTTPServer —— 每个请求都是一个全新线程，
    查不到本线程上一次的采样点，于是它每次都拿"当前值减当前值"，
    永远返回 0.0。这正是"系统状态里 CPU 一直是 0%"的真正原因。
    自己按整机 total/idle 差值算，就和调用线程无关了。
    """
    global _CPU_PREV
    if psutil is None:
        return -1.0
    try:
        t = psutil.cpu_times()
        total = float(sum(t))
        idle = float(t.idle)
    except Exception:
        return -1.0

    now = time.monotonic()
    with _CPU_LOCK:
        prev = _CPU_PREV
        _CPU_PREV = (total, idle, now)

    if prev is None:
        return -1.0
    d_total = total - prev[0]
    d_idle = idle - prev[1]
    if d_total <= 0 or now - prev[2] <= 0:
        return -1.0
    return max(0.0, min(100.0, (1.0 - d_idle / d_total) * 100.0))


def _nvidia_smi() -> Optional[dict]:
    exe = shutil.which('nvidia-smi')
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, '--query-gpu=name,utilization.gpu,memory.used,memory.total,'
                  'temperature.gpu',
             '--format=csv,noheader,nounits'],
            capture_output=True, text=True, timeout=5,
            encoding='utf-8', errors='replace').stdout.strip()
    except (subprocess.SubprocessError, OSError):
        return None
    try:
        name, util, used, total, temp = [x.strip() for x in out.split(',')]
        return {
            'gpu_name': name,
            'gpu_util': int(util.replace('%', '')),
            'vram_used': int(used),
            'vram_total': int(total),
            'gpu_temp': int(temp),
        }
    except (ValueError, AttributeError):
        return None


def snapshot() -> dict:
    """返回当前硬件状态字典。"""
    gpu = _nvidia_smi()
    if psutil is not None:
        vm = psutil.virtual_memory()
        cpu = cpu_percent()
    else:
        cpu, vm = -1.0, None
    return {
        'cpu': cpu,
        'mem_used': vm.used / (1024 ** 3) if vm else -1,
        'mem_total': vm.total / (1024 ** 3) if vm else -1,
        'gpu': gpu,
    }


def format_snapshot(s: dict) -> str:
    """把 snapshot 结果格式化为单行文本。"""
    gpu = s['gpu']
    gpu_txt = 'GPU: N/A'
    if gpu:
        gpu_txt = (f"GPU: {gpu['gpu_util']}% | "
                   f"显存: {gpu['vram_used']}/{gpu['vram_total']} MB")
    mem_txt = '内存: N/A'
    if s['mem_used'] >= 0:
        mem_txt = (f"内存: {s['mem_used']:.1f}/{s['mem_total']:.1f} GB")
    return f"CPU: {s['cpu']:.0f}% | {mem_txt} | {gpu_txt}"


# 导入时先采一个基准点，这样第一个请求就有真实读数，而不是先显示一次 N/A。
if psutil is not None:  # pragma: no cover - 取决于运行环境
    try:
        cpu_percent()
    except Exception:
        pass
