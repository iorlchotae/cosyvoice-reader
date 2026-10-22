"""成品分集：把一整个长 MP3 按「一小时左右」切成若干集。

这是 CosyVoice Reader 的成品输出标准（可在界面「成品分集」里改）：

- 目标每集 60 分钟，允许 55–72 分钟；
- **尽量切在句子之间的静音处**，不切在半个词上；
- 集数与每集长度按时长均衡分配，避免最后一集只剩几分钟；
- 总时长核对通过才允许删除原文件。

`app.py` 在每篇文档合并完成后调用 `split_file()`；`scripts/split_mp3_episodes.py`
是同一个函数的手工命令行入口（用于补切历史成品）。
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from config import (EPISODE_MAX_SECONDS, EPISODE_MIN_SECONDS, EPISODE_SECONDS,
                    get_ffmpeg)

NOISE_DB = '-35dB'
MIN_SIL = 0.40      # 至少 0.4 秒的停顿才当作可切点
WINDOW = 300.0      # 在理想切点前后各找 5 分钟静音


def _ffprobe() -> str:
    ff = get_ffmpeg()
    if ff.lower().endswith('ffmpeg'):
        return ff[:-6] + 'ffprobe'
    if ff.lower().endswith('ffmpeg.exe'):
        return ff[:-10] + 'ffprobe.exe'
    return 'ffprobe'


def probe_duration(path: Path) -> float:
    """读音频时长（秒）。读不到就抛错，绝不静默返回 0。"""
    out = subprocess.run(
        [_ffprobe(), '-v', 'error', '-show_entries', 'format=duration',
         '-of', 'csv=p=0', str(path)],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    txt = (out.stdout or '').strip()
    if not txt:
        raise RuntimeError(f'读不到时长：{Path(path).name}（{(out.stderr or "").strip()[:200]}）')
    return float(txt.splitlines()[0])


def plan_count(total: float, target: float = EPISODE_SECONDS,
               min_ep: float = EPISODE_MIN_SECONDS,
               max_ep: float = EPISODE_MAX_SECONDS) -> int:
    """决定切几集。

    取 [ceil(total/max), floor(total/min)] 里最接近 target 的集数；这个区间为空
    （比如 2.5 小时：2 集=75 分钟超上限、3 集=50 分钟低于下限）时，**以上限为准**，
    用 ceil(total/max)，宁可短一点也不留一个超过上限的大文件。
    """
    if total <= max_ep:
        return 1
    n_min = max(2, int(-(-total // max_ep)))       # ceil(total / max_ep)
    n_max = max(n_min, int(total // min_ep))       # floor(total / min_ep)
    best = None
    for n in range(n_min, n_max + 1):
        gap = abs(total / n - target)
        if best is None or gap < best[0]:
            best = (gap, n)
    return best[1]


def find_silences(path: Path, start: float, length: float) -> list:
    """在 [start, start+length] 里找静音，返回 [(中点秒, 静音时长)]（绝对时间）。"""
    proc = subprocess.run(
        [get_ffmpeg(), '-hide_banner', '-nostats', '-ss', f'{max(0.0, start):.3f}',
         '-t', f'{length:.3f}', '-i', str(path),
         '-af', f'silencedetect=noise={NOISE_DB}:d={MIN_SIL:g}', '-f', 'null', '-'],
        capture_output=True, text=True, encoding='utf-8', errors='replace')
    text = (proc.stderr or '') + (proc.stdout or '')
    starts = [float(m) for m in re.findall(r'silence_start:\s*([0-9.]+)', text)]
    ends = [float(m) for m in re.findall(r'silence_end:\s*([0-9.]+)', text)]
    base = max(0.0, start)
    out = []
    for i, s in enumerate(starts):
        if i >= len(ends):
            continue
        out.append((base + (s + ends[i]) / 2.0, ends[i] - s))
    return out


def plan_cuts(path: Path, total: float, n: int, window: float = WINDOW) -> list:
    """返回 [(切点秒, 该处静音时长；0 表示没找到静音、硬切)]。"""
    cuts = []
    for k in range(1, n):
        ideal = total * k / n
        lo = max(1.0, ideal - window)
        sil = find_silences(path, lo, min(window * 2, total - lo))
        if sil:
            mid, dur = min(sil, key=lambda p: abs(p[0] - ideal))
            cuts.append((mid, dur))
        else:
            cuts.append((ideal, 0.0))
    return cuts


def fmt(sec: float) -> str:
    m, s = divmod(int(round(sec)), 60)
    h, m = divmod(m, 60)
    return f'{h}:{m:02d}:{s:02d}'


def split_file(src: Path, out_dir: Path = None, prefix: str = '',
               target: float = EPISODE_SECONDS, window: float = WINDOW,
               min_ep: float = EPISODE_MIN_SECONDS,
               max_ep: float = EPISODE_MAX_SECONDS,
               delete_source: bool = True) -> list:
    """把 src 切成若干集，返回生成的文件路径列表。

    集数为 1（时长不够）时原样返回 [src]，不做任何破坏性操作。
    总时长核对不上（差 > 2 秒）时保留原文件并抛错。
    """
    src = Path(src)
    total = probe_duration(src)
    n = plan_count(total, target, min_ep, max_ep)
    if n <= 1:
        return [src]
    cuts = plan_cuts(src, total, n, window)
    bounds = [0.0] + [c[0] for c in cuts] + [total]
    out_dir = Path(out_dir) if out_dir else src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    prefix = prefix or src.stem
    made = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        dst = out_dir / f'{prefix} 第{i + 1:02d}集.mp3'
        subprocess.run(
            [get_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y',
             '-ss', f'{a:.3f}', '-i', str(src), '-t', f'{b - a:.3f}',
             '-c', 'copy', '-map', '0:a:0', str(dst)],
            check=True)
        made.append(dst)
    got = sum(probe_duration(p) for p in made)
    if abs(got - total) > 2.0:
        raise RuntimeError(f'分集总时长对不上（{fmt(got)} vs 源 {fmt(total)}），已保留原文件')
    if delete_source:
        src.unlink()
    return made
