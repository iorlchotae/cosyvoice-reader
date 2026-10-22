"""声音档案管理。

每个档案是一个目录：
    voice_profiles/<id>/prompt.wav   16kHz 单声道 PCM 参考音频（≤30 秒）
    voice_profiles/<id>/prompt.txt   与音频逐字对应的文本稿
    voice_profiles/<id>/meta.json    元信息
    voice_profiles/<id>/prompt.source.txt   裁剪前的原始文字稿（只有超长录音才有）

CosyVoice 的零样本克隆只吃 30 秒以内的参考音频，超出时上游直接 assert 失败
（`do not support extract speech token for audio longer than 30s`）。所以
`create_profile()` 会：转 16k 单声道 → 超长就按**停顿**裁到 29.4 秒 →
用本地听写把逐字稿对齐到裁完的这一段 → 写盘。

对齐这一步是必须的：用户给的稿子通常按整段录音写，硬裁之后直接沿用，文字稿
会比音频多出一大截，克隆出来的音色明显发飘。听写只用来**定位**（在原稿里找出
「这 29 秒念到了第几个字」），写进 prompt.txt 的仍然是用户自己那份干净文字，
所以听写偶尔错几个字不影响结果。

档案创建后调用后端 register_spk 注册进模型（spk2info.pt 持久化）。
"""

import difflib
import json
import os
import re
import shutil
import threading
import time
from pathlib import Path

from audio import find_pause_cut, to_mono_16k_wav, trim_wav, wav_duration
from config import (MAX_PROMPT_SECONDS, PROJECT_ROOT, PROMPT_MIN_SECONDS,
                    PROMPT_TRIM_MIN, PROMPT_TRIM_TARGET, VOICE_PROFILES_DIR)

_ID_RE = re.compile(r'^[\w\u4e00-\u9fff-]{1,32}$')

PRESETS_FILE = VOICE_PROFILES_DIR / 'presets.json'

# 听写默认用 base：它足够把「29 秒念到第几个字」定位准（实测与 medium 给出同一个
# 切点，但快 10 倍）。想更准可以设 COSYVOICE_PROMPT_ASR_MODEL=medium。
_ASR_MODEL_ENV = 'COSYVOICE_PROMPT_ASR_MODEL'
_ASR_DEFAULT_MODEL = 'base'
_ASR_ALIGN_MIN_CHARS = 12     # 听写太短（几乎没听到字）说明对不齐
_ASR_ALIGN_MIN_COVER = 0.45   # 听写里有 45% 以上的字能在原稿里找到才算对齐
_ALIGN_GAP = 24               # 对齐时允许一次跳过多少字（听写漏掉半句话）
_RATE_SANE = (1.0, 8.0)       # 每秒几个字算正常（中文旁白约 3~5）

_asr_lock = threading.Lock()
_asr_cache = [None, None]     # [模型名, 模型对象]


def is_valid_id(spk_id: str) -> bool:
    return bool(spk_id) and bool(_ID_RE.match(spk_id))


# --- 逐字稿对齐 -----------------------------------------------------------
#
# 听写结果和用户原稿都去掉标点后做 difflib 比对，按顺序吃掉匹配块，最后一块在
# 原稿里的结束位置就是「这段音频念到哪儿」。听写有错字也没关系——SequenceMatcher
# 会跳过对不上的片段，只要能连着匹配上就继续往后走。

_KEEP_RE = re.compile(r'[\u4e00-\u9fff0-9a-zA-Z]')


def _norm_map(text: str):
    """去掉标点空白，返回 (纯文字串, 每个字在原文里的下标)。"""
    chars, idx = [], []
    for i, ch in enumerate(text or ''):
        if _KEEP_RE.match(ch):
            chars.append(ch)
            idx.append(i)
    return ''.join(chars), idx


def _align_prefix(full_text: str, heard: str) -> tuple:
    """把听写结果对到原稿上，返回 (原稿里念到的结束下标, 匹配覆盖率)。"""
    a, idx = _norm_map(full_text)
    b, _ = _norm_map(heard)
    if not a or not b:
        return 0, 0.0
    sm = difflib.SequenceMatcher(None, a, b, autojunk=False)
    pos = 0
    for blk in sm.get_matching_blocks():
        if blk.size < 2:
            continue
        if blk.a > pos + _ALIGN_GAP:      # 跳得太远，后面那句不属于这一段
            break
        pos = blk.a + blk.size
    if pos <= 0:
        return 0, 0.0
    return idx[pos - 1] + 1, pos / float(len(b))


def transcribe(wav_path) -> tuple:
    """本地听写一段音频（16k 单声道 WAV），返回 (文本, 模型名)。"""
    model_name = (os.environ.get(_ASR_MODEL_ENV) or _ASR_DEFAULT_MODEL).strip()
    try:
        import soundfile as sf
        import whisper
    except Exception as e:  # noqa: BLE001
        raise ValueError(f'本机没有可用的语音听写（openai-whisper）：{e}') from e

    with _asr_lock:
        if _asr_cache[1] is None or _asr_cache[0] != model_name:
            try:
                _asr_cache[1] = whisper.load_model(model_name, device='cpu')
                _asr_cache[0] = model_name
            except Exception as e:  # noqa: BLE001
                raise ValueError(
                    f'加载听写模型 {model_name} 失败：{e}'
                    f'（首次使用需要联网下载，或用 {_ASR_MODEL_ENV} 换个小模型）') from e
        model = _asr_cache[1]

        data, sr = sf.read(str(wav_path), dtype='float32')
        if data.ndim > 1:
            data = data.mean(axis=1)
        if sr != 16000:                    # 理论上不会发生（前面刚转过 16k）
            raise ValueError(f'参考音频采样率 {sr} 不是 16k，无法听写')
        # 中文初始提示能明显压低「整段翻成英文」的概率
        res = model.transcribe(data, language='zh', fp16=False,
                               condition_on_previous_text=False,
                               initial_prompt='以下是普通话的句子。')
    return (res.get('text') or '').strip(), model_name


def _window_text(full_text: str, heard: str) -> tuple:
    """按听写结果把整篇文字稿裁到「真正喂给模型的那一段」，返回 (文本, 覆盖率)。"""
    cut, cover = _align_prefix(full_text, heard)
    if cut >= _ASR_ALIGN_MIN_CHARS and cover >= _ASR_ALIGN_MIN_COVER:
        return full_text[:cut].strip(), cover
    raise ValueError(
        '参考音频超过 30 秒，需要先裁到 30 秒内并把文字稿对齐，但听写出来的内容'
        '和你给的文字稿对不上（可能不是同一段话）。请核对文字稿，'
        '或者直接上传 30 秒以内的片段。')


# --- 档案读写 -------------------------------------------------------------

def create_profile(spk_id: str, src_wav: str, prompt_text: str) -> dict:
    """创建声音档案：转 16k 单声道、超长自动裁到 30 秒内并对齐文字稿。

    返回 meta（含 `seconds` / `note` / `trimmed`）。失败时不留半个档案目录。
    """
    if not is_valid_id(spk_id):
        raise ValueError('档案名只能包含中文、字母、数字、下划线、连字符（1~32 字符）')
    text = (prompt_text or '').strip()
    if not text:
        raise ValueError('参考文字稿不能为空，且必须与音频内容逐字对应')
    profile_dir = VOICE_PROFILES_DIR / spk_id
    if profile_dir.exists():
        raise ValueError(f'档案 [{spk_id}] 已存在')

    profile_dir.mkdir(parents=True)
    try:
        return _build_profile(profile_dir, str(src_wav), text)
    except Exception:
        # 建到一半失败必须回滚：同名目录留着，用户再建只会得到「档案已存在」
        shutil.rmtree(profile_dir, ignore_errors=True)
        raise


def _build_profile(profile_dir: Path, src_wav: str, text: str) -> dict:
    wav = profile_dir / 'prompt.wav'
    to_mono_16k_wav(src_wav, wav)
    seconds = wav_duration(wav)
    if seconds < PROMPT_MIN_SECONDS:
        raise ValueError(f'参考音频只有 {seconds:.1f} 秒，太短了（至少 {PROMPT_MIN_SECONDS:g} 秒）')

    meta = {'name': profile_dir.name, 'created': time.time()}
    note = ''

    if seconds > MAX_PROMPT_SECONDS:
        original_seconds = seconds
        cut = find_pause_cut(wav, PROMPT_TRIM_TARGET, min_cut=PROMPT_TRIM_MIN)
        seconds = trim_wav(wav, cut)
        heard, model_name = transcribe(wav)
        window, cover = _window_text(text, heard)
        (profile_dir / 'prompt.source.txt').write_text(text, encoding='utf-8')
        text = window
        meta['trimmed'] = {
            'from_seconds': round(original_seconds, 1),
            'to_seconds': round(seconds, 2),
            'window_chars': len(_norm_map(text)[0]),
            'asr_model': model_name,
            'align_cover': round(cover, 2),
        }
        note = (f'参考音频 {original_seconds:.0f} 秒超过 30 秒上限，已按停顿裁到 '
                f'{seconds:.1f} 秒，文字稿同步对齐到这一段的 '
                f'{len(_norm_map(text)[0])} 字')
    else:
        # 时长没超也要看一眼字数比例：稿子明显不是这段录音的稿子时先提醒一句
        chars = len(_norm_map(text)[0])
        rate = chars / seconds if seconds > 0 else 0
        if rate < _RATE_SANE[0] or rate > _RATE_SANE[1]:
            note = (f'文字稿 {chars} 字 / 参考音频 {seconds:.1f} 秒'
                    f'（约 {rate:.1f} 字/秒），这个比例不太寻常，'
                    f'请确认文字稿和音频是同一段话')

    (profile_dir / 'prompt.txt').write_text(text, encoding='utf-8')
    meta['seconds'] = round(seconds, 1)
    if note:
        meta['note'] = note
    (profile_dir / 'meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return meta


def list_profiles() -> list:
    """返回档案信息列表（按创建时间倒序）。"""
    items = []
    for d in VOICE_PROFILES_DIR.iterdir():
        if not d.is_dir():
            continue
        wav = d / 'prompt.wav'
        txt = d / 'prompt.txt'
        meta_path = d / 'meta.json'
        meta = {}
        if meta_path.exists():
            try:
                meta = json.loads(meta_path.read_text(encoding='utf-8'))
            except (json.JSONDecodeError, OSError):
                meta = {}
        if not wav.exists() or not txt.exists():
            continue
        try:
            import soundfile as sf
            with sf.SoundFile(str(wav)) as f:
                seconds = f.frames / f.samplerate
        except Exception:  # noqa: BLE001
            seconds = 0
        items.append({
            'name': d.name,
            'wav': str(wav),
            'txt': txt.read_text(encoding='utf-8'),
            'seconds': round(seconds, 1),
            'created': meta.get('created', 0),
            'trimmed': bool(meta.get('trimmed')),
        })
    items.sort(key=lambda x: x['created'], reverse=True)
    return items


def get_profile(spk_id: str) -> dict | None:
    d = VOICE_PROFILES_DIR / spk_id
    wav, txt = d / 'prompt.wav', d / 'prompt.txt'
    if not d.is_dir() or not wav.exists() or not txt.exists():
        return None
    return {'name': d.name, 'wav': str(wav), 'txt': txt.read_text(encoding='utf-8')}


def delete_profile(spk_id: str) -> bool:
    """删除档案文件目录。注意：模型内存中的 spk2info 需另行移除。"""
    d = VOICE_PROFILES_DIR / spk_id
    if not d.is_dir():
        return False
    shutil.rmtree(d)
    return True


# 预置音色 ---------------------------------------------------------------

def list_presets() -> list:
    """读取预置音色清单。"""
    if not PRESETS_FILE.exists():
        return []
    try:
        data = json.loads(PRESETS_FILE.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    out = []
    for k, v in data.items():
        wav = v.get('wav', '')
        # 清单里的 wav 可能因为授权/体积原因没有随仓库分发（.gitignore 掉的那几个）。
        # 文件不在就当这个预置不存在，免得界面列出一个点了会报错的音色。
        if wav and not (PROJECT_ROOT / wav).exists():
            continue
        out.append({'name': k, 'wav': wav, 'txt': v.get('txt', ''),
                    'desc': v.get('desc', '')})
    return out


def import_preset(preset_name: str, new_name: str = '') -> dict:
    """把预置音色导入为正式声音档案（复制 wav/逐字稿到 voice_profiles）。

    返回 {'name', 'wav', 'txt'}，调用方需自行 register_spk。
    """
    presets = {p['name']: p for p in list_presets()}
    if preset_name not in presets:
        raise ValueError(f'预置音色 [{preset_name}] 不存在')
    p = presets[preset_name]
    name = (new_name or preset_name).strip()
    if not is_valid_id(name):
        raise ValueError('档案名只能包含中文、字母、数字、下划线、连字符（1~32 字符）')
    profile_dir = VOICE_PROFILES_DIR / name
    if profile_dir.exists():
        raise ValueError(f'档案 [{name}] 已存在，请换个名字')
    src_wav = PROJECT_ROOT / p['wav']
    if not src_wav.exists():
        raise ValueError(f'预置音频文件缺失：{src_wav}')
    profile_dir.mkdir(parents=True)
    shutil.copy2(src_wav, profile_dir / 'prompt.wav')
    (profile_dir / 'prompt.txt').write_text(p['txt'].strip(), encoding='utf-8')
    meta = {'name': name, 'created': time.time(), 'preset': preset_name}
    (profile_dir / 'meta.json').write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    return {'name': name, 'wav': str(profile_dir / 'prompt.wav'), 'txt': p['txt']}
