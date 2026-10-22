"""成品分集的确定性测试（不需要模型、不需要 GPU）。

跑法：
    $env:PYTHONIOENCODING='utf-8'
    .\.venv\Scripts\python.exe -u scripts\test_episode_split.py

覆盖：
  1. plan_count：短文件不切、长文件按「一小时左右」均衡切、每集落在 55–72 分钟；
  2. 用 ffmpeg 造一段「9 秒音 + 1 秒静音」的音频，确认切点真的落在静音区；
  3. 切完总时长与源一致、源文件按约定删除、文件名是「第NN集」；
  4. 时长不够时不切、不删源；
  5. 明确传 target/min/max 时按传入值切（界面上的 30/60/90 分钟就是这么走的）。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import episode  # noqa: E402
from config import get_ffmpeg  # noqa: E402

OK = 0
BAD = 0


def check(cond, msg):
    global OK, BAD
    if cond:
        OK += 1
        print(f'  ok   {msg}')
    else:
        BAD += 1
        print(f'  FAIL {msg}')


def make_tone(path: Path, seconds: float) -> None:
    """9 秒 440Hz + 1 秒静音，循环；每 10 秒有一个确定的静音点。"""
    subprocess.run(
        [get_ffmpeg(), '-hide_banner', '-loglevel', 'error', '-y',
         '-f', 'lavfi',
         '-i', f"aevalsrc=sin(2*PI*440*t)*lt(mod(t\\,10)\\,9):s=16000:d={seconds:g}",
         '-c:a', 'libmp3lame', '-b:a', '64k', str(path)],
        check=True)


def main() -> int:
    print('[1] plan_count 分集数')
    check(episode.plan_count(30 * 60) == 1, '30 分钟 → 1 集（不切）')
    check(episode.plan_count(70 * 60) == 1, '70 分钟 → 1 集（还在 72 分钟内）')
    n2 = episode.plan_count(130 * 60)
    check(n2 == 2, f'130 分钟 → {n2} 集')
    check(60 * 60 <= 130 * 60 / n2 <= 72 * 60, '130 分钟切完每集在 60–72 分钟内')
    n7 = episode.plan_count(6.8 * 3600)
    check(n7 == 7, f'6.8 小时 → {n7} 集（山音的实际时长）')
    check(55 * 60 <= 6.8 * 3600 / n7 <= 72 * 60, '6.8 小时切完每集落在 55–72 分钟')
    n24 = episode.plan_count(24 * 3600)
    check(n24 == 24, f'24 小时 → {n24} 集')
    for total in (2.5 * 3600, 3.2 * 3600, 9 * 3600, 15.5 * 3600):
        n = episode.plan_count(total)
        per = total / n
        # 上限是硬约束（不留超长文件）；下限是软的，区间冲突时允许偏短
        check(n == 1 or 50 * 60 <= per <= 72 * 60 + 1,
              f'{total / 3600:.1f} 小时 → {n} 集，每集 {per / 60:.1f} 分钟')
    check(episode.plan_count(2.5 * 3600) == 3,
          '2.5 小时这种切不开的时长，按上限优先切成 3 集（50 分钟）')

    tmp = Path(tempfile.mkdtemp(prefix='epsplit'))
    try:
        print('[2] 切点必须落在静音区（9 秒音 + 1 秒静音）')
        src = tmp / 'book.mp3'
        make_tone(src, 610)            # 10 分 10 秒
        total = episode.probe_duration(src)
        check(abs(total - 610) < 2, f'造出来的音频 {total:.1f} 秒')
        # 每集 3 分钟、允许 2–4 分钟，切点窗口 60 秒
        n = episode.plan_count(total, 180, 120, 240)
        check(n == 3, f'按 3 分钟切 → {n} 集')
        cuts = episode.plan_cuts(src, total, n, window=60)
        check(len(cuts) == 2, f'得到 {len(cuts)} 个切点')
        for i, (pos, sil) in enumerate(cuts, 1):
            check(sil > 0, f'第 {i} 个切点落在静音里（静音 {sil:.2f}s，位置 {pos:.1f}s）')
            # 静音在每 10 秒的最后一秒：切点应落在 (k*10-1, k*10) 区间
            check((pos % 10) > 8.0, f'第 {i} 个切点在周期静音段内（{pos % 10:.2f}s）')

        print('[3] 真的切：时长对得上、源文件删除、命名正确')
        made = episode.split_file(src, out_dir=tmp / 'out', prefix='书',
                                  target=180, window=60,
                                  min_ep=120, max_ep=240, delete_source=True)
        check(len(made) == 3, f'生成 {len(made)} 个文件')
        check(all(p.exists() for p in made), '文件都在磁盘上')
        check([p.name for p in made] == ['书 第01集.mp3', '书 第02集.mp3', '书 第03集.mp3'],
              f'文件名：{[p.name for p in made]}')
        got = sum(episode.probe_duration(p) for p in made)
        check(abs(got - total) < 2, f'分集总时长 {got:.1f}s ≈ 源 {total:.1f}s')
        check(not src.exists(), '源文件已删除')

        print('[4] 时长不够时不动手')
        short = tmp / 'short.mp3'
        make_tone(short, 40)
        out = episode.split_file(short, out_dir=tmp / 'out2', prefix='短',
                                 delete_source=True)
        check(out == [short], '返回原文件本身，没有切')
        check(short.exists(), '短文件没有被删')

        print('[5] 界面上的 30/90 分钟档位')
        src2 = tmp / 'book2.mp3'
        make_tone(src2, 610)
        made2 = episode.split_file(src2, out_dir=tmp / 'out3', prefix='书2',
                                   target=300, window=60, min_ep=240, max_ep=360,
                                   delete_source=False)
        check(len(made2) == 2, f'target=300s、min/max=240/360 → {len(made2)} 集')
        check(src2.exists(), 'delete_source=False 时源文件保留')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print(f'\n通过 {OK} 项，失败 {BAD} 项')
    print('EPISODE_SPLIT_OK' if BAD == 0 else 'EPISODE_SPLIT_FAILED')
    return 0 if BAD == 0 else 1


if __name__ == '__main__':
    raise SystemExit(main())
