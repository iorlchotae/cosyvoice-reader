# -*- coding: utf-8 -*-
"""回归测试：参考音频超过 30 秒时，音色档案必须能建成、且能真的注册。

真实事故（2026-10-20）：新建音色「喻大华」时上传了 172 秒的旁白，档案提示
「已保存」，但随后每次转换都整篇失败，错误是上游的一句英文断言：
    do not support extract speech token for audio longer than 30s
原因链：CosyVoice 的零样本克隆只吃 30 秒以内的参考音频（cosyvoice/cli/
frontend.py 里是 assert），而 `create_profile()` 从来不检查时长 → 档案写盘成功、
注册失败 → 之后每个任务在开头 register_spk 时再炸一次，整篇文档 0 段。

修好之后要保证：
  1. 超长音频按**停顿**裁剪，不许切在半个字上；
  2. 文字稿跟着裁到同一段（听写只拿来定位，写进去的仍是用户原稿）；
  3. 建档案失败不许留半个目录（否则同名档案再也建不起来）；
  4. 后端 register_spk 自己也要拦住超长音频，给出看得懂的中文提示。

不需要显卡、不需要加载模型（听写被 mock 掉了）。
"""
import shutil
import sys
import tempfile
import wave
from pathlib import Path

import numpy as np
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import audio as audio_mod                                    # noqa: E402
import voice_profiles as vp                                  # noqa: E402
from config import MAX_PROMPT_SECONDS, PROMPT_TRIM_TARGET  # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name} {extra}')


def write_speechlike(path, seconds, burst=0.8, gap=0.45, noise_db=-60.0):
    """造一段「有人说话」的 16k 单声道：高电平短音 + 明显停顿 + 底噪。"""
    sr = 16000
    rng = np.random.default_rng(7)
    out = np.zeros(int(seconds * sr), dtype=np.float32)
    t = 0.0
    while t < seconds:
        a, b = int(t * sr), min(len(out), int((t + burst) * sr))
        tone = np.sin(2 * np.pi * 320 * np.arange(b - a) / sr) * 0.25
        out[a:b] = tone.astype(np.float32)
        t += burst + gap
    out += rng.normal(0, 10 ** (noise_db / 20), len(out)).astype(np.float32)
    sf.write(str(path), out, sr, subtype='PCM_16')
    return path


print('=== 1. find_pause_cut：切在停顿上，不切开字')
tmp = Path(tempfile.mkdtemp(prefix='cv_profile_'))
long_wav = write_speechlike(tmp / 'long.wav', 172.0)
cut = audio_mod.find_pause_cut(long_wav, PROMPT_TRIM_TARGET, min_cut=18.0)
check('裁剪点不超过 30 秒上限', cut <= MAX_PROMPT_SECONDS, f'{cut:.2f}')
check('裁剪点落在目标附近（不早于 18 秒）', 18.0 <= cut <= PROMPT_TRIM_TARGET, f'{cut:.2f}')
data, sr = sf.read(str(long_wav), dtype='float32')
half = int(0.08 * sr)
around = data[max(0, int(cut * sr) - half):int(cut * sr) + half]
check('裁剪点附近是静音（没有切在字上）',
      float(np.sqrt(np.mean(around ** 2))) < 0.05,
      f'rms={float(np.sqrt(np.mean(around ** 2))):.4f}')

flat = tmp / 'flat.wav'
sf.write(str(flat), (np.sin(2 * np.pi * 220 * np.arange(40 * 16000) / 16000) * 0.2)
         .astype(np.float32), 16000, subtype='PCM_16')
check('全程没有停顿就退回目标硬切',
      abs(audio_mod.find_pause_cut(flat, PROMPT_TRIM_TARGET) - PROMPT_TRIM_TARGET) < 0.01)

print('=== 2. trim_wav：时长真的变短，文件仍是合法 WAV')
trimmed = tmp / 'trimmed.wav'
shutil.copy2(long_wav, trimmed)
new_seconds = audio_mod.trim_wav(trimmed, cut)
check('返回时长与裁剪点一致', abs(new_seconds - cut) < 0.05, f'{new_seconds:.2f}')
check('磁盘上的文件也变短了', abs(audio_mod.wav_duration(trimmed) - cut) < 0.05,
      str(audio_mod.wav_duration(trimmed)))
with wave.open(str(trimmed), 'rb') as w:
    check('仍是 16k 单声道 PCM', w.getframerate() == 16000 and w.getnchannels() == 1)

print('=== 3. _align_prefix：听写有错字也能定位到原稿的第几个字')
SCRIPT = ('道光十一年四月十二日，道光皇帝接到一个噩耗，痛哭失声。'
          '太监立即响应，很快养心殿内外哭声一片。'
          '原来道光皇帝的这个长子奕纬，是丫头生的。是道光皇帝做皇帝。')
HEARD = ('十一年四月十二日,道光皇帝接到一个噩耗,痛哭失声。'
         '太监立即响应,很快养心殿内外哭声一片。'
         '原来道光皇帝的长子易伟死了。')
hit, cover = vp._align_prefix(SCRIPT, HEARD)
check('定位落在第二段中间（不是开头也不是整篇）', 40 < hit < len(SCRIPT), f'hit={hit}')
check('覆盖率合理', cover >= vp._ASR_ALIGN_MIN_COVER, f'{cover:.2f}')
check('听写完全跑偏时返回 0', vp._align_prefix(SCRIPT, 'hello world')[0] == 0)

print('=== 4. create_profile：超长参考音频能建成且已裁到 30 秒内')
vp.VOICE_PROFILES_DIR = tmp / 'profiles'          # 别碰用户真实的音色目录
vp.VOICE_PROFILES_DIR.mkdir(parents=True)

real_transcribe = vp.transcribe
vp.transcribe = lambda path: (              # 用「听了一半」的结果模拟听写
    '十一年四月十二日,道光皇帝接到一个噩耗,痛哭失声。', 'base')
try:
    meta = vp.create_profile('长音频_yu', str(long_wav), SCRIPT * 3)
    prof = vp.get_profile('长音频_yu')
    secs = audio_mod.wav_duration(prof['wav'])
    check('档案落盘成功', prof is not None)
    check('参考音频已裁到 30 秒内', secs <= MAX_PROMPT_SECONDS, f'{secs:.2f}')
    check('meta 记录了裁剪信息', meta.get('trimmed', {}).get('to_seconds') is not None)
    check('原稿另存了一份（可回查）',
          (vp.VOICE_PROFILES_DIR / '长音频_yu' / 'prompt.source.txt').exists())
    check('写进去的文字稿被裁短（和音频对齐）',
          0 < len(prof['txt']) < len(SCRIPT * 3), f'{len(prof["txt"])} 字')
    check('返回了给用户看的说明', '30 秒' in (meta.get('note') or ''), meta.get('note'))
finally:
    vp.transcribe = real_transcribe

print('=== 5. 失败必须回滚：不留半个档案目录')
vp.transcribe = lambda path: (_ for _ in ()).throw(ValueError('听写不可用'))
try:
    try:
        vp.create_profile('坏音频_z', str(long_wav), SCRIPT)
        check('听写失败时抛错', False)
    except ValueError as e:
        check('听写失败时抛错', True)
        check('错误信息说清楚了原因', '听写' in str(e), str(e))
    check('失败后目录被清掉（同名可以原地重建）',
          not (vp.VOICE_PROFILES_DIR / '坏音频_z').exists())
finally:
    vp.transcribe = real_transcribe

print('=== 6. 正常长度的参考音频：原样通过，比例离谱时才提醒')
short = write_speechlike(tmp / 'short.wav', 12.0)
meta2 = vp.create_profile('短音频_ok', str(short), '这是十二秒的测试文稿内容。' * 3)
check('12 秒能正常建档案', (vp.VOICE_PROFILES_DIR / '短音频_ok').exists())
check('正常比例不打扰用户', not meta2.get('note'), str(meta2.get('note')))
try:
    vp.create_profile('太短_x', str(write_speechlike(tmp / 'tiny.wav', 0.8)), '字')
    check('过短的参考音频被拒', False)
except ValueError as e:
    check('过短的参考音频被拒', '太短' in str(e), str(e))
check('被拒后不留目录', not (vp.VOICE_PROFILES_DIR / '太短_x').exists())

print('=== 7. 后端 register_spk 也要拦住超长音频（中文提示，不是英文断言）')
from backends.cosyvoice3 import CosyVoice3Backend                 # noqa: E402
class _Dummy:            # register_spk 在碰模型之前就该拦下，用空壳就够
    def normalize_prompt(self, t): return t
try:
    CosyVoice3Backend.register_spk(_Dummy(), '旧档案', '文字', str(long_wav))
    check('超长参考音频被拦住', False)
except ValueError as e:
    check('超长参考音频被拦住', True)
    check('提示是中文且带音色名和秒数',
          '旧档案' in str(e) and '30' in str(e), str(e))

shutil.rmtree(tmp, ignore_errors=True)
print(f'\n通过 {PASS} 项，失败 {FAIL} 项')
print('VOICE_PROFILE_OK' if FAIL == 0 else 'VOICE_PROFILE_FAIL')
sys.exit(1 if FAIL else 0)
