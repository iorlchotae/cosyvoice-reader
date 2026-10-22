"""后端实例池：同时持有多个 CosyVoice3Backend，支持并行合成多份文件。

显存约为 实例数 × 6GB（fp16）。16GB 显存建议最多 2 个实例。

设计要点
--------
- **稳定单例**：`get_pool()` 一旦创建就不再重建，实例数用 `ensure_async()` /
  `shrink_to()` 原地增减。老版本按 `size` 重建整个池，等于每次改并发数都要把
  模型重新加载一遍（约 60 秒 + 6GB），"随时增减线程"根本无从谈起。
- **租借令牌是槽位自己的 Lock 对象**，`release(token)` 直接释放它。这样回收中间
  某个槽位时，其它持有者手里的令牌依然有效；按下标释放会在这里踩坑。
- **加载是后台静默的**：`ensure_async()` 立即返回，加载进度通过 `status()` 暴露。
  调度线程不必等模型加载完才能继续照看已经在跑的文档。
"""

import threading
import time

from backends.cosyvoice3 import CosyVoice3Backend
from config import MODEL_DIR, PARALLEL_DEFAULT, USE_FP16


class BackendPool:
    """懒创建 N 个后端实例的池，任务提交时按空闲分配。"""

    def __init__(self, size: int = 2, model_dir=None, fp16=USE_FP16):
        self.model_dir = model_dir or MODEL_DIR
        self.fp16 = fp16
        self._slots: list = []          # [{'lock': Lock, 'inst': backend}]
        self._lock = threading.RLock()
        self._target = max(1, int(size))
        self._loading = 0
        self._loader = None
        self.last_error = ''

    # ── 状态 ────────────────────────────────────────────────────────────────
    @property
    def size(self) -> int:
        """向后兼容旧调用：目标实例数。"""
        return self._target

    def status(self) -> dict:
        with self._lock:
            busy = sum(1 for s in self._slots if s['lock'].locked())
            return {
                'loaded': len(self._slots),
                'busy': busy,
                'target': self._target,
                'loading': self._loading > 0,
                'error': self.last_error,
            }

    def instances(self) -> list:
        with self._lock:
            return [s['inst'] for s in self._slots]

    def loaded_ids(self) -> list:
        """所有实例上已注册的音色 ID 并集。"""
        ids = set()
        for inst in self.instances():
            try:
                ids.update(inst.spk_ids)
            except Exception:  # noqa: BLE001
                pass
        return sorted(ids)

    # ── 扩缩容 ──────────────────────────────────────────────────────────────
    def ensure_async(self, need: int) -> None:
        """把目标实例数提到 need，并在后台补齐（立即返回，不阻塞调用方）。"""
        need = max(1, int(need))
        with self._lock:
            if need > self._target:
                self._target = need
            if len(self._slots) + self._loading >= self._target:
                return
            if self._loader is not None and self._loader.is_alive():
                # 已有加载线程在跑，它会读到更新后的 _target 继续补齐
                return
            self._loader = threading.Thread(target=self._load_loop, daemon=True)
            self._loader.start()

    def ensure(self, need: int, timeout: float = 600.0) -> int:
        """同步版本：等到至少有 need 个实例就绪（或超时），返回实际个数。"""
        self.ensure_async(need)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if len(self._slots) >= need:
                    return len(self._slots)
                if self.last_error:
                    raise RuntimeError(f'后端实例加载失败：{self.last_error}')
            time.sleep(0.2)
        raise TimeoutError(f'等待 {need} 个后端实例超时')

    def _load_loop(self) -> None:
        # 每轮都重新读 _target：并发数在加载过程中被调大时，这一个线程会接着补，
        # 不用等它退出再新起一个线程。
        while True:
            with self._lock:
                if len(self._slots) + self._loading >= self._target:
                    return
                self._loading += 1
                nth = len(self._slots) + 1
            print(f'[pool] 加载第 {nth} 个后端实例（约 60 秒 / 约 6GB 显存）...',
                  flush=True)
            try:
                inst = CosyVoice3Backend(model_dir=self.model_dir, fp16=self.fp16)
            except Exception as e:  # noqa: BLE001
                with self._lock:
                    self._loading = max(0, self._loading - 1)
                    self.last_error = str(e)
                print(f'[pool] 后端实例加载失败：{e}', flush=True)
                return
            with self._lock:
                self._loading = max(0, self._loading - 1)
                self._slots.append({'lock': threading.Lock(), 'inst': inst})
                self.last_error = ''
            print(f'[pool] 第 {nth} 个后端实例就绪', flush=True)

    def shrink_to(self, n: int) -> int:
        """回收多余的空闲实例，返回实际回收个数。正在干活的一个都不动。"""
        n = max(1, int(n))
        with self._lock:
            if n < self._target:
                self._target = n
        dropped = 0
        while True:
            victim = None
            with self._lock:
                if len(self._slots) <= n:
                    break
                # 从后往前找一个当前空闲的槽位（acquire 成功即说明没人在用）
                for i in range(len(self._slots) - 1, -1, -1):
                    if self._slots[i]['lock'].acquire(blocking=False):
                        victim = self._slots.pop(i)
                        break
                if victim is None:
                    break               # 全在忙，等它们空了再回收
            victim['inst'] = None       # 交给 GC 释放显存
            dropped += 1
        if dropped:
            try:
                import torch
                torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            print(f'[pool] 已回收 {dropped} 个空闲实例', flush=True)
        return dropped

    # ── 租借 ────────────────────────────────────────────────────────────────
    def try_acquire(self):
        """尝试拿一个空闲实例，没有就返回 None。成功返回 (token, instance)。"""
        with self._lock:
            for s in self._slots:
                if s['lock'].acquire(blocking=False):
                    return s['lock'], s['inst']
        return None

    def acquire(self, timeout: float | None = None):
        """拿一个空闲实例，必要时等待（并触发懒加载）。返回 (token, instance)。"""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            got = self.try_acquire()
            if got is not None:
                return got
            self.ensure_async(max(1, self._target))
            if deadline is not None and time.time() >= deadline:
                raise TimeoutError('等待后端实例超时')
            time.sleep(0.1)

    def acquire_up_to(self, k: int) -> list:
        """一次拿最多 k 个空闲实例（拿不满也返回，不额外等待）。"""
        self.ensure_async(k)
        got = []
        while len(got) < k:
            item = self.try_acquire()
            if item is None:
                break
            got.append(item)
        return got

    def release(self, token) -> None:
        """释放租借令牌（就是 acquire 返回的第一个元素）。"""
        try:
            token.release()
        except (RuntimeError, AttributeError):
            pass

    # ── 音色档案同步 ────────────────────────────────────────────────────────
    def register_all(self, spk_id: str, prompt_text: str, prompt_wav: str) -> bool:
        """把声音档案注册到所有实例，只注册缺失的那些。"""
        ok = False
        for inst in self.instances():
            try:
                if spk_id in inst.spk_ids:
                    continue
                ok = bool(inst.register_spk(spk_id, prompt_text, prompt_wav)) or ok
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)
        return ok

    def remove_all(self, spk_id: str) -> None:
        for inst in self.instances():
            try:
                inst.remove_spk(spk_id)
            except Exception as e:  # noqa: BLE001
                self.last_error = str(e)


_pool = None
_pool_lock = threading.Lock()


def get_pool(size: int = PARALLEL_DEFAULT) -> BackendPool:
    """返回稳定的后端实例池单例。

    `size` 只在**首次创建**时决定初始目标实例数；之后再怎么改都不会重建池——
    重建会让已经加载好的模型全部作废，那正是"一改并发数就卡一分钟"的根源。
    要改实例数请用 `ensure_async()` / `shrink_to()`。
    """
    global _pool
    if _pool is not None:
        return _pool
    with _pool_lock:
        if _pool is None:
            _pool = BackendPool(size=size)
    return _pool
