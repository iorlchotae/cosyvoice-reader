# CosyVoice Reader — 断点续传 + 分段落盘 设计文档

日期：2026-10-18
状态：已实现（正文中的「20 分钟」为初版阈值，现行值见下方修订）
前置文档：`2026-10-18-queue-control-design.md`（队列控制 / 暂停停止 / 多角色，已实现）

## 0. 修订记录

**2026-10-18 修订 1（用户 m01317）：切片阈值 20 分钟 → 10 分钟。**
用户原话：「我不小心把断点续传测试断掉了，按照10分钟左右一段把，根据要转换的文章来断，20分钟有些偏长了」。
- `config.py`：`CHUNK_SECONDS = 10 * 60`（原 `20 * 60`）。正文所有「20 分钟」按 10 分钟理解。
- 切点仍然**跟着文章自身的段（synthesis unit）走**：每段合成完才判定累计时长是否满一块，绝不在句子中间切。
  所以「根据要转换的文章来断」= 每个 `parts/<篇名>.pNN.mp3` 的边界都落在原文的段落边界上，且按篇独立计算。
- `index.html` 文案、`app.py` 注释同步更新；`scripts/test_resume.py` 增加 T0 断言 `CHUNK_SECONDS == 600`。

## 1. 需求

用户原话（m00960）：
> 停止时保留半成品 MP3 ，后续内容可续传，如果发现是同文档的部分，直接检测并生成后续部分，并合并成一个完整的MP3文件，同时保留之前半成品和断点的后续MP3。

追加需求（m00985，阈值后经 m01317 改为 10 分钟）：
> 为减少错误，超过20分钟的音频，每20分钟存成一个独立的MP3，等全部完成后，再合并成一个完整的MP3

### 用户已确认的决定

| 问题 | 决定 |
| --- | --- |
| 续传触发方式 | 两条都要：停止后任务卡片上的按钮 + 重新拖入同文件时自动检测（m00975 第 1 项） |
| "同一文档"判定 | 文件名 + 内容指纹；文本/音色/语速/角色映射任一变化即作废旧分段，从头重念（m00975 第 2 项） |
| 文件保留 | 半成品与续传 MP3 都保留（m00975 第 3 项） |
| 20 分钟 `parts` | **完整 MP3 合并成功后自动删除**（m00998） |
| `segments` | **完整 MP3 合并成功后自动删除**（合并成功前绝不删） |
| 并发上限 | 硬锁 2，不可超过（m00533） |

## 2. 两条必须守住的不变量

**不变量 A：段是原子的。** 暂停/停止只在段边界生效（`ctl.wait_if_paused()` 在每一段循环顶部调用），正在合成的那一段一定会念完。因此磁盘上只可能存在"整段"，不存在半段。这是断点精度的全部依据。

**不变量 B：切片边界是算出来的，不是记下来的。** 每段时长可以从磁盘上已落盘的分段 WAV 读出来（实测 1.03 ms/个，320 段约 0.33 s）。所以 20 分钟切片的边界由"按段序累计时长"确定性推导。台账丢失、进程被 SIGKILL 也不会切错。

## 3. 磁盘结构

```
outputs\<输出目录>\<篇名>\
├── segments\
│   ├── 0001_0000.wav ...       每段按句拆成多个 wav，先写 .tmp 再原子改名
│   ├── 0001.done               该段全部句落盘后才写此标记
│   └── ...
├── parts\
│   └── <篇名>.p01.mp3 ...       每满 20 分钟落一块（仅 >20 分钟的文档才有）
├── resume.json                 身份指纹 + 进度台账
├── <篇名>.mp3                   当前最佳版本：完整版，或停止时的半成品
├── <篇名>.部分-MMDDHHMMSS.mp3    停止瞬间的半成品快照
└── <篇名>.续传-MMDDHHMMSS.mp3    续传这一批新合成的音频
```

`.done` 标记是"完成"的**唯一**判据（不靠 `resume.json` 的计数），所以强杀进程不会把残缺的 wav 当成已完成。段级 WAV 写入流程固定为：写 `NNNN_SSSS.wav.tmp` → `os.replace()` 原子改名 → 全部句完成后写 `NNNN.done`。

## 4. 断点身份指纹

`resume.json`：

```json
{
  "version": 1,
  "unit_sha": "<16位>",
  "units_total": 320,
  "spk": "周报",
  "speed": 1.0,
  "mode": "single",
  "role_map": {},
  "source_name": "撞人后.txt",
  "source_sha256": "<64位>",
  "done": 158,
  "complete": false,
  "updated": 1792339200.0
}
```

- `unit_sha` = 解析后段落清单 `[(角色, 文本), ...]` 的 JSON 规范化序列化的 sha256 前 16 位。用清单而不是原始文件字节，因为角色标注（`auto_mark_roles` / 手动改 `[角色]`）会改变切段结果，而决定音频的是清单本身。
- `mode` = `roles`（多角色）或 `single`。
- 身份比对必须**六项全等**：`version`、`unit_sha`、`spk`、`speed`、`mode`、`role_map`。任一不等 → 旧分段作废。
- 作废时：清空 `segments\*.wav`、`segments\*.done`、`parts\*.mp3`，**不动**已经产出的 `<篇名>.部分-*.mp3` / `<篇名>.续传-*.mp3` / `<篇名>.mp3`（那是用户的成品，不是工作数据）。
- `resume.json` 存在但身份不匹配，或身份可匹配但文件损坏 → 都走作废路径。
- `resume.json` 缺失但存在 `segments\*.done` → 无法验证身份 → 走作废路径（宁可重念，也不要把两篇文档的音频拼在一起）。

## 5. 合成主流程

伪代码（`backends/synthesize.py :: synthesize_core`）：

```
units = 解析(src)                      # 单音色 → split_chunks；多角色 → load_document_roles
total = len(units)
seg_dir = work_dir/'segments'; parts_dir = work_dir/'parts'
old = read_manifest(work_dir)
ident = make_identity(units, spk, speed, mode, role_map)

if resume and old and old_identity == ident:
    保留分段
else:
    清空 seg_dir/*.wav *.done, parts_dir/*.mp3
    write_manifest(ident, source, total)     # 立刻落盘，便于崩溃后诊断

done = {i | seg_dir/f'{i:04d}.done' 存在}
acc = 0.0; slice_no = 1; slice_wavs = []; closed = []

for i, (role, text) in enumerate(units, 1):
    if i in done:                            # 续传：跳过，但要重放切片边界
        wavs = sorted(seg_dir.glob(f'{i:04d}_*.wav'))
        acc += sum(wav_duration(w) for w in wavs)
        slice_wavs += wavs
        if acc >= CHUNK_SECONDS: 关块(i)      # 见 §6
        continue

    if ctl and ctl.wait_if_paused():         # 不变量 A：只在段边界
        stopped = True; break

    actual_spk = role_map.get(role, spk_name) if mode=='roles' else spk_name
    segs = backend.synthesize(text, spk_id=actual_spk, speed=speed)
    for si, seg in enumerate(segs):
        写 NNNN_SSSS.wav.tmp → os.replace
    write NNNN.done
    acc += 本段时长; slice_wavs += 本段 wav
    unit_cb(i, total)                        # 绝对值（含续传已完成的段）
    if acc >= CHUNK_SECONDS: 关块(i)
    write_manifest(done=i, total=total)

清理 parts_dir 里不在 closed 集合中的 mp3
all_wavs = 按段序 1..total 收集磁盘上存在的全部 wav
if not all_wavs: return (None, 0.0)

if stopped:
    merge(all_wavs) → <篇名>.部分-<ts>.mp3; 复制为 <篇名>.mp3
    write_manifest(complete=False)
    return (<篇名>.mp3, 时长)

if 续传过: merge(本次新合成的 wav) → <篇名>.续传-<ts>.mp3
merge(all_wavs) → <篇名>.mp3
校验时长; write_manifest(complete=True)
if cleanup: 删除 parts\*.mp3 与 segments\ 整个目录
return (<篇名>.mp3, 时长)
```

`关块(i)`：`merge(slice_wavs) → parts/<篇名>.p{slice_no:02d}.mp3`；记录 `(slice_no, from, to, seconds)`；`slice_no += 1; slice_wavs = []; acc = 0.0`。**收尾那一块不落 parts**（它就是完整 MP3 的尾巴），所以 ≤20 分钟的文档根本不产生 `parts` 目录，行为与现状完全一致。

返回值保持 `(mp3_path, duration)` 二元组不变，避免波及 `synthesize_file` / `_parallel_one` / 既有测试。UI 需要的 `units_total / units_done / complete / resumed` 由 app 层读 `resume.json` + `count_units` 得到。

## 6. 20 分钟切块

- 阈值常量：`CHUNK_SECONDS = 20 * 60`，放在 `config.py`；以 `chunk_seconds` 参数注入 `synthesize_core` 以便测试（默认取 config 值）。
- 只在**段边界**结算，绝不切在句子中间。
- 边界由累计时长决定，累计时长可随时从磁盘重建（不变量 B）。
- `parts\<篇名>.pNN.mp3` 文件名**确定性**、不含时间戳：存在即有效，续传时按同一规则重建，不会重复也不会错位。
- 块内时长用刚合成的 numpy 数组长度 ÷ 采样率直接算（零 I/O）；续传跳过的段用 `audio.wav_duration()` 读 WAV 头。

## 7. 三个成品的生成规则

| 文件 | 何时写 | 内容 |
| --- | --- | --- |
| `<篇名>.部分-<ts>.mp3` | 每次停止 | 停止瞬间**所有**已完成段的合并 |
| `<篇名>.续传-<ts>.mp3` | 续传且**本次补齐了全部剩余段** | 仅本次新合成的段 |
| `<篇名>.mp3` | 每次运行结束 | 完整=全部段；停止=半成品 |

- 停止时先合并到 `<篇名>.部分-<ts>.mp3`，再 `shutil.copyfile` 覆盖 `<篇名>.mp3`（一次编码 + 一次文件复制，不重复编码）。
- 中途再次停止：只更新 `<篇名>.mp3` 与新的 `部分-<ts>`，**不**产生续传文件（本次没补完）。
- 时间戳 `%m%d-%H%M%S`（精确到秒，同一分钟连停两次也不会互相覆盖）。
- 反复停止会累积多个 `部分-*.mp3`（用户明确要求保留）。

## 8. HTTP 接口

新增：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/task/doc/resume` | body `{task_id, doc_id}`：重新武装并派发该文档；无断点则按新任务从头跑 |

修改：

- `GET /api/task/<id>` 与 `/api/tasks` 的文档载荷新增：`resumable`(bool)、`units_done`、`resumed`(bool)、`parts`(已落盘的 parts 文件名列表)、`parts_dir`。
- `POST /api/convert`、`POST /api/task/doc/add`：加入文档时扫描输出目录内已有的 `resume.json`，指纹匹配且未完成 → 该文档进入续传模式（`units_done` 预填、日志提示"检测到断点：已完成 158/320 段，本次只合成剩余 162 段"）。受设置项 `resume_enabled` 控制。
- `settings.json` 新增 `resume_enabled`，默认 `true`；`GET/POST /api/settings` 已存在，直接复用。

## 9. UI 变更（index.html）

- 转换设置新增勾选「检测到断点就续传」，默认勾选；取消勾选 = 强制从头重念。
- 文档行（被停止/可续传时）显示「已完成 158/320 段 · 可续传」+「继续续传」按钮。
- 任务卡片产物区列出 `parts`（每 20 分钟一块）、部分、续传、完整，均可直接播放（复用现有 `playMp3`）。
- 文案说明：并行只对"多篇文档"提速，并发上限硬锁 2。

## 10. 测试计划

**`scripts/test_resume.py`（假后端，不加载模型）** —— 用真实 `soundfile` 写真 WAV，`chunk_seconds=1.0`、每段 0.6 s：

1. **切断点**：5 段、念到第 3 段后置 cancel → 磁盘有 `0001..0003.done`、无 `0004.done`；`<篇名>.部分-*.mp3` 与 `<篇名>.mp3` 存在且时长 ≈1.8 s；`resume.json.complete == false`。
2. **续传只念缺的**：复用同一 work_dir 再跑一次 → 假后端记录到的合成段号恰为 `['4','5']`（1..3 未被重念）；`<篇名>.续传-*.mp3` 存在且时长 ≈1.2 s；`<篇名>.mp3` 完整 ≈3.0 s。
3. **切片确定性**：`chunk_seconds=1.0` 下 5 段应关块于第 2、4 段 → 存续期间 `parts/*.p01.mp3`≈1.2 s、`p02.mp3`≈1.2 s，无 `p03`；完整合并成功后 `parts\` 下 mp3 全被删除。
4. **清理**：合并成功后 `segments\` 目录被删除；停止时**不**被删除。
5. **指纹失效**：改一个字的文本（或改 speed）→ 旧 `segments` 被清空、5 段全部重念。
6. **时长守恒**：`部分` 时长 + `续传` 时长 ≈ `完整` 时长（容差 0.3 s）。

**真机端到端**：起服务 → 拖入一份 >20 分钟用不到的样本文档 → 起跑 → 中途停止 → 检查三个文件与 `parts` → 点「继续续传」→ 断言只补剩余段、`parts`/`segments` 被清、完整 MP3 时长正确 → 再测"重新拖入同文件自动认出断点"。

## 11. 明确不做的

- 不做"已完成文档重复投喂自动跳过"（去重是另一个需求）。
- 不做 `部分-*.mp3` 的数量上限/自动清理（用户要求保留）。
- 不改动并发硬锁 2、不改多角色映射的实时生效逻辑。
- 不删用户已产出的 `<篇名>.部分-*.mp3` / `<篇名>.续传-*.mp3` / `<篇名>.mp3`，即使指纹失效。
