"""篇内按段并发 的确定性测试（**不加载模型、不碰显卡**）。

跑法（PYTHONIOENCODING 必须设，否则 PowerShell 控制台会把中文打乱）：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u scripts\\test_unit_parallel.py

全绿时打印 UNIT_PARALLEL_OK。

测的是 backends/synthesize.py 里「多个实例各领一段、提交线程按段序接表」这条路。
要点只有三个：
  1. 真的同时跑起来了（峰值并发 = 2，且两个实例都用上了）；
  2. 合并时的段序仍然是 1,2,3...（并发不能把顺序打乱）；
  3. 中途停下拿到的半成品是**连续前缀**，续传能接着补齐。
"""

import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audio as audio_mod                      # noqa: E402
import backends.synthesize as synth            # noqa: E402

TMP = ROOT / '.tmp' / 'unitparallel'
UNIT_SECONDS = 1.0
SR = 24000

failures = []


def check(label, cond, extra=''):
    print(('  [OK]   ' if cond else '  [FAIL] ') + label
          + (f'  <- {extra}' if extra else ''))
    if not cond:
        failures.append(label)


# --- 假件 ------------------------------------------------------------------

class FakeSeg:
    def __init__(self, seconds):
        self.seconds = seconds

    def __len__(self):
        return int(self.seconds * SR)


CONC = {'live': 0, 'peak': 0, 'lock': threading.Lock()}


class FakeBackend:
    """记下自己被用过几次；synthesize 里故意留一点睡眠，让并发真的重叠。"""
    sample_rate = SR

    def __init__(self, name, spk='周报', sleep=0.02):
        self.name = name
        self.spk_ids = [spk]
        self.sleep = sleep
        self.used = 0

    def register_spk(self, spk_id, prompt_text, prompt_wav):
        self.spk_ids.append(spk_id)
        return True

    def synthesize(self, text, spk_id='', speed=1.0):
        with CONC['lock']:
            CONC['live'] += 1
            CONC['peak'] = max(CONC['peak'], CONC['live'])
            self.used += 1
        try:
            time.sleep(self.sleep)
            return [FakeSeg(UNIT_SECONDS)]
        finally:
            with CONC['lock']:
                CONC['live'] -= 1

    def write_wav(self, seg, path, sr):
        path = Path(path)
        if path.suffix.lower() != '.wav':
            raise TypeError(
                'No format specified and unable to get format from file '
                f'extension: {path}')
        path.write_bytes(b'RIFF' + b'\x00' * 32)


class CountCtl:
    """前 allowed 次段边界检查返回 False，之后返回 True（模拟「停止」）。

    计数由 worker 在每段开头调用，因此是确定性的：允许念多少段就念多少段。
    """
    def __init__(self, allowed=None):
        self.allowed = allowed
        self.seen = 0
        self.lock = threading.Lock()

    def wait_if_paused(self):
        with self.lock:
            self.seen += 1
            n = self.seen
        return self.allowed is not None and n > self.allowed


MERGES = []


def _fake_merge(wav_paths, mp3_path):
    names = [Path(p).name for p in wav_paths]
    row = len(MERGES)
    if row < 200:
        MERGES.append(names)
    Path(mp3_path).write_bytes(b'MP3')
    return len(names) * UNIT_SECONDS


audio_mod.wav_duration = lambda p: UNIT_SECONDS
audio_mod.merge_wavs_to_mp3 = _fake_merge


def make_doc(tag, units=8):
    d = TMP / tag
    if d.exists():
        import shutil
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    p = d / 'book.txt'
    p.write_text('\n\n'.join('啊' * 1200 for _ in range(units)), encoding='utf-8')
    return p, d / 'out'


def seq_ok(names):
    """合并进 MP3 的段文件名必须严格是 0001_0000.wav, 0002_0000.wav …"""
    return names == [f'{i:04d}_0000.wav' for i in range(1, len(names) + 1)]


# --- 1. 两个实例真的并发，且顺序不乱 ---------------------------------------

def test_parallel_runs():
    print('\n[1] 两个实例并发 + 输出顺序')
    doc, out = make_doc('p1', units=8)
    CONC.update(live=0, peak=0)
    a, b = FakeBackend('A'), FakeBackend('B')
    MERGES.clear()

    mp3, dur = synth.synthesize_core(
        a, doc, '周报', 1.0, out, extra_backends=[b])

    check('峰值并发 = 2', CONC['peak'] == 2, f"peak={CONC['peak']}")
    check('两个实例都用上了', a.used > 0 and b.used > 0,
          f'A={a.used} B={b.used}')
    check('段数 8', a.used + b.used == 8, f'A+B={a.used + b.used}')
    full = MERGES[-1] if MERGES else []
    check('完整版段序 1..8', seq_ok(full), str(full[:3]))
    check('时长 = 8 秒', abs(dur - 8 * UNIT_SECONDS) < 1e-6, str(dur))
    check('产出 mp3', mp3 is not None and Path(mp3).exists())
    done = sorted(p.name for p in (Path(mp3).parent / 'segments').glob('*.done')) \
        if (Path(mp3).parent / 'segments').is_dir() else []
    check('成功后 segments 已清理（cleanup 生效）', done == [], str(done[:3]))


# --- 2. 单实例走串行，不被改坏 ---------------------------------------------

def test_serial_unchanged():
    print('\n[2] 单实例仍走串行路径')
    doc, out = make_doc('p2', units=5)
    CONC.update(live=0, peak=0)
    a = FakeBackend('A')
    MERGES.clear()

    mp3, dur = synth.synthesize_core(a, doc, '周报', 1.0, out)

    check('峰值并发 = 1', CONC['peak'] == 1, f"peak={CONC['peak']}")
    check('5 段都念了', a.used == 5, str(a.used))
    check('段序 1..5', seq_ok(MERGES[-1] if MERGES else []), '')
    check('时长 = 5 秒', abs(dur - 5 * UNIT_SECONDS) < 1e-6, str(dur))


# --- 3. 并发下停止：半成品必须是连续前缀 -------------------------------

def test_stop_prefix():
    print('\n[3] 并发中停止 → 半成品是连续前缀')
    doc, out = make_doc('p3', units=10)
    CONC.update(live=0, peak=0)
    a, b = FakeBackend('A', sleep=0.05), FakeBackend('B', sleep=0.05)
    MERGES.clear()
    ctl = CountCtl(allowed=4)          # 只放 4 段进去，然后停止

    mp3, dur = synth.synthesize_core(
        a, doc, '周报', 1.0, out, extra_backends=[b], ctl=ctl)

    full = MERGES[-1] if MERGES else []
    check('有半成品 mp3', mp3 is not None and Path(mp3).exists())
    check('半成品段是连续前缀', seq_ok(full) and 1 <= len(full) < 10,
          f'{len(full)} 段')
    check('快照文件已生成',
          any(Path(p).name.startswith('book.部分-')
              for p in Path(mp3).parent.glob('*.mp3')), '')


# --- 4. 并发下续传：接着补齐且顺序正确 -----------------------------------

def test_resume_parallel():
    print('\n[4] 并发下续传补齐')
    doc, out = make_doc('p4', units=10)
    CONC.update(live=0, peak=0)
    a, b = FakeBackend('A', sleep=0.02), FakeBackend('B', sleep=0.02)
    ctl = CountCtl(allowed=4)
    synth.synthesize_core(a, doc, '周报', 1.0, out, extra_backends=[b], ctl=ctl)

    # 第二轮：新的两个实例，同音色同语速 → 指纹匹配，应该只补剩下的段
    a2, b2 = FakeBackend('A2'), FakeBackend('B2')
    MERGES.clear()
    mp3, dur = synth.synthesize_core(
        a2, doc, '周报', 1.0, out, extra_backends=[b2])

    check('续传只补了剩下的段', a2.used + b2.used < 10,
          f'本轮念了 {a2.used + b2.used} 段')
    full = MERGES[-1] if MERGES else []
    check('完整版段序 1..10', seq_ok(full) and len(full) == 10, str(len(full)))
    check('时长 = 10 秒', abs(dur - 10 * UNIT_SECONDS) < 1e-6, str(dur))


# --- 5. 切片边界在并发下仍然正确 -----------------------------------------

def test_parts_parallel():
    print('\n[5] 10 分钟切片在并发下仍按段序切')
    doc, out = make_doc('p5', units=12)
    a, b = FakeBackend('A', sleep=0.01), FakeBackend('B', sleep=0.01)
    MERGES.clear()
    # 每 3 段切一块（UNIT_SECONDS=1 → chunk_seconds=3）
    mp3, dur = synth.synthesize_core(
        a, doc, '周报', 1.0, out, extra_backends=[b], chunk_seconds=3,
        cleanup=False)

    Work = Path(mp3).parent
    parts = sorted(p.name for p in (Work / 'parts').glob('*.mp3'))
    check('切出 4 块', len(parts) == 4, str(parts))
    # 每块合并的段必须连续且在正确位置上
    slice_rows = [n for n in MERGES if len(n) == 3]
    check('切片合并的段都在', len(slice_rows) >= 3, f'{len(slice_rows)} 块')
    check('完整版段序 1..12', seq_ok(MERGES[-1] if MERGES else []), '')


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    test_parallel_runs()
    test_serial_unchanged()
    test_stop_prefix()
    test_resume_parallel()
    test_parts_parallel()

    print()
    if failures:
        print(f'失败 {len(failures)} 项：' + '；'.join(failures))
        print('UNIT_PARALLEL_FAILED')
        return 1
    print('全部通过')
    print('UNIT_PARALLEL_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
