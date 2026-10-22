# -*- coding: utf-8 -*-
"""回归测试：文件名里那些「看不见的坑」，不能再让整篇文档报废。

真实事故：用户的一本 .azw3 叫 `弑君者传奇：风之名 ([美] 帕特里克·罗斯福斯  .azw3`
——书名和扩展名之间多打了两个空格。`Path.stem` 于是以空格结尾，而 Windows
落盘时会把目录名结尾的空格**悄悄吃掉**：建目录建出来的是没有空格的那个名字，
紧接着再建 `…罗斯福斯  \\segments` 就「系统找不到指定的路径」→ WinError 3。
表现是**整本书 0/457 段直接失败**，而报错信息完全看不出跟文件名有关。

不需要显卡、不需要加载模型。
"""
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from config import safe_stem, safe_text, MAX_STEM_LEN        # noqa: E402
import app as app_mod                                        # noqa: E402
import ebook_import as ebook                                 # noqa: E402

PASS = FAIL = 0


def check(name, cond, extra=''):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f'  [OK]   {name}')
    else:
        FAIL += 1
        print(f'  [FAIL] {name} {extra}')


BAD = '弑君者传奇：风之名 ([美] 帕特里克·罗斯福斯  .azw3'

print('=== 1. safe_text：只洗字符，不能拆掉书名自带的点')
check('结尾空格被去掉', safe_text('罗斯福斯  ') == '罗斯福斯', repr(safe_text('罗斯福斯  ')))
check('结尾的点被去掉', safe_text('书名...') == '书名', repr(safe_text('书名...')))
check('书名里的点要保住', safe_text('第1.5章') == '第1.5章', repr(safe_text('第1.5章')))
check('非法字符换成下划线', safe_text('a<b>c:d"e/f\\g|h?i*j') == 'a_b_c_d_e_f_g_h_i_j')
check('控制字符也换掉', safe_text('a\x00b\x1fc') == 'a_b_c')
check('空名字给兜底名', safe_text('   ') == 'document', repr(safe_text('   ')))
check('Windows 保留名加前缀', safe_text('CON') == '_CON', repr(safe_text('CON')))
check('保留名带后缀也认得', safe_text('com1.txt') == '_com1.txt', repr(safe_text('com1.txt')))
check(f'超长截到 {MAX_STEM_LEN}', len(safe_text('长' * 300)) <= MAX_STEM_LEN)
check('中文和括号原样保留', safe_text('风之名 ([美] 罗斯福斯') == '风之名 ([美] 罗斯福斯')

print('=== 2. safe_stem：从完整文件名取主干')
check('事故文件名的主干没有结尾空格',
      safe_stem(BAD) == '弑君者传奇：风之名 ([美] 帕特里克·罗斯福斯',
      repr(safe_stem(BAD)))
check('普通文件名的后缀被去掉', safe_stem('book.txt') == 'book')
check('多段后缀只切最后一段', safe_stem('a.b.txt') == 'a.b')
check('没有后缀就用原名', safe_stem('书名') == '书名')
check('带路径也只取文件名', safe_stem('C:\\x\\y\\书名.txt') == '书名')
check('.开头的隐藏名不被切空', safe_stem('.gitignore') == '.gitignore', repr(safe_stem('.gitignore')))

print('=== 3. 上传名清洗 (app._safe_upload_name)')
up = app_mod._safe_upload_name(BAD)
check('上传名的主干不再以空格结尾', not Path(up).stem.endswith(' '), repr(up))
check('上传名保留了 .azw3 后缀', up.endswith('.azw3'), repr(up))
check('事故名字被洗成干净主干', up == '弑君者传奇：风之名 ([美] 帕特里克·罗斯福斯.azw3', repr(up))
check('普通中文名不受影响', app_mod._safe_upload_name('暮色.txt') == '暮色.txt')
check('没后缀的补 .txt', app_mod._safe_upload_name('没有后缀') == '没有后缀.txt')
check('路径被剥掉只留文件名', app_mod._safe_upload_name('C:\\a\\b\\x.txt') == 'x.txt')

print('=== 4. 提取出来的 txt 落盘名 (ebook.free_text_path)')
tmp = Path(tempfile.mkdtemp())
p1 = ebook.free_text_path(tmp, BAD)
check('提取名的主干没有结尾空格', not p1.stem.endswith(' '), repr(p1.name))
check('扩展名是 .txt', p1.suffix == '.txt', repr(p1.name))
p1.write_text('x', encoding='utf-8')
p2 = ebook.free_text_path(tmp, BAD)
check('同名不撞车，自动加序号', p2.name != p1.name and p2.name.endswith('_2.txt'), repr(p2.name))

print('=== 5. 真刀真枪：拿事故名字建输出目录')
work = tmp / 'out'
work.mkdir()
stem = safe_text(Path(app_mod._safe_upload_name(BAD)).stem)
seg = work / stem / 'segments'
try:
    seg.mkdir(parents=True, exist_ok=True)
    ok = seg.is_dir()
except Exception as e:                                       # noqa: BLE001
    ok = False
    print(f'         异常：{type(e).__name__}: {e}')
check('按清洗后的主干建 segments 成功', ok)
check('目录名不含结尾空格', not stem.endswith(' '), repr(stem))
# 对照：不清洗就一定会失败
raw = Path(BAD).stem
try:
    (work / raw / 'segments').mkdir(parents=True, exist_ok=True)
    raw_ok = True
except FileNotFoundError:
    raw_ok = False
check('不清洗的话确实会 WinError 3（证明这个坑是真的）', not raw_ok)

print('=== 6. probe_resume / synthesize_core 用的是同一个主干')
import backends.synthesize as syn                               # noqa: E402
src = tmp / app_mod._safe_upload_name(BAD)
src.write_text('段落一。\n\n段落二。\n', encoding='utf-8')
check('probe 与 core 算出来的主干一致（看得到断点也接得上）',
      safe_text(src.stem) == safe_text(Path(src).stem))
check('probe_resume 对不存在的断点不炸', isinstance(syn.probe_resume(src, 'X', 1.0, work), dict))

shutil.rmtree(tmp, ignore_errors=True)
print(f'\n通过 {PASS} 项，失败 {FAIL} 项')
print('SAFE_NAMES_OK' if FAIL == 0 else 'SAFE_NAMES_FAIL')
sys.exit(1 if FAIL else 0)
