"""模型自动就位：检测 Fun-CosyVoice3-0.5B 权重，缺了就自动下载。

借鉴 folivox（Apache-2.0，src/folivox/tts/weights.py）的思路：
- 只下载真正需要的文件（跳过 TensorRT 导出、batch tokenizer、llm.rl.pt，
  三者合计约 4.3 GB，本项目的推理链路用不到）
- 下载进度用一个轮询线程按"目标目录里已有多少字节"计量，配合
  平滑速度与剩余时间；modelscope 的 ._____temp 分块文件天然支持
  断点续传，失败重试会接着上次继续
- 按大小校验文件完整性（空文件/半截文件一律视为缺失，重新下载）

下载源：默认 ModelScope（国内快），HuggingFace 作为备选；
环境变量 COSYVOICE_MODEL_SOURCE=modelscope|huggingface 可指定。
"""

from __future__ import annotations

import fnmatch
import threading
import time
from collections.abc import Callable
from pathlib import Path

REPO_ID = 'FunAudioLLM/Fun-CosyVoice3-0.5B-2512'   # ModelScope 与 HuggingFace 同名

# 推理必需的文件（存在且非空即视为完整）
REQUIRED = [
    'cosyvoice3.yaml',
    'llm.pt',
    'flow.pt',
    'hift.pt',
    'campplus.onnx',
    'speech_tokenizer_v3.onnx',
    'CosyVoice-BlankEN/config.json',
    'CosyVoice-BlankEN/model.safetensors',
    'CosyVoice-BlankEN/vocab.json',
    'CosyVoice-BlankEN/merges.txt',
    'CosyVoice-BlankEN/tokenizer_config.json',
]

# 允许下载的文件集合（ModelScope 用 allow_file_pattern，HF 用 allow_patterns）
ALLOW = [
    'cosyvoice3.yaml', 'config.json', 'configuration.json',
    'llm.pt', 'flow.pt', 'hift.pt',
    'campplus.onnx', 'speech_tokenizer_v3.onnx',
    'CosyVoice-BlankEN/*', 'README.md',
]

# pinned 权重的实测大小（字节）：用来显示百分比与剩余时间；
# 上游若重新发布导致对不上，也只是百分比显示偏差，不影响下载正确性
EXPECTED_TOTAL = 5_410_000_000   # ≈5.4 GB（llm 2.02 + flow 1.33 + BlankEN 0.95 + tokenizer 0.97 + 其余）

PollHook = Callable[[str, int, int], None]   # (文件名/阶段名, 已下载字节, 总字节；总 0 = 未知)


def missing_files(model_dir: Path) -> list[str]:
    """缺失/为空的必需文件清单；空列表 = 权重就位。"""
    missing = []
    for rel in REQUIRED:
        p = Path(model_dir) / rel
        try:
            if not (p.is_file() and p.stat().st_size > 0):
                missing.append(rel)
        except OSError:
            missing.append(rel)
    return missing


def needs_download(model_dir: Path) -> bool:
    return bool(missing_files(model_dir))


class DownloadMeter:
    """按最近约10秒样本平滑的速度与剩余时间。"""

    WINDOW = 10.0

    def __init__(self):
        self.samples: list[tuple[float, int]] = []

    def update(self, done: int, now: float) -> tuple[float, int | None]:
        self.samples.append((now, done))
        while len(self.samples) > 2 and now - self.samples[0][0] > self.WINDOW:
            self.samples.pop(0)
        t0, d0 = self.samples[0]
        speed = (done - d0) / (now - t0) if now > t0 and done >= d0 else 0.0
        eta = int((EXPECTED_TOTAL - done) / speed) if speed > 0 and done < EXPECTED_TOTAL else None
        return speed, eta


def _dir_bytes(model_dir: Path) -> int:
    """目标目录里已完成 + 下载中的字节数（含 modelscope 的 ._____temp 分块）。"""
    total = 0
    root = Path(model_dir)
    if not root.exists():
        return 0
    for p in root.rglob('*'):
        if not p.is_file():
            continue
        try:
            rel = p.relative_to(root).as_posix()
        except ValueError:
            continue
        if rel.startswith('._____temp') or rel.endswith(('.tmp', '.part', '.incomplete')):
            total += p.stat().st_size          # 半截文件也算进度
        elif any(fnmatch.fnmatch(rel, pat) for pat in ALLOW):
            total += p.stat().st_size
        # 其余（无关文件）不计
    return total


def _watch(model_dir: Path, hook: PollHook, stop: threading.Event) -> None:
    """轮询目录字节量并回报进度；每秒一次。"""
    meter = DownloadMeter()
    last_bytes = -1
    while True:
        done = _dir_bytes(model_dir)
        if done != last_bytes:
            last_bytes = done
            speed, eta = meter.update(done, time.time())
            pct = min(99, int(done / EXPECTED_TOTAL * 100)) if EXPECTED_TOTAL else 0
            detail = f'{done / 1e9:.1f} GB'
            if EXPECTED_TOTAL:
                detail += f' / {EXPECTED_TOTAL / 1e9:.1f} GB'
            if speed > 0:
                detail += f' · {speed / 1e6:.1f} MB/s'
            if eta:
                detail += f' · 剩余约 {eta // 60}分{eta % 60:02d}秒'
            hook(f'模型下载 {pct}% · {detail}', done, EXPECTED_TOTAL)
        if stop.wait(1.0):
            return


def download_missing(model_dir: Path, hook: PollHook | None = None,
                     source: str | None = None, attempts: int = 4) -> None:
    """把缺失的必需权重下载到 model_dir；全部就位后返回。

    hook(detail, done, total) 每秒收到一次进度。下载中断后重跑会
    从断点继续（modelscope 的 ._____temp 分块文件）。
    """
    import os

    model_dir = Path(model_dir)
    model_dir.mkdir(parents=True, exist_ok=True)
    source = (source or os.environ.get('COSYVOICE_MODEL_SOURCE') or 'modelscope').lower()

    stop = threading.Event()
    watcher = None
    if hook is not None:
        watcher = threading.Thread(target=_watch, args=(model_dir, hook, stop),
                                   daemon=True, name='model-download-watch')
        watcher.start()
    try:
        last_err: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                if source == 'huggingface':
                    from huggingface_hub import snapshot_download
                    snapshot_download(REPO_ID, local_dir=str(model_dir),
                                      allow_patterns=ALLOW)
                else:
                    from modelscope import snapshot_download
                    snapshot_download(REPO_ID, local_dir=str(model_dir),
                                      allow_file_pattern=ALLOW)
                break
            except ImportError:
                raise RuntimeError(
                    f'缺少下载依赖：pip install modelscope==1.20.0'
                    + (' 或 huggingface_hub' if source != 'modelscope' else ''))
            except Exception as e:  # noqa: BLE001 — 网络/超时/5xx，可重试
                last_err = e
                if attempt == attempts:
                    raise RuntimeError(
                        f'模型下载失败（已重试 {attempts} 次）：{e}　'
                        '请检查网络后重试；已下载的部分会保留，重跑接着继续') from e
                time.sleep(min(5 * 2 ** (attempt - 1), 60))
    finally:
        stop.set()
        if watcher is not None:
            watcher.join(timeout=3)

    still = missing_files(model_dir)
    if still:
        raise RuntimeError(f'下载后仍缺少文件：{still[:4]}……权重不完整，请重试')
