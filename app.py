"""CosyVoice 3.0 Reader WebUI - Custom Frontend Server (可变队列 / 多任务)

和早期版本的区别：
  - 任务持有一个**可变**队列：运行中可以加文档、删文档、改并发、改角色映射。
  - 并发数在运行中可调（1~2），改并发不再重建后端实例池，所以不会重新加载模型。
  - 每个任务可暂停 / 继续 / 停止，都在**段边界**生效。
  - 进度以"已完成段 / 总段数"表达，文档一入队就把分母扫描出来。
"""

import contextlib
import logging
import os
import stat
import sys
import json
import shutil
import socket
import threading
import time
import traceback
import uuid
import webbrowser
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

import audio as audio_mod
import document as doc_mod
import ebook_import as ebook
import episode
import task_store as store
import voice_profiles as vp
from backends.pool import get_pool
from backends.synthesize import probe_resume, synthesize_core
from config import (CHUNK_SECONDS, COSYVOICE_DIR, EPISODE_CHOICES, EPISODE_MAX_SECONDS,
                    EPISODE_SECONDS, HARD_MAX_WORKERS, MODEL_DIR, MONITOR_INTERVAL,
                    OUTPUT_DIR, PARALLEL_DEFAULT, PROJECT_ROOT, SPEED_MAX, SPEED_MIN,
                    TASKS_DIR, TMP_DIR, WEBUI_HOST, WEBUI_PORT, clamp_workers,
                    ensure_sys_path, safe_stem, safe_text)
from runtime_monitor import snapshot, format_snapshot

LOG = []
_LOG_LOCK = threading.Lock()
SETTINGS_FILE = PROJECT_ROOT / "settings.json"
backend_state = {"status": "loading", "error": ""}

# 多任务队列：task_id -> task_info
_active_tasks = {}
_task_lock = threading.Lock()

# 上传源文件的保留天数。任务跑完**不再**删除上传目录：角色标注副本就放在
# 那里，而且"停止之后接着跑/重新跑"都要用原始文件。改为按天数清理。
UPLOAD_KEEP_DAYS = 7
# 内存里最多保留多少个已结束的任务
TASK_KEEP = 12


def log(msg):
    with _LOG_LOCK:
        LOG.append(f'[{time.strftime("%H:%M:%S")}] {msg}')
        if len(LOG) > 500:
            del LOG[:100]


def log_text():
    with _LOG_LOCK:
        return '\n'.join(LOG)


def tb_tail(n: int = 10) -> list:
    """把 traceback 压成最后 n 行。

    只写最后一行（以前的做法）在遇到没有 message 的 assert 时只会留下
    「AssertionError」，等于什么都没说；全量 traceback 又会把 500 行日志刷爆。
    最后几帧正好是「哪个库、哪一行炸的」，够定位。
    """
    lines = [ln.rstrip() for ln in traceback.format_exc().splitlines() if ln.strip()]
    return lines[-n:]


def _decode_filename(raw: str) -> str:
    """还原被编码过的上传文件名。

    浏览器直接发 UTF-8 文件名，但 curl / PowerShell 一类客户端会把非 ASCII
    文件名按 RFC 2047 或 RFC 5987 编码掉；不还原的话，文件名会变成
    "=_utf-8_B_xxx_=" 这种乱码，输出目录也跟着乱。
    """
    s = (raw or '').strip()
    if not s:
        return s
    if s[:7].lower() == "utf-8''":
        try:
            from urllib.parse import unquote
            return unquote(s[7:])
        except Exception:
            return s
    if s.startswith('=?') and s.endswith('?='):
        try:
            from email.header import decode_header
            out = []
            for chunk, enc in decode_header(s):
                out.append(chunk.decode(enc or 'utf-8', 'replace')
                           if isinstance(chunk, bytes) else chunk)
            return ''.join(out)
        except Exception:
            return s
    return s


_UNSAFE_NAME_CHARS = '<>:"/\\|?*'


def _safe_upload_name(name: str) -> str:
    """把浏览器给的上传文件名洗成 Windows 上能落盘的文件名（保留中文）。

    主干一定要过 `safe_stem`：`…罗斯福斯  .azw3` 这种「空格紧挨着扩展名」的名字，
    主干会以空格结尾，而 Windows 会把结尾空格悄悄吃掉，之后建子目录必失败
    （WinError 3，且报错完全看不出是文件名的问题）。
    """
    base = Path(str(name).replace('\\', '/')).name or 'document.txt'
    suffix = Path(base).suffix
    stem = safe_stem(base, fallback='document')
    if suffix and all(ord(c) >= 32 and c not in _UNSAFE_NAME_CHARS for c in suffix):
        return stem + suffix
    return stem + '.txt'


def load_settings():
    try:
        return json.loads(SETTINGS_FILE.read_text(encoding='utf-8'))
    except Exception:
        return {}


def save_settings(**kw):
    s = load_settings()
    s.update(kw)
    SETTINGS_FILE.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding='utf-8')


def _parse_duration(txt) -> float:
    """把 '12.3 秒' / 12.3 / '12.3s' 一律解析成秒数（float），失败给 0。"""
    if isinstance(txt, (int, float)):
        return float(txt)
    try:
        return float(str(txt).split()[0])
    except (ValueError, IndexError):
        return 0.0


# ─── 任务队列 ---------------------------------------------------------------
# 老版本在任务创建时就把文件列表、音色、并发数固化，运行中改不了，而且并发数一改
# 就重建整个后端实例池（等于重新加载模型）。这里改成：任务持有一个可变队列
# （docs）和一个活的任务级角色映射（roles），由一个调度线程按 workers 上界不断
# 把 pending 文档派出去；并发数、文档、角色都可以在运行中改。


class _TaskCtl:
    """任务级的暂停/取消开关。"""

    def __init__(self):
        self.pause = threading.Event()
        self.cancel = threading.Event()

    def wait_if_paused(self) -> bool:
        """暂停时阻塞在这里；返回 True 表示任务已被取消。"""
        while self.pause.is_set() and not self.cancel.is_set():
            time.sleep(0.2)
        return self.cancel.is_set()


class _DocCtl:
    """单篇文档的控制器 = 任务级开关 + 这一篇自己的取消开关。"""

    def __init__(self, task_ctl: _TaskCtl, doc_cancel: threading.Event):
        self.task_ctl = task_ctl
        self.doc_cancel = doc_cancel

    def wait_if_paused(self) -> bool:
        if self.doc_cancel.is_set():
            return True
        return self.task_ctl.wait_if_paused()


def _now() -> float:
    return time.time()


def _as_bool(v, default: bool = True) -> bool:
    """表单/JSON 里的布尔值五花八门，统一收口。缺省（None/空）走 default。"""
    if v is None:
        return default
    s = str(v).strip().lower()
    if s in ('', 'none', 'null'):
        return default
    return s in ('1', 'true', 'yes', 'on', 'y')


def _episode_from_form(raw) -> int:
    """「成品分集」表单值 → 秒。0 表示不分集。

    只接受配置里给出的几档（0/30/60/90 分钟），其它值一律回落到默认 60 分钟；
    这样前端传什么都改不成一个奇怪的时长。
    """
    if raw is None or str(raw).strip() == '':
        return EPISODE_SECONDS
    try:
        secs = int(float(str(raw).strip()) * 60)
    except ValueError:
        return EPISODE_SECONDS
    if secs <= 0:
        return 0
    for c in EPISODE_CHOICES:
        if c and abs(secs - c) <= 60:
            return c
    return EPISODE_SECONDS


def _new_task(task_id, out_dir, spk_name, speed, workers,
              episode_seconds: int = EPISODE_SECONDS) -> dict:
    return {
        'id': task_id,
        'name': f'Batch {task_id[:6]}',
        'out_dir': str(out_dir),
        'spk_name': spk_name,
        'speed': speed,
        'workers': clamp_workers(workers),
        'episode_seconds': int(episode_seconds or 0),   # 成品分集时长，0 = 不分集
        'roles': {},        # {角色名: 档案名}，运行中可改，对未合成的段立刻生效
        'docs': [],         # 文档队列，运行中可增删
        'status': 'queued',
        'created': _now(),
        'started': None,
        'finished': None,
        'last_desc': '排队中',
        'error': '',
        'ctl': _TaskCtl(),
        'dispatcher': None,
        'seq': 0,
        'lock': threading.RLock(),
    }


def _new_doc(task, path, name, resume: bool = True) -> dict:
    task['seq'] += 1
    return {
        'doc_id': f'd{task["seq"]}',
        'name': name,
        'path': str(path),
        'role_path': None,     # 「识别角色」写出的 [角色] 标注副本
        'spk': task['spk_name'],
        'status': 'pending',
        'units_total': 0,
        'units_done': 0,
        'mp3': None,
        'duration': 0.0,
        'error': '',
        'created': _now(),
        'started': None,
        'finished': None,
        'cancel': threading.Event(),
        # --- 断点续传（跑过长音频最怕前功尽弃） ---
        'resume': bool(resume),   # 这篇是否允许吃磁盘上的断点
        'resumed': False,         # 本次确实跳过了已合成的段
        'resumable': False,       # 磁盘上有可用断点，UI 用来提示
        'parts': [],              # 已落盘的分段 MP3（合并成功后会删掉）
        'partial': None,          # 上次中断时留下的半成品 MP3
        'chunks': [[0, 0]],       # 10 分钟分片的人话说明
        # --- 成品分集 ---
        'episodes': [],           # 切好的分集文件名（第01集…），空 = 没切
        'episode_error': '',      # 分集失败的原因（成品仍在，只是没切）
    }


def _doc_roles_mode(task, doc) -> bool:
    """这篇文档是否走多角色模式：有标注副本，且映射表里至少有一个角色。"""
    return bool(doc.get('role_path')) and bool(task['roles'])


def _rescan_doc(task, doc) -> int:
    """重算段数（纯文本运算，不碰显卡），进度条一加入就知道分母。"""
    src = doc['role_path'] or doc['path']
    roles_mode = _doc_roles_mode(task, doc)
    try:
        doc['units_total'] = doc_mod.count_units(src, roles_mode)
    except Exception as e:  # noqa: BLE001
        log(f'预扫描段数失败：{doc["name"]} - {e}')
        doc['units_total'] = 0

    # 顺手看一眼磁盘上有没有能接上的断点——判定逻辑和真正跑的时候同一套，
    # 免得 UI 说「可续传」、真跑起来却从头开始。
    try:
        probe = probe_resume(src, doc['spk'], task['speed'], task['out_dir'],
                             dict(task['roles']) if roles_mode else None)
    except Exception as e:  # noqa: BLE001
        probe = {}
        log(f'断点探测失败：{doc["name"]} - {e}')
    if probe.get('units_total'):
        doc['units_total'] = probe['units_total']
    doc['resumable'] = bool(probe.get('resumable')) and bool(doc.get('resume', True))
    doc['parts'] = list(probe.get('parts') or [])
    doc['partial'] = probe.get('partial_mp3')
    if doc['status'] == 'pending' and doc['resumable']:
        # 已经攒下的段数直接显示出来，用户一拖进来就看到「158/320 段」而不是 0
        doc['units_done'] = int(probe.get('units_done') or 0)
    return doc['units_total']


def _rescan_pending(task) -> None:
    """只重算还没开始的文档——跑着的那些分母不能动，否则百分比会跳。"""
    for doc in task['docs']:
        if doc['status'] == 'pending':
            _rescan_doc(task, doc)


def _add_docs(task, items, resume: bool = True) -> list:
    """items: [(显示名, 落盘路径)]，加入队列并预扫描段数。"""
    docs = []
    for name, path in items:
        doc = _new_doc(task, path, name, resume=resume)
        _rescan_doc(task, doc)
        task['docs'].append(doc)
        docs.append(doc)
    return docs


def _find_doc(task, doc_id):
    for d in task['docs']:
        if d['doc_id'] == doc_id:
            return d
    return None


def _gc_tasks(keep: int = TASK_KEEP) -> None:
    """内存里只留最近若干个已结束的任务，长时间运行不会越积越多。"""
    with _task_lock:
        finished = [t for t in _active_tasks.values()
                    if t['status'] in ('done', 'failed', 'stopped')]
        if len(finished) <= keep:
            return
        finished.sort(key=lambda t: t['finished'] or t['created'])
        for t in finished[:-keep]:
            _active_tasks.pop(t['id'], None)


def _elapsed(task) -> float:
    if not task['started']:
        return 0.0
    return max(0.0, (task['finished'] or _now()) - task['started'])


def _status_text(task) -> str:
    docs = task['docs']
    done = sum(1 for d in docs if d['status'] == 'done')
    failed = sum(1 for d in docs if d['status'] == 'failed')
    units = sum(d['units_total'] for d in docs)
    udone = sum(d['units_done'] for d in docs)
    txt = f'已完成 {done}/{len(docs)} 篇 · {udone}/{units} 段'
    return txt + (f' · 失败 {failed}' if failed else '')


def _doc_payload(task, d) -> dict:
    total, done = d['units_total'], d['units_done']
    if total:
        pct = round(done / total * 100)
    else:
        pct = 100 if d['status'] == 'done' else 0
    return {
        'doc_id': d['doc_id'],
        'name': d['name'],
        'status': d['status'],
        'spk': d['spk'],
        'roles_mode': _doc_roles_mode(task, d),
        'units_total': total,
        'units_done': done,
        'progress': pct,
        'mp3': d['mp3'],
        'duration': round(d['duration'], 1),
        'error': d['error'],
        'resume': bool(d.get('resume', True)),
        'resumed': bool(d.get('resumed')),
        'resumable': bool(d.get('resumable')),
        'parts': list(d.get('parts') or []),
        'partial': d.get('partial'),
        'episodes': list(d.get('episodes') or []),
        'episode_error': d.get('episode_error') or '',
    }


def _task_payload(task) -> dict:
    """把内存里的任务转成能 JSON 序列化的结构。

    任务里带着 threading.Event、线程对象和后端实例，直接 json.dumps 会
    TypeError，所以对外一律走这里。
    """
    docs = task['docs']
    units_total = sum(d['units_total'] for d in docs)
    units_done = sum(d['units_done'] for d in docs)
    if units_total:
        pct = units_done / units_total * 100
    else:
        settled = all(d['status'] in ('done', 'cancelled', 'failed') for d in docs)
        pct = 100.0 if (docs and settled) else 0.0
    return {
        'id': task['id'],
        'name': task['name'],
        'status': task['status'],
        'speed': task['speed'],
        'spk_name': task['spk_name'],
        'out_dir': task['out_dir'],
        'workers': clamp_workers(task['workers']),
        'workers_max': HARD_MAX_WORKERS,
        'episode_seconds': int(task.get('episode_seconds') or 0),
        'roles': dict(task['roles']),
        'progress': round(pct, 1),
        'last_desc': task['last_desc'],
        'error': task['error'],
        'created': task['created'],
        'started': task['started'],
        'finished': task['finished'],
        'elapsed': round(_elapsed(task), 1),
        'totals': {
            'docs': len(docs),
            'docs_done': sum(1 for d in docs if d['status'] == 'done'),
            'failed': sum(1 for d in docs if d['status'] == 'failed'),
            'units': units_total,
            'units_done': units_done,
        },
        'docs': [_doc_payload(task, d) for d in docs],
        'pool': get_pool().status(),
    }


def _record_history(task, doc) -> None:
    """往 tasks/ 里记一笔，供「最近任务」表格用。"""
    try:
        hid = store.create_task(doc['name'], task['out_dir'], doc['spk'], task['speed'])
        units_total = int(doc.get('units_total') or 0)
        units_done = int(doc.get('units_done') or 0)
        if doc['status'] == 'done':
            store.update_task(hid, status=store.STATUS_DONE,
                              mp3_path=doc['mp3'] or '', duration=doc['duration'],
                              chunks_total=units_total, chunks_done=units_done)
        elif doc['status'] == 'cancelled' and doc.get('mp3'):
            # 用户按了「停止」：半成品 MP3 还在，这不是失败，单独记成「已停止」。
            store.update_task(hid, status='stopped',
                              mp3_path=doc['mp3'], duration=doc['duration'],
                              chunks_total=units_total, chunks_done=units_done,
                              error=f'已停止（保留 {units_done}/{units_total} 段的半成品）')
        else:
            store.update_task(hid, status=store.STATUS_FAILED,
                              mp3_path=doc['mp3'] or '',
                              chunks_total=units_total, chunks_done=units_done,
                              error=doc['error'] or doc['status'])
    except Exception as e:  # noqa: BLE001
        log(f'写任务历史失败：{e}')


def _ensure_dispatcher(task) -> None:
    """确保调度线程在跑；任务结束后又加了文档，这里会把它重新拉起来。"""
    d = task.get('dispatcher')
    if d is not None and d.is_alive():
        return
    with _task_lock:
        if task['status'] in ('done', 'failed', 'stopped'):
            # 新一轮：清掉上一轮的暂停/取消状态，否则新文档会被旧的 cancel 挡住
            task['status'] = 'queued'
            task['finished'] = None
            task['ctl'] = _TaskCtl()
            task['last_desc'] = '排队中'
        t = threading.Thread(target=_dispatch, args=(task,), daemon=True)
        task['dispatcher'] = t
    t.start()


def _dispatch(task) -> None:
    """调度循环：按 workers 上界把 pending 文档派出去，直到队列清空。

    并发数、文档列表、角色映射都在这个循环里**每轮重新读**，所以运行中改它们
    立刻生效：加并发会去后台静默加载新实例，减并发会让空闲实例被回收。
    """
    tid = task['id']
    if task['started'] is None:
        task['started'] = _now()
        log(f'任务 {tid[:8]}：开始处理 {len(task["docs"])} 个文档')
    pool = get_pool()
    inflight = {}
    try:
        while True:
            if backend_state['status'] == 'error':
                raise RuntimeError(f'模型加载失败：{backend_state["error"]}')
            if task['ctl'].cancel.is_set():
                task['status'] = 'stopped'
                break
            if task['ctl'].pause.is_set():
                task['status'] = 'paused'
                if task['ctl'].wait_if_paused():
                    task['status'] = 'stopped'
                    break
                continue                    # 恢复后重新算一遍队列
            task['status'] = 'running'

            for did in [d for d, t in inflight.items() if not t.is_alive()]:
                inflight.pop(did, None)

            target = clamp_workers(task['workers'])
            pool.ensure_async(target)       # 需要更多实例时后台静默加载
            if pool.status()['target'] > target:
                pool.shrink_to(target)      # 减并发：回收空闲实例
            pending = [d for d in task['docs'] if d['status'] == 'pending']

            # 只剩这一篇、又允许用多个实例时，必须**等实例加载好再开跑**：
            # 一旦带着 1 个实例跑起来，这篇就会一路串到底（改不了）。加载中的
            # 那几十秒是一次性的，值得等。加载失败/已结束就不再等。
            if len(pending) == 1 and not inflight and target > 1:
                st = pool.status()
                if st['loaded'] < target and st['loading'] and not st['error']:
                    time.sleep(0.25)
                    continue

            while len(inflight) < target and pending:
                got = pool.try_acquire()
                if got is None:
                    # 实例还没加载好（或都在忙），下一轮再试——
                    # 已经在跑的文档不受影响。
                    break
                doc = pending.pop(0)
                tokens = [got]
                # 只剩这一篇要跑了：把剩下的空闲实例也交给它，让它**按段并发**，
                # 一本书也能吃满显卡。两本书同时在跑时仍是一本一个实例，
                # 因为篇间并发才是第一优先。
                if not inflight and not pending and target > 1:
                    while len(tokens) < target:
                        more = pool.try_acquire()
                        if more is None:
                            break
                        tokens.append(more)
                doc['status'] = 'running'
                doc['started'] = _now()
                t = threading.Thread(target=_run_doc, args=(task, doc, tokens),
                                     daemon=True)
                inflight[doc['doc_id']] = t
                t.start()

            if not inflight and not [d for d in task['docs']
                                     if d['status'] == 'pending']:
                break
            time.sleep(0.25)
    except Exception as e:  # noqa: BLE001
        log(f'任务 {tid[:8]} 调度出错：{e}')
        for ln in tb_tail():
            log('    ' + ln)
        task['error'] = str(e)
        task['status'] = 'failed'
    finally:
        for t in inflight.values():
            t.join(timeout=2)
        if task['ctl'].cancel.is_set():
            # 只有真被"停止"了才把剩下的划掉；否则留着，等追加文档或续传时
            # 再捡起来跑，不然刚排队就被标记成已取消。
            for d in task['docs']:
                if d['status'] == 'pending':
                    d['status'] = 'cancelled'
        if task['status'] == 'running':
            docs = task['docs']
            done = sum(1 for d in docs if d['status'] == 'done')
            if task['ctl'].cancel.is_set():
                task['status'] = 'stopped'
            elif docs and done == 0:
                task['status'] = 'failed'
            else:
                task['status'] = 'done'
        task['finished'] = _now()
        task['last_desc'] = _status_text(task)
        with _task_lock:
            if task.get('dispatcher') is threading.current_thread():
                task['dispatcher'] = None
        log(f'任务 {tid[:8]}：{task["status"]}（{task["last_desc"]}）')
        # 收尾这一瞬间被追加/续传进来的文档不能丢：还有 pending 就再拉一轮。
        # 模型加载失败时不重试，否则会空转。
        if (not task['ctl'].cancel.is_set()
                and backend_state['status'] != 'error'
                and any(d['status'] == 'pending' for d in task['docs'])):
            log(f'任务 {tid[:8]}：还有 {sum(1 for d in task["docs"] if d["status"] == "pending")} 篇待跑，继续调度')
            _ensure_dispatcher(task)


_work_lock = threading.Lock()
_work_busy = set()


@contextlib.contextmanager
def _stem_guard(key: str):
    """同一个工作目录（输出目录 + 文档名）一次只让一篇文档写。

    两篇同名文档同时跑会抢同一批段文件，合并出来的 MP3 会串成一篇。
    工作目录名来自文件名的 stem，所以这里也按 stem 排队。
    """
    while True:
        with _work_lock:
            if key not in _work_busy:
                _work_busy.add(key)
                break
        time.sleep(0.3)
    try:
        yield
    finally:
        with _work_lock:
            _work_busy.discard(key)


def _run_doc(task, doc, tokens) -> None:
    """合成一篇文档。暂停/取消都在段边界生效（模型一次只吃一段）。

    `tokens` 是这篇文档可以使用的实例令牌列表：通常一个，只剩它一篇时会是
    多个，此时 `synthesize_core` 会在篇内按段并发。
    """
    pool = get_pool()
    inst = tokens[0][1]
    extra = [t[1] for t in tokens[1:]]
    tid, name = task['id'], doc['name']
    src = doc['role_path'] or doc['path']
    stem = safe_text(Path(src).stem)
    log(f'任务 {tid[:8]}：开始《{name}》（共 {doc["units_total"]} 段）'
        + (f'，篇内并发 {len(tokens)} 个实例' if extra else ''))
    try:
        if not Path(src).exists():
            # 上传目录是按天回收的：老任务过几天再点「续传」时源文件可能已经没了。
            # 直接说人话，别把 FileNotFoundError 的英文堆栈甩到界面上。
            raise FileNotFoundError(
                '源文件已不在了（临时上传目录会定期清理）。'
                '请把这本书重新拖进来再试。')
        role_map = dict(task['roles']) if _doc_roles_mode(task, doc) else None
        ctl = _DocCtl(task['ctl'], doc['cancel'])
        resume = bool(doc.get('resume', True))

        # 先看上次留下的段能不能接上：音色/语速/角色映射/文本任一项变了就作废重来，
        # 判定逻辑和真正跑的时候是同一套，不会出现"显示可续传、实际从头跑"。
        before = probe_resume(src, doc['spk'], task['speed'], task['out_dir'], role_map)
        doc['resumable'] = bool(before.get('resumable')) and resume
        doc['units_done'] = int(before.get('units_done') or 0)
        if doc['resumable'] and doc['units_done']:
            doc['resumed'] = True
            log(f'《{name}》发现断点：已有 {doc["units_done"]}/{doc["units_total"]} 段，接着往后跑')

        def unit_cb(done, total):
            doc['units_done'] = done
            doc['units_total'] = total
            task['last_desc'] = f'《{name}》第 {done}/{total} 段'

        def step_cb(frac, desc):
            task['last_desc'] = f'《{name}》{desc}'

        with _stem_guard(f"{task['out_dir']}|{stem}"):
            mp3, dur = synthesize_core(inst, src, doc['spk'], task['speed'],
                                       task['out_dir'], role_map=role_map,
                                       step_cb=step_cb, unit_cb=unit_cb, ctl=ctl,
                                       resume=resume, extra_backends=extra)
        cancelled = doc['cancel'].is_set() or task['ctl'].cancel.is_set()
        if mp3 is None:
            doc['status'] = 'cancelled' if cancelled else 'failed'
            doc['error'] = '' if cancelled else '没有生成任何音频'
        else:
            doc['mp3'] = str(mp3)
            doc['duration'] = float(dur)
            if cancelled:
                doc['status'] = 'cancelled'
                doc['error'] = '已中断（保留半成品，可续传）'
            else:
                doc['status'] = 'done'
                doc['units_done'] = doc['units_total']
                # 成品分集：一本 20 小时的书合成一个 2 GB 的 MP3 没法用，
                # 按「一小时左右、切在句子之间」切成若干集，然后删掉整本。
                secs = int(task.get('episode_seconds') or 0)
                if secs > 0:
                    try:
                        task['last_desc'] = f'《{name}》正在分集（每集约 {secs // 60} 分钟）'
                        eps = episode.split_file(mp3, target=float(secs),
                                                 prefix=stem, delete_source=True)
                        doc['episodes'] = [p.name for p in eps]
                        if len(eps) > 1:
                            doc['mp3'] = str(eps[0])
                            log(f'《{name}》已切成 {len(eps)} 集（每集约 {secs // 60} 分钟）')
                    except Exception as ex:  # noqa: BLE001
                        # 分集失败不影响成品：保留整个 MP3，只是没切。
                        doc['episode_error'] = str(ex) or type(ex).__name__
                        log(f'《{name}》分集失败（保留整个 MP3）：{doc["episode_error"]}')
        _rescan_doc(task, doc)   # 刷新 parts/partial/resumable，界面立刻能看到断点状态
        _record_history(task, doc)
        log(f'任务 {tid[:8]}：《{name}》{doc["status"]}'
            + (f' -> {doc["mp3"]}' if doc['mp3'] else ''))
    except Exception as e:  # noqa: BLE001
        doc['status'] = 'failed'
        doc['error'] = str(e) or type(e).__name__
        log(f'任务 {tid[:8]}：《{name}》失败 - {doc["error"]}')
        for ln in tb_tail():
            log('    ' + ln)
        _record_history(task, doc)
    finally:
        doc['finished'] = _now()
        for tok, _inst in tokens:
            pool.release(tok)


def _prune_old_uploads(days: int = UPLOAD_KEEP_DAYS) -> int:
    """清理过期上传目录（默认 7 天前）。

    任务跑完不再删上传目录，因为角色标注副本就在里面，而且"停止后重新跑"
    还要用原始文件；改为按天数兜底清理，磁盘不会无限涨。
    """
    root = TMP_DIR / 'uploads'
    if not root.is_dir():
        return 0
    cutoff = _now() - days * 86400
    n = 0
    for d in root.iterdir():
        try:
            if d.stat().st_mtime < cutoff:
                shutil.rmtree(d, ignore_errors=True)
                n += 1
        except OSError:
            pass
    if n:
        log(f'已清理 {n} 个过期上传目录（超过 {days} 天）')
    return n


def detect_roles(task, doc) -> list:
    """用规则引擎把这篇文档整理成 [角色] 标记，并登记识别到的角色。

    返回角色名列表（不含"旁白"）。标注副本写在原始文件旁边，随上传目录一起
    在 7 天后清理。
    """
    text = doc_mod.load_text(doc['path'])
    marked = doc_mod.auto_mark_roles(text)
    out = Path(doc['path']).with_suffix('.roles.txt')
    out.write_text(marked, encoding='utf-8')
    doc['role_path'] = str(out)
    names = []
    for role, _seg in doc_mod.parse_roles(marked):
        if role == doc_mod.DEFAULT_ROLE or role in names:
            continue
        names.append(role)
    for n in names:
        # 新角色默认借用任务音色，用户可以在界面上改
        task['roles'].setdefault(n, task['spk_name'])
    _rescan_doc(task, doc)
    log(f'《{doc["name"]}》识别到 {len(names)} 个角色：{"、".join(names) or "无"}')
    return names


# ─── 环境与监控 --------------------------------------------------------------
def monitor_payload():
    """把 runtime_monitor.snapshot() 归一化成前端要用的字段。

    snapshot() 的真实键是 cpu / mem_used / mem_total / gpu(dict)，
    老版本这里读的是 cpu_percent / memory_used_gb 之类根本不存在的键，
    所以"系统状态"永远显示 0。缺失项统一给 None，前端显示 N/A。
    """
    s = snapshot()
    cpu = s.get('cpu', -1)
    mem_used = s.get('mem_used', -1)
    mem_total = s.get('mem_total', -1)
    gpu = s.get('gpu') or {}
    vram_used = gpu.get('vram_used')
    vram_total = gpu.get('vram_total')
    return {
        'cpu': None if cpu is None or cpu < 0 else int(round(float(cpu))),
        'mem_used': None if mem_used is None or mem_used < 0 else round(float(mem_used), 1),
        'mem_total': None if mem_total is None or mem_total < 0 else round(float(mem_total), 1),
        'gpu': gpu.get('gpu_util'),
        'gpu_name': gpu.get('gpu_name'),
        'gpu_temp': gpu.get('gpu_temp'),
        'vram_used': None if vram_used is None else round(vram_used / 1024, 1),
        'vram_total': None if vram_total is None else round(vram_total / 1024, 1),
        'backend': backend_state['status'],
        'pool': get_pool().status(),
    }


def open_out_dir():
    d = load_settings().get('out_dir') or str(OUTPUT_DIR)
    os.startfile(d)
    return f'Opened: {d}'


_PICK_LOCK = threading.Lock()


def _pick_directory(initial: str = '') -> str:
    """在服务端（就是本机）弹出系统文件夹选择框，返回所选路径。

    服务端和浏览器在同一台机器上，所以这里等价于"选择输出文件夹"。
    tkinter 对话框只允许同时开一个，第二个请求直接报错而不是排队卡住。
    """
    if not _PICK_LOCK.acquire(blocking=False):
        raise RuntimeError('已有一个文件夹选择框打开着，请先处理它')
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        try:
            root.attributes('-topmost', True)
        except tk.TclError:
            pass
        path = filedialog.askdirectory(title='选择输出文件夹',
                                       initialdir=initial or None)
        root.destroy()
        return path or ''
    finally:
        _PICK_LOCK.release()


# ─── 模型预加载 --------------------------------------------------------------
def _missing_setup() -> str:
    """检查"上游源码 + 模型权重"是否就位。返回中文提示，空串表示都就位。

    这两个东西都不进 Git 仓库（模型 10.3 GB，CosyVoice 是上游仓库），
    所以新人第一次跑最常见的失败就是"目录是空的"，这里提前给出可照抄的命令，
    而不是让 torch 抛一长串 FileNotFoundError。
    """
    if not COSYVOICE_DIR.exists():
        return (f'未找到 CosyVoice 源码目录：{COSYVOICE_DIR}　'
                '请先运行 scripts\\setup_windows.ps1，或手动执行 '
                'git clone --recursive https://github.com/FunAudioLLM/CosyVoice.git')
    if not (MODEL_DIR / 'cosyvoice3.yaml').exists():
        return (f'未找到模型权重：{MODEL_DIR}　'
                '请运行 .venv\\Scripts\\python.exe scripts\\download_model.py '
                '（约 10 GB），或设置环境变量 COSYVOICE_MODEL_DIR 指向已有的模型目录。')
    return ''


def _preload_backend():
    """后台加载第一个后端实例，让"系统状态"尽早变成 Ready。"""
    global backend_state
    try:
        problem = _missing_setup()
        if problem:
            backend_state['status'] = 'error'
            backend_state['error'] = problem
            log(problem)
            return
        log('正在加载模型（首次启动约 60 秒）...')
        pool = get_pool()
        # 启动只备 1 个实例：够跑单篇，也不白占 6GB 显存。
        # 用户把并发调到 2 时，由调度线程 pool.ensure_async(2) 现场补第二个。
        pool.shrink_to(1)
        n = pool.ensure(1)
        backend_state['status'] = 'ready'
        log(f'模型已就绪：实例 {n} 个，已注册音色 {len(pool.loaded_ids())} 个')
    except Exception as e:  # noqa: BLE001
        backend_state['status'] = 'error'
        backend_state['error'] = str(e)
        log(f'模型加载失败：{e}')


def _start_preload():
    """开始后台加载模型。由 __main__ 在确认要真正启动后才调用，
    避免"检测到已有一个实例、准备退出"时还白加载一次模型。"""
    threading.Thread(target=_preload_backend, daemon=True).start()


# ─── API Handler ──────────────────────────────────────────────────────────────
class APIHandler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(PROJECT_ROOT), **kwargs)

    # ── 工具 ────────────────────────────────────────────────────────────────
    def read_json(self) -> dict:
        try:
            n = int(self.headers.get('Content-Length', 0) or 0)
            if n <= 0:
                return {}
            return json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception:  # noqa: BLE001
            return {}

    def _task_or_404(self, tid: str):
        with _task_lock:
            task = _active_tasks.get(tid) if tid else None
        if task is None:
            self.send_json({'ok': False, 'error': f'任务不存在：{tid}'})
            return None
        return task

    def do_GET(self):
        parsed = urlparse(self.path)
        route = parsed.path
        query = parse_qs(parsed.query)

        if route == '/api/voices':
            voices = vp.list_profiles()
            if isinstance(voices, list):
                voices = [v.get('name') if isinstance(v, dict) else v for v in voices]
            elif isinstance(voices, dict):
                voices = list(voices.keys())
            self.send_json(voices)
        elif route == '/api/monitor':
            self.send_json(monitor_payload())
        elif route == '/api/status':
            self.send_json({'status': backend_state['status'],
                            'error': backend_state['error']})
        elif route == '/api/pool':
            self.send_json(get_pool().status())
        elif route.startswith('/api/audition/'):
            self._serve_audition(route.split('/api/audition/', 1)[1])
        elif route == '/api/audio':
            self._serve_audio((query.get('path') or [''])[0])
        elif route == '/api/log':
            self.send_json({'text': log_text()})
        elif route == '/api/log/clear':
            with _LOG_LOCK:
                LOG.clear()
            self.send_json({'ok': True})
        elif route == '/api/tasks':
            with _task_lock:
                tasks = sorted(_active_tasks.values(), key=lambda t: t['seq'], reverse=True)
            self.send_json([_task_payload(t) for t in tasks])
        elif route.startswith('/api/task/'):
            task_id = route.split('/api/task/')[1].split('/')[0]
            with _task_lock:
                task = _active_tasks.get(task_id)
            self.send_json(_task_payload(task) if task else {'error': 'Task not found'})
        elif route == '/api/history':
            self.send_json(store.list_tasks(limit=50))
        elif route == '/api/open_dir':
            target = (query.get('path') or [None])[0] or load_settings().get('out_dir') or str(OUTPUT_DIR)
            try:
                p = Path(target)
                if p.suffix:                      # 传进来的是 mp3 就打开它所在目录
                    p = p.parent
                p.mkdir(parents=True, exist_ok=True)
                os.startfile(str(p))
                self.send_json({'ok': True, 'path': str(p)})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})
        elif route == '/api/open_file':
            target = (query.get('path') or [''])[0]
            try:
                if not target or not Path(target).exists():
                    raise FileNotFoundError(target or '(空路径)')
                os.startfile(target)
                self.send_json({'ok': True, 'path': target})
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})
        elif route == '/api/settings':
            s = load_settings()
            self.send_json({
                'out_dir': s.get('out_dir') or str(OUTPUT_DIR),
                'default_out_dir': str(OUTPUT_DIR),
                'spk_name': s.get('spk_name') or '',
                'speed': s.get('speed') or 1.0,
                'workers': clamp_workers(s.get('workers') or PARALLEL_DEFAULT),
                'speed_min': SPEED_MIN,
                'speed_max': SPEED_MAX,
                'workers_max': HARD_MAX_WORKERS,
                'resume': _as_bool(s.get('resume'), True),
                'chunk_seconds': CHUNK_SECONDS,
                'episode_seconds': int(s.get('episode') if s.get('episode') is not None
                                       else EPISODE_SECONDS),
                'episode_choices': list(EPISODE_CHOICES),
                'episode_max_seconds': EPISODE_MAX_SECONDS,
            })
        elif route == '/api/set_out_dir':
            path = (query.get('path') or [''])[0].strip()
            if not path:
                self.send_json({'ok': False, 'error': '路径不能为空'})
            else:
                try:
                    p = Path(path)
                    p.mkdir(parents=True, exist_ok=True)
                    save_settings(out_dir=str(p))
                    log(f'输出目录已设为 {p}')
                    self.send_json({'ok': True, 'path': str(p)})
                except Exception as e:
                    self.send_json({'ok': False, 'error': str(e)})
        elif route == '/api/pick_dir':
            initial = (query.get('initial') or [None])[0] or load_settings().get('out_dir') or str(OUTPUT_DIR)
            try:
                self.send_json({'ok': True, 'path': _pick_directory(initial)})
            except Exception as e:
                self.send_json({'ok': False, 'path': '', 'error': str(e)})
        else:
            super().do_GET()

    def _serve_audition(self, raw_name: str):
        """回放声音档案的参考原声（prompt.wav）。秒播、不占显存。"""
        from urllib.parse import unquote
        name = unquote(raw_name or '').strip()
        prof = vp.get_profile(name) if vp.is_valid_id(name) else None
        if not prof:
            self.send_error(404, 'no such profile')
            return
        try:
            data = Path(prof['wav']).read_bytes()
        except OSError:
            self.send_error(404, 'audio missing')
            return
        self.send_response(200)
        self.send_header('Content-Type', 'audio/wav')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        self.wfile.write(data)

    def _serve_audio(self, raw_path: str):
        """按绝对路径回放音频。

        成品 MP3 和半成品 MP3 都是绝对路径（输出目录可能在项目外，比如 F:\\outputs），
        所以不能直接当静态文件取。本机工具，只要求"确实存在的音频文件"。
        """
        from urllib.parse import unquote
        p = Path(unquote(raw_path or ''))
        if not p.is_file() or p.suffix.lower() not in ('.mp3', '.wav'):
            self.send_error(404, 'audio missing')
            return
        size = p.stat().st_size
        self.send_response(200)
        self.send_header('Content-Type',
                         'audio/mpeg' if p.suffix.lower() == '.mp3' else 'audio/wav')
        self.send_header('Content-Length', str(size))
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            with p.open('rb') as fh:
                shutil.copyfileobj(fh, self.wfile, 256 * 1024)
        except OSError:
            pass                       # 客户端提前断开（切歌）很正常

    # ── POST ────────────────────────────────────────────────────────────────
    def do_POST(self):
        route = urlparse(self.path).path
        try:
            if route == '/api/convert':
                self._handle_convert()
            elif route == '/api/task/doc/add':
                self._doc_add()
            elif route == '/api/task/doc/remove':
                self._doc_remove()
            elif route == '/api/task/doc/resume':
                self._doc_resume()
            elif route in ('/api/task/pause', '/api/task/resume', '/api/task/stop'):
                self._task_control(route.rsplit('/', 1)[1])
            elif route == '/api/task/workers':
                self._task_workers()
            elif route == '/api/roles/detect':
                self._roles_detect()
            elif route in ('/api/roles/set', '/api/roles/delete'):
                self._roles_edit(route.rsplit('/', 1)[1])
            elif route in ('/api/profile/create', '/api/profile/delete'):
                self._handle_profile_post()
            else:
                self.send_error(404)
        except Exception as e:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            log(f'{route} 出错：{e}')
            self.send_json({'ok': False, 'error': str(e)})

    @staticmethod
    def _save_uploads(upload_dir, uploads) -> tuple:
        """把上传内容落盘，返回 ([(显示名, 文本路径)], [提取失败清单])。

        电子书（pdf/epub/mobi/azw3/docx）在这里**就地提取成 .txt**：显示名仍是
        用户拖进来的原文件名，原始电子书也留在同目录（提取不理想时可以回看）。
        下游的切段 / 角色识别 / 断点续传永远只看见文本文件，不用知道来源格式。

        认不出的后缀按纯文本处理（和以前行为一致），只有明确是二进制电子书
        （含暂不支持的 .doc）才走提取层——失败原因也由提取层统一成人话。
        """
        items, errors = [], []
        upload_dir.mkdir(parents=True, exist_ok=True)
        for fname, data in uploads:
            safe = _safe_upload_name(fname)
            dest = upload_dir / safe
            # 同一个文件被重复拖进同一个任务：原地复用，别改名叫 book_2 ——
            # 文件名一变，work 目录就变，断点续传会认不出这是同一篇文档
            reused = dest.exists() and dest.read_bytes() == data
            if not reused:
                n = 2
                while dest.exists():
                    dest = upload_dir / f'{Path(safe).stem}_{n}{Path(safe).suffix}'
                    n += 1
                dest.write_bytes(data)

            ext = Path(safe).suffix.lower()
            if ext not in ebook.EBOOK_EXTS and ext not in ebook.LEGACY_EXTS:
                items.append((fname or safe, dest))       # 纯文本，直接用
                continue

            # 电子书：上轮已经提取过文本就直接接着用，省一次解析也保住续传
            text_path = upload_dir / f'{Path(safe).stem}.txt'
            if reused and text_path.exists():
                log(f'复用已提取的文本：{text_path.name}')
                items.append((fname or safe, text_path))
                continue

            text_path = ebook.free_text_path(upload_dir, safe)
            try:
                info = ebook.extract_text(dest, text_path)
                log(f'提取电子书：{safe} → {text_path.name}'
                    f'（{info["engine"]}，{info["chars"]} 字，{info["paragraphs"]} 段）')
                items.append((fname or safe, text_path))
            except ebook.EbookError as e:
                log(f'提取失败：{safe} —— {e.user_text()}')
                errors.append({'name': fname or safe, 'code': e.code,
                               'error': e.user_text()})
            except Exception as e:  # noqa: BLE001
                log(f'提取异常：{safe} —— {e}')
                errors.append({'name': fname or safe, 'code': 'corrupt',
                               'error': f'提取失败：{e}'})
        return items, errors

    def _multipart(self):
        """解析 multipart body，返回 (fields, {字段名: [(文件名, bytes)]})。"""
        length = int(self.headers.get('Content-Length', 0) or 0)
        body = self.rfile.read(length)
        ctype = self.headers.get('Content-Type', '')
        if 'boundary=' not in ctype:
            return None, None
        boundary = ctype.split('boundary=', 1)[1].strip().strip('"')
        return self.parse_multipart(body, boundary)

    @staticmethod
    def _pick_files(files) -> list:
        """从解析结果里挑出文档类上传（排除参考音频字段）。"""
        uploads = files.get('files') or files.get('file') or []
        if not uploads:
            for k, v in files.items():
                if k not in ('wav', 'audio'):
                    uploads = list(v)
                    break
        return uploads

    def _handle_convert(self):
        """建一个新任务：上传的文档入队后立刻拉起调度线程。"""
        fields, files = self._multipart()
        if fields is None:
            self.send_json({'status': 'Error: 请求格式不正确（需要 multipart/form-data）'})
            return

        spk_name = (fields.get('spk_name') or '').strip()
        if not spk_name:
            self.send_json({'status': 'Error: 请先选择音色档案'})
            return
        try:
            speed = float(fields.get('speed') or 1.0)
        except ValueError:
            speed = 1.0
        workers = clamp_workers(fields.get('workers') or PARALLEL_DEFAULT)
        resume = _as_bool(fields.get('resume'), True)
        episode_seconds = _episode_from_form(fields.get('episode'))
        out_dir = (fields.get('out_dir') or '').strip() or str(
            load_settings().get('out_dir') or OUTPUT_DIR)
        out_path = Path(out_dir)
        out_path.mkdir(parents=True, exist_ok=True)
        save_settings(out_dir=str(out_path), spk_name=spk_name,
                      speed=speed, workers=workers, resume=resume,
                      episode=episode_seconds)

        uploads = self._pick_files(files)
        if not uploads:
            self.send_json({'status': 'No files selected', 'task_id': None})
            return

        task_id = uuid.uuid4().hex[:12]
        items, errors = self._save_uploads(TMP_DIR / 'uploads' / task_id, uploads)
        if not items:
            # 一个都没提取成功：不建空任务，把原因原样给前端
            detail = '；'.join(f'{e["name"]}：{e["error"]}' for e in errors)
            self.send_json({'status': f'Error: {detail or "没有可用的文档"}',
                            'task_id': None, 'errors': errors})
            return
        task = _new_task(task_id, out_path, spk_name, speed, workers,
                         episode_seconds=episode_seconds)
        _add_docs(task, items, resume=resume)
        with _task_lock:
            _active_tasks[task_id] = task
        _gc_tasks()
        log(f'收到 {len(items)} 个文件：{[i[0] for i in items]}，'
            f'音色 {spk_name}，语速 {speed}，并发 {workers}，'
            f'断点续传 {"开" if resume else "关"}，'
            f'分集 {(str(episode_seconds // 60) + " 分钟") if episode_seconds else "不分集"}'
            + (f'（{len(errors)} 个提取失败）' if errors else ''))
        _ensure_dispatcher(task)
        self.send_json({'status': 'started', 'task_id': task_id,
                        'message': f'已加入 {len(items)} 个文件'
                                   + (f'，{len(errors)} 个提取失败' if errors else ''),
                        'errors': errors})

    def _doc_add(self):
        """往运行中的任务追加文档（multipart，可一次多篇）。"""
        fields, files = self._multipart()
        if fields is None:
            self.send_json({'ok': False, 'error': '请求格式不正确（需要 multipart/form-data）'})
            return
        task = self._task_or_404((fields.get('task_id') or '').strip())
        if task is None:
            return
        uploads = self._pick_files(files)
        if not uploads:
            self.send_json({'ok': False, 'error': '没有收到文件'})
            return
        items, errors = self._save_uploads(TMP_DIR / 'uploads' / task['id'], uploads)
        if not items:
            detail = '；'.join(f'{e["name"]}：{e["error"]}' for e in errors)
            self.send_json({'ok': False, 'errors': errors,
                            'error': detail or '没有可用的文档'})
            return
        resume = _as_bool(fields.get('resume'), True)
        with task['lock']:
            docs = _add_docs(task, items, resume=resume)
        log(f'任务 {task["id"][:8]}：追加 {len(docs)} 个文档'
            + ('（允许续传）' if resume else '（不续传，重跑）')
            + (f'，{len(errors)} 个提取失败' if errors else ''))
        _ensure_dispatcher(task)
        self.send_json({'ok': True, 'docs': [_doc_payload(task, d) for d in docs],
                        'errors': errors, 'task': _task_payload(task)})

    def _doc_remove(self):
        """移除文档：还没开始就移出队列，正在跑的置取消（当前段结束即停）。"""
        data = self.read_json()
        task = self._task_or_404((data.get('task_id') or '').strip())
        if task is None:
            return
        doc_id = (data.get('doc_id') or '').strip()
        with task['lock']:
            doc = _find_doc(task, doc_id)
            if doc is None:
                self.send_json({'ok': False, 'error': f'文档不存在：{doc_id}'})
                return
            if doc['status'] == 'running':
                doc['cancel'].set()
                msg = '正在中断（当前段结束后停止，保留半成品）'
            else:
                task['docs'].remove(doc)
                doc['status'] = 'cancelled'
                msg = '已移除'
        name = doc['name']
        log(f'任务 {task["id"][:8]}：{msg}《{name}》')
        self.send_json({'ok': True, 'message': msg, 'name': name,
                        'task': _task_payload(task)})

    def _doc_resume(self):
        """把一篇中断/失败/跑完的文档重新排进队列，可以选择接着上次的段继续。

        resume=true：只补后面没合成的段（音色/语速/角色映射/文本任一变了会自动
        作废重来）；resume=false：清掉旧段从头重跑。
        """
        data = self.read_json()
        task = self._task_or_404((data.get('task_id') or '').strip())
        if task is None:
            return
        doc = _find_doc(task, (data.get('doc_id') or '').strip())
        if doc is None:
            self.send_json({'ok': False, 'error': '文档不存在'})
            return
        if doc['status'] in ('running', 'pending'):
            self.send_json({'ok': False, 'error': '这篇还在队列里，不用续传'})
            return

        want = _as_bool(data.get('resume'), True)
        src = doc['role_path'] or doc['path']
        role_map = dict(task['roles']) if _doc_roles_mode(task, doc) else None
        with task['lock']:
            doc['resume'] = want
            doc['resumed'] = False
            doc['error'] = ''
            doc['status'] = 'pending'
            doc['started'] = None
            doc['finished'] = None
            doc['cancel'] = threading.Event()   # 上一轮的取消/中断标记不能留
            _rescan_doc(task, doc)
        probe = probe_resume(src, doc['spk'], task['speed'], task['out_dir'], role_map)
        can = bool(want and probe.get('resumable'))
        if can:
            msg = (f'《{doc["name"]}》重新排队：从第 '
                   f'{probe["units_done"] + 1}/{probe["units_total"]} 段接着跑')
        elif want:
            msg = f'《{doc["name"]}》重新排队：没有可用断点（或参数已变），从头重跑'
        else:
            msg = f'《{doc["name"]}》重新排队：不续传，从头重跑'
        log(f'任务 {task["id"][:8]}：{msg}')
        _ensure_dispatcher(task)   # 任务若已收尾，这里会重新拉起调度线程
        self.send_json({'ok': True, 'message': msg, 'resumed': can,
                        'task': _task_payload(task)})

    def _task_control(self, action: str):
        data = self.read_json()
        task = self._task_or_404((data.get('task_id') or '').strip())
        if task is None:
            return
        ctl = task['ctl']
        if action == 'pause':
            ctl.pause.set()
            task['status'] = 'paused'
            task['last_desc'] = '已暂停（当前段合成完后挂起）'
            msg = '已暂停'
        elif action == 'resume':
            ctl.pause.clear()
            task['status'] = 'running'
            task['last_desc'] = '已继续'
            msg = '已继续'
        else:                                   # stop
            ctl.cancel.set()
            ctl.pause.clear()                   # 别让暂停把取消挡住
            task['status'] = 'stopped'
            msg = '已停止（已生成的段落会合并成 MP3）'
            with task['lock']:
                for doc in task['docs']:
                    if doc['status'] == 'pending':
                        doc['cancel'].set()
        log(f'任务 {task["id"][:8]}：{msg}')
        _ensure_dispatcher(task)
        self.send_json({'ok': True, 'message': msg, 'task': _task_payload(task)})

    def _task_workers(self):
        """改并发数（1~2）。加：后台静默加载新实例；减：回收空闲实例。"""
        data = self.read_json()
        task = self._task_or_404((data.get('task_id') or '').strip())
        if task is None:
            return
        want = clamp_workers(data.get('workers'))
        task['workers'] = want
        pool = get_pool()
        pool.ensure_async(want)
        if pool.status()['target'] > want:
            pool.shrink_to(want)
        log(f'任务 {task["id"][:8]}：并发数改为 {want}')
        _ensure_dispatcher(task)
        self.send_json({'ok': True, 'workers': want,
                        'workers_max': HARD_MAX_WORKERS,
                        'pool': pool.status(), 'task': _task_payload(task)})

    def _roles_detect(self):
        data = self.read_json()
        task = self._task_or_404((data.get('task_id') or '').strip())
        if task is None:
            return
        doc = _find_doc(task, (data.get('doc_id') or '').strip())
        if doc is None:
            self.send_json({'ok': False, 'error': '文档不存在'})
            return
        try:
            with task['lock']:
                names = detect_roles(task, doc)
        except Exception as e:  # noqa: BLE001
            log(f'角色识别失败：{e}')
            self.send_json({'ok': False, 'error': f'识别失败：{e}'})
            return
        self.send_json({'ok': True, 'roles': names, 'task': _task_payload(task)})

    def _roles_edit(self, action: str):
        data = self.read_json()
        task = self._task_or_404((data.get('task_id') or '').strip())
        if task is None:
            return
        role = (data.get('role') or '').strip()
        if not role:
            self.send_json({'ok': False, 'error': '角色名不能为空'})
            return
        with task['lock']:
            if action == 'set':
                spk = (data.get('spk') or '').strip()
                if not spk:
                    self.send_json({'ok': False, 'error': '请选择音色'})
                    return
                task['roles'][role] = spk
                msg = f'角色 [{role}] → 音色 [{spk}]'
            else:
                task['roles'].pop(role, None)
                msg = f'已删除角色 [{role}]'
            _rescan_pending(task)
        log(f'任务 {task["id"][:8]}：{msg}')
        self.send_json({'ok': True, 'message': msg, 'task': _task_payload(task)})

    # ─── 声音档案 API ─────────────────────────────────────────────────────────
    def parse_multipart(self, body, boundary):
        """极简 multipart/form-data 解析，返回 (fields, files)。

        fields: {字段名: str}
        files:  {字段名: [(原始文件名, bytes), ...]}

        files 用列表累积：老版本用 dict，同一个字段名传多个文件时会互相覆盖，
        只剩最后一个——多选上传就是这么丢文件的。
        """
        delim = b'--' + boundary.encode('utf-8')
        fields, files = {}, {}
        for part in body.split(delim):
            if not part or part.strip() in (b'', b'--'):
                continue
            if part.startswith(b'\r\n'):
                part = part[2:]
            if part.endswith(b'\r\n'):
                part = part[:-2]
            if b'\r\n\r\n' not in part:
                continue
            head, content = part.split(b'\r\n\r\n', 1)
            name = filename = ''
            for line in head.decode('utf-8', 'replace').splitlines():
                if not name:
                    name = self._param(line, 'name')
                if not filename:
                    filename = self._param(line, 'filename')
            if not name:
                continue
            if filename:
                files.setdefault(name, []).append(
                    (_decode_filename(filename), content))
            else:
                fields[name] = content.decode('utf-8', 'replace')
        return fields, files

    @staticmethod
    def _param(line: str, key: str) -> str:
        """从 Content-Disposition 头取参数值，兼容带引号/不带引号两种写法。"""
        lower = line.lower()
        key_eq = key + '='
        idx = -1
        if lower.startswith(key_eq):
            idx = 0
        else:
            for sep in (' ', ';'):
                pos = lower.find(sep + key_eq)
                if pos >= 0:
                    idx = pos + 1
                    break
        if idx < 0:
            return ''
        val = line[idx + len(key_eq):].lstrip()
        if val.startswith('"'):
            return val[1:].split('"', 1)[0]
        return val.split(';', 1)[0].strip()

    def _handle_profile_post(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        ctype = self.headers.get('Content-Type', '')
        if 'boundary=' not in ctype:
            self.send_json({'ok': False, 'error': '请求格式不正确（需要 multipart/form-data）'})
            return
        boundary = ctype.split('boundary=', 1)[1].strip().strip('"')
        fields, files = self.parse_multipart(body, boundary)
        if self.path == '/api/profile/create':
            self._profile_create(fields, files)
        else:
            self._profile_delete(fields)

    def _profile_create(self, fields, files):
        name = (fields.get('name') or '').strip()
        txt = (fields.get('txt') or '').strip()
        uploads = (files.get('wav') or files.get('audio')
                   or files.get('files') or [])
        if not name:
            self.send_json({'ok': False, 'error': '请填写档案名称'})
            return
        if not vp.is_valid_id(name):
            self.send_json({'ok': False, 'error': '档案名只能包含中文、字母、数字、下划线、连字符（1~32 字符）'})
            return
        if not uploads:
            self.send_json({'ok': False, 'error': '请选择参考音频文件'})
            return
        if not txt:
            self.send_json({'ok': False, 'error': '逐字文字稿不能为空，必须与参考音频完全对应'})
            return
        if (vp.VOICE_PROFILES_DIR / name).exists():
            self.send_json({'ok': False, 'error': f'档案 [{name}] 已存在，请换个名字或先删除旧档案'})
            return
        if backend_state['status'] != 'ready':
            self.send_json({'ok': False, 'error': '模型尚未加载完成，请等状态变成 Ready 后再试'})
            return

        fname, data = uploads[0]
        suffix = Path(fname).suffix or '.wav'
        tmp = PROJECT_ROOT / '.tmp' / f'upload_{uuid.uuid4().hex[:8]}{suffix}'
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_bytes(data)
        try:
            # create_profile 内部会把超长参考音频按停顿裁到 30 秒内并对齐文字稿
            meta = vp.create_profile(name, str(tmp), txt)
            prof = vp.get_profile(name)
            seconds = round(audio_mod.wav_duration(prof['wav']), 1)
            note = meta.get('note') or ''
            ok = get_pool().register_all(name, prof['txt'], prof['wav'])
            if not ok:
                # 注册失败还留着档案＝留着个永远转不出来的音色，下次同名也建不了
                raise RuntimeError(get_pool().last_error
                                   or '模型没有确认注册这个音色（可看 logs/reader.log）')
            log(f'Voice profile created: {name} ({seconds}s, registered=True)'
                + (f' | {note}' if note else ''))
            self.send_json({'ok': True, 'name': name, 'seconds': seconds, 'note': note})
        except Exception as e:
            log(f'Voice profile create failed: {name} | {e}')
            try:
                if backend_state['status'] == 'ready':
                    get_pool().remove_all(name)      # 可能已经注册进某个实例了
            except Exception:  # noqa: BLE001
                pass
            vp.delete_profile(name)                  # 半个档案不留，用户改完能原地重试
            self.send_json({'ok': False, 'error': str(e)})
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass

    def _profile_delete(self, fields):
        name = (fields.get('name') or '').strip()
        if not vp.is_valid_id(name):
            self.send_json({'ok': False, 'error': '档案名不合法'})
            return
        try:
            if backend_state['status'] == 'ready':
                get_pool().remove_all(name)
        except Exception as e:
            log(f'remove_spk failed: {e}')
        ok = vp.delete_profile(name)
        log(f'Voice profile deleted: {name} ({ok})')
        self.send_json({'ok': ok, 'name': name,
                        'error': '' if ok else f'档案 [{name}] 不存在'})

    def send_json(self, data):
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(json.dumps(data, ensure_ascii=False).encode())

    def log_message(self, format, *args):
        pass


class FrontendHandler(APIHandler):
    def translate_path(self, path):
        if path == '/' or path == '':
            return str(PROJECT_ROOT / 'index.html')
        return super().translate_path(path)


def _port_busy(host: str, port: int, timeout: float = 1.0) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _reader_alive(host: str, port: int, timeout: float = 1.5) -> bool:
    """端口上有东西在监听，而且它确实是 CosyVoice Reader。"""
    import urllib.request
    try:
        with urllib.request.urlopen(f'http://{host}:{port}/api/status',
                                    timeout=timeout) as r:
            return b'"status"' in r.read(200)
    except Exception:
        return False


def _open_browser_when_ready(host: str, port: int, timeout: float = 180.0):
    """等服务端真的能响应了再开浏览器。

    老版本 start.bat 把 start 写在了 pause 之后，等于永远不开；
    这里直接由服务端自己负责，加载模型再慢也不会开出空白页。
    """
    import urllib.request
    url = f'http://{host}:{port}/'
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=3):
                pass
            # 3 分钟内重复启动不再开新标签页（旧标签页刷新一下就是最新的）
            _open_browser_once(url)
            return
        except Exception:
            time.sleep(0.5)
    print(f'浏览器没能自动打开，请手动访问 {url}')


# --- 别把浏览器标签页堆起来 ----------------------------------------------
#
# 真实反馈：「为什么在 Edge 里打开了好多个 127.0.0.1:7860？」
#
# 原因不是程序循环开页，而是：
#   1. Windows 下 webbrowser.open 就是 os.startfile(url)，**每次都新开一个标签页**，
#      浏览器不会复用已经开着的那个（URL 一直没变，旧标签页刷新一下就是最新的）；
#   2. 模型要加载 15-20 秒，这段时间里再双击一次快捷方式，就会走「已经在运行」
#      这条分支，于是又开一个；
#   3. Edge 的「恢复上次会话」会把这些标签页原样带回来，看起来就像越开越多。
#
# 所以短时间内重复启动时不再重复开：记录上次开页的时间，3 分钟内只开一次。
BROWSER_STAMP = PROJECT_ROOT / 'logs' / '.browser_opened'
BROWSER_OPEN_GAP = 180.0


def _browser_recently_opened(gap: float = BROWSER_OPEN_GAP) -> bool:
    try:
        return (time.time() - BROWSER_STAMP.stat().st_mtime) < gap
    except OSError:
        return False


def _mark_browser_opened() -> None:
    try:
        BROWSER_STAMP.parent.mkdir(parents=True, exist_ok=True)
        BROWSER_STAMP.write_text(str(int(time.time())), encoding='utf-8')
    except OSError:
        pass


def _open_browser_once(url: str) -> bool:
    """开浏览器，但 3 分钟内不重复开新标签页。"""
    if _browser_recently_opened():
        print(f'浏览器刚刚已经开过，不重复开新标签页。界面地址：{url}')
        return False
    webbrowser.open(url)
    _mark_browser_opened()
    print(f'已打开浏览器：{url}')
    return True


# --- 输出接管：绝不让自己卡在「写不出去」上 ------------------------------
#
# 真实事故（GPU 0%、界面显示运行中、两篇文档一段都不动）：
# 服务是用 `Start-Process cmd.exe /c start.bat` 拉起来的，启动它的进程退出以后，
# 子进程的 stdout 还握在一个没人读的管道上。管道缓冲区（几十 KB）填满后，
# 下一次写入就**永远阻塞**；而日志的 handler 锁在阻塞期间一直被占着，于是之后
# **每个**想打日志的线程都排队死等——两篇文档的合成线程就是这样一起停住的。
# （py-spy 抓到的现场：两个 _run_doc 线程一个卡在 logging emit，一个在等同一把锁。）
#
# 写文件不会因为没人读而阻塞，所以把 stdout/stderr 落到 logs\reader.log。
LOG_DIR = PROJECT_ROOT / 'logs'
LOG_FILE = LOG_DIR / 'reader.log'
_LOG_STREAM = None


class _UiLogHandler(logging.Handler):
    """把依赖库（CosyVoice 等）的 WARNING 以上日志也送进界面的运行日志。

    只收 WARNING 以上：合成进度走的是我们自己的 log()，这里再收 INFO 会把
    界面刷屏，而且每段一条会让 LOG 环形缓冲把有用的信息挤掉。
    """

    def emit(self, record):
        try:
            if record.levelno < logging.WARNING:
                return
            msg = record.getMessage().replace('\n', ' ')
            log(f'[{record.name}] {msg[:200]}')
        except Exception:      # noqa: BLE001 —— 日志绝不能反过来把主流程搞挂
            pass


def _is_regular_file(stream) -> bool:
    """这个流是不是已经落到真正的文件上了（start.bat 里重定向过就是）。"""
    try:
        return stat.S_ISREG(os.fstat(stream.fileno()).st_mode)
    except Exception:          # noqa: BLE001 —— 控制台/管道/已关闭都当"不是文件"
        return False


def _setup_output(max_mb: int = 8) -> None:
    """确保 stdout/stderr 写在文件上，并把 logging 的 handler 也挪过去。"""
    global _LOG_STREAM
    orig_out, orig_err = sys.stdout, sys.stderr
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    try:
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > max_mb * 1024 * 1024:
            LOG_FILE.unlink()                       # 别长成无限大的日志
    except OSError:
        # start.bat 的重定向正握着这个文件，Windows 不让删打开的文件。
        # 这里绝不能因此提前 return，否则下面 logging handler 的改绑就被跳过了。
        pass
    try:
        if not _is_regular_file(orig_err):
            stream = open(LOG_FILE, 'a', encoding='utf-8', errors='replace',
                          buffering=1)
            _LOG_STREAM = stream
            sys.stdout = sys.stderr = stream
            try:                                    # 只有这一次机会告诉用户在哪儿
                sys.__stdout__.write(f'[日志写入 {LOG_FILE}]\n')
                sys.__stdout__.flush()
            except Exception:                       # noqa: BLE001
                pass
        else:
            _LOG_STREAM = orig_err                  # start.bat 已经重定向好了
    except OSError:
        return

    # CosyVoice 在 import 时就建好了 StreamHandler，那时候绑的还是老的控制台，
    # 这里把它们改指到文件，否则它照样会往那个可能已经堵死的流里写。
    root = logging.getLogger()
    if root.level > logging.INFO or root.level == logging.NOTSET:
        root.setLevel(logging.INFO)
    target = _LOG_STREAM
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
            if any(getattr(h, 'stream', None) is s for s in (orig_out, orig_err)):
                try:
                    h.setStream(target)
                except Exception:                   # noqa: BLE001
                    pass
    have_file = any(isinstance(h, logging.FileHandler) for h in root.handlers)
    if not have_file:
        try:
            fh = logging.FileHandler(LOG_FILE, encoding='utf-8', errors='replace')
            fh.setFormatter(logging.Formatter(
                '%(asctime)s %(levelname)s %(name)s: %(message)s'))
            root.addHandler(fh)
        except OSError:
            pass
    root.addHandler(_UiLogHandler())


if __name__ == '__main__':
    _setup_output()

    no_browser = os.environ.get('COSYVOICE_NO_BROWSER') == '1'
    url = f'http://{WEBUI_HOST}:{WEBUI_PORT}/'

    # 已经在运行就直接把浏览器开出来，不要再叠一个新实例：
    # 每个实例都会各自加载一份完整模型，16G 显存几次就被占满——这正是之前
    # "越用越卡、显存 0/0" 的根源。
    if _port_busy(WEBUI_HOST, WEBUI_PORT):
        if _reader_alive(WEBUI_HOST, WEBUI_PORT):
            print(f'CosyVoice Reader 已经在运行：{url}')
            if not no_browser:
                _open_browser_once(url)
            # 退出码 3 = "已在运行，浏览器已打开"；start.bat 据此自动关窗，
            # 不停在 pause 上，免得用户以为又卡住了。
            sys.exit(3)
        print(f'端口 {WEBUI_PORT} 被其它程序占用了，无法启动。请先关掉占用它的程序，'
              f'或者运行 scripts\\stop.bat。')
        sys.exit(1)

    # 禁止 SO_REUSEADDR：Windows 上它会让第二个实例悄悄绑上同一端口，
    # 请求随机落到其中一个实例上，界面和日志就对不上了。
    ThreadingHTTPServer.allow_reuse_address = False
    ThreadingHTTPServer.daemon_threads = True

    print(f'CosyVoice Reader starting on {url}')
    print('Press Ctrl+C to stop')
    try:
        httpd = ThreadingHTTPServer((WEBUI_HOST, WEBUI_PORT), FrontendHandler)
    except OSError as e:
        print(f'端口 {WEBUI_PORT} 已被占用，启动失败：{e}')
        print(f'可能已经开着一个了，直接访问 {url} 即可；'
              f'要彻底重启用 scripts\\stop.bat。')
        sys.exit(1)

    _prune_old_uploads()
    if not no_browser:
        threading.Thread(target=_open_browser_when_ready,
                         args=(WEBUI_HOST, WEBUI_PORT), daemon=True).start()
    _start_preload()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print('\nServer stopped')
    finally:
        httpd.server_close()
