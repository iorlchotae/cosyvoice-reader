"""电子书 → 纯文本 提取层。

把 .pdf / .epub / .mobi / .azw3 / .docx 统一提取成 UTF-8 纯文本，之后
**完全交给 document.py 既有的分段 / 角色识别 / 断点续传流程**，下游一行都不用改。

三条设计约束（改这个文件前请先读）：

1. **确定性**：同一本书提取两次，文字必须一模一样。断点续传的指纹是按文本
   算的（document.units_fingerprint），一旦不确定，用户重拖同一本书就认不出
   旧断点，会莫名其妙从头重念。
2. **段落用空行分隔**：document.split_chunks 是按 `\\n\\n` 切段的。这里统一
   约定「一个自然行 = 一个段落」，切块器才能自由地把短行合并到 ~1000 字。
3. **失败要说人话**：DRM、扫描件、老 .doc 各自的提示都不一样，见 EbookError。
"""

from __future__ import annotations

import importlib
import posixpath
import re
import shutil
import zipfile
from pathlib import Path
from urllib.parse import unquote

from config import safe_stem

# 纯文本：直接读，不需要提取
TEXT_EXTS = {'.txt', '.text', '.md', '.markdown'}
# 能提取的电子书
EBOOK_EXTS = {'.pdf', '.epub', '.mobi', '.azw3', '.azw', '.prc', '.docx'}
# 认得出来但暂不支持：给专门提示，好过干巴巴一句「格式不支持」
LEGACY_EXTS = {
    '.doc': '暂不支持 Word 97-2003 的 .doc 老格式',
    '.rtf': '暂不支持 .rtf',
    '.odt': '暂不支持 .odt',
    '.wps': '暂不支持 WPS 的 .wps',
    '.fb2': '暂不支持 .fb2',
    '.lit': '暂不支持 .lit',
}
SUPPORTED_EXTS = TEXT_EXTS | EBOOK_EXTS
ALL_EXTS = SUPPORTED_EXTS | set(LEGACY_EXTS)

MIN_PDF_CHARS = 50           # PDF 整本少于这么多字 → 判为扫描件
RUNNING_HEAD_MIN_PAGES = 5   # 少于这么多页不做页眉页脚过滤
RUNNING_HEAD_RATIO = 0.5     # 在超过一半页面上出现的行才算页眉页脚

_HTML_BLOCKS = ('p', 'div', 'li', 'tr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6',
                'blockquote', 'section', 'article', 'pre', 'figcaption',
                'td', 'th', 'dd', 'dt')
_PAGE_NUM_RE = re.compile(r'^[\s\-—–_·.\[\]()【】]*\d{1,4}[\s\-—–_·.\[\]()【】]*$')
_CN_PAGE_RE = re.compile(r'^第?\s*\d{1,4}\s*[页頁]\s*$|^\s*[页頁]\s*\d{1,4}\s*$')


class EbookError(Exception):
    """提取失败，带一句能直接给用户看的中文。"""

    def __init__(self, code: str, message: str, hint: str = ''):
        super().__init__(message)
        self.code = code          # drm / scanned / encrypted / unsupported / missing_dep / corrupt / empty
        self.message = message
        self.hint = hint

    def user_text(self) -> str:
        return f'{self.message}｜{self.hint}' if self.hint else self.message

    def payload(self) -> dict:
        return {'code': self.code, 'error': self.user_text()}


# --------------------------------------------------------------------------
# 对外接口
# --------------------------------------------------------------------------

def is_ebook(name) -> bool:
    """是不是需要提取的电子书格式。"""
    return Path(str(name)).suffix.lower() in EBOOK_EXTS


def is_supported(name) -> bool:
    """这个后缀整体上认不认（含暂时不支持的 .doc，好给专门提示）。"""
    return Path(str(name)).suffix.lower() in ALL_EXTS


def free_text_path(directory, original_name) -> Path:
    """给提取出来的文本挑一个不撞车的落盘名（同目录可能已有同名 .txt）。

    主干必须过 `safe_stem`：`…罗斯福斯  .azw3` 这种「空格紧挨着扩展名」的名字，
    主干以空格结尾，而 Windows 会把结尾空格悄悄吃掉，之后拿它建输出目录必失败
    （WinError 3，报错完全看不出是文件名的问题）。
    """
    directory = Path(directory)
    stem = safe_stem(original_name, fallback='book')
    cand = directory / f'{stem}.txt'
    n = 2
    while cand.exists():
        cand = directory / f'{stem}_{n}.txt'
        n += 1
    return cand


def extract_text(src, dest) -> dict:
    """把电子书 src 提取成纯文本写到 dest。

    返回 {'chars', 'engine', 'paragraphs'}；失败抛 EbookError。
    """
    src, dest = Path(src), Path(dest)
    ext = src.suffix.lower()

    if ext == '.pdf':
        text, engine = _pdf_text(src)
    elif ext == '.epub':
        text, engine = _epub_text(src)
    elif ext == '.docx':
        text, engine = _docx_text(src)
    elif ext in ('.mobi', '.azw3', '.azw', '.prc'):
        text, engine = _mobi_text(src)
    elif ext in LEGACY_EXTS:
        hint = ('用 Word 打开后「另存为」.docx，再重新拖进来'
                if ext == '.doc' else '请先转换成 .txt / .docx / .pdf')
        raise EbookError('unsupported', LEGACY_EXTS[ext], hint)
    else:
        raise EbookError('unsupported', f'不支持的格式：{ext or src.name}',
                         '支持 .txt .md .pdf .epub .mobi .azw3 .docx')

    text = _tidy(text)
    if not text.strip():
        raise EbookError('empty', f'《{src.name}》里没提取到文字',
                         '可能是加密书、扫描件或空文件')
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(text, encoding='utf-8', newline='\n')
    return {'chars': len(text),
            'engine': engine,
            'paragraphs': text.count('\n\n') + 1}


# --------------------------------------------------------------------------
# 通用工具
# --------------------------------------------------------------------------

def _need(modname: str, package: str, what: str):
    try:
        return importlib.import_module(modname)
    except ImportError:
        raise EbookError('missing_dep', f'缺少 {what} 解析库 {package}',
                         f'请运行：.venv\\Scripts\\pip install {package}') from None


def _short(e, n: int = 120) -> str:
    s = ' '.join(str(e).split())
    return s[:n] + ('…' if len(s) > n else '')


def _tidy(text: str) -> str:
    """规整成「一行一段、空行分隔」。

    空行全部丢掉再重新用 `\\n\\n` 拼——因为约定就是一个自然行一个段落，
    这样拆开能保证切块器拿到最大的合并自由度。
    """
    if not text:
        return ''
    text = (text.replace('\ufeff', '')
                .replace('\u200b', '')
                .replace('\u00a0', ' '))
    text = text.replace('\r\n', '\n').replace('\r', '\n')
    lines = []
    for raw in text.split('\n'):
        ln = ' '.join(raw.split())
        if ln:
            lines.append(ln)
    return '\n\n'.join(lines)


def _is_page_number(ln: str) -> bool:
    if len(ln) > 8:
        return False
    return bool(_PAGE_NUM_RE.match(ln) or _CN_PAGE_RE.match(ln))


def _drop_running_heads(pages: list) -> list:
    """去掉每页都重复的页眉页脚和孤立页码。

    保守做法：只在页数够多、且某行出现在超过一半页面上时才丢。正文里
    重复率超过 50% 的句子几乎不存在，所以不会误伤。
    """
    def clean(page):
        return [ln for ln in page if not _is_page_number(ln)]

    if len(pages) < RUNNING_HEAD_MIN_PAGES:
        return [clean(p) for p in pages]

    seen: dict = {}
    for page in pages:
        for ln in set(page):
            if len(ln) <= 60:
                seen[ln] = seen.get(ln, 0) + 1
    need = max(2, int(len(pages) * RUNNING_HEAD_RATIO))
    running = {ln for ln, c in seen.items() if c >= need}

    out = []
    for page in pages:
        out.append([ln for ln in page if ln not in running and not _is_page_number(ln)])
    return out


# --------------------------------------------------------------------------
# PDF
# --------------------------------------------------------------------------

def _fitz():
    for modname in ('pymupdf', 'fitz'):
        try:
            return importlib.import_module(modname)
        except ImportError:
            continue
    raise EbookError('missing_dep', '缺少 PDF 解析库 PyMuPDF',
                     '请运行：.venv\\Scripts\\pip install pymupdf')


def _pdf_text(src: Path):
    fitz = _fitz()
    try:
        doc = fitz.open(str(src))
    except Exception as e:  # noqa: BLE001
        raise EbookError('corrupt', f'打不开这个 PDF：{_short(e)}',
                         '确认文件没有损坏') from None
    try:
        if getattr(doc, 'needs_pass', False):
            raise EbookError('encrypted', '这个 PDF 有密码保护，读不出文字',
                             '请先用密码打开、另存为普通 PDF 再重试')
        pages = []
        for page in doc:
            try:
                raw = page.get_text('text') or ''
            except Exception:  # noqa: BLE001
                raw = ''
            pages.append([ln.strip() for ln in raw.split('\n') if ln.strip()])
    finally:
        try:
            doc.close()
        except Exception:  # noqa: BLE001
            pass

    pages = _drop_running_heads(pages)
    lines = [ln for page in pages for ln in page]
    if sum(len(x) for x in lines) < MIN_PDF_CHARS:
        raise EbookError('scanned',
                         f'《{src.name}》是扫描件（整页图片），没有文字层，提取不出文字',
                         '需要 OCR 才能识别，本项目暂不支持；建议找这本书的 epub/txt 版本')
    return '\n\n'.join(lines), 'PyMuPDF'


# --------------------------------------------------------------------------
# EPUB
# --------------------------------------------------------------------------

def _resolve_href(base: str, href: str) -> str:
    href = unquote(str(href).split('#')[0].split('?')[0]).replace('\\', '/')
    return posixpath.normpath(posixpath.join(base, href)).lstrip('/')


def _read_zip_text(zf: zipfile.ZipFile, name: str) -> str:
    raw = zf.read(name)
    for enc in ('utf-8-sig', 'utf-8', 'gb18030', 'latin-1'):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace')


def _find_opf(zf: zipfile.ZipFile, names: list):
    if 'META-INF/container.xml' in names:
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(_read_zip_text(zf, 'META-INF/container.xml'))
            for el in root.iter():
                if el.tag.split('}')[-1] == 'rootfile' and el.get('full-path'):
                    return el.get('full-path')
        except Exception:  # noqa: BLE001
            pass
    for n in sorted(names):
        if n.lower().endswith('.opf'):
            return n
    return None


def _spine_hrefs(zf: zipfile.ZipFile, opf_name: str) -> list:
    """按 OPF 的 spine 顺序取出正文文件（顺序稳定，指纹才不会飘）。"""
    import xml.etree.ElementTree as ET
    try:
        root = ET.fromstring(_read_zip_text(zf, opf_name))
    except Exception:  # noqa: BLE001
        return []
    manifest = {}
    for el in root.iter():
        if el.tag.split('}')[-1] == 'item' and el.get('id') and el.get('href'):
            media = (el.get('media-type') or '').lower()
            if media in ('application/xhtml+xml', 'text/html', '') or not media:
                manifest[el.get('id')] = el.get('href')
    hrefs = []
    for el in root.iter():
        if el.tag.split('}')[-1] == 'itemref' and el.get('idref'):
            href = manifest.get(el.get('idref'))
            if href and href not in hrefs:
                hrefs.append(href)
    return hrefs


def _html_to_text(html: str) -> str:
    if not html:
        return ''
    try:
        from bs4 import BeautifulSoup
    except ImportError:
        return _strip_tags(html)
    soup = BeautifulSoup(html, 'html.parser')
    for bad in soup(['script', 'style', 'head', 'title', 'nav', 'svg']):
        bad.decompose()
    for br in soup.find_all('br'):
        br.replace_with('\n')
    for tag in soup.find_all(list(_HTML_BLOCKS)):
        tag.insert_after('\n\n')
    return soup.get_text()


def _strip_tags(html: str) -> str:
    html = re.sub(r'(?is)<(script|style|head)[^>]*>.*?</\1>', '', html)
    html = re.sub(r'(?i)<br\s*/?>', '\n', html)
    html = re.sub(r'(?i)</(p|div|li|tr|h[1-6]|blockquote)>', '\n\n', html)
    return re.sub(r'<[^>]+>', '', html)


def _epub_text(src: Path):
    try:
        zf = zipfile.ZipFile(src)
    except Exception as e:  # noqa: BLE001
        raise EbookError('corrupt', f'打不开这个 EPUB：{_short(e)}',
                         '确认文件完整、且没有 DRM 加密') from None
    with zf:
        names = zf.namelist()
        opf = _find_opf(zf, names)
        if opf:
            base = posixpath.dirname(opf)
            hrefs = _spine_hrefs(zf, opf)
        else:
            base, hrefs = '', []
        if not hrefs:
            # 没有 OPF 或 spine 坏了：退回按文件名顺序读所有 html
            hrefs = [n for n in sorted(names)
                     if n.lower().endswith(('.xhtml', '.html', '.htm'))]
        parts, used = [], set()
        for href in hrefs:
            name = _resolve_href(base, href) if opf else href
            if name in used or name not in names:
                continue
            used.add(name)
            parts.append(_html_to_text(_read_zip_text(zf, name)))

    text = '\n\n'.join(p for p in parts if p and p.strip())
    if not text.strip():
        raise EbookError('empty', f'《{src.name}》里没提取到文字',
                         '内容可能全是图片，或这本书有 DRM 保护')
    return text, 'EPUB'


# --------------------------------------------------------------------------
# Kindle（.mobi / .azw3）
# --------------------------------------------------------------------------

def _mobi_text(src: Path):
    mobi = _need('mobi', 'mobi', 'MOBI/AZW3')
    tmp = None
    try:
        tmp, out = mobi.extract(str(src))
    except Exception as e:  # noqa: BLE001
        low = str(e).lower()
        if 'encrypt' in low or 'drm' in low:
            raise EbookError('drm', '这本书有 DRM 数字版权保护，提取不出文字',
                             '亚马逊正版电子书要先去掉 DRM；可改用无 DRM 的 epub/txt') from None
        raise EbookError('corrupt', f'解析 Kindle 格式失败：{_short(e)}',
                         '亚马逊正版书多半有 DRM；否则请确认文件完整') from None
    try:
        out = Path(out)
        suffix = out.suffix.lower()
        if suffix == '.epub':
            text, _ = _epub_text(out)
        elif suffix in ('.html', '.htm'):
            text = _html_to_text(out.read_text(encoding='utf-8', errors='replace'))
        elif suffix == '.pdf':
            text, _ = _pdf_text(out)
        else:
            raise EbookError('corrupt', f'解包出来的格式不认识：{suffix}')
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)

    if not text.strip():
        raise EbookError('empty', f'《{src.name}》里没提取到文字',
                         '可能是 DRM 保护或内容全是图片')
    return text, 'KindleUnpack'


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------

def _docx_text(src: Path):
    _need('docx', 'python-docx', '.docx')
    from docx import Document
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        d = Document(str(src))
    except Exception as e:  # noqa: BLE001
        raise EbookError('corrupt', f'打不开这个 .docx：{_short(e)}',
                         '确认文件没有损坏；.doc 老格式请先另存为 .docx') from None

    out = []
    # 按 body 子元素的**文档顺序**遍历，段落和表格交错也不会错位
    for child in d.element.body.iterchildren():
        tag = child.tag.split('}')[-1]
        if tag == 'p':
            t = Paragraph(child, d).text.strip()
            if t:
                out.append(t)
        elif tag == 'tbl':
            try:
                table = Table(child, d)
            except Exception:  # noqa: BLE001
                continue
            for row in table.rows:
                cells = []
                for cell in row.cells:
                    t = ' '.join(cell.text.split())
                    if t:
                        cells.append(t)
                if cells:
                    out.append('，'.join(cells))

    text = '\n\n'.join(out)
    if not text.strip():
        raise EbookError('empty', f'《{src.name}》里没提取到文字',
                         '文件可能是空的，或内容全在图片里')
    return text, 'python-docx'
