# -*- coding: utf-8 -*-
"""回归测试：上游文本前端的两个坑有没有被保险堵住。

背景（真实事故）：一本 950 段的 mobi 里，只有**第 1 段**命中了 wetext 的一个坑——
`normalize()` 不报错但返回空串——下游 `split_paragraph` 拿空串取 `text[-1]` 直接
IndexError，整篇文档瞬间失败。所以这里的每个用例都对应一个真实崩法：

  1. normalize 抛异常（wetext 内部 `assert len(input) > 0`）→ 应退回原文
  2. normalize 返回空串（不抛异常，最阴的一种）→ 应退回原文
  3. normalize 正常 → 原样返回，不能被保险改坏
  4. split_paragraph 收到空串 → 产出空结果，不能 IndexError
  5. 保险装两次不能叠加（幂等）

不需要显卡、不需要加载模型。
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import ensure_sys_path                              # noqa: E402

ensure_sys_path()

from backends.cosyvoice3 import install_frontend_guards         # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name} {extra}')


class FakeTN:
    """够用的假 normalizer：按行为表决定抛异常 / 返回空串 / 正常。"""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.calls = 0

    def normalize(self, text):
        self.calls += 1
        if self.behaviour == 'raise':
            raise AssertionError
        if self.behaviour == 'empty':
            return ''
        return f'<{text}>'


class FakeFrontend:
    def __init__(self, zh, en):
        self.zh_tn_model = zh
        self.en_tn_model = en


class FakeCosyVoice:
    def __init__(self, frontend):
        self.frontend = frontend


print('=== 1. normalize 抛异常 → 退回原文')
zh = FakeTN('raise')
en = FakeTN('raise')
install_frontend_guards(FakeCosyVoice(FakeFrontend(zh, en)))
check('en 抛异常时退回原文', en.normalize('Chapter #5') == 'Chapter #5',
      f'得到 {en.normalize("Chapter #5")!r}')
check('zh 抛异常时退回原文', zh.normalize('第#3章') == '第#3章')
check('正常输入仍然被归一化', FakeTN('ok').normalize('abc') == '<abc>')

print('=== 2. normalize 返回空串 → 退回原文')
zh2, en2 = FakeTN('empty'), FakeTN('empty')
install_frontend_guards(FakeCosyVoice(FakeFrontend(zh2, en2)))
url = '/www.gutenberg.org/license.'
check('en 返回空串时退回原文', en2.normalize(url) == url,
      f'得到 {en2.normalize(url)!r}')
check('zh 返回空串时退回原文', zh2.normalize('内容') == '内容')

print('=== 3. 正常输入不被改坏')
zh3, en3 = FakeTN('ok'), FakeTN('ok')
install_frontend_guards(FakeCosyVoice(FakeFrontend(zh3, en3)))
check('en 正常值透传', en3.normalize('hello') == '<hello>')
check('zh 正常值透传', zh3.normalize('你好') == '<你好>')

print('=== 4. split_paragraph 容忍空串')
try:
    from cosyvoice.utils import frontend_utils as fu
    from cosyvoice.cli import frontend as fe_mod

    got = list(fu.split_paragraph('', lambda s: [1] * len(s), 'zh'))
    check('fu.split_paragraph("") 返回空结果而不是崩', got == [], f'得到 {got!r}')
    got2 = list(fe_mod.split_paragraph('', lambda s: [1] * len(s), 'en'))
    check('frontend 里绑定的那个也被换掉了', got2 == [], f'得到 {got2!r}')
    normal = list(fu.split_paragraph('你好。世界。', lambda s: [1] * len(s), 'zh'))
    check('非空文本照常分段', len(normal) >= 1, f'得到 {normal!r}')
except ImportError as e:
    print(f'  [SKIP] 拿不到 cosyvoice 前端模块（{e}）')

print('=== 5. 重复安装幂等')
counter = FakeTN('raise')
fe5 = FakeFrontend(counter, FakeTN('raise'))
install_frontend_guards(FakeCosyVoice(fe5))
first = fe5.zh_tn_model.normalize
install_frontend_guards(FakeCosyVoice(fe5))
check('第二次安装不再包一层', fe5.zh_tn_model.normalize is first)

print(f'\n通过 {PASS} 项，失败 {FAIL} 项')
print('FRONTEND_GUARD_OK' if FAIL == 0 else 'FRONTEND_GUARD_FAIL')
sys.exit(1 if FAIL else 0)
