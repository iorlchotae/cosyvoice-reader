"""TXT / Markdown 读取、清理与智能分段。

分段目标：块长度控制在模型舒适区间（约 1000 字），优先沿段落
边界切，超长段落内部按句子切，保证每块语义完整。
"""

import hashlib
import json
import re
from pathlib import Path

CHUNK_CHARS = 1000
CHUNK_MIN_CHARS = 200

_SENT_SPLIT = re.compile(r'(?<=[。！？；.!?;])')


def read_text(path) -> str:
    """读取文本文件，自动识别 UTF-8（含 BOM）与 GBK。"""
    raw = Path(path).read_bytes()
    for enc in ('utf-8-sig', 'utf-8', 'gb18030'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def clean_text(text: str) -> str:
    """清理文本：去零宽字符、统一换行、压缩空行。"""
    text = text.replace('\ufeff', '').replace('\u200b', '')
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    text = re.sub(r'\n\s*\n', '\n\n', text)
    return text.strip()


def strip_markdown(text: str) -> str:
    """去除常见 Markdown 标记，只留正文。"""
    lines = []
    for line in text.split('\n'):
        line = line.strip()
        if not line:
            lines.append('')
            continue
        if re.match(r'^#{1,6}\s', line):                # 标题
            line = re.sub(r'^#{1,6}\s*', '', line)
        line = re.sub(r'^[-*+]\s+', '', line)           # 无序列表
        line = re.sub(r'^\d+[.、)]\s+', '', line)       # 有序列表
        line = re.sub(r'^>\s?', '', line)               # 引用
        line = re.sub(r'`([^`]+)`', r'\1', line)        # 行内代码
        line = re.sub(r'\*\*([^*]+)\*\*', r'\1', line)  # 加粗
        line = re.sub(r'\*([^*]+)\*', r'\1', line)      # 斜体
        line = re.sub(r'!\[[^\]]*\]\([^)]*\)', '', line)   # 图片
        line = re.sub(r'\[([^\]]*)\]\([^)]*\)', r'\1', line)  # 链接
        line = re.sub(r'<[^>]+>', '', line)             # HTML 标签
        line = line.replace('|', '，')
        lines.append(line.strip())
    return '\n'.join(lines)


def split_chunks(text: str, target: int = CHUNK_CHARS,
                 min_chars: int = CHUNK_MIN_CHARS) -> list:
    """把文本切成块，返回块列表（按原文顺序）。"""
    paragraphs = [p.strip() for p in text.split('\n\n') if p.strip()]
    chunks: list[str] = []
    cur = ''

    def push(chunk: str) -> None:
        nonlocal cur
        if len(cur) + len(chunk) + 1 <= target:
            cur = f'{cur}\n{chunk}' if cur else chunk
        else:
            if cur:
                chunks.append(cur)
            if len(chunk) > target:
                chunks.extend(_split_long(chunk, target))
                cur = ''
            else:
                cur = chunk

    for para in paragraphs:
        push(para)
    if cur:
        if chunks and len(cur) < min_chars:
            chunks[-1] = f'{chunks[-1]}\n{cur}'
        else:
            chunks.append(cur)
    return [c for c in chunks if c.strip()]


def _split_long(para: str, target: int) -> list:
    """超长段落按句子边界切块。"""
    sentences = [s for s in _SENT_SPLIT.split(para) if s.strip()]
    parts: list[str] = []
    cur = ''
    for s in sentences:
        if len(cur) + len(s) <= target:
            cur += s
        else:
            if cur:
                parts.append(cur)
            cur = s
    if cur:
        parts.append(cur)
    return parts


def load_text(path, is_markdown: bool = None) -> str:
    """读文件并清理成正文纯文本（Markdown 会自动去标记）。

    单一入口：`load_document` / `load_document_roles` / 角色识别都走这里，
    避免各处重复写编码判断和 Markdown 分支。
    """
    p = Path(path)
    if is_markdown is None:
        is_markdown = p.suffix.lower() in ('.md', '.markdown')
    text = clean_text(read_text(p))
    if is_markdown:
        text = strip_markdown(text)
    return tts_safe(text)          # 最后一道：拆掉会让 wetext 崩掉的写法


# TTS 清洗 -------------------------------------------------------------
# wetext 是 CosyVoice 的文本正则化前端。它在几种写法上会在内部切出一个空串，
# 直接 `assert len(input) > 0` 崩掉，而崩的是「一段」——一段失败整篇文档就失败，
# 用户看到的是「几小时的任务白跑」。实测能踩到的写法有两类：
#   1. `#` 紧跟数字或另一个 `#`：`Chapter #5`、`##`、`[eBook #1342]`
#   2. 符号紧贴数字：[1342]、42]、1#、%1、^1、a[1……（但 a#1、42 ]、## 没事）
# 所以在唯一入口处把它们拆开：`#1342` → `# 1342`、`[1342]` → `[ 1342 ]`。
# 语义不变（wetext 本来就把 # 读成 number，方括号只是噪声），但不会再踩到空串。
_WETEXT_TRAP = re.compile(r'#(?=[0-9#])')
# 只列实测过的「麻烦符号」。`{}<>\\` 不在其中：它们单独、成对、贴数字都安全，
# 加进来反而会把我自己的替换结果变成新的崩法（`{{` 本来是好的，拆成 `{ {` 就崩）。
_WETEXT_SYM = r'#*|/~^_+=&%@$\[\]'
_WETEXT_GLUE_L = re.compile(rf'(?<=[{_WETEXT_SYM}])(?=\d)')
_WETEXT_GLUE_R = re.compile(rf'(?<=\d)(?=[{_WETEXT_SYM}])')
_WETEXT_GLUE_S = re.compile(rf'(?<=[{_WETEXT_SYM}])(?=[{_WETEXT_SYM}])')


def tts_safe(text: str) -> str:
    """拆掉会让 wetext 崩掉的写法（`#数字`、`##`、符号紧贴数字或符号）。"""
    text = _WETEXT_TRAP.sub('# ', text)
    text = _WETEXT_GLUE_L.sub(' ', text)
    text = _WETEXT_GLUE_R.sub(' ', text)
    text = _WETEXT_GLUE_S.sub(' ', text)
    return text


def tts_safe_hard(text: str) -> str:
    """兜底清洗：万一第一遍还是炸，用这个再试一次。

    把这一类符号整段扫掉，宁可读得糙一点，也不能让一整篇文档因为一个符号全废。
    """
    text = tts_safe(text)
    text = re.sub(rf'[{_WETEXT_SYM}`]+', ' ', text)
    text = re.sub(r'[ \t]{2,}', ' ', text)
    return text.strip()


def load_document(path, is_markdown: bool = False) -> list:
    """读文件并切块，返回块列表。is_markdown 由扩展名自动判断。"""
    return split_chunks(load_text(path, is_markdown or None))


# 多角色模式 ----------------------------------------------------------

DEFAULT_ROLE = '旁白'
_ROLE_PREFIX = re.compile(r'^\[([^\[\]]{1,20})\]\s*(.*)$')


def parse_roles(text: str) -> list:
    """按行解析 [角色名] 标记文本，返回 [(角色, 文本)] 列表。

    规则：
      - 每行独立判断；行以 [角色名] 开头则归为角色名
      - 无标记行归为 DEFAULT_ROLE（旁白）
      - 连续同角色行合并为一段，减少切换次数
      - 空行、只有标记没有内容的行会被跳过
    """
    segments: list = []
    cur_role, cur_lines = None, []
    for raw_line in text.split('\n'):
        line = raw_line.strip()
        if not line:
            continue
        m = _ROLE_PREFIX.match(line)
        if m:
            role, content = m.group(1).strip(), m.group(2).strip()
            if not content:
                continue
        else:
            role, content = DEFAULT_ROLE, line
        if cur_role is None:
            cur_role, cur_lines = role, [content]
        elif role == cur_role:
            cur_lines.append(content)
        else:
            segments.append((cur_role, '\n'.join(cur_lines)))
            cur_role, cur_lines = role, [content]
    if cur_role is not None:
        segments.append((cur_role, '\n'.join(cur_lines)))
    return segments


def load_document_roles(path, is_markdown: bool = False) -> list:
    """读取并解析带 [角色] 标记的文档，返回 [(角色, 文本)] 列表。"""
    return parse_roles(load_text(path, is_markdown or None))


def load_units(path, roles_mode: bool = False) -> list:
    """统一的「段落清单」入口，返回 [(角色, 文本)]。

    单音色模式把每块的角色统一写成 DEFAULT_ROLE —— 那时角色只用于日志标签，
    真正的朗读者由 spk_name 决定。合成、断点指纹、进度分母都必须走这一个函数，
    否则三者对「一段」的理解会不一致，续传就会错位。
    """
    if roles_mode:
        return parse_roles(load_text(path))
    return [(DEFAULT_ROLE, c) for c in split_chunks(load_text(path))]


def units_fingerprint(units) -> str:
    """段落清单的内容指纹，断点续传用它判断「是不是同一篇文档」。

    用清单而不是原始文件字节：角色标注（自动识别或手工改 [角色]）会改变切段
    结果，而真正决定音频的是清单本身。
    """
    payload = json.dumps([[r, t] for r, t in units],
                         ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(payload.encode('utf-8')).hexdigest()[:16]


def count_units(path, roles_mode: bool = False) -> int:
    """预先数出文档一共会被切成几段（纯文本运算，不碰显卡）。

    用于"文档一加入队列，进度条就知道分母"，避免进度条从 0 突然跳到 100。
    切分逻辑必须和真正合成时用的那套完全一致，否则分母会错。
    """
    try:
        return len(load_units(path, roles_mode))
    except OSError:
        return 0


# 自动整理成显式标记 -----------------------------------------------------

# 说话人关键词（长词在前，避免 '说道' 被拆成 '说'+'道'）
_SPEAK_VERBS = (r'说道|问道|答道|回答说|低声道|轻声道|大声道|自言自语|')
_SPEAK_VERBS += r'说|道|问|答|讲|喊|叫|叹|应|骂|嚷'
# 说话人与动词之间的修饰语（又问/再说/便道…）
_SPEAK_MOD = (r'又|再|也|便|就|还|却|忙|竟|忽|连|都|已|正|曾|只|才|稍|略|'
              r'微微|终于|忍不住|接着|忽然|突然|随后|低声|轻声|大声|厉声|'
              r'缓缓|冷冷|笑着|淡声|沉声|终于忍不住')

# 模式1：XX说：内容（冒号分隔，内容不限）
_SPEAKER_COLON = re.compile(
    rf'^([^\s「」“”『』，。！？；]{{1,10}}?)(?:{_SPEAK_MOD})?(?:{_SPEAK_VERBS})[：:]\s*(.+)$')
# 模式2：XX说「内容」（无冒号，内容必须以引号开头）
_SPEAKER_QUOTE = re.compile(
    rf'^([^\s「」“”『』，。！？；]{{1,10}}?)(?:{_SPEAK_MOD})?(?:{_SPEAK_VERBS})[，,]?\s*'
    rf'([「“"『』].*)$')
# 纯说话人标注行（如“张三说。”），用于丢弃
_TAG_ONLY = re.compile(
    rf'^[^\s「」“”『』，。！？；]{{1,10}}?(?:{_SPEAK_MOD})?(?:{_SPEAK_VERBS})[。！？；，,.!?;]*$')

_QUOTE_PAIRS = [('「', '」'), ('『', '』'), ('“', '”'), ('"', '"')]


def _strip_quotes(s: str) -> str:
    """去除整行首尾引号。"""
    s = s.strip()
    for left, right in _QUOTE_PAIRS:
        if s.startswith(left) and s.endswith(right) and len(s) >= 2:
            return s[len(left):-len(right)].strip()
    return s


def _split_quoted(line: str) -> list:
    """把一行拆成 [(is_dialog, content)]，引号内为对话。"""
    parts, buf = [], ''
    i, n = 0, len(line)
    while i < n:
        matched = False
        for left, right in _QUOTE_PAIRS:
            if line.startswith(left, i):
                end = line.find(right, i + len(left))
                if end != -1:
                    if buf.strip():
                        parts.append((False, buf.strip()))
                        buf = ''
                    parts.append((True, line[i + len(left):end].strip()))
                    i = end + len(right)
                    matched = True
                    break
        if not matched:
            buf += line[i]
            i += 1
    if buf.strip():
        parts.append((False, buf.strip()))
    return parts


def _find_speaker(line: str):
    """在行内找 'XX说' 说话人，找不到返回 None。"""
    m = re.search(
        rf'([^\s「」“”『』，。！？；]{{1,10}}?)(?:{_SPEAK_MOD})?(?:{_SPEAK_VERBS})[：:，,]?',
        line)
    return m.group(1) if m else None


def auto_mark_roles(text: str) -> str:
    """自动把文章整理成 [角色名] 标记格式（规则引擎第一版）。

    识别规则：
      - 'XX说：内容' / 'XX道「内容」' → 该行归 XX（去引号）
      - 行内含「」对话且说话人明确 → 拆成 [XX] 对话 + 旁白
      - 行内含「」对话但无说话人 → 继承上一说话人；无则标 [对话]
      - 纯说话人标注行（'张三说。'）→ 丢弃
      - 其余行 → 旁白（无标记）
    """
    out = []
    last_speaker = None
    for raw in text.split('\n'):
        line = raw.strip()
        if not line:
            continue
        m = _SPEAKER_COLON.match(line) or _SPEAKER_QUOTE.match(line)
        if m:
            speaker, rest = m.group(1), m.group(2).strip()
            rest = _strip_quotes(rest)
            if rest:
                out.append(f'[{speaker}] {rest}')
                last_speaker = speaker
            continue
        parts = _split_quoted(line)
        if len(parts) > 1 or (len(parts) == 1 and parts[0][0]):
            speaker = _find_speaker(line) or last_speaker
            if speaker:
                last_speaker = speaker
            for is_dialog, content in parts:
                if is_dialog:
                    out.append(f'[{speaker or "对话"}] {content}')
                elif content and not _TAG_ONLY.match(content):
                    out.append(content)
            continue
        out.append(line)
    return '\n'.join(out)
