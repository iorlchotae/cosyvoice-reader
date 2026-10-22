"""手工把已有的长 MP3 切成「一小时左右」的分集（补切历史成品用）。

正式流程里这件事由 `app.py` 在每篇文档合并完成后自动做（`episode.py`）；
这个脚本只是同一个函数的命令行入口，例如：

    python scripts/split_mp3_episodes.py --input "outputs\\某书\\某书.mp3" --dry-run
    python scripts/split_mp3_episodes.py --input "outputs\\某书\\某书.mp3" --delete-source
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import episode  # noqa: E402
from config import EPISODE_SECONDS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--input', required=True)
    ap.add_argument('--outdir', default='')
    ap.add_argument('--prefix', default='')
    ap.add_argument('--target', type=float, default=EPISODE_SECONDS)
    ap.add_argument('--window', type=float, default=episode.WINDOW)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--delete-source', action='store_true')
    a = ap.parse_args()

    src = Path(a.input).expanduser().resolve()
    if not src.is_file():
        print(f'找不到文件：{src}')
        return 1
    total = episode.probe_duration(src)
    n = episode.plan_count(total, a.target)
    print(f'源文件：{src.name}')
    print(f'总时长：{episode.fmt(total)}（{total / 3600:.2f} 小时，'
          f'{src.stat().st_size / 1048576:.1f} MB）')
    if n <= 1:
        print(f'不到 {int(episode.EPISODE_MAX_SECONDS / 60)} 分钟，不用切。')
        return 0
    print(f'计划切成 {n} 集，平均每集 {episode.fmt(total / n)}')
    cuts = episode.plan_cuts(src, total, n, a.window)
    bounds = [0.0] + [c[0] for c in cuts] + [total]
    for i in range(len(bounds) - 1):
        seg = bounds[i + 1] - bounds[i]
        if i < len(cuts):
            how = '硬切' if cuts[i][1] == 0 else f'静音 {cuts[i][1]:.2f}s'
        else:
            how = '结尾'
        print(f'  第{i + 1:02d}集  {episode.fmt(seg)}  ({seg / 60:.1f} 分钟)  '
              f'起点 {episode.fmt(bounds[i])}  [{how}]')
    if a.dry_run:
        print('（dry-run，未写文件）')
        return 0

    out_dir = Path(a.outdir).expanduser().resolve() if a.outdir else src.parent
    made = episode.split_file(src, out_dir=out_dir, prefix=a.prefix or src.stem,
                              target=a.target, window=a.window,
                              delete_source=a.delete_source)
    print(f'已生成 {len(made)} 个文件：')
    for p in made:
        print(f'  {p.name}  {p.stat().st_size / 1048576:.1f} MB')
    if a.delete_source and len(made) > 1:
        print(f'已删除源文件：{src.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
