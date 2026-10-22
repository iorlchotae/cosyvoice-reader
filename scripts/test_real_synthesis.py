"""端到端冒烟测试：加载模型并合成一小段语音。

用法：
    .venv\\Scripts\\python scripts\\test_real_synthesis.py

成功时输出 REAL_SYNTHESIS_OK，并在 outputs/_smoke_test/ 生成 WAV。
若已存在声音档案，用第一个档案测试；否则用临时生成的参考音频跑通
零样本链路（结果语音内容无意义，仅验证流程）。
"""

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from backends.cosyvoice3 import get_backend  # noqa: E402
from config import MODEL_DIR, OUTPUT_DIR  # noqa: E402

TEST_TEXT = '八百标兵奔北坡，北坡炮兵并排跑，炮兵怕把标兵碰，标兵怕碰炮兵炮。'


def make_dummy_prompt(path: Path, seconds: float = 3.0) -> Path:
    """生成 3 秒 16kHz 参考音频（正弦 + 噪声），仅用于跑通链路。"""
    sr = 16000
    t = np.arange(int(sr * seconds)) / sr
    tone = 0.3 * np.sin(2 * np.pi * 180 * t)
    noise = 0.02 * np.random.randn(len(t))
    sf.write(str(path), (tone + noise).astype(np.float32), sr)
    return path


def main() -> int:
    print(f'模型目录：{MODEL_DIR}')
    t0 = time.time()
    backend = get_backend()
    print(f'模型加载成功（{time.time() - t0:.1f}s），采样率 {backend.sample_rate}')
    print('已注册音色：', backend.spk_ids or '（无）')

    out = OUTPUT_DIR / '_smoke_test'
    out.mkdir(parents=True, exist_ok=True)

    if backend.spk_ids:
        spk_id = backend.spk_ids[0]
        print(f'使用声音档案 [{spk_id}] 合成')
        segments = backend.synthesize(TEST_TEXT, spk_id=spk_id, speed=1.0)
    else:
        print('无声音档案，使用临时参考音频跑通零样本链路')
        tmp = out / '_dummy_prompt.wav'
        make_dummy_prompt(tmp)
        segments = backend.synthesize(TEST_TEXT, prompt_text='你好。',
                                      prompt_wav=str(tmp), speed=1.0)

    if not segments:
        print('FAIL: 没有生成任何音频段')
        return 1

    total = 0
    for i, seg in enumerate(segments):
        wav = out / f'smoke_{i:03d}.wav'
        backend.write_wav(seg, wav, backend.sample_rate)
        total += len(seg) / backend.sample_rate
        print(f'  段 {i}: {wav.name}  {len(seg) / backend.sample_rate:.2f}s')

    print(f'共 {len(segments)} 段，合计 {total:.2f}s，输出目录：{out}')
    print('REAL_SYNTHESIS_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
