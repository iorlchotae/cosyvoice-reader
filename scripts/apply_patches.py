#!/usr/bin/env python3
"""CosyVoice 3.0 Windows 原生部署补丁。

用法（在项目根目录执行）：
    .venv\\Scripts\\python scripts\\apply_patches.py

脚本幂等：重复执行不会重复打补丁。
"""

import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
COSYVOICE = PROJECT / "CosyVoice"
MARK = "PATCHED-BY-HANAKO"

FILE_UTILS = COSYVOICE / "cosyvoice" / "utils" / "file_utils.py"
FRONTEND = COSYVOICE / "cosyvoice" / "cli" / "frontend.py"

# ---- file_utils.py：补 soundfile import + load_wav 改 SoundFile（绕开 TorchCodec）----
FILE_UTILS_IMPORT_OLD = "import torchaudio\nimport logging\n"
FILE_UTILS_IMPORT_NEW = "import torchaudio\nimport soundfile as sf\nimport logging\n"

LOAD_WAV_OLD = '''def load_wav(wav, target_sr, min_sr=16000):
    speech, sample_rate = torchaudio.load(wav, backend='soundfile')
    speech = speech.mean(dim=0, keepdim=True)
    if sample_rate != target_sr:
        assert sample_rate >= min_sr, 'wav sample rate {} must be greater than {}'.format(sample_rate, target_sr)
        speech = torchaudio.transforms.Resample(orig_freq=sample_rate, new_freq=target_sr)(speech)
    return speech
'''

LOAD_WAV_NEW = '''def load_wav(wav, target_sr, min_sr=16000):
    """PATCHED-BY-HANAKO: 使用 SoundFile 读取本地 WAV，绕开 TorchCodec。

    TorchAudio 2.9+ 会把 torchaudio.load() 强制路由到 TorchCodec，
    即使传 backend='soundfile' 也会报 ImportError，这里改用 SoundFile 读取。
    """
    if isinstance(wav, torch.Tensor):
        speech = wav.detach().cpu().float()
        if speech.ndim == 1:
            speech = speech.unsqueeze(0)
        sample_rate = 16000
    else:
        audio, sample_rate = sf.read(str(wav), dtype='float32', always_2d=True)
        if audio.size == 0:
            raise ValueError('empty audio file: {}'.format(wav))
        speech = torch.from_numpy(audio.T).float()
    speech = speech.mean(dim=0, keepdim=True)
    if sample_rate != target_sr:
        assert sample_rate >= min_sr, 'wav sample rate {} must be greater than {}'.format(sample_rate, target_sr)
        speech = torchaudio.functional.resample(speech, sample_rate, target_sr)
    return speech
'''

# ---- frontend.py：预加载 CUDA DLL（复用 PyTorch 自带的 CUDA/cuDNN）----
FRONTEND_OLD = "import onnxruntime\nimport torch\n"
FRONTEND_NEW = """import onnxruntime
# PATCHED-BY-HANAKO: 复用 PyTorch 自带的 CUDA/cuDNN 库，避免 onnxruntime
# CUDAExecutionProvider 在 Windows 上找不到动态库。
if hasattr(onnxruntime, 'preload_dlls'):
    onnxruntime.preload_dlls(directory='')
import torch
"""


def patch_file(path: Path, old: str, new: str, label: str, already_patched: str | None = None) -> str:
    text = path.read_text(encoding="utf-8")
    if already_patched and already_patched in text:
        return f"[跳过] {path.name}（已打过补丁）"
    if MARK in text:
        return f"[跳过] {path.name}（已打过补丁）"
    if old not in text:
        return f"[警告] {path.name}：未找到待替换原文，请人工核对"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")
    return f"[完成] {label} → {path.name}"


def main() -> int:
    results = []
    results.append(patch_file(FILE_UTILS, FILE_UTILS_IMPORT_OLD, FILE_UTILS_IMPORT_NEW,
                              "file_utils.py 补 soundfile import",
                              already_patched="import soundfile as sf"))
    results.append(patch_file(FILE_UTILS, LOAD_WAV_OLD, LOAD_WAV_NEW,
                              "file_utils.load_wav 改 SoundFile",
                              already_patched="sf.read(str(wav), dtype='float32'"))
    results.append(patch_file(FRONTEND, FRONTEND_OLD, FRONTEND_NEW,
                              "frontend.py 预加载 CUDA DLL",
                              already_patched="onnxruntime.preload_dlls(directory='')"))
    print("\n".join(results))
    print("全部补丁就位。" if any(r.startswith("[完成]") for r in results) else "没有新的补丁需要应用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
