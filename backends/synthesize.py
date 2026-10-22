"""批量合成核心逻辑（不依赖 Gradio/WebUI，可被串行与并行路径共用）。

断点续传的两条不变量（改动本文件前请先读懂）：

A. **段是原子的。** 暂停/取消只在段边界生效（`ctl.wait_if_paused()` 在每段循环
   顶部调用），正在合成的那一段一定会念完。所以磁盘上只可能出现整段，不存在
   半段；`NNNN.done` 标记是「这一段已完成」的唯一判据（不靠台账计数），强杀
   进程也不会把残缺的 WAV 当成已完成。

B. **切片边界是算出来的，不是记下来的。** 每段时长可以直接从磁盘上已落盘的
   分段 WAV 读出来（实测约 1ms/个）。所以「每 10 分钟一块」的边界由按段序累计
   时长确定性推导，台账丢失、进程被 SIGKILL 也不会切错。
"""

import hashlib
import json
import os
import queue
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import audio as audio_mod
import config
import document as doc_mod
import voice_profiles as vp
from backends.pool import BackendPool, get_pool
from config import safe_text

PARTS_DIRNAME = 'parts'


# --- 工作目录与断点台账 -------------------------------------------------

def work_paths(out_dir, stem, create: bool = True) -> tuple:
    """返回 (work_dir, seg_dir, parts_dir)。

    create=False 用于「只探一探有没有断点」：不要因为看一眼就在输出目录里留下
    一堆空文件夹。
    """
    work_dir = Path(out_dir) / stem
    seg_dir = work_dir / 'segments'
    parts_dir = work_dir / PARTS_DIRNAME
    if create:
        seg_dir.mkdir(parents=True, exist_ok=True)
        parts_dir.mkdir(parents=True, exist_ok=True)
    return work_dir, seg_dir, parts_dir


def resume_path(work_dir) -> Path:
    return Path(work_dir) / config.RESUME_FILE


def read_resume(work_dir) -> dict:
    """读台账；文件不存在或损坏都返回 {}（当作「没有断点」处理）。"""
    p = resume_path(work_dir)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_resume(work_dir, **fields) -> None:
    """字段级更新台账（读-改-写，先写 .tmp 再原子改名）。"""
    data = read_resume(work_dir)
    data.update(fields)
    data['updated'] = time.time()
    target = resume_path(work_dir)
    tmp = target.with_name(target.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                   encoding='utf-8')
    os.replace(tmp, target)


def _identity(units, spk_name, speed, mode, role_map) -> dict:
    """断点身份：六项全等才算「同一篇文档、同样的念法」。"""
    return {
        'version': config.RESUME_VERSION,
        'unit_sha': doc_mod.units_fingerprint(units),
        'units_total': len(units),
        'spk': spk_name,
        'speed': float(speed),
        'mode': mode,
        'role_map': dict(role_map or {}),
    }


def _identity_same(old: dict, new: dict) -> bool:
    if not old:
        return False
    for key in ('version', 'unit_sha', 'units_total', 'spk', 'mode', 'role_map'):
        if old.get(key) != new.get(key):
            return False
    try:
        return abs(float(old.get('speed', 0)) - float(new.get('speed', 0))) < 1e-6
    except (TypeError, ValueError):
        return False


def _file_sha(path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            for block in iter(lambda: f.read(1 << 20), b''):
                h.update(block)
    except OSError:
        return ''
    return h.hexdigest()


# --- 分段文件 ----------------------------------------------------------

def _seg_wavs(seg_dir, i) -> list:
    return sorted(Path(seg_dir).glob(f'{i:04d}_*.wav'))


def _done_units(seg_dir, total) -> list:
    """已完成段号。标记 + 至少一个 WAV 都齐了才算完成。"""
    ok = []
    for i in range(1, total + 1):
        if (Path(seg_dir) / f'{i:04d}.done').exists() and _seg_wavs(seg_dir, i):
            ok.append(i)
    return ok


def _wipe_work(seg_dir, parts_dir) -> None:
    """清掉上一版留下的工作数据（不动用户的成品 MP3）。"""
    for pat in ('*.wav', '.*.part.wav', '*.wav.tmp', '*.done'):
        for f in Path(seg_dir).glob(pat):
            try:
                f.unlink()
            except OSError:
                pass
    for f in Path(parts_dir).glob('*.mp3'):
        try:
            f.unlink()
        except OSError:
            pass


def _cleanup_work(work_dir, seg_dir, parts_dir, stem) -> None:
    """整篇合成成功后的回收：成品已经出来了，这些中间件一律删掉。

    删的是「续传用的东西」——分段 WAV、10 分钟分片、以及每次中断留下的
    「部分/续传」快照。成品 `<篇名>.mp3` 和台账 resume.json 留着：台账很小，
    记录了这一篇是怎么念出来的，下次重跑能立刻看出指纹是否一致。
    """
    for f in Path(parts_dir).glob('*.mp3'):
        try:
            f.unlink()
        except OSError:
            pass
    shutil.rmtree(seg_dir, ignore_errors=True)
    for pat in (f'{stem}.部分-*.mp3', f'{stem}.续传-*.mp3'):
        for f in Path(work_dir).glob(pat):
            try:
                f.unlink()
            except OSError:
                pass


def _stamp() -> str:
    return time.strftime('%m%d-%H%M%S')


def _unique(path: Path) -> Path:
    """避免同一秒内两次停止互相覆盖。"""
    path = Path(path)
    if not path.exists():
        return path
    for n in range(2, 100):
        cand = path.with_name(f'{path.stem}-{n}{path.suffix}')
        if not cand.exists():
            return cand
    return path


# --- 断点探测（给 UI 用） -------------------------------------------------

def probe_resume(filepath, spk_name, speed, out_dir, role_map=None) -> dict:
    """看看这篇文档在输出目录里有没有可用的断点（不加载模型、不改磁盘）。

    用的身份判定和 `synthesize_core` 完全同一套代码，所以 UI 显示「可续传」
    与真正跑起来的判定不会打架。
    """
    src = Path(filepath)
    mode = 'roles' if role_map else 'single'
    blank = {'resumable': False, 'units_total': 0, 'units_done': 0, 'parts': [],
             'partial_mp3': None}
    try:
        units = doc_mod.load_units(src, roles_mode=(mode == 'roles'))
    except OSError:
        return dict(blank)
    if not units:
        return dict(blank)

    total = len(units)
    work_dir, seg_dir, parts_dir = work_paths(out_dir, safe_text(src.stem), create=False)
    ident = _identity(units, spk_name, speed, mode, role_map)
    old = read_resume(work_dir)
    if not _identity_same(old, ident):
        return {'resumable': False, 'units_total': total, 'units_done': 0,
                'parts': [], 'partial_mp3': old.get('partial_mp3')}
    done = _done_units(seg_dir, total)
    return {
        'resumable': bool(done) and len(done) < total,
        'units_total': total,
        'units_done': len(done),
        'parts': sorted(f.name for f in parts_dir.glob('*.mp3')),
        'partial_mp3': old.get('partial_mp3'),
    }


# --- 主流程 ------------------------------------------------------------

def synthesize_core(backend, filepath, spk_name, speed, out_dir,
                    role_map: dict | None = None,
                    step_cb=None, unit_cb=None, ctl=None,
                    resume: bool = True, chunk_seconds=None,
                    cleanup: bool = True,
                    extra_backends=None) -> tuple:
    """用指定后端实例合成单份文档，返回 (mp3_path, duration)。

    role_map 为 {角色名: 档案名}：非空时走多角色模式；为空时单音色。
    step_cb(fraction, desc) 可选回调上报进度（粗粒度，含合并阶段）。
    unit_cb(done, total)    可选回调上报「已完成段数 / 总段数」（**绝对值**，
                            续传时含上次已完成的段），用于进度条。
    ctl                     可选的暂停/取消控制器，需提供
                            `wait_if_paused() -> bool`（返回 True 表示已被取消）。
                            只在**段边界**生效。
    resume                  True 时若台账指纹匹配，就跳过已完成段只补缺的。
    chunk_seconds           每满这么多秒落一个分段 MP3；默认取 config.CHUNK_SECONDS。
    cleanup                 整篇合成成功后是否删除 segments/parts。
    extra_backends          额外可用的后端实例。非空且剩余段 > 1 时启用**篇内按段
                            并发**：多个实例各念一段，念完的段落按段序由提交线程
                            接进总表，所以段序、切片边界、`.done` 标记与串行完全
                            一致。一本书也能吃满多个实例，不必等第二本书。

    产出：
      <篇名>.mp3                完整版；被中断时是「目前已完成段的半成品」
      <篇名>.部分-<时间>.mp3      每次中断的半成品快照（整篇合成成功后自动删除）
      <篇名>.续传-<时间>.mp3      续传且本次补齐全部剩余段时，本次新合成的部分
                                （整篇合成成功后自动删除）
      parts/<篇名>.pNN.mp3      每满 chunk_seconds 一块的检查点（成功后自动删除）
      segments/                 分段 WAV 与 NNNN.done 标记（成功后自动删除）
      resume.json               断点台账：指纹 + 已完成段数 + 最后半成品名
    """
    src = Path(filepath)
    stem = safe_text(src.stem)
    mode = 'roles' if role_map else 'single'
    chunk_seconds = float(config.CHUNK_SECONDS if chunk_seconds is None
                          else chunk_seconds)

    # 校验/补注册所需音色。篇内并发时每个实例都要登记，音色表不共享。
    insts = [backend] + list(extra_backends or [])
    need_spks = {spk_name}
    if mode == 'roles':
        need_spks.update(role_map.values())
    for inst in insts:
        for s in need_spks:
            if not s or s in inst.spk_ids:
                continue
            prof = vp.get_profile(s)
            if prof:
                inst.register_spk(s, prof['txt'], prof['wav'])
            else:
                raise ValueError(f'声音档案 [{s}] 不存在')

    # 段落清单：合成 / 指纹 / 进度分母共用同一个入口，保证三者一致
    units = doc_mod.load_units(src, roles_mode=(mode == 'roles'))
    if not units:
        raise ValueError('文档内容为空或没有可朗读的文本')
    total = len(units)

    work_dir, seg_dir, parts_dir = work_paths(out_dir, stem)
    ident = _identity(units, spk_name, speed, mode, role_map)
    old = read_resume(work_dir)
    same = bool(resume) and _identity_same(old, ident)

    carried = _done_units(seg_dir, total) if same else []
    if not carried:
        # 指纹不匹配（或没开续传）→ 旧分段一律作废，宁可重念也不拼错
        _wipe_work(seg_dir, parts_dir)
    write_resume(work_dir, **ident,
                 source_name=src.name, source_sha256=_file_sha(src),
                 done=len(carried), complete=False)

    done_set = set(carried)
    done_count = len(carried)
    if carried and step_cb:
        step_cb(done_count / total,
                f'检测到断点：已完成 {done_count}/{total} 段，'
                f'本次只合成剩余 {total - done_count} 段')

    all_wavs: list = []
    newly_wavs: list = []
    stopped = False
    acc = 0.0
    slice_no = 1
    slice_wavs: list = []
    closed: set = set()

    def close_slice(upto: int) -> None:
        """累计满 chunk_seconds：把当前这一块落成独立的 parts MP3。"""
        nonlocal slice_no, slice_wavs, acc
        if not slice_wavs:
            return
        out = parts_dir / f'{stem}.p{slice_no:02d}.mp3'
        if not out.exists():
            audio_mod.merge_wavs_to_mp3(slice_wavs, out)
        closed.add(out.name)
        if step_cb:
            step_cb(done_count / total,
                    f'已满 {chunk_seconds / 60:.0f} 分钟，'
                    f'存为第 {slice_no} 块 {out.name}')
        slice_no += 1
        slice_wavs = []
        acc = 0.0

    def label_of(i: int, role: str) -> str:
        return f'[{role}]' if mode == 'roles' else f'块{i}'

    def render(i: int, inst) -> tuple:
        """合成第 i 段并原子落盘，返回 (wav 路径列表, 句数)。

        只碰第 i 段自己的文件，所以可以被多个线程同时调用（各用各的实例）。
        """
        role, text = units[i - 1]
        actual_spk = role_map.get(role, spk_name) if mode == 'roles' else spk_name
        try:
            segs = inst.synthesize(text, spk_id=actual_spk, speed=speed)
        except Exception as e:  # noqa: BLE001
            # 文本前端偶尔会在某个符号上翻车（wetext 碰到 `#1342` 会内部
            # assert 空串）。一个符号毁掉一整篇——尤其是一篇要跑几小时的书——
            # 代价太大，所以把这一段硬清一遍再试一次。清洗后还是炸才真正失败。
            safe = doc_mod.tts_safe_hard(text)
            if not safe.strip() or safe == text:
                raise
            if step_cb:
                step_cb(done_count / total,
                        f'{label_of(i, role)} 第 {i}/{total} 段文本异常'
                        f'（{type(e).__name__}），已清洗重试')
            segs = inst.synthesize(safe, spk_id=actual_spk, speed=speed)
        wrote = []
        for si, seg in enumerate(segs):
            final = seg_dir / f'{i:04d}_{si:04d}.wav'
            # 临时名必须**仍然以 .wav 结尾**：soundfile 靠扩展名推断格式，
            # 写成 xxx.wav.tmp 会直接报 "No format specified and unable to get
            # format from file extension"。前面加点是为了让 _seg_wavs 的
            # `0012_*.wav` 通配不到这个半成品。
            tmp = seg_dir / f'.{i:04d}_{si:04d}.part.wav'
            inst.write_wav(seg, tmp, inst.sample_rate)
            os.replace(tmp, final)          # 原子改名：不会留下半截 WAV
            wrote.append(final)
        # 全部句子落盘后才写完成标记，这才是「这段已完成」的判据
        (seg_dir / f'{i:04d}.done').write_text('ok', encoding='utf-8')
        return wrote, len(segs)

    def finish(i: int, wrote: list, nsent: int) -> None:
        """按段序把新合成的一段接进总表。只在提交线程里调用，保证段序。"""
        nonlocal done_count, acc
        all_wavs.extend(wrote)
        newly_wavs.extend(wrote)
        slice_wavs.extend(wrote)
        acc += sum(audio_mod.wav_duration(w) for w in wrote)
        done_count += 1
        if unit_cb:
            unit_cb(done_count, total)
        if step_cb:
            step_cb(done_count / total,
                    f'{label_of(i, units[i - 1][0])} {i}/{total} 完成（{nsent} 句）')
        write_resume(work_dir, done=done_count)
        if acc >= chunk_seconds:
            close_slice(i)

    def carry(i: int) -> None:
        """续传：这段上次已经念完。跳过合成，但必须重放切片累计（不变量 B）。"""
        nonlocal acc
        wavs = _seg_wavs(seg_dir, i)
        all_wavs.extend(wavs)
        slice_wavs.extend(wavs)
        acc += sum(audio_mod.wav_duration(w) for w in wavs)
        if acc >= chunk_seconds:
            close_slice(i)

    todo = [i for i in range(1, total + 1) if i not in done_set]

    if len(insts) <= 1 or len(todo) <= 1:
        # ---- 串行（默认）：一个实例一段一段念 ----
        for i in range(1, total + 1):
            # 不变量 A：段边界是唯一能安全暂停/取消的时机
            if ctl is not None and ctl.wait_if_paused():
                stopped = True
                if step_cb:
                    step_cb(done_count / total, f'在第 {i - 1}/{total} 段处停下')
                break
            if i in done_set:
                carry(i)
                continue
            finish(i, *render(i, backend))
    else:
        # ---- 篇内并发：多实例各领一段，提交线程按段序接进总表 ----
        jobs: queue.Queue = queue.Queue()
        for i in todo:
            jobs.put(i)
        cv = threading.Condition()
        results: dict = {}
        state = {'alive': len(insts), 'err': None}
        workers = []

        def _worker(inst) -> None:
            try:
                while state['err'] is None:
                    try:
                        i = jobs.get_nowait()
                    except queue.Empty:
                        return
                    if ctl is not None and ctl.wait_if_paused():
                        return
                    try:
                        got = render(i, inst)
                    except BaseException as e:  # noqa: BLE001
                        with cv:
                            state['err'] = e
                            cv.notify_all()
                        return
                    with cv:
                        results[i] = got
                        cv.notify_all()
            finally:
                with cv:
                    state['alive'] -= 1
                    cv.notify_all()

        for inst in insts:
            t = threading.Thread(target=_worker, args=(inst,), daemon=True)
            workers.append(t)
            t.start()

        for i in range(1, total + 1):
            if i in done_set:
                carry(i)
                continue
            with cv:
                while (i not in results and state['err'] is None
                       and state['alive'] > 0):
                    cv.wait(0.5)
                err = state['err']
                got = results.pop(i, None)
            if err is not None:
                raise err
            if got is None:
                stopped = True
                if step_cb:
                    step_cb(done_count / total, f'在第 {i - 1}/{total} 段处停下')
                break
            finish(i, *got)

        # 停下来时 worker 可能正念着手上的那一段：等它念完再合并，免得
        # 「实例已经还给调度器、却还在被这个线程用着」。实例内部有锁，等不到
        # 也不会写坏数据，只是会白白多等一会儿。
        for t in workers:
            t.join(timeout=600)

    # 不属于任何完整块的 parts 一律清掉（例如上一版切得不一样）
    for f in parts_dir.glob('*.mp3'):
        if f.name not in closed:
            try:
                f.unlink()
            except OSError:
                pass

    # 没停下来却缺段 → 宁可报错，也不要悄悄产出「缺一段的完整版」
    if not stopped:
        missing = [i for i in range(1, total + 1) if not _seg_wavs(seg_dir, i)]
        if missing:
            preview = ','.join(str(x) for x in missing[:8])
            raise RuntimeError(
                f'有 {len(missing)} 段没有音频（{preview}…），未生成完整 MP3')

    if not all_wavs:
        return None, 0.0

    mp3 = work_dir / f'{stem}.mp3'

    if stopped:
        snap = _unique(work_dir / f'{stem}.部分-{_stamp()}.mp3')
        if step_cb:
            step_cb(0.98, f'已中断：合并已完成的 {done_count}/{total} 段 → {snap.name}')
        duration = audio_mod.merge_wavs_to_mp3(all_wavs, snap)
        shutil.copyfile(snap, mp3)          # 一次编码 + 一次复制，不重复编码
        write_resume(work_dir, done=done_count, complete=False,
                     partial_mp3=snap.name)
        return mp3, duration

    if carried and newly_wavs:
        cont = _unique(work_dir / f'{stem}.续传-{_stamp()}.mp3')
        if step_cb:
            step_cb(0.97, f'续传批次单独存一份 → {cont.name}')
        try:
            audio_mod.merge_wavs_to_mp3(newly_wavs, cont)
        except Exception:  # noqa: BLE001  续传副本失败不该拖垮完整版
            pass

    if step_cb:
        step_cb(0.98, f'合并全部 {done_count} 段 → {mp3.name}')
    duration = audio_mod.merge_wavs_to_mp3(all_wavs, mp3)
    write_resume(work_dir, done=done_count, complete=True, complete_mp3=mp3.name)
    if cleanup:
        _cleanup_work(work_dir, seg_dir, parts_dir, stem)
    return mp3, duration


def _parallel_one(pool, task) -> tuple:
    """并行 worker：从池里取空闲实例合成单文件。"""
    token, inst = pool.acquire()
    try:
        return synthesize_core(inst, task['filepath'], task['spk_name'],
                               task['speed'], task['out_dir'],
                               task.get('role_map'),
                               resume=task.get('resume', True))
    finally:
        pool.release(token)


def parallel_run(files_data, out_dir, workers=2, done_cb=None) -> list:
    """用后端实例池并行合成多个文件，返回 [(name, status, dur, detail)]。

    done_cb(done, total, result_row) 可选回调（用于 UI 进度/日志）。
    """
    pool = get_pool()
    workers = max(1, int(workers))
    pool.ensure(workers)          # 需要几个实例就先把几个加载好（阻塞）
    total = len(files_data)
    results = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futures = {ex.submit(_parallel_one, pool, t): t for t in files_data}
        for fut in as_completed(futures):
            t = futures[fut]
            fname = Path(t['filepath']).name
            done += 1
            try:
                mp3, dur = fut.result()
                row = [fname, '成功', f'{dur:.1f} 秒', str(mp3)]
                results.append(row)
            except Exception as e:  # noqa: BLE001
                row = [fname, '失败', '-', str(e)]
                results.append(row)
            if done_cb:
                done_cb(done, total, row)
    return results
