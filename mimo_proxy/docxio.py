# -*- coding: utf-8 -*-
"""docx 读写（纯标准库，无需 python-docx）

两个方向：
  · generate(markdown) -> .docx 字节      把模型输出落成 Word 文档
  · parse(data)        -> 文本/段落        读 .docx 交给模型问答

docx 本质是 zip 包 + OOXML，标准库 zipfile / xml 足够处理，
Termux 上不用装任何依赖。
"""

import re
import zipfile
import io as _io

try:
    from xml.sax.saxutils import escape as _xml_escape
except Exception:      # pragma: no cover
    def _xml_escape(s):
        return (s.replace("&", "&amp;").replace("<", "&lt;")
                 .replace(">", "&gt;"))

# ---------------------------------------------------------------- 生成

_CT = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.main+xml"/>
</Types>'''

_RELS = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>'''

_HEAD = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
         '<w:document xmlns:w="http://schemas.openxmlformats.org/'
         'wordprocessingml/2006/main"><w:body>')
_TAIL = '<w:sectPr/></w:body></w:document>'


def _p(text, style=None, bold=False, size=None):
    """构造一个段落的 OOXML"""
    pr = ""
    if style:
        pr += f'<w:pStyle w:val="{style}"/>'
    rpr = ""
    if bold:
        rpr += "<w:b/>"
    if size:
        rpr += f'<w:sz w:val="{int(size * 2)}"/>'
    body = (f'<w:rPr>{rpr}</w:rPr>' if rpr else "")
    # 保留换行：xml:space="preserve" 必须在 w:t 上
    t = _xml_escape(text or "")
    return (f'<w:p><w:pPr>{pr}</w:pPr><w:r>{body}'
            f'<w:t xml:space="preserve">{t}</w:t></w:r></w:p>')


def _lvl(line):
    """返回 (标题级别 1-6, 去掉 # 后的文本)，非标题返回 (0, 原文)"""
    m = re.match(r"^(#{1,6})\s+(.*)$", line)
    if m:
        return len(m.group(1)), m.group(2)
    return 0, line


def markdown_to_body(md):
    """极简 Markdown -> OOXML 段落列表

    支持：# 标题 / - 或 * 列表 / > 引用 / 空行 / 普通段落
    不做表格与代码块解析（够用即可，复杂排版建议用真 docx 库）。
    """
    out = []
    in_list = False
    for raw in (md or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            if in_list:
                in_list = False
            continue
        lvl, text = _lvl(line)
        if lvl:
            out.append(_p(text, style=f"Heading{lvl}", bold=True))
            continue
        if re.match(r"^\s*[-*+]\s+", line):
            text = re.sub(r"^\s*[-*+]\s+", "", line)
            out.append(_p("• " + text))
            in_list = True
            continue
        if line.startswith(">"):
            out.append(_p(line.lstrip("> ").strip()))
            continue
        out.append(_p(line))
    return "".join(out)


def generate(md, title=None):
    """Markdown/纯文本 -> .docx 字节"""
    body = ""
    if title:
        body += _p(title, style="Title", bold=True, size=18)
    body += markdown_to_body(md)
    if not body:
        body = _p("")
    doc = _HEAD + body + _TAIL

    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", _CT)
        z.writestr("_rels/.rels", _RELS)
        z.writestr("word/document.xml", doc)
    return buf.getvalue()


# ---------------------------------------------------------------- 解析

_P_RE = re.compile(r"<w:p[ >].*?</w:p>|<w:p/>", re.S)
_T_RE = re.compile(r"<w:t[^>]*>(.*?)</w:t>", re.S)


def _unescape(s):
    return (s.replace("&lt;", "<").replace("&gt;", ">")
             .replace("&quot;", '"').replace("&apos;", "'")
             .replace("&amp;", "&"))


def parse(data):
    """.docx 字节 -> {"text", "paragraphs", "chars"}

    只取正文文本，忽略页眉页脚/批注/文本框（够模型问答用）。
    """
    try:
        z = zipfile.ZipFile(_io.BytesIO(data))
    except Exception as e:      # noqa: BLE001
        return {"error": f"不是有效的 docx（zip 打开失败）: {e}"}
    try:
        names = z.namelist()
        if "word/document.xml" not in names:
            return {"error": "不是 docx：缺少 word/document.xml"}
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    except Exception as e:      # noqa: BLE001
        return {"error": f"读取 document.xml 失败: {e}"}
    finally:
        try:
            z.close()
        except Exception:
            pass

    paras = []
    for pm in _P_RE.finditer(xml):
        seg = pm.group(0)
        parts = [_unescape(t) for t in _T_RE.findall(seg)]
        # 段落里的换行标记 <w:br/> 也算分隔
        text = "".join(parts)
        if "<w:br/>" in seg:
            text = text.replace("\n", " ") + ""
        paras.append(text)

    # 去掉尾部连续空段（docx 常带大量空段落）
    while paras and not paras[-1].strip():
        paras.pop()
    text = "\n".join(paras)
    return {"text": text, "paragraphs": paras, "chars": len(text)}


def sniff(data):
    """是不是 docx"""
    return data[:2] == b"PK" and b"word/document.xml" in data[:4096]
