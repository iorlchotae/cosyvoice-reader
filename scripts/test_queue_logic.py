"""任务队列逻辑自测（**不加载模型**）。

用假的实例池和假的合成函数驱动**真实的**调度代码，验证最难验证的那部分：

  - 文档一入队就预扫描出段数，进度有分母
  - 暂停在段边界生效（百分比冻结、已生成段落不丢），继续后接着跑
  - 停止时保留半成品 MP3
  - 运行中增减并发数：扩容后才并行，缩容不打断在跑的文档
  - 运行中追加 / 移除文档

用法：
    .venv\\Scripts\\python scripts\\test_queue_logic.py
"""

import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT))

import app  # noqa: E402
from config import OUTPUT_DIR  # noqa: E402

OUT = OUTPUT_DIR / '_queue_test'

# ── 假实例池：扩容瞬间完成，用来观察调度行为 ────────────────────────────────
class FakePool:
    def __init__(self, cap: int = 2):
        self.cap = cap
        self._locks = [threading.Lock() for _ in range(cap)]
        self.target = 1
        self.loaded = 1
        self.shrunk = []

    def status(self):
        return {'loaded': self.loaded,
                'busy': sum(1 for l in self._locks if l.locked()),
                'target': self.target, 'loading': False, 'error': ''}

    def ensure_async(self, n):
        n = max(1, min(self.cap, int(n)))
        self.target = max(self.target, n)
        self.loaded = max(self.loaded, n)

    def ensure(self, n, timeout=10.0):
        self.ensure_async(n)
        return self.loaded

    def shrink_to(self, n):
        n = max(1, min(self.cap, int(n)))
        self.target = min(self.target, n)
        if self.loaded > n:
            self.shrunk.append(n)
            self.loaded = n
        return 1

    def try_acquire(self):
        for l in self._locks[:self.loaded]:
            if l.acquire(blocking=False):
                return l, object()
        return None

    def acquire(self, timeout=None):
        deadline = time.time() + (timeout or 10)
        while time.time() < deadline:
            got = self.try_acquire()
            if got:
                return got
            time.sleep(0.02)
        raise TimeoutError('fake pool empty')

    def release(self, token):
        token.release()

    def loaded_ids(self):
        return []

    def register_all(self, *a, **k):
        return True

    def remove_all(self, *a, **k):
        return None


POOL = FakePool()

# 并发峰值：用来断言"加线程之后确实是并行在跑"
CONCURRENT = {'now': 0, 'peak': 0}
_cc_lock = threading.Lock()


def fake_synthesize(inst, filepath, spk_name, speed, out_dir, role_map=None,
                    step_cb=None, unit_cb=None, ctl=None,
                    resume=True, chunk_seconds=None, extra_backends=None):
    """假的合成：文档内容用 '啊'*1200 的段落数表示段数。

    签名必须跟着真的 `synthesize_core` 走——少一个关键字参数就会 TypeError，
    而异常是在 dispatcher 线程里被吞进 doc['error'] 的，表现成「进度不动」，
    很难看出是打桩过时。
    """
    text = Path(filepath).read_text(encoding='utf-8')
    units = len([p for p in text.split('\n\n') if p.strip()])
    with _cc_lock:
        CONCURRENT['now'] += 1
        CONCURRENT['peak'] = max(CONCURRENT['peak'], CONCURRENT['now'])
    try:
        produced = 0
        for i in range(1, units + 1):
            if ctl is not None and ctl.wait_if_paused():
                break
            time.sleep(0.12)            # 假装在合成一段
            produced += 1
            if unit_cb:
                unit_cb(i, units)
    finally:
        with _cc_lock:
            CONCURRENT['now'] -= 1
    if not produced:
        return None, 0.0
    mp3 = Path(out_dir) / Path(filepath).stem / f'{Path(filepath).stem}.mp3'
    mp3.parent.mkdir(parents=True, exist_ok=True)
    mp3.write_bytes(b'MP3' * produced)
    return mp3, produced * 1.5


# ── 打桩 ────────────────────────────────────────────────────────────────────
app.get_pool = lambda size=None: POOL
app.synthesize_core = fake_synthesize
app.backend_state['status'] = 'ready'
app.store.create_task = lambda *a, **k: 'stub'
app.store.update_task = lambda *a, **k: None


def make_doc(name: str, units: int) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    p = OUT / name
    p.write_text('\n\n'.join(['啊' * 1200] * units), encoding='utf-8')
    return p


def new_task(units=5, workers=1, name='a.txt'):
    tid = f't{int(time.time() * 1000) % 100000}'
    task = app._new_task(tid, OUT, 'nar', 1.0, workers)
    app._add_docs(task, [(name, make_doc(name, units))])
    app._active_tasks[tid] = task
    return task


def wait_until(pred, timeout=15.0, what='条件'):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.03)
    raise AssertionError(f'等待超时：{what}')


def check(label, cond, extra=''):
    if not cond:
        raise AssertionError(f'FAIL {label} {extra}')
    print(f'  ok  {label} {extra}', flush=True)


# ── 各项测试 ────────────────────────────────────────────────────────────────
def test_prescan():
    print('[1] 入队即预扫描段数', flush=True)
    task = new_task(units=4)
    check('预扫描段数=4', task['docs'][0]['units_total'] == 4)
    p = app._task_payload(task)
    check('payload 有分母', p['totals']['units'] == 4)
    check('并发上限=2', p['workers_max'] == 2)


def test_pause_resume():
    print('[2] 暂停 / 继续（段边界）', flush=True)
    task = new_task(units=5)
    app._ensure_dispatcher(task)
    doc = task['docs'][0]
    wait_until(lambda: doc['units_done'] >= 2, what='跑到第 2 段')

    app._task_control_stub = None
    task['ctl'].pause.set()
    time.sleep(0.15)                       # 让在跑的那一段收尾
    frozen = doc['units_done']
    time.sleep(0.5)                        # 暂停期间不该再涨
    check('暂停后进度冻结', doc['units_done'] == frozen, f'at {frozen}')
    check('状态=paused', task['status'] == 'paused', task['status'])

    task['ctl'].pause.clear()
    wait_until(lambda: doc['status'] == 'done' and task['status'] == 'done',
               what='继续后跑完')
    check('继续后跑完 5 段', doc['units_done'] == 5)
    check('状态=done', task['status'] == 'done', task['status'])
    check('半成品都在', Path(doc['mp3']).exists())


def test_stop_keeps_partial():
    print('[3] 停止保留半成品', flush=True)
    task = new_task(units=8)
    app._ensure_dispatcher(task)
    doc = task['docs'][0]
    wait_until(lambda: doc['units_done'] >= 2, what='跑到第 2 段')
    task['ctl'].cancel.set()
    wait_until(lambda: doc['status'] in ('cancelled', 'done'), what='停下')
    check('停在段边界', 0 < doc['units_done'] < 8, f"units_done={doc['units_done']}")
    check('保留了半成品 mp3', bool(doc['mp3']) and Path(doc['mp3']).exists())
    check('任务状态=stopped', task['status'] == 'stopped', task['status'])


def test_live_workers():
    print('[4] 运行中增减并发', flush=True)
    CONCURRENT['peak'] = 0
    task = new_task(units=10, workers=1, name='w1.txt')
    with task['lock']:
        app._add_docs(task, [('w2.txt', make_doc('w2.txt', 10))])
    app._ensure_dispatcher(task)
    time.sleep(0.3)
    check('并发 1 时没有并行', CONCURRENT['peak'] == 1, f"peak={CONCURRENT['peak']}")

    task['workers'] = 2                    # 相当于前端点了 ＋
    wait_until(lambda: CONCURRENT['peak'] == 2, what='两个实例同时忙')
    check('加到 2 后确实并行', CONCURRENT['peak'] == 2)
    check('池子扩到 2', POOL.status()['loaded'] == 2)

    task['workers'] = 1                    # 相当于前端点了 －
    wait_until(lambda: POOL.shrunk, what='空闲实例被回收')
    check('缩容回收了空闲实例', bool(POOL.shrunk), str(POOL.shrunk))

    wait_until(lambda: all(d['status'] == 'done' for d in task['docs']),
               what='两篇跑完')
    check('两篇都完成', all(d['status'] == 'done' for d in task['docs']))


def test_add_remove_docs():
    print('[5] 运行中增删文档', flush=True)
    task = new_task(units=6, name='m1.txt')
    app._ensure_dispatcher(task)
    wait_until(lambda: task['docs'][0]['units_done'] >= 1, what='开跑')

    with task['lock']:
        added = app._add_docs(task, [('m2.txt', make_doc('m2.txt', 2)),
                                     ('m3.txt', make_doc('m3.txt', 2))])
    check('追加的文档立刻有段数', added[0]['units_total'] == 2)

    with task['lock']:
        victim = added[1]                  # 撤掉后加的第二篇
        task['docs'].remove(victim)
        victim['status'] = 'cancelled'
    check('移除后不在队列里', all(d['doc_id'] != victim['doc_id']
                                for d in task['docs']))
    wait_until(lambda: task['status'] in ('done', 'failed'), what='跑完')
    check('其余文档跑完', task['docs'][0]['status'] == 'done')
    check('追加的也没被落下', added[0]['status'] == 'done', added[0]['status'])
    check('撤掉的没被跑', victim['status'] == 'cancelled')


def main() -> int:
    import shutil
    shutil.rmtree(OUT, ignore_errors=True)
    for fn in (test_prescan, test_pause_resume, test_stop_keeps_partial,
               test_live_workers, test_add_remove_docs):
        fn()
    print('\nQUEUE_LOGIC_OK', flush=True)
    return 0


if __name__ == '__main__':
    sys.exit(main())
