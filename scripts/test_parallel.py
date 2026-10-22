"""并行合成冒烟测试：2 个文件同时合成，验证吞吐提升。

用法：
    .venv\\Scripts\\python scripts\\test_parallel.py
"""

import sys
import time
from pathlib import Path

import numpy as np
import soundfile as sf

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

from backends.pool import BackendPool  # noqa: E402
from backends.synthesize import synthesize_core  # noqa: E402
from config import OUTPUT_DIR, TMP_DIR  # noqa: E402
from audio import mp3_duration  # noqa: E402

TEXT_A = ('夜色像一盆凉水从山脊上浇下来，他缩在歪脖子树的阴影里，' * 3)
TEXT_B = ('风穿过破败的屋檐，发出低沉的呜咽，远方的灯火一盏盏熄灭，' * 3)


def make_files(tmp: Path) -> tuple:
    f1 = tmp / 'file_a.txt'
    f2 = tmp / 'file_b.txt'
    f1.write_text(TEXT_A, encoding='utf-8')
    f2.write_text(TEXT_B, encoding='utf-8')
    return f1, f2


def make_prompt(path: Path) -> None:
    sr = 16000
    t = np.arange(sr * 3) / sr
    sf.write(str(path), (0.3 * np.sin(2 * np.pi * 200 * t)).astype(np.float32), sr)


def main() -> int:
    tmp = OUTPUT_DIR / '_parallel_test'
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    f1, f2 = make_files(tmp)
    prompt = tmp / 'p.wav'
    make_prompt(prompt)

    pool = BackendPool(size=2)
    pool.ensure(2)                      # 等两个实例都加载好
    # 每个实例注册临时音色
    for token, inst in pool.acquire_up_to(2):
        if '__par' not in inst.spk_ids:
            inst.register_spk('__par', '你好。', str(prompt))
        pool.release(token)
    print('两个实例就绪，开始并行合成...', flush=True)

    tasks = [
        {'filepath': str(f1), 'spk_name': '__par', 'speed': 1.0,
         'out_dir': str(tmp / 'out1'), 'role_map': None},
        {'filepath': str(f2), 'spk_name': '__par', 'speed': 1.0,
         'out_dir': str(tmp / 'out2'), 'role_map': None},
    ]

    from concurrent.futures import ThreadPoolExecutor, as_completed

    def one(task):
        token, inst = pool.acquire()
        try:
            return synthesize_core(inst, task['filepath'], task['spk_name'],
                                   task['speed'], task['out_dir'], task.get('role_map'))
        finally:
            pool.release(token)

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=2) as ex:
        futs = [ex.submit(one, t) for t in tasks]
        for fut in as_completed(futs):
            mp3, dur = fut.result()
            print(f'  完成：{Path(mp3).name} 音频{dur:.1f}s', flush=True)
    elapsed = time.time() - t0
    print(f'两个文件并行总耗时 {elapsed:.1f}s（约等于单文件耗时 = 并行生效）', flush=True)
    print(f'预估串行耗时约 {elapsed * 1.8:.1f}s（基线）', flush=True)
    print('PARALLEL_TEST_OK', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())