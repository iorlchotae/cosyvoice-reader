"""多角色合成冒烟测试：注册临时音色，按 [角色] 标记合成一段并合并 MP3。

用法：
    .venv\\Scripts\\python scripts\\test_multi_role.py

成功输出 MULTI_ROLE_OK，结果在 outputs/_multi_role_test/。
测试用的临时音色（__t_ 前缀）会在结束后自动清理。
"""

import sys
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from backends.cosyvoice3 import get_backend  # noqa: E402
from document import parse_roles  # noqa: E402
from audio import merge_wavs_to_mp3  # noqa: E402
from config import OUTPUT_DIR  # noqa: E402

TEST_TEXT = '''[旁白] 夜色渐深，他推开门，屋里点着一盏昏黄的灯。
[张三] 你来了，我等了你好久。
[李四] 路上耽搁了，抱歉。
[旁白] 张三的话音未落，窗外忽然闪过一道影子。'''


def make_prompt(path: Path, freq: float) -> Path:
    """生成 3 秒 16kHz 参考音频（不同频率正弦，模拟不同音色）。"""
    sr = 16000
    t = np.arange(int(sr * 3)) / sr
    audio = 0.3 * np.sin(2 * np.pi * freq * t) + 0.02 * np.random.randn(len(t))
    sf.write(str(path), audio.astype(np.float32), sr)
    return path


def main() -> int:
    backend = get_backend()
    print(f'模型就绪，采样率 {backend.sample_rate}')

    tmp = OUTPUT_DIR / '_multi_role_test'
    tmp.mkdir(parents=True, exist_ok=True)

    # 注册三个临时音色（旁白/张三/李四）
    specs = [('__t_旁白', 100), ('__t_张三', 150), ('__t_李四', 260)]
    prompts = {}
    for sid, freq in specs:
        wav = make_prompt(tmp / f'{sid}.wav', freq)
        if sid not in backend.spk_ids:
            backend.register_spk(sid, '你好。', str(wav))
        prompts[sid] = wav
    print('临时音色已注册：', [s for s in backend.spk_ids if s.startswith('__t_')])

    units = parse_roles(TEST_TEXT)
    print(f'解析 {len(units)} 段：{[r for r, _ in units]}')
    role_spk = {'旁白': '__t_旁白', '张三': '__t_张三', '李四': '__t_李四'}

    all_wavs = []
    for i, (role, text) in enumerate(units, 1):
        spk = role_spk.get(role, '__t_旁白')
        segs = backend.synthesize(text, spk_id=spk, speed=1.0)
        print(f'  [{role}] → {spk}，{len(segs)} 句')
        for si, seg in enumerate(segs):
            wav = tmp / f'seg_{i:02d}_{si:02d}.wav'
            backend.write_wav(seg, wav, backend.sample_rate)
            all_wavs.append(wav)

    mp3 = tmp / 'multi_role.mp3'
    dur = merge_wavs_to_mp3(all_wavs, mp3)
    print(f'MULTI_ROLE_OK: {len(all_wavs)} 段 → {mp3.name}（{dur:.1f}s）')

    for sid in list(backend.spk_ids):
        if sid.startswith('__t_'):
            backend.remove_spk(sid)
    print('临时音色已清理')
    return 0


if __name__ == '__main__':
    sys.exit(main())
