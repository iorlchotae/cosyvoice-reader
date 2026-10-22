"""电子书导入的确定性测试：不需要显卡、不需要模型，秒级跑完。

用法（在项目根目录）：
    $env:PYTHONIOENCODING='utf-8'
    .\\.venv\\Scripts\\python.exe -u scripts\\test_ebook_import.py

覆盖：pdf / epub / mobi / azw3 / docx 提取、页眉页脚过滤、扫描件识别、
.doc 提示、确定性（同书两次提取字节一致）、段落契约（空行分隔）。
"""

import io
import os
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import ebook_import as ei  # noqa: E402

WORK = ROOT / '.tmp' / 'ebook_test'
SAMPLES = ROOT / '.tmp' / 'ebooks'
ok_count = 0
fail_count = 0
skip_count = 0


def check(label, cond, extra=''):
    global ok_count, fail_count
    if cond:
        ok_count += 1
        print(f'  [OK]   {label}')
    else:
        fail_count += 1
        print(f'  [FAIL] {label} {extra}')


def skip(label, why):
    global skip_count
    skip_count += 1
    print(f'  [SKIP] {label} —— {why}')


def extract(name: str, data: bytes = None, src: Path = None) -> dict:
    """把 bytes 或文件提取到 WORK 下的 txt，返回 (结果, 文本)。"""
    work = WORK / name
    if work.exists():
        shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True, exist_ok=True)
    if src is None:
        src = work / 'source.bin'
        src.write_bytes(data)
    dest = ei.free_text_path(work, src.name)
    info = ei.extract_text(src, dest)
    return info, dest.read_text(encoding='utf-8')


# --------------------------------------------------------------------------
# 造样书
# --------------------------------------------------------------------------

def make_pdf(path: Path, pages: int = 10) -> None:
    import pymupdf
    doc = pymupdf.open()
    for i in range(1, pages + 1):
        page = doc.new_page()
        page.insert_text((72, 60), 'RUNNING-HEADER-MARK')          # 页眉，每页相同
        page.insert_text((72, 90), f'Body of page {i}: the real text goes here.')
        page.insert_text((72, 780), f'- {i} -')                    # 页脚页码，每页不同
    doc.save(str(path))
    doc.close()


def make_pdf_scan(path: Path) -> None:
    """没有文字层的 PDF —— 模拟扫描件。"""
    import pymupdf
    doc = pymupdf.open()
    for _ in range(3):
        doc.new_page()
    doc.save(str(path))
    doc.close()


def make_docx(path: Path) -> None:
    from docx import Document
    d = Document()
    d.add_paragraph('第一段正文，应该在表格之前。')
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).text = '表格甲'
    t.cell(0, 1).text = '表格乙'
    t.cell(1, 0).text = '表格丙'
    t.cell(1, 1).text = '表格丁'
    d.add_paragraph('第二段正文，应该在表格之后。')
    d.save(str(path))


CONTAINER = '''<?xml version="1.0"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf"
     media-type="application/oebps-package+xml"/></rootfiles>
</container>'''


def make_epub(path: Path) -> None:
    """故意让文件名顺序和 spine 顺序不一致，验证是按 spine 读的。"""
    chapters = {'a.xhtml': ('第三章', 'ChapterThreeBody'),
                'b.xhtml': ('第一章', 'ChapterOneBody'),
                'c.xhtml': ('第二章', 'ChapterTwoBody')}
    opf = '''<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="i">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:title>T</dc:title></metadata>
 <manifest>
  <item id="b" href="Text/b.xhtml" media-type="application/xhtml+xml"/>
  <item id="c" href="Text/c.xhtml" media-type="application/xhtml+xml"/>
  <item id="a" href="Text/a.xhtml" media-type="application/xhtml+xml"/>
 </manifest>
 <spine><itemref idref="b"/><itemref idref="c"/><itemref idref="a"/></spine>
</package>'''
    with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', CONTAINER)
        z.writestr('OEBPS/content.opf', opf)
        for fname, (title, marker) in chapters.items():
            z.writestr(f'OEBPS/Text/{fname}',
                       '<html><head><title>x</title>'
                       '<style>p{color:red}</style></head><body>'
                       f'<h1>{title}</h1><p>{marker} 正文段落。</p>'
                       '<p>第二段 <b>加粗</b> 内容。</p>'
                       '<script>var x=1;</script></body></html>')


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def t_pdf():
    print('\n[1] PDF：页眉页脚过滤 + 正文保留')
    src = WORK / 'pdf_case' / 'book.pdf'
    src.parent.mkdir(parents=True, exist_ok=True)
    make_pdf(src, pages=10)
    info, text = extract('pdf_case_out', src=src)
    check('提取成功', info['chars'] > 100, f"chars={info['chars']}")
    check('引擎是 PyMuPDF', info['engine'] == 'PyMuPDF', info['engine'])
    check('页眉被过滤掉', 'RUNNING-HEADER-MARK' not in text)
    check('页码被过滤掉', '- 1 -' not in text and '- 10 -' not in text)
    check('正文都在', all(f'Body of page {i}' in text for i in (1, 5, 10)))
    check('段落用空行分隔', '\n\n' in text and '\r' not in text)


def t_pdf_scanned():
    print('\n[2] PDF 扫描件：给出人话提示')
    src = WORK / 'pdf_scan' / 'scan.pdf'
    src.parent.mkdir(parents=True, exist_ok=True)
    make_pdf_scan(src)
    try:
        extract('pdf_scan_out', src=src)
        check('应该抛 EbookError', False)
    except ei.EbookError as e:
        check('code=scanned', e.code == 'scanned', e.code)
        check('文案提到扫描件', '扫描件' in e.user_text(), e.user_text())


def t_docx():
    print('\n[3] DOCX：段落 + 表格按文档顺序')
    src = WORK / 'docx_case' / 'report.docx'
    src.parent.mkdir(parents=True, exist_ok=True)
    make_docx(src)
    info, text = extract('docx_case_out', src=src)
    check('提取成功', info['chars'] > 20, f"chars={info['chars']}")
    check('引擎是 python-docx', info['engine'] == 'python-docx', info['engine'])
    check('第一段在', '第一段正文' in text)
    check('表格内容在', '表格甲' in text and '表格丁' in text)
    check('第二段在', '第二段正文' in text)
    i1, it, i2 = text.find('第一段正文'), text.find('表格甲'), text.find('第二段正文')
    check('顺序正确（段落→表格→段落）', 0 <= i1 < it < i2, f'{i1},{it},{i2}')


def t_epub():
    print('\n[4] EPUB：按 spine 顺序，不按文件名顺序')
    src = WORK / 'epub_case' / 'book.epub'
    src.parent.mkdir(parents=True, exist_ok=True)
    make_epub(src)
    info, text = extract('epub_case_out', src=src)
    check('提取成功', info['chars'] > 30, f"chars={info['chars']}")
    i1 = text.find('ChapterOneBody')
    i2 = text.find('ChapterTwoBody')
    i3 = text.find('ChapterThreeBody')
    check('三章都在', i1 >= 0 and i2 >= 0 and i3 >= 0, f'{i1},{i2},{i3}')
    check('顺序跟 spine（1→2→3）', 0 <= i1 < i2 < i3, f'{i1},{i2},{i3}')
    check('script/style 被丢掉', 'var x=1' not in text and 'color:red' not in text)
    check('标签被清掉', '<p>' not in text and '<b>' not in text)
    check('加粗文字保留', '加粗' in text)


def t_mobi():
    for name, label in (('test_mobi7.mobi', 'MOBI7'), ('test_kf8.azw3', 'AZW3/KF8')):
        print(f'\n[5] Kindle {label}')
        src = SAMPLES / name
        if not src.exists():
            skip(label, f'缺少样书 {src}')
            continue
        info, text = extract(f'kindle_{name}', src=src)
        check(f'{label} 提取成功', info['chars'] > 5000, f"chars={info['chars']}")
        check(f'{label} 引擎是 KindleUnpack', info['engine'] == 'KindleUnpack', info['engine'])
        check(f'{label} 拿到英文正文', 'Chapter' in text or 'CHAPTER' in text)
        check(f'{label} 没残留 HTML 标签', '<html' not in text.lower())


def t_doc_unsupported():
    print('\n[6] .doc 老格式：明确提示另存为 .docx')
    src = WORK / 'legacy' / 'old.doc'
    src.parent.mkdir(parents=True, exist_ok=True)
    src.write_bytes(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 512)  # OLE 魔数
    try:
        extract('legacy_out', src=src)
        check('应该抛 EbookError', False)
    except ei.EbookError as e:
        check('code=unsupported', e.code == 'unsupported', e.code)
        check('提示里给出 .docx 出路', '.docx' in e.user_text(), e.user_text())


def t_deterministic():
    print('\n[7] 确定性：同一本书两次提取字节完全一致')
    src = WORK / 'epub_case' / 'book.epub'
    if not src.exists():
        make_epub(src)
        src.parent.mkdir(parents=True, exist_ok=True)
    _, a = extract('det_a', src=src)
    _, b = extract('det_b', src=src)
    check('字节一致', a.encode('utf-8') == b.encode('utf-8'))
    if (SAMPLES / 'test_mobi7.mobi').exists():
        _, c = extract('det_c', src=SAMPLES / 'test_mobi7.mobi')
        _, d = extract('det_d', src=SAMPLES / 'test_mobi7.mobi')
        check('MOBI 两次提取也一致', c.encode('utf-8') == d.encode('utf-8'))
        # 指纹稳定 = 断点续传能认出旧断点
        import document as doc_mod
        f1 = doc_mod.units_fingerprint(doc_mod.load_units(WORK / 'det_c' / 'test_mobi7.txt'))
        f2 = doc_mod.units_fingerprint(doc_mod.load_units(WORK / 'det_d' / 'test_mobi7.txt'))
        check('段落指纹一致（能续传）', f1 == f2, f'{f1} vs {f2}')
    else:
        skip('MOBI 确定性', '缺少样书')


def t_helpers():
    print('\n[8] 小工具：后缀识别 + 重名避让')
    check('.txt 不算电子书', not ei.is_ebook('a.txt'))
    check('.md 不算电子书', not ei.is_ebook('a.md'))
    for ext in ('.pdf', '.epub', '.mobi', '.azw3', '.docx'):
        check(f'{ext} 算电子书', ei.is_ebook('book' + ext))
    check('.doc 认得出但不在可提取列表', ei.is_supported('a.doc') and not ei.is_ebook('a.doc'))

    d = WORK / 'naming'
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    d.mkdir(parents=True, exist_ok=True)
    first = ei.free_text_path(d, 'book.mobi')
    first.write_text('x', encoding='utf-8')
    second = ei.free_text_path(d, 'book.mobi')
    check('重名自动避让', first.name == 'book.txt' and second.name == 'book_2.txt',
          f'{first.name},{second.name}')


def main():
    for mod in ('pymupdf', 'docx', 'mobi', 'bs4'):
        try:
            __import__(mod)
        except ImportError as e:
            print(f'缺依赖 {mod}：{e}')
            print('请先：.venv\\Scripts\\pip install python-docx mobi pymupdf beautifulsoup4')
            return 1
    WORK.mkdir(parents=True, exist_ok=True)

    t_pdf()
    t_pdf_scanned()
    t_docx()
    t_epub()
    t_mobi()
    t_doc_unsupported()
    t_deterministic()
    t_helpers()

    print(f'\n通过 {ok_count} 项，失败 {fail_count} 项，跳过 {skip_count} 项')
    if fail_count:
        print('EBOOK_IMPORT_FAILED')
        return 1
    print('EBOOK_IMPORT_OK')
    return 0


if __name__ == '__main__':
    sys.exit(main())
