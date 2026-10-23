"""中文数字/量词/货币/单位规范化，在送进模型前改正 wetext 读不好的写法。

借鉴 folivox（Apache-2.0，src/folivox/lang/zh.py）并按长篇小说场景精简：
- 量词前的 2 读「两」不读「二」（两个/两本/两条……；但「第2层」保持二）
- 年份逐位读：1984年 → 一九八四年
- 货币符号：¥12.5 → 12.5元，$3 → 3美元，€/£ 同理
- 百分比：30% → 百分之30
- 时刻：19:30 → 19点30分
- 常见单位：km/h → 公里每小时、°C → 摄氏度、GB → 吉字节……

只做"纠正"，不做整段数字转汉字（wetext 对普通基数没问题，别越俎代庖）。
"""

from __future__ import annotations

import re

_DIGITS = "零一二三四五六七八九"

# 量词：与 folivox 同源的常用集合，覆盖日常与网文高频量词
_MEASURE = (
    "人个只条本位名件张辆台双把支种块次间家层点封所道门颗头首顶幅份杯瓶碗倍年月日"
    "步句声眼手记座片朵粒滴圈遍趟场课节课题字纸页行盘棋杆枪剑刀座桥扇窗罐坛笼圈巢"
)

_UNITS = {
    'km/h': '公里每小时',
    'km/h²': '公里每平方小时',
    'm/s': '米每秒',
    'km²': '平方公里',
    'm²': '平方米',
    'cm²': '平方厘米',
    'm³': '立方米',
    'km': '公里',
    'kg': '公斤',
    'mg': '毫克',
    'cm': '厘米',
    'mm': '毫米',
    'ml': '毫升',
    'kHz': '千赫兹',
    'MHz': '兆赫兹',
    'GHz': '吉赫兹',
    'kW': '千瓦',
    'kWh': '千瓦时',
    'GB': '吉字节',
    'MB': '兆字节',
    'KB': '千字节',
    '°C': '摄氏度',
    '°F': '华氏度',
    'rpm': '转每分',
    'psi': '磅每平方英寸',
    'hp': '马力',
    '°': '度',
    'g': '克',
    'm': '米',
    'l': '升',
}

# 单位按长度降序替换，避免 km 抢在 km/h 前面
_UNIT_KEYS = sorted(_UNITS, key=len, reverse=True)
_UNIT_RE = re.compile(
    r'(?<=[\d])\s*(' + '|'.join(re.escape(k) for k in _UNIT_KEYS) + r')(?![A-Za-z])'
)

_YEAR_RE = re.compile(r'(?<![\d.,])([12]\d{3})(?=年)')
_TWO_RE = re.compile(r'(?<![\d.,第])(2)(?=[%s])' % _MEASURE)
_PCT_RE = re.compile(r'(?<![\d])(\d+(?:\.\d+)?)\s*%(?![\d])')
_MONEY = (('[¥￥]', '元'), (r'\$', '美元'), ('€', '欧元'), ('£', '英镑'))
_TIME_RE = re.compile(r'(?<![\d:.,/-])(\d{1,2}):(\d{2})(?![\d:])')


def _year(m: re.Match) -> str:
    return ''.join(_DIGITS[int(d)] for d in m.group(1))


def _time(m: re.Match) -> str:
    h, mi = int(m.group(1)), int(m.group(2))
    if h > 24 or mi > 59:
        return m.group(0)
    if mi == 0:
        return f'{h}点'
    pad = '零' if mi < 10 else ''
    return f'{h}点{pad}{mi}分'


def normalize(text: str) -> str:
    """把 wetext 读不好的写法替换成它读得好的等价写法。"""
    if not text:
        return text
    text = _UNIT_RE.sub(lambda m: _UNITS[m.group(1)], text)
    text = _YEAR_RE.sub(_year, text)
    # 货币：符号移到数字后面变成「12.5元」
    for sym, word in _MONEY:
        text = re.sub(rf'{sym}\s?(\d+(?:\.\d+)?)', rf'\1{word}', text)
    text = _PCT_RE.sub(r'百分之\1', text)
    text = _TIME_RE.sub(_time, text)
    text = _TWO_RE.sub('两', text)
    return text
