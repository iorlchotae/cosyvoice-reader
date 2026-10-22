"""CosyVoice3 推理后端。

封装 AutoModel 的加载与合成，全局单例，推理串行（onnxruntime
session 与 torch 推理都不适合并发调用）。
"""

import logging
import threading

import numpy as np
import soundfile as sf

from audio import wav_duration
from config import (MAX_PROMPT_SECONDS, ensure_sys_path, MODEL_DIR, USE_FP16,
                    USE_TRT)

logger = logging.getLogger(__name__)

PROMPT_PREFIX = 'You are a helpful assistant.<|endofprompt|>'


def install_frontend_guards(cosyvoice) -> list:
    """给上游文本前端加两道保险，装在每个后端实例自己的 frontend 上。

    上游 wetext 有两个坑，任一命中都会让「一段文本」直接废掉，而一段失败 =
    整篇文档失败（用户看到的是「几小时的任务白跑」）：

      1. `normalize()` 对少数写法会内部崩掉（`assert len(input) > 0`），或者
         **不报错但返回空串**。实测能触发的有：`Chapter #5`、`[eBook #1342]`、
         `42]`、`%1`，以及 URL 片段 `/www.gutenberg.org/license.`。
         最后这个尤其阴——它不抛异常，下游才炸。
      2. `frontend.split_paragraph()` 拿到空串后会去取 `text[-1]` → IndexError。

    两处都是上游实现细节，直接改 vendored 代码会被 CosyVoice 升级覆盖，所以在
    实例上就地包一层：normalize 崩了或返回空 → 退回原文（读得糙一点，但一段
    不会废）；split_paragraph 收到空串 → 直接产出空结果。

    返回被包装过的名字列表，方便日志留痕。
    """
    frontend = getattr(cosyvoice, 'frontend', None)
    if frontend is None:
        return []
    patched = []

    for attr in ('zh_tn_model', 'en_tn_model'):
        model = getattr(frontend, attr, None)
        if model is None or getattr(model, '_ds_guarded', False):
            continue
        orig = model.normalize

        def _safe_normalize(text, _orig=orig, _attr=attr):
            try:
                out = _orig(text)
            except Exception as e:  # noqa: BLE001
                logger.warning('wetext %s.normalize 崩了（%s），退回原文不归一化',
                               _attr, type(e).__name__)
                return text
            if out is None or not str(out).strip():
                logger.warning('wetext %s.normalize 把整段清成了空串，退回原文',
                               _attr)
                return text
            return out

        model.normalize = _safe_normalize
        model._ds_guarded = True
        patched.append(attr)

    try:
        from cosyvoice.cli import frontend as fe_mod
        from cosyvoice.utils import frontend_utils as fu
    except Exception:  # noqa: BLE001
        return patched

    orig_split = fu.split_paragraph
    if not getattr(orig_split, '_ds_guarded', False):

        def _safe_split_paragraph(text, tokenize, lang='zh', **kw):
            if not text:
                return
            yield from orig_split(text, tokenize, lang=lang, **kw)

        _safe_split_paragraph._ds_guarded = True
        # frontend.py 是 `from ... import split_paragraph`，两处名字都要换掉
        fu.split_paragraph = _safe_split_paragraph
        fe_mod.split_paragraph = _safe_split_paragraph
        patched.append('split_paragraph')

    if patched:
        logger.info('text frontend guards installed: %s', ', '.join(patched))
    return patched


class CosyVoice3Backend:
    """加载一次模型，提供合成、声音档案注册等能力。"""

    def __init__(self, model_dir=None, fp16=USE_FP16, load_trt=USE_TRT):
        ensure_sys_path()
        from cosyvoice.cli.cosyvoice import AutoModel
        self.model_dir = str(model_dir or MODEL_DIR)
        self.lock = threading.Lock()
        self._cosyvoice = AutoModel(model_dir=self.model_dir, fp16=fp16,
                                    load_trt=load_trt)
        self.sample_rate = self._cosyvoice.sample_rate
        install_frontend_guards(self._cosyvoice)
        logger.info('CosyVoice3 loaded (trt=%s), sample_rate=%s, spk=%s',
                    load_trt, self.sample_rate, self.spk_ids)

    @property
    def spk_ids(self) -> list:
        return list(self._cosyvoice.frontend.spk2info.keys())

    @staticmethod
    def normalize_prompt(prompt_text: str) -> str:
        """确保参考文字稿带 <|endofprompt|> 控制标记，否则模型走默认前缀。"""
        prompt_text = (prompt_text or '').strip()
        if '<|endofprompt|>' in prompt_text:
            return prompt_text
        return PROMPT_PREFIX + prompt_text

    def synthesize(self, text, prompt_text='', prompt_wav='', spk_id='',
                   speed=1.0, progress_cb=None) -> list:
        """合成一段文本。

        返回 [(audio: np.float32 一维数组)] 列表，采样率为 self.sample_rate。
        spk_id 非空时忽略 prompt_text/prompt_wav，直接使用已注册档案。
        """
        prompt_text = self.normalize_prompt(prompt_text)
        segments = []
        with self.lock:
            for out in self._cosyvoice.inference_zero_shot(
                    text, prompt_text, prompt_wav,
                    zero_shot_spk_id=spk_id, stream=False, speed=float(speed),
                    text_frontend=True):
                speech = out['tts_speech'].detach().cpu().float().numpy()
                segments.append(np.asarray(speech, dtype=np.float32).squeeze())
                if progress_cb:
                    progress_cb(len(segments))
        return segments

    def register_spk(self, spk_id: str, prompt_text: str, prompt_wav: str) -> bool:
        """注册并持久化声音档案。prompt_text 需与音频逐字对应。"""
        # 上游对参考音频有 30 秒硬上限（cosyvoice/cli/frontend.py 里是 assert），
        # 超了只会抛一句英文断言，看不出是哪个音色、也不知道该改哪儿。这里先拦一道。
        seconds = wav_duration(prompt_wav)
        if seconds > MAX_PROMPT_SECONDS + 0.05:
            raise ValueError(
                f'音色 [{spk_id}] 的参考音频 {seconds:.1f} 秒，超过 CosyVoice 的 '
                f'{MAX_PROMPT_SECONDS:g} 秒上限。请在「声音资产库」删掉这个音色后重新'
                f'新建（现在会自动裁到 30 秒内），或者换一段 30 秒以内的录音。')
        prompt_text = self.normalize_prompt(prompt_text)
        with self.lock:
            ok = self._cosyvoice.add_zero_shot_spk(prompt_text, prompt_wav, spk_id)
            if ok:
                self._cosyvoice.save_spkinfo()
                logger.info('voice profile registered: %s', spk_id)
        return ok

    def remove_spk(self, spk_id: str) -> None:
        with self.lock:
            self._cosyvoice.frontend.spk2info.pop(spk_id, None)
            self._cosyvoice.save_spkinfo()
            logger.info('voice profile removed: %s', spk_id)

    @staticmethod
    def write_wav(audio, path, sr) -> None:
        """用 SoundFile 写 WAV，绕开 torchaudio.save 的 TorchCodec 问题。"""
        sf.write(str(path), audio, sr, subtype='PCM_16')


# 全局单例
_backend = None
_backend_lock = threading.Lock()
_backend_error = None


def get_backend() -> CosyVoice3Backend:
    """懒加载单例；首次调用会阻塞直到模型加载完成。"""
    global _backend, _backend_error
    if _backend is not None:
        return _backend
    with _backend_lock:
        if _backend is None:
            try:
                _backend = CosyVoice3Backend()
            except Exception as e:  # noqa: BLE001
                _backend_error = e
                logger.exception('backend load failed')
                raise
    return _backend


def reset_backend() -> None:
    global _backend, _backend_error
    with _backend_lock:
        _backend = None
        _backend_error = None
