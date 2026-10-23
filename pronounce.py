"""发音词典：放在项目根目录或书旁边的纯文本替换表，改人名/专有词的错读。

格式（借鉴 folivox 的 pronounce.py，Apache-2.0；中文场景用同音字/改写）：

    # 一行一个词条，「=」左边是原文，右边是替换成什么
    菲洛米娜   = 飞罗米娜
    Kvothe     = Quothe            # 英文名用重拼
    迷雾之子*  = 迷雾之子            # 「*」只起注释作用，替换始终是子串级

词典位置：
    pronounce.txt              项目根目录，全局生效
    <书名>.pronounce.txt       与书同目录、同名，只对这本书生效（同名词条覆盖全局）

实现说明：词条按长度降序合成一条正则做**单遍**替换——长词优先命中，
且 re.sub 不会回扫替换结果，所以天然不会连环替换；空词典零开销。
"""

from __future__ import annotations

import re
from pathlib import Path

from config import PROJECT_ROOT

_GLOBAL = PROJECT_ROOT / 'pronounce.txt'


def _parse(path: Path) -> dict[str, str]:
    """解析一个词典文件 → {原词: 替换}。"""
    rules: dict[str, str] = {}
    try:
        raw = path.read_text(encoding='utf-8-sig')
    except OSError:
        return rules
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        left, sep, right = line.partition('=')
        if not sep:
            continue
        left = left.strip().rstrip('*').strip()   # «*» 仅表示前缀通配；中文无词边界，语义等同子串
        right = right.split('#', 1)[0].strip()
        if not left or not right:
            continue
        rules[left] = right
    return rules


def lexicon_for(src: Path) -> list[tuple[str, str]]:
    """一本书生效的词典：全局 + 书旁同名词典，长词在前。"""
    src = Path(src)
    merged = _parse(_GLOBAL)
    merged.update(_parse(src.parent / f'{src.stem}.pronounce.txt'))
    return sorted(merged.items(), key=lambda kv: len(kv[0]), reverse=True)


def apply(text: str, lexicon: list[tuple[str, str]]) -> str:
    """应用词典；无词条时原样返回。"""
    if not text or not lexicon:
        return text
    pattern = re.compile('|'.join(re.escape(word) for word, _ in lexicon))
    repl = dict(lexicon)
    return pattern.sub(lambda m: repl[m.group(0)], text)
