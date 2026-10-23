"""任务记录：每个转换任务一个 JSON，落在 tasks/ 目录。"""

import json
import threading
import time
import uuid
from pathlib import Path

from config import TASKS_DIR

_LOCK = threading.Lock()

STATUS_QUEUED = 'queued'
STATUS_RUNNING = 'running'
STATUS_DONE = 'done'
STATUS_FAILED = 'failed'


def _path(task_id: str) -> Path:
    return TASKS_DIR / f'{task_id}.json'


def create_task(filename: str, out_dir: str, spk_id: str, speed: float) -> str:
    task_id = uuid.uuid4().hex[:12]
    task = {
        'id': task_id,
        'filename': filename,
        'status': STATUS_QUEUED,
        'chunks_total': 0,
        'chunks_done': 0,
        'segments_done': 0,
        'speed': speed,
        'spk_id': spk_id,
        'out_dir': out_dir,
        'mp3_path': '',
        'error': '',
        'duration': 0.0,
        'created': time.time(),
        'finished': None,
    }
    with _LOCK:
        _path(task_id).write_text(json.dumps(task, ensure_ascii=False, indent=2),
                                  encoding='utf-8')
    return task_id


def update_task(task_id: str, **fields) -> None:
    with _LOCK:
        p = _path(task_id)
        if not p.exists():
            return
        task = json.loads(p.read_text(encoding='utf-8'))
        task.update(fields)
        if fields.get('status') in (STATUS_DONE, STATUS_FAILED, 'stopped'):
            task['finished'] = time.time()
        p.write_text(json.dumps(task, ensure_ascii=False, indent=2),
                     encoding='utf-8')


def get_task(task_id: str) -> dict:
    p = _path(task_id)
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding='utf-8'))


def list_tasks(limit: int = 20) -> list:
    files = sorted(TASKS_DIR.glob('*.json'),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:limit]
    return [json.loads(p.read_text(encoding='utf-8')) for p in files]


def reconcile_interrupted(
    note: str = '应用重启，任务中断；重新拖入同一文件可续传',
) -> int:
    """启动时把上次遗留的 queued/running 任务标记为 stopped。

    任务本体只活在内存里，重启后 _active_tasks 清空；若不在落盘记录上
    收尾，这些任务在界面上会凭空消失（历史里永远显示“进行中”）。
    返回改写的条数。
    """
    changed = 0
    with _LOCK:
        for p in TASKS_DIR.glob('*.json'):
            try:
                task = json.loads(p.read_text(encoding='utf-8'))
            except (OSError, json.JSONDecodeError):
                continue
            if task.get('status') in (STATUS_QUEUED, STATUS_RUNNING):
                task['status'] = 'stopped'
                task['error'] = task.get('error') or note
                task['finished'] = time.time()
                p.write_text(json.dumps(task, ensure_ascii=False, indent=2),
                             encoding='utf-8')
                changed += 1
    return changed
