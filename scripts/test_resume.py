"""断点续传 / 10 分钟检查点 的确定性测试（**不加载模型、不碰显卡**）。

跑法（PYTHONIOENCODING 必须设，否则 PowerShell 控制台会把中文打乱）：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u scripts\\test_resume.py

全绿时打印 RESUMETEST_OK。

测的是 backends/synthesize.py 的真实逻辑：只把「模型」和「音频编解码」两层换成
假的——假 backend 每段睡 0 秒、假 merge 只写一个占位文件，段的时长固定 1 秒，
所以切片边界是可算的定值。
"""

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audio as audio_mod                      # noqa: E402
import backends.synthesize as synth            # noqa: E402
import config                                  # noqa: E402

TMP = ROOT / '.tmp' / 'resumetest'
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


class FakeBackend:
    """只记「合成过哪几段」，不产生真音频。"""
    sample_rate = SR

    def __init__(self, spk='周报'):
        self.spk_ids = [spk]
        self.calls = []

    def register_spk(self, spk_id, prompt_text, prompt_wav):
        self.spk_ids.append(spk_id)
        return True

    def synthesize(self, text, spk_id='', speed=1.0):
        self.calls.append(text)
        return [FakeSeg(UNIT_SECONDS)]

    def write_wav(self, seg, path, sr):
        # 故意学 soundfile：它靠扩展名推断格式，非 .wav 结尾会直接报错。
        # 之前这里不检查，结果真机上「原子写到 xxx.wav.tmp」的 bug 溜了过去。
        path = Path(path)
        if path.suffix.lower() != '.wav':
            raise TypeError(
                'No format specified and unable to get format from file '
                f'extension: {path}')
        path.write_bytes(b'RIFF' + b'\x00' * 32)


class FakeCtl:
    """第 stop_after 段之后返回「已取消」，用来在段边界停住。"""
    def __init__(self, stop_after=None):
        self.stop_after = stop_after
        self.seen = 0

    def wait_if_paused(self):
        self.seen += 1
        return self.stop_after is not None and self.seen > self.stop_after


def _fake_merge(wav_paths, mp3_path):
    wav_paths = list(wav_paths)
    Path(mp3_path).write_bytes(b'MP3')
    return len(wav_paths) * UNIT_SECONDS


# 音频层整个换掉：时长固定、合并不编码
audio_mod.wav_duration = lambda p: UNIT_SECONDS
audio_mod.merge_wavs_to_mp3 = _fake_merge


def make_doc(tag, units=5):
    """每段是一个 1200 字的无标点长段 —— split_chunks 会原样保留成一段。"""
    d = TMP / tag
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    p = d / 'book.txt'
    p.write_text('\n\n'.join('啊' * 1200 for _ in range(units)), encoding='utf-8')
    return p


def fresh_out(name):
    """每个用例从干净的输出目录开始。

    否则被中断的上一次运行会留下 `NNNN.done` 分段，下一次 T1 就会被「续传」跳过，
    断言看到的是上次的残留而不是本用例的行为（这个坑真踩过一次）。
    """
    d = TMP / name
    shutil.rmtree(d, ignore_errors=True)
    return d


def paths(out):
    return synth.work_paths(out, 'book', create=False)


# --- 用例 ------------------------------------------------------------------

def test_stop_keeps_breakpoint():
    print('T1 停止：半成品留下、断点台账写对')
    doc, out = make_doc('t1'), fresh_out('out1')
    be = FakeBackend()
    mp3, dur = synth.synthesize_core(be, doc, '周报', 1.0, out,
                                     ctl=FakeCtl(stop_after=2), chunk_seconds=600)
    work, seg, parts = paths(out)
    check('停在段边界（只念了 2 段）', len(be.calls) == 2, f'calls={len(be.calls)}')
    check('两段的完成标记都在', len(list(seg.glob('*.done'))) == 2)
    check('返回了半成品 MP3', mp3 is not None and Path(mp3).exists())
    check('半成品时长 = 2 秒', abs(dur - 2.0) < 0.01, f'dur={dur}')
    man = synth.read_resume(work)
    check('台账 done=2 且 complete=False',
          man.get('done') == 2 and man.get('complete') is False,
          f'done={man.get("done")} complete={man.get("complete")}')
    check('台账记下了半成品文件名',
          str(man.get('partial_mp3') or '').startswith('book.部分-'),
          str(man.get('partial_mp3')))
    pr = synth.probe_resume(doc, '周报', 1.0, out)
    check('探测：可续传 2/5', pr['resumable'] and pr['units_done'] == 2, str(pr))
    return doc, out


def test_resume_only_missing(doc, out):
    print('T2 续传：只补缺的段，成品收回全部段落')
    be = FakeBackend()
    mp3, dur = synth.synthesize_core(be, doc, '周报', 1.0, out, chunk_seconds=600)
    work, seg, parts = paths(out)
    check('只合成了剩下的 3 段', len(be.calls) == 3, f'calls={len(be.calls)}')
    check('成品时长 = 5 秒（5 段都在）', abs(dur - 5.0) < 0.01, f'dur={dur}')
    check('成品文件在 <篇名>.mp3', (work / 'book.mp3').exists())
    check('台账 complete=True', synth.read_resume(work).get('complete') is True)
    check('分段目录已回收', not seg.exists())
    check('10 分钟检查点已回收', not list(parts.glob('*.mp3')))
    check('半成品快照已回收',
          not list(work.glob('book.部分-*.mp3')))
    check('再次探测：没有可续传的断点了',
          not synth.probe_resume(doc, '周报', 1.0, out)['resumable'])
    return doc, out


def test_identity_change_invalidates():
    print('T3 换了语速：断点作废，整篇重跑（不拼错）')
    doc, out = make_doc('t3'), fresh_out('out3')
    synth.synthesize_core(FakeBackend(), doc, '周报', 1.0, out,
                          ctl=FakeCtl(stop_after=2), chunk_seconds=600)
    pr = synth.probe_resume(doc, '周报', 1.2, out)
    check('探测：语速变了 → 不可续传', not pr['resumable'], str(pr))
    be = FakeBackend()
    mp3, dur = synth.synthesize_core(be, doc, '周报', 1.2, out, chunk_seconds=600)
    check('确实整篇重跑（5 段）', len(be.calls) == 5, f'calls={len(be.calls)}')
    check('成品时长 = 5 秒', abs(dur - 5.0) < 0.01, f'dur={dur}')


def test_resume_off_forces_full_rerun():
    print('T4 关掉续传：清掉旧段从头重跑')
    doc, out = make_doc('t4'), fresh_out('out4')
    synth.synthesize_core(FakeBackend(), doc, '周报', 1.0, out,
                          ctl=FakeCtl(stop_after=2), chunk_seconds=600)
    be = FakeBackend()
    synth.synthesize_core(be, doc, '周报', 1.0, out, resume=False, chunk_seconds=600)
    check('resume=False → 5 段全部重念', len(be.calls) == 5, f'calls={len(be.calls)}')
    check('旧断点没留下',
          not synth.probe_resume(doc, '周报', 1.0, out)['resumable'])


def test_chunking_makes_checkpoints():
    print('T5 每满 N 秒切一个检查点（模拟 10 分钟）')
    doc, out = make_doc('t5', units=10), fresh_out('out5')
    be = FakeBackend()
    mp3, dur = synth.synthesize_core(be, doc, '周报', 1.0, out,
                                     chunk_seconds=4, cleanup=False)
    work, seg, parts = paths(out)
    names = sorted(f.name for f in parts.glob('*.mp3'))
    check('10 秒 / 每 4 秒一块 → 2 个检查点',
          names == ['book.p01.mp3', 'book.p02.mp3'], str(names))
    check('检查点命名带 <篇名>.pNN', all(n.startswith('book.p') for n in names), str(names))
    check('成品覆盖全部 10 段', abs(dur - 10.0) < 0.01, f'dur={dur}')
    # 收尾那一小块（这里是 2 秒）不单独存：它只是检查点，成品里本来就全
    check('收尾不满一块就不再落盘', not (parts / 'book.p03.mp3').exists())


def test_stopped_run_keeps_checkpoints_then_cleans():
    print('T6 带检查点地中断 → 续传补齐 → 检查点全清')
    doc, out = make_doc('t6', units=10), fresh_out('out6')
    synth.synthesize_core(FakeBackend(), doc, '周报', 1.0, out,
                          ctl=FakeCtl(stop_after=5), chunk_seconds=4)
    work, seg, parts = paths(out)
    check('停下时已闭合 1 个检查点',
          [f.name for f in parts.glob('*.mp3')] == ['book.p01.mp3'],
          str([f.name for f in parts.glob('*.mp3')]))
    snaps = [f.name for f in work.glob('book.部分-*.mp3')]
    check('半成品快照留在工作目录', len(snaps) == 1, str(snaps))
    check('半成品快照带时间戳（%m%d-%H%M%S）',
          bool(snaps) and len(snaps[0].split('-')) == 3, str(snaps))
    check('台账记下了半成品名',
          synth.read_resume(work).get('partial_mp3') == snaps[0],
          str(synth.read_resume(work).get('partial_mp3')))

    be = FakeBackend()
    mp3, dur = synth.synthesize_core(be, doc, '周报', 1.0, out, chunk_seconds=4)
    check('续传只补 5 段', len(be.calls) == 5, f'calls={len(be.calls)}')
    check('成品时长 = 10 秒', abs(dur - 10.0) < 0.01, f'dur={dur}')
    check('续传副本 <篇名>.续传-*.mp3 生成过又被清掉',
          not list(work.glob('book.续传-*.mp3')))
    check('合并成功后：检查点清空', not list(parts.glob('*.mp3')))
    check('合并成功后：半成品快照清空', not list(work.glob('book.部分-*.mp3')))
    check('合并成功后：段落目录清空', not seg.exists())
    check('合并成功后：成品保留', (work / 'book.mp3').exists())
    check('合并成功后：台账保留（记录这篇怎么念的）',
          synth.read_resume(work).get('complete') is True)


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    print('T0 默认切片间隔')
    check('默认每 10 分钟一块（用户要求，20 分钟偏长）',
          config.CHUNK_SECONDS == 600, f'CHUNK_SECONDS={config.CHUNK_SECONDS}')
    doc, out = test_stop_keeps_breakpoint()
    test_resume_only_missing(doc, out)
    test_identity_change_invalidates()
    test_resume_off_forces_full_rerun()
    test_chunking_makes_checkpoints()
    test_stopped_run_keeps_checkpoints_then_cleans()
    print()
    if failures:
        print(f'RESUMETEST_FAILED  共 {len(failures)} 项没过：')
        for f in failures:
            print('  - ' + f)
        return 1
    print('RESUMETEST_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
