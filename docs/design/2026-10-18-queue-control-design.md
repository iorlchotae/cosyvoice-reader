# CosyVoice Reader — 任务队列控制与多角色配音 设计文档

日期：2026-10-18
状态：已与用户确认，待实现

## 背景

用户提出五项需求（2026-10-18）：

1. 多线程处理，**运行中**可随时增减并发数
2. **运行中**可随时增删文档、增删人物（角色）
3. 当前任务随时暂停 / 停止的按钮
4. 转换任务进度用**百分比 + 进度条**表达
5. 现有音色可**试听**

现状问题（读码确认）：

- `backends/pool.py:73 get_pool(size)` 一旦 `size` 变化就丢掉整个池重建
  （`if _pool is None or _pool.size != size: _pool = BackendPool(size=size)`），
  等于每次改并发数都要重新加载模型（约 60 秒 + 6GB 显存）。
- `BackendPool.acquire()` 内部只 `_ensure_loaded(1)`，池永远不会自动长到 2 个实例，
  所以"并行 2 个文件"实际上一直串行。
- `synthesize_core()` 是一个跑到底的循环，中间没有暂停/取消的切入点。
- `app.py` 的任务在创建时就把文件列表、音色、并发数固化，运行中无法改动。
- 多角色能力后端早就写好（`document.auto_mark_roles()` 规则引擎、
  `synthesize_core(role_map=...)`、`load_document_roles()`），但 Web 界面从未接上，
  且 `run_batch()` 里写死 `role_map = build_role_map(None)`（= 空）→ 永远是单音色。
- 前端只显示 `progress` 百分比数字，没有进度条，也没有"已完成段/总段数/失败数"。
- 音色档案有 `prompt.wav` 参考音频，但没有任何播放入口。

## 硬约束（用户确认）

- **并发上限锁死 2。** 每个后端实例约 6GB 显存（fp16），16GB 卡实际只扛得住 2 个。
  第 3 个开始爆显存并变慢，因此不做"允许 3 个但警告"，直接锁 2。
- **停止时保留半成品 MP3。** 已经生成的段落照常合并输出，不丢弃已花费的算力。
- 暂停语义为**段边界暂停**：当前段合成完才挂起，已落盘的段落不重做。

## 现实约束（必须写进 UI 文案，避免用户误解）

CosyVoice 合成是**按段**调用的（一段约 1000 字 / 10~20 秒），同一篇文档内部的段落
**严格串行**——单个实例一次只能吃一段。所以：

- 加并发只能让**多篇文档同时跑**，**不能让单篇文档变快**。
- 同时丢 3 篇文档进去，2 个实例 ≈ 快 2 倍；只跑 1 篇，加线程无用。

## 数据模型

任务（`app.py` 内存中 `_active_tasks[id]`）：

```python
task = {
  'id', 'name', 'out_dir', 'spk_name', 'speed',
  'workers': int,             # 运行中可变，实际生效值 clamp 到 [1, 2]
  'roles': {角色名: 档案名},   # 活映射，改完对"尚未合成的段"立刻生效
  'docs': [doc, ...],         # 可变列表，运行中可增删
  'status': 'queued'|'running'|'paused'|'done'|'failed'|'stopped',
  'created', 'started', 'finished', 'last_desc', 'error',
  'ctl': _TaskCtl(),          # pause / cancel 两个 Event
  'dispatcher': Thread|None,  # 调度线程，空了可重新拉起
}
```

文档（`task['docs']` 中的一项）：

```python
doc = {
  'doc_id', 'name', 'path',            # path = 原始上传文件
  'role_path': str|None,               # 「识别角色」后写出的 [角色] 标注副本
  'spk': str,                          # 单音色模式下用的档案
  'status': 'pending'|'running'|'done'|'failed'|'cancelled',
  'units_total': int, 'units_done': int,
  'mp3': str|None, 'duration': float, 'error': str,
  'created', 'started', 'finished',
  'cancel': threading.Event(),          # 单文档取消（用于"移除正在跑的文档"）
}
```

模式判定：`doc['role_path'] and task['roles']` 为真 → 多角色模式，音色按
`task['roles']` 查表（查不到回落 `doc['spk']`）；否则单音色模式。

进度汇总（服务端计算，前端直接显示）：

- 总进度 = `Σ units_done / Σ units_total`（`units_total` 在文档加入时**预先扫描**得出，
  纯文本运算、不占显卡，所以进度条一开始就知道分母）
- 同时给出 `已完成文档/总文档`、`总段数`、`已完成段`、`失败数`、`已用时间`

## 组件改动

### `config.py`

新增 `HARD_MAX_WORKERS = 2`，注释说明显存依据。Web 路径的 `workers` 一律 clamp 到它。

### `backends/pool.py`（重写）

- `get_pool()` 改为**稳定单例**：只有 `_pool is None` 时才创建，`size` 参数仅决定
  初始目标实例数，**不再因为 size 变化而重建**。这是"随时增减线程不重新加载模型"的前提。
- 槽位改为对象列表 `self._slots = [{'lock': Lock, 'inst': backend}]`。
  **租借令牌 = 那个 Lock 对象本身**，`release(token)` 直接 `token.release()`。
  这样删除槽位不会让别的持有者索引错位（按 idx 删除会踩这个坑）。
- 新增 API：
  - `try_acquire() -> (token, inst) | None`
  - `acquire(timeout=None) -> (token, inst)`（兼容旧调用，循环 + 懒加载）
  - `release(token)`
  - `ensure_async(n)` — **后台静默加载**到 n 个实例，立即返回；
    加载状态通过 `status()` 暴露，UI 显示"正在加载第 2 个实例…"
  - `shrink_to(n)` — 只回收**当前空闲**的实例（`lock.acquire(False)` 成功的），
    忙着的一个不动；回收后 `torch.cuda.empty_cache()`（torch 此时必然已在进程中）
  - `status() -> {'loaded', 'target', 'loading', 'error'}`
  - `register_all(name, txt, wav)` / `remove_all(name)` — 档案增删时同步到所有实例
- `acquire()` 不再只 `_ensure_loaded(1)`：调用方通过 `ensure_async(n)` 明确表达要几个。

### `backends/synthesize.py`

`synthesize_core(backend, filepath, spk_name, speed, out_dir, role_map=None,
step_cb=None, unit_cb=None, ctl=None)`：

- `ctl`（鸭子类型）需提供 `wait_if_paused() -> bool`（返回 `True` = 已被取消）。
  在**每段开始前**调用一次：暂停时阻塞在这里，取消时跳出循环。
- `unit_cb(done, total)` 每段写完 wav 后回调，用于精确的段进度。
- 取消时：跳出循环后**仍然合并已生成的 wav**（半成品 MP3 保留），
  若一个 wav 都没有则返回 `(None, 0.0)`。
- `_parallel_one` / `parallel_run` 跟着改成令牌 API（`pool.acquire()` → `(token, inst)`、
  `pool.release(token)`），`parallel_run` 里 `get_pool(workers)` → `get_pool()` +
  `ensure_async(workers)`。`scripts/test_parallel.py` 的两处调用同步更新。

### `document.py`

- 抽一个 `load_text(path) -> str`（clean + 按后缀决定是否 strip_markdown），
  `load_document()` / `load_document_roles()` 改为调用它，消除重复的编码/清洗分支。
- 新增 `count_units(path, roles_mode: bool) -> int`，供"加入文档时预扫描段数"使用。

### `app.py`

**任务控制块**

```python
class _TaskCtl:
    def __init__(self): self.pause = Event(); self.cancel = Event()
    def wait_if_paused(self) -> bool:
        while self.pause.is_set() and not self.cancel.is_set():
            time.sleep(0.2)
        return self.cancel.is_set()
```

**调度循环 `_dispatch(task)`**（每任务一个后台线程）

1. 若已取消 → 退出；暂停则阻塞（`ctl.wait_if_paused()`）。
2. 回收已结束的 in-flight 线程。
3. `target = clamp(task['workers'], 1, HARD_MAX_WORKERS)`；
   `pool.ensure_async(target)`（非阻塞，需要时后台加实例）。
4. `while len(inflight) < target and 有 pending 文档`：取一个实例（`try_acquire`），
   起 `_run_doc` 线程。实例还没加载好就等一下，**不影响已在跑的文档**。
5. 无 in-flight 且无 pending → 结束。`sleep(0.25)` 后回到 1。
6. 退出时结算 `status`（`stopped` / `failed` / `done`）与 `finished`，
   并把从未开始的 pending 文档标成 `cancelled`。
7. **可重入**：任务结束后又加了文档 → `_ensure_dispatcher()` 重新拉起调度线程。

**`_run_doc(task, doc)`**

- `pool.acquire()` → `synthesize_core(inst, doc['role_path'] or doc['path'], doc['spk'],
  task['speed'], task['out_dir'], role_map=..., unit_cb=..., ctl=_DocCtl(task_ctl, doc_cancel))`
- 成功 → `store.create_task/update_task` 记一笔历史，`doc['status']='done'`
- 取消（段边界）→ `doc['status']='cancelled'`，但**保留** `mp3`（半成品）
- 异常 → `doc['status']='failed'` + `doc['error']`

**移除正在跑的文档**：置 `doc['cancel']`，当前段结束即停，半成品同样保留。

**上传目录不再随任务结束删除。** 新设计里 `role_path`（标注副本）就放在
`.tmp/uploads/<task_id>/`，而且是"停止后可继续/重新跑"的依据；任务跑完就删会
把用户原文和角色标注一起删掉。改为**启动时清理 7 天前的 `.tmp/uploads/*`**。

**新增/改动的 HTTP 接口**

| 方法 | 路径 | 作用 |
|---|---|---|
| POST | `/api/convert` | 建任务 + 加文档 + 拉起调度（保留旧入口，走新队列） |
| POST | `/api/task/doc/add` | 往运行中的任务追加文档（multipart，多文件） |
| POST | `/api/task/doc/remove` | 移除/取消文档 `{task_id, doc_id}` |
| POST | `/api/task/pause` \| `/api/task/resume` \| `/api/task/stop` | `{task_id}` |
| POST | `/api/task/workers` | `{task_id, workers}`，clamp 到 [1,2]，缩小=回收空闲实例 |
| POST | `/api/roles/detect` | `{task_id, doc_id}` → 跑 `auto_mark_roles()` 写出 `role_path`，返回角色列表 |
| POST | `/api/roles/set` \| `/api/roles/delete` | `{task_id, role, spk}` 维护活映射 |
| GET | `/api/audition/<name>` | 回放 `voice_profiles/<name>/prompt.wav`（参考原声，零显存） |
| GET | `/api/open_file?path=` | `os.startfile` 打开指定的 mp3 |
| GET | `/api/pool` | 实例池状态（已加载/目标/是否在加载） |

控制类接口统一收 **JSON body**（`read_json()` 辅助），不再走 multipart。
`parse_multipart` 改为按字段名累积**列表**（旧实现同一字段名多文件会互相覆盖，
只剩最后一个），`_profile_create` 取第一个。

`/api/tasks` 与 `/api/task/<id>` 必须**序列化**：任务里现在有 `threading.Event`
和线程对象，直接 json.dumps 会 TypeError。新增 `_task_payload(task)` 统一转换。

**音色档案注册**改走池：`pool.register_all(...)` / `pool.remove_all(...)`，
替代原来的 `get_backend()`（那是另一个独立单例，会和池各持一份模型）。

### `index.html`

- 任务卡重做：
  - 头部：任务名 · 时间 · 状态徽章 · `暂停/继续` `停止` 按钮
  - **主进度条 + 百分比**，下面一排数字：`已完成 N / 总段数 M / 失败 K / 已用时间`
  - 并发控制：`－ 2 ＋`（上限 2，到顶时 ＋ 禁用）
  - 文档列表：每行 名称 / 状态 / 段进度 / `识别角色` `试听` `打开` `移除`
  - 角色表：角色名 → 音色下拉 → `删除`；`＋ 新增角色`
  - `添加文档` 按钮（任务进行中也可用）
- 音色档案列表与音色选择区每项加 `▶ 试听`（`<audio>` 指向 `/api/audition/<name>`）。
- 文案明确写清"加线程只对多篇文档有效，单篇文档内部是串行的"。

## 验收方式（端到端，必须真实跑）

1. 上传 2 篇短文档 → 并发从 1 加到 2 → 观察池状态出现"正在加载第 2 个实例"，
   加载完成后两篇文档确实同时 `running`，且**没有重建旧实例**（不重新加载第一份）。
2. 运行中点 `暂停` → 段边界停下、百分比冻结、wav 不丢；点 `继续` → 从下一段接着跑。
3. 运行中点 `停止` → 已生成段落合出 MP3（半成品），状态 `stopped`。
4. 运行中追加一篇文档 → 立刻入队并显示段数；移除一篇未开始的文档 → 立刻消失。
5. 点 `识别角色` → 返回角色清单（应含"旁白"以外的说话人）；给某角色换音色 →
   后一段换声。用一小段含 `张三说：…` 的文本验证。
6. 音色列表点 `▶` → 浏览器播放参考原声。
7. 并发数改回 1 → 空闲实例被回收，显存下降。

## 不做什么

- 不做"单篇文档内部分段并行"（模型串行是硬约束，做不到）。
- 不做第 3 个及以上实例（显存不够）。
- 不做暂停/停止后的**续传持久化**（进程重启后任务丢失，与现有行为一致）。
