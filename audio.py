"""音频处理：WAV 合并为 MP3、格式转换与验证。

依赖 ffmpeg / ffprobe（8.x，Windows 本机已装）。可用环境变量
FFMPEG_BIN / FFPROBE_BIN 覆盖可执行文件路径。
"""

import json
import os
import subprocess
import tempfile
from pathlib import Path

import numpy as np
import soundfile as sf

from config import get_ffmpeg

BITRATE = '192k'


def _ffprobe() -> str:
    return os.environ.get('FFPROBE_BIN', 'ffprobe')


def _run(cmd: list) -> None:
    proc = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                          errors='replace')
    if proc.returncode != 0:
        err = proc.stderr or ''
        lines = [l for l in err.splitlines()
                 if 'Error' in l or 'error' in l or 'Impossible' in l]
        detail = '\n'.join(lines[-5:]) or err[-800:]
        raise RuntimeError(f'ffmpeg failed: {" ".join(cmd)}\n{detail}')


def wav_duration(path) -> float:
    """返回 WAV 时长（秒），用 soundfile 读取（快速，不依赖 ffmpeg）。"""
    with sf.SoundFile(str(path)) as f:
        return f.frames / f.samplerate


def mp3_duration(path) -> float:
    """返回 MP3 时长（秒），用 ffprobe。"""
    cmd = [_ffprobe(), '-v', 'quiet', '-print_format', 'json',
           '-show_format', str(path)]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                         errors='replace').stdout
    try:
        return float(json.loads(out)['format']['duration'])
    except (KeyError, ValueError, json.JSONDecodeError):
        return -1.0


def to_mono_16k_wav(src, dst) -> None:
    """转成 16kHz 单声道 PCM WAV（声音档案参考音频标准格式）。"""
    dst = Path(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    _run([get_ffmpeg(), '-y', '-i', str(src), '-ar', '16000', '-ac', '1',
          '-c:a', 'pcm_s16le', str(dst)])


# --- 参考音频裁剪 ---------------------------------------------------------
#
# CosyVoice 的零样本参考音频有一条硬上限：超过 30 秒，`_extract_speech_token`
# 会直接 assert 失败（错误文案还留在上游里）。而用户录一段旁白经常是几分钟，
# 直接硬切又会让「逐字稿」和音频错位——所以这里做两件事：
#   1) 按 20ms 帧的 RMS 找一段**自然停顿**，切在停顿正中间，不切在半个字上；
#   2) 交给调用方（voice_profiles）把逐字稿也裁到同一处。

def find_pause_cut(src, target: float = 29.4, min_cut: float = 18.0,
                   min_silence: float = 0.22) -> float:
    """在 target 之前找一个自然停顿点，返回建议的裁剪秒数。

    阈值跟着这条录音自己的底噪走（取 5% 分位的 dB 再抬 10dB），
    所以安静棚录和带底噪的手机录音都能用同一套逻辑。
    找不到合适停顿（或者停顿落在 min_cut 之前）就退回 target 硬切。
    """
    data, sr = sf.read(str(src), dtype='float32', always_2d=True)
    mono = data.mean(axis=1).astype(np.float64)
    win = max(1, int(sr * 0.02))
    hop = max(1, int(sr * 0.01))
    if len(mono) < win * 2:
        return float(target)

    # 用前缀和算滑动能量（O(n)）：直接对 2.8M 采样做卷积会慢到几秒
    csum = np.concatenate(([0.0], np.cumsum(mono * mono)))
    starts = np.arange(0, len(mono) - win + 1, hop)
    rms = np.sqrt(np.maximum((csum[starts + win] - csum[starts]) / win, 0.0))
    db = 20.0 * np.log10(rms + 1e-9)

    floor = float(np.percentile(db, 5))
    loud = float(np.percentile(db, 90))
    thr = min(floor + 10.0, loud - 18.0)
    silent = db < thr

    best = None                     # (中点秒, 静音时长)
    n = len(silent)
    i = 0
    while i < n:
        if not silent[i]:
            i += 1
            continue
        j = i
        while j + 1 < n and silent[j + 1]:
            j += 1
        dur = (j - i + 1) * hop / sr
        mid = starts[i] / sr + dur / 2.0      # 停顿正中间（起点 + 半段时长）
        if dur >= min_silence and min_cut <= mid <= target:
            if best is None or mid > best[0]:   # 取最靠近 target 的那段停顿
                best = (mid, dur)
        i = j + 1
    return float(best[0]) if best else float(target)


def trim_wav(src, seconds: float, dst=None) -> float:
    """把 WAV 截到前 seconds 秒（默认原地覆盖），返回截断后的时长。"""
    src, dst = Path(src), Path(dst) if dst else Path(src)
    with sf.SoundFile(str(src)) as f:
        sr = f.samplerate
        data = f.read(int(round(seconds * sr)), dtype='float32', always_2d=True)
    tmp = dst.with_suffix('.part' + dst.suffix)
    sf.write(str(tmp), data, sr, subtype='PCM_16')
    os.replace(tmp, dst)
    return len(data) / float(sr)


def merge_wavs_to_mp3(wav_paths, mp3_path, expect_seconds=None,
                      tolerance=0.08) -> float:
    """按顺序合并 WAV 为 MP3，返回实际时长（秒）。

    wav_paths    : 有序的 WAV 路径列表（同采样率 PCM）
    mp3_path     : 输出 MP3 路径
    expect_seconds: 预期总时长，合并后验证（容差 tolerance）
    """
    wav_paths = [str(p) for p in wav_paths]
    if not wav_paths:
        raise ValueError('没有可合并的音频')
    mp3_path = Path(mp3_path)
    mp3_path.parent.mkdir(parents=True, exist_ok=True)

    if len(wav_paths) == 1:
        _run([get_ffmpeg(), '-y', '-i', wav_paths[0],
              '-c:a', 'libmp3lame', '-b:a', BITRATE, str(mp3_path)])
    else:
        with tempfile.NamedTemporaryFile('w', suffix='.txt', delete=False,
                                         encoding='utf-8') as f:
            for p in wav_paths:
                # concat demuxer 对相对路径按 list 文件所在目录解析，必须写绝对路径
                escaped = str(Path(p).resolve()).replace('\\', '/').replace("'", "'\\''")
                f.write(f"file '{escaped}'\n")
            list_file = f.name
        try:
            _run([get_ffmpeg(), '-y', '-f', 'concat', '-safe', '0',
                  '-i', list_file, '-c:a', 'libmp3lame', '-b:a', BITRATE,
                  str(mp3_path)])
        finally:
            try:
                os.remove(list_file)
            except OSError:
                pass

    duration = mp3_duration(mp3_path)
    if expect_seconds is not None and duration > 0:
        expect = sum(wav_duration(p) for p in wav_paths)
        if abs(expect - duration) / expect > tolerance:
            raise RuntimeError(
                f'MP3 合并验证失败：预期 {expect:.1f}s，实际 {duration:.1f}s')
    return duration
