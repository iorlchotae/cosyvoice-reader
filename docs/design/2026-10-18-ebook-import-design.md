# 电子书导入（PDF / EPUB / MOBI / AZW3 / DOCX）设计

日期：2026-10-18
状态：已实现

## 1. 需求

用户要求（原话）：

> 可导入的文档，增加 .mobi和.azw3 ,.pdf,. doc,.docx 格式电子书支持

## 2. 用户拍板的取舍

| 问题 | 决定 |
| --- | --- |
| 装新依赖 | 可以，直接装进 `.venv` |
| `.doc`（Word 97-2003 老二进制格式） | **不支持**，提示用户另存为 `.docx` |
| 扫描版 PDF（整页图片、无文字层） | **不做 OCR**，检测到就给人话提示 |
| 顺带支持 `.epub` | 要 |

## 3. 核心设计：把电子书先「变成 txt」

这是整个方案的关键——**不在下游做特例**。

```
拖入 book.mobi
   ↓ ebook_import.extract_text()
.tmp/uploads/<task_id>/book.txt        ← 提取出的纯文本
   ↓ 完全走既有流程，一行都不用改
document.load_text / split_chunks / parse_roles / count_units / units_fingerprint
   ↓
synthesize_core（分段 / 角色 / 断点续传 / 10 分钟分片）
```

理由：

1. `document.py`、`backends/synthesize.py`、断点续传的指纹逻辑全部以「一个文本文件」
   为前提。提取层把电子书归一成文本后，**这四类格式自动获得全部既有能力**：
   多角色识别、暂停续传、10 分钟分片、进度百分比。
2. 提取只发生一次，落盘成 `.txt`，之后每次探测断点都是读文本，不重复解压电子书。
3. 失败点集中在一个模块，错误信息也好统一成人话。

`doc['name']` 仍显示用户拖进来的原文件名（`book.mobi`），只有 `doc['path']`
指向提取出的 `book.txt`。上传目录里**原始电子书也保留**，方便用户在提取结果
不理想时回看原文。

## 4. 确定性要求（断点续传的命门）

断点续传的指纹是按**文本内容**算的（`document.units_fingerprint`）。所以提取必须
确定性：同一本书提取两次，文字必须一模一样。

- PDF：PyMuPDF 按页取文字，页内按行切段，顺序稳定。
- EPUB：严格按 OPF spine 顺序拼接，不用文件系统遍历顺序。
- MOBI/AZW3：`mobi.extract()` 解包后走同一套 EPUB/HTML 解析。
- DOCX：按 `document.element.body` 的子元素**文档顺序**遍历，段落与表格交错不错位。

章节标题、页码、页眉页脚不做「智能」删除（只做 5.3 那条保守的重复行过滤），
避免同一本书因版本差异产生不同文本。

## 5. 各格式的具体做法

| 格式 | 引擎 | 说明 |
| --- | --- | --- |
| `.pdf` | PyMuPDF（已装 1.28.2） | 逐页 `get_text('text')`；`needs_pass` → 密码保护提示 |
| `.epub` | 标准库 `zipfile` + BeautifulSoup | 解析 `container.xml` → OPF → spine 顺序 |
| `.mobi` `.azw3` `.azw` `.prc` | `mobi` 0.4.1（内核 KindleUnpack） | KF8 → epub，MOBI7 → html，再走上面两条路 |
| `.docx` | `python-docx` 1.2.0 | 按 body 子元素顺序取段落 + 表格 |
| `.doc` | —— | 明确不支持，给「另存为 .docx」的提示 |

### 5.1 段落结构约定

`document.split_chunks` 是按 `\n\n` 切段的（`document.py:64`）。所以提取结果统一
遵守：**一个自然行 = 一个段落，用空行分隔**。

这样切块器有最大自由度把短行合并到 ~1000 字的目标长度，而不是被 PDF 的硬换行
困住。对 TTS 无害：合并时用 `\n` 连接，换行只是空白符。

### 5.2 重复行过滤（页眉页脚）

300 页的 PDF 每页都带一行「第 12 页」或书名，会污染朗读。做法保守：

- 只在页数 ≥ 5 时启用；
- 只丢掉**出现在 ≥ 50% 页面上**的行（真正的页眉页脚才会这么频繁）；
- 另外丢掉纯粹是页码的行（`12`、`- 12 -`、`第 12 页`，且长度 ≤ 8）。

正文里重复 50% 以上的句子几乎不存在，所以这条规则不会吃掉正文。

### 5.3 扫描版 PDF 判定

整本提取出的字符数 < 50 → 判为「没有文字层」，提示这是扫描件。这个阈值
足够宽松：真有文字的 PDF 不可能整本少于 50 字。

## 6. 失败信息（要人话）

`EbookError(code, message, hint)`，`code` 供程序判断，`message`/`hint` 直接给用户看：

| code | 场景 | 文案 |
| --- | --- | --- |
| `drm` | Kindle 加密书 | 这本书有 DRM 数字版权保护，无法提取文字（正版 Kindle 书需要先去 DRM；可改用无 DRM 的 epub/txt） |
| `scanned` | 扫描版 PDF | 这个 PDF 是扫描件，没有文字层，提取不出文字（需要 OCR，暂不支持） |
| `encrypted` | 带密码 PDF | 这个 PDF 有密码保护 |
| `unsupported` | `.doc` 等 | 暂不支持 .doc（请用 Word 另存为 .docx） |
| `missing_dep` | 缺库 | 缺少 XX 库（pip install XX） |
| `corrupt` | 文件损坏/解析失败 | 解析失败 + 原始错误摘要 |
| `empty` | 提取到 0 字 | 没提取到文字 |

## 7. 改动清单

1. **新增 `ebook_import.py`** —— 唯一的新文件，全部提取逻辑在此。
2. **`app.py`**
   - `_save_uploads()` 对电子书后缀调用 `ebook_import.extract_text()`，
     返回 `(items, errors)`：items 里电子书的路径已换成 `.txt`，显示名仍是原名。
   - `_handle_convert()` / `_doc_add()` 两个调用点接收 errors 并回传前端。
   - 全部文件都提取失败时，不建任务，直接返回错误（避免出现空任务）。
3. **`index.html`**
   - `accept` 加电子书后缀；拖拽区文案说明支持的格式。
   - 提取失败的文档显示在文件列表里带红字原因，而不是静默消失。
4. **`requirements.txt`** —— 补 `python-docx`、`mobi`。

## 8. 验收

- [ ] `.docx`：用用户桌面上真实的《探究学习报告》。docx 提取，字数合理
- [ ] `.pdf`：用 `26-08 抬板项目案例 UDGX1.pdf`（6.6 MB）提取成功、页眉过滤生效
- [ ] `.epub`：Gutenberg 样书提取成功
- [ ] `.mobi`：Gutenberg MOBI7 样书提取成功
- [ ] `.azw3`：Gutenberg KF8 样书提取成功
- [ ] `.doc`：给出「另存为 .docx」的提示，不崩
- [ ] 提取出的文本能正常切段、能在界面上开始合成
- [ ] 同一本书提取两次文本完全一致（断点续传指纹稳定）
