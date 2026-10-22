"""路径与环境配置。

所有路径尽量由本文件推导，避免硬编码。模型目录约定为
项目上一级目录下的 models/Fun-CosyVoice3-0.5B-2512，
即与 cosyvoice_reader 平级的 models 文件夹。
"""

import os
import sys
from pathlib import Path
# 项目根目录（config.py 所在目录的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent

# CosyVoice 官方仓库与 Matcha-TTS（submodule）
COSYVOICE_DIR = PROJECT_ROOT / "CosyVoice"
MATCHA_DIR = COSYVOICE_DIR / "third_party" / "Matcha-TTS"

# 模型目录：与项目平级的 models/Fun-CosyVoice3-0.5B-2512
MODEL_DIR = Path(os.environ.get(
    "COSYVOICE_MODEL_DIR",
    str(PROJECT_ROOT.parent / "models" / "Fun-CosyVoice3-0.5B-2512"),
))

# 输出与数据目录
OUTPUT_DIR = PROJECT_ROOT / "outputs"
VOICE_PROFILES_DIR = PROJECT_ROOT / "voice_profiles"
TASKS_DIR = PROJECT_ROOT / "tasks"
TMP_DIR = PROJECT_ROOT / ".tmp"

for d in (OUTPUT_DIR, VOICE_PROFILES_DIR, TASKS_DIR, TMP_DIR):
    d.mkdir(parents=True, exist_ok=True)

# 推理参数
SAMPLE_RATE = 24000          # CosyVoice3 输出采样率
USE_FP16 = True              # Blackwell 上 fp16 更快且省显存
# TensorRT 实测对整体 RTF 无提升（瓶颈在 LLM 自回归），默认关闭以免占用显存/加载时间
USE_TRT = os.environ.get('COSYVOICE_USE_TRT', '0') == '1'
SPEED_MIN, SPEED_MAX = 0.7, 1.5
PARALLEL_DEFAULT = 2         # 并行合成文件数（显存 16GB 可跑 2 实例）
# 并发硬上限：每个后端实例约 6GB 显存（fp16），16GB 卡实际只扛得住 2 个；
# 第 3 个开始爆显存并明显变慢，所以这里直接锁死，不做"允许 3 个但警告"。
# 注意：加并发只能让**多篇文档同时跑**，单篇文档内部的段落是严格串行的
# （模型一次只吃一段），所以只跑一篇文档时加线程没有用。
HARD_MAX_WORKERS = 2


def clamp_workers(n) -> int:
    """把用户给的并发数夹到 [1, HARD_MAX_WORKERS]。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 1
    return max(1, min(HARD_MAX_WORKERS, n))


# --- 声音档案（参考音频）--------------------------------------------------
#
# CosyVoice 的零样本克隆只有一条硬限制：参考音频**不能超过 30 秒**。
# 超出时上游 `frontend.py` 直接 assert，报一句英文
# "do not support extract speech token for audio longer than 30s"，
# 既看不出是哪个音色，也不会在注册阶段拦下——档案会「保存成功但没注册」，
# 之后每次转换都整篇失败。所以上限写在这里，由创建/注册两处共同守住。
MAX_PROMPT_SECONDS = 30.0
PROMPT_TRIM_TARGET = 29.4    # 裁剪目标：留 0.6 秒余量，别贴着 30 秒的线
PROMPT_TRIM_MIN = 18.0       # 太早的停顿不要（宁可在 29.4 秒处硬切）
PROMPT_MIN_SECONDS = 2.0     # 短于这个长度的参考音频克隆不出音色

# 分段参数（document.py）
CHUNK_CHARS = 1000           # 每块目标字数，过小则句子碎片多，过大则 GPU 负担重
CHUNK_MIN_CHARS = 200        # 小于此长度的尾部块并入上一块

# WebUI
WEBUI_HOST = "127.0.0.1"
WEBUI_PORT = 7860
MONITOR_INTERVAL = 2.0       # 硬件监控刷新秒数


# 超过这个时长的音频，每满这么多秒就落一个独立的分段 MP3（断点续传的中间产物，
# 也是防崩溃的增量落盘；一次只拼几段，ffmpeg 不会面对几百个文件的长命令）。
# 完整 MP3 合并成功并校验时长后，分段 MP3 与 segments 一起自动删除。
#
# 取 10 分钟：20 分钟偏长——真停下来时能保住的成品太少，一次断点也跨太多文本，
# 想「只要前半小时」这种部分成果拿不出来。切片落点仍然**跟着文章自己的段走**
# （每段合成完才判定是否满一块，绝不从句子中间切开），所以切点永远落在段落边界。
CHUNK_SECONDS = 10 * 60

# 断点续传台账文件名（放在每篇文档的工作目录里）
RESUME_FILE = 'resume.json'
RESUME_VERSION = 1


# --- 成品分集 -------------------------------------------------------------
#
# 一本 20 小时的书合成一个 MP3 会有 2 GB，播放器拖着卡、也传不动。所以成品
# 统一切成「一小时左右」若干集（`episode.py`）：
#   - 目标 60 分钟，允许 55–72 分钟；
#   - 切点找句子之间的**真静音**（不是整点硬切），听不出断口；
#   - 集数按时长均衡，不会剩一条几分钟的尾巴。
# 界面「成品分集」可改成 30 / 60 / 90 分钟，或选「不分集」保留一个完整文件。
EPISODE_SECONDS = 60 * 60
EPISODE_MIN_SECONDS = 55 * 60
EPISODE_MAX_SECONDS = 72 * 60
EPISODE_CHOICES = (0, 30 * 60, 60 * 60, 90 * 60)


# --- 文件名清洗 -----------------------------------------------------------
#
# Windows 会在落盘时**悄悄吃掉名字结尾的空格和点**：`mkdir('书名  ')` 实际建出来的
# 是 `书名`，紧接着再建 `书名  /segments` 就「系统找不到指定的路径」→ WinError 3。
# 电子书文件名里「空格紧挨着扩展名」太常见了（`…罗斯福斯  .azw3`），这种名字一旦流到
# 输出目录名上，整篇文档会**一段都念不出来**，而且报错信息完全看不出是文件名的问题。
# 所以：凡是要拿去当目录名/文件名主干的字符串，先过这两个函数。

_UNSAFE_NAME_CHARS = '<>:"/\\|?*'
_WIN_RESERVED = frozenset(
    ['CON', 'PRN', 'AUX', 'NUL']
    + [f'COM{i}' for i in range(1, 10)]
    + [f'LPT{i}' for i in range(1, 10)]
)
MAX_STEM_LEN = 80


def safe_text(text, fallback: str = 'document', max_len: int = MAX_STEM_LEN) -> str:
    """把一段文字洗成能安全当**文件名主干**的样子（不拆后缀）。

    用于已经拿到主干的地方（比如 `Path(x).stem`），此时名字里的点可能是书名
    自带的（`第1.5章`），不能当后缀切掉。
    """
    s = ''.join('_' if (c in _UNSAFE_NAME_CHARS or ord(c) < 32) else c
                for c in str(text))
    s = s.strip().rstrip(' .')            # 结尾的空格和点必须去掉，否则 WinError 3
    if not s:
        s = fallback
    if s.upper() in _WIN_RESERVED or s.upper().split('.')[0] in _WIN_RESERVED:
        s = '_' + s
    if len(s) > max_len:                  # 太长会撞 Windows 路径长度上限
        s = s[:max_len].rstrip(' .') or fallback
    return s


def safe_stem(filename, fallback: str = 'document', max_len: int = MAX_STEM_LEN) -> str:
    """从一个**完整文件名**里取出安全的主干（会去掉后缀）。"""
    base = str(filename).replace('\\', '/').rsplit('/', 1)[-1]
    if '.' in base:
        head = base.rsplit('.', 1)[0]
        if head.strip(' .'):              # 别把 `.gitignore` 之类切成空
            base = head
    return safe_text(base, fallback=fallback, max_len=max_len)


def ensure_sys_path() -> None:
    """把 CosyVoice 与 Matcha-TTS 加入 sys.path（幂等）。"""
    for p in (str(MATCHA_DIR), str(COSYVOICE_DIR)):
        if p not in sys.path:
            sys.path.insert(0, p)


def get_ffmpeg() -> str:
    """返回 ffmpeg 可执行文件路径。优先环境变量，其次 PATH。"""
    exe = os.environ.get("FFMPEG_BIN")
    if exe:
        return exe
    return "ffmpeg"
