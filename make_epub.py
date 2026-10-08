#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 Obsidian 库里的小约翰可汗笔记压成两本 EPUB3（全量内容：带图片/逐字稿/热评）。

书一：硬核狠人(按编号升序) + 单集(按发布时间升序)
书二：奇葩小国 + 神奇组织（均按编号升序）
图片：嵌入前用 PIL 限宽重编码；PNG 含透明通道则保留 PNG。

用法：
  python3 make_epub.py --vault "/path/to/知识库/bilibili/小约翰可汗" \
      --assets "/path/to/知识库" --out-dir ~/Desktop

笔记约定（自建归档格式，见 README）：
  - frontmatter 含 title / created / cover(可选)
  - 系列期号写在标题后缀，如「xxx？【硬核狠人23】【小约翰】」
  - 无系列后缀的算「单集」
"""
import argparse, os, re, uuid, zipfile, html, io
from datetime import datetime, timezone, timedelta
from PIL import Image, ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True  # 容忍尾部截断的 JPEG

IMG_MAXW = 1024   # 由 --maxw 覆盖
IMG_Q = 72        # 由 --quality 覆盖
VAULT = ""
ASSETS = ""       # 库根目录（素材库/ 所在处），默认取 vault 的上上级

SKIP_LAZY = ("#", ">", "- ", "|", "![[", "[[", "---", "```")

def esc(s):
    return html.escape(s, quote=False)

def md_inline(s):
    s = wiki_to_text(s)  # 统一在此转换（段落/引用/列表/表格单元全覆盖）
    # <small> 白名单（图注来源行）
    s = s.replace("<small>", "\x01").replace("</small>", "\x02")
    s = esc(s)
    # 保险：奇数个 ** 时把最后一个降级为占位符，避免错位配对
    if s.count("**") % 2 == 1:
        i = s.rfind("**")
        s = s[:i] + "\x00" + s[i + 2:]
    s = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = s.replace("\x00", "**")  # 占位符还原为字面 **
    # [text](url) 链接
    s = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r'<a href="\2">\1</a>', s)
    # 剩余裸链接
    s = re.sub(r"(?<![\"'>=])(https?://[^\s<]+)", r'<a href="\1">\1</a>', s)
    s = s.replace("\x01", "<small>").replace("\x02", "</small>")
    return s

def wiki_to_text(s):
    def rep(m):
        inner = m.group(1)
        return inner.split("|")[-1].strip()
    return re.sub(r"\[\[([^\]]+)\]\]", rep, s)

def strip_suffix(title):
    return re.sub(r"(【[^】]*】)+\s*$", "", title).strip()

def resolve_img(p):
    p = p.strip()
    for base in (ASSETS, VAULT):
        full = os.path.join(base, p)
        if os.path.exists(full):
            return full
    hit = os.path.join(ASSETS, "素材库", os.path.basename(p))
    if os.path.exists(hit):
        return hit
    return None

FM_RE = re.compile(r'^([A-Za-z_]+):\s*"?(.*?)"?\s*$', re.M)

def scan_vault(vault):
    """扫描笔记目录 → [{f, series, ep, created, cover}]。
    系列与期号从标题后缀解析：xxx？【硬核狠人23】【小约翰】 → 硬核狠人 / 23"""
    CST = timezone(timedelta(hours=8))
    notes = []
    for f in sorted(os.listdir(vault)):
        if not f.endswith(".md"):
            continue
        raw = open(os.path.join(vault, f), encoding="utf-8").read()
        fm = {}
        if raw.startswith("---"):
            end = raw.find("\n---", 3)
            if end > 0:
                for k, v in FM_RE.findall(raw[3:end]):
                    fm[k.strip().lower()] = v.strip()
        # 跳过索引页 / hub 页（frontmatter 带「索引」标签，或标题含「索引」）
        fm_all = raw[3:end] if raw.startswith("---") and end > 0 else ""
        h1m = re.search(r"^# (.+)$", raw, re.M)
        h1 = h1m.group(1).strip() if h1m else os.path.splitext(f)[0]
        if "索引" in fm_all or "索引" in h1:
            continue
        h1m = re.search(r"^# (.+)$", raw, re.M)
        h1 = h1m.group(1).strip() if h1m else os.path.splitext(f)[0]
        series, ep = "", None
        for c in reversed(re.findall(r"【([^】]+)】", h1 or f)):
            m = re.fullmatch(r"(.+?)(\d+)", c.strip())
            if m and m.group(1) not in ("小约翰", "小约翰可汗"):
                series, ep = m.group(1), int(m.group(2))
                break
        created = None
        for k in ("created", "date", "updated"):
            if fm.get(k):
                try:
                    created = datetime.fromisoformat(fm[k]).isoformat()
                    break
                except ValueError:
                    pass
        notes.append({"f": f, "title": fm.get("title", h1), "h1": h1,
                      "series": series, "ep": ep, "created": created,
                      "cover": fm.get("cover")})
    return notes

def parse_md(path):
    """返回 (h1_title, cover_path_or_None, body_blocks)
    body blocks: (kind, payload)，kind ∈ h2/p/quote/ul/table/img"""
    raw = open(path, encoding="utf-8").read()
    lines = raw.split("\n")
    cover = None
    # 剥 frontmatter（同时取 cover 字段）
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                fm = "\n".join(lines[1:i])
                m = re.search(r'^cover:\s*"([^"]+)"', fm, re.M)
                if m:
                    cover = m.group(1).strip()
                lines = lines[i + 1:]
                break
    h1 = ""
    out = []
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip()
        s = ln.strip()
        if s.startswith("# ") and not h1:
            h1 = s[2:].strip()
            i += 1
            continue
        if s.startswith("## "):
            out.append(("h2", s[3:].strip()))
            i += 1
            continue
        m3 = re.match(r"^(#{3,6})\s+(.+)$", s)
        if m3:                             # 三级以下标题（老笔记有 ###）
            out.append(("h3", m3.group(2).strip()))
            i += 1
            continue
        if not s:
            i += 1
            continue
        m = re.fullmatch(r"!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]", s)
        if m:                              # 图片嵌入
            out.append(("img", m.group(1).strip()))
            i += 1
            continue
        if re.fullmatch(r"\[\[[^\]]+\]\]", s):  # 独行面包屑 wiki 链接：删
            i += 1
            continue
        if s.startswith("| "):             # 表格
            rows = []
            while i < len(lines) and lines[i].strip().startswith("|"):
                r = lines[i].strip()
                if not re.fullmatch(r"\|[\s:|-]+\|", r):
                    cells = [c.strip() for c in r.strip("|").split("|")]
                    rows.append(cells)
                i += 1
            out.append(("table", rows))
            continue
        if s.startswith("- "):             # 列表
            items = []
            while i < len(lines) and lines[i].strip().startswith("- "):
                items.append(lines[i].strip()[2:])
                i += 1
            out.append(("ul", items))
            continue
        if s.startswith(">"):              # 引用块
            qs = []
            while i < len(lines):
                q = lines[i].strip()
                if q.startswith(">"):
                    qs.append(wiki_to_text(q[1:].strip()))
                elif qs and q and not q.startswith(SKIP_LAZY) \
                        and (qs[-1].count("**") % 2 == 1 or qs[-1][-1:] in "，、：（"):
                    # 惰性续行：引文断在句中，直接拼进上一行（**跨行可配对）
                    if qs[-1][-1:].isascii() and qs[-1][-1:].isalnum() and q[:1].isascii() and q[:1].isalnum():
                        qs[-1] += " " + wiki_to_text(q)
                    else:
                        qs[-1] += wiki_to_text(q)
                else:
                    break
                i += 1
            out.append(("quote", qs))
            continue
        out.append(("p", wiki_to_text(s)))
        i += 1
    # 过滤「跨库延伸」：标记行 + 紧随的列表（站内导航性质，书里不要）
    cleaned = []
    skip_ku = False
    for k, p in out:
        if k == "p" and p.strip() == "**跨库延伸**":
            skip_ku = True
            continue
        if skip_ku and k == "ul":
            continue
        skip_ku = False
        cleaned.append((k, p))
    out = cleaned
    # frontmatter 封面若正文没有嵌入，补到章首
    if cover and cover not in [p for k, p in out if k == "img"]:
        out.insert(0, ("img", cover))
    return h1, cover, out

class ImageStore:
    """每本书一个：源路径 -> OEBPS/images/imgNNNN.{jpg,png}"""
    def __init__(self):
        self.map = {}       # src -> (filename, media, bytes)
        self.n = 0
        self.fail = []
    def get(self, src):
        if src in self.map:
            return self.map[src]
        full = resolve_img(src)
        if not full:
            self.fail.append(src)
            return None
        try:
            im = Image.open(full)
            im.load()
            alpha = im.mode in ("RGBA", "LA") or (
                im.mode == "P" and "transparency" in im.info)
            if alpha:
                im = im.convert("RGBA")
                if im.width > IMG_MAXW:
                    im = im.resize((IMG_MAXW, int(im.height * IMG_MAXW / im.width)), Image.LANCZOS)
                fmt, ext, media = "PNG", "png", "image/png"
                buf = io.BytesIO()
                im.save(buf, fmt, optimize=True)
            else:
                im = im.convert("RGB")
                if im.width > IMG_MAXW:
                    im = im.resize((IMG_MAXW, int(im.height * IMG_MAXW / im.width)), Image.LANCZOS)
                fmt, ext, media = "JPEG", "jpg", "image/jpeg"
                buf = io.BytesIO()
                im.save(buf, fmt, quality=IMG_Q, optimize=True, progressive=True)
        except Exception as e:
            self.fail.append(f"{src} ({e})")
            return None
        self.n += 1
        fn = f"img{self.n:04d}.{ext}"
        self.map[src] = (fn, media, buf.getvalue())
        return self.map[src]

def block_html(kind, payload, istore):
    if kind == "h2":
        return f"<h2>{esc(payload)}</h2>"
    if kind == "h3":
        return f"<h3>{esc(payload)}</h3>"
    if kind == "p":
        return f"<p>{md_inline(payload)}</p>"
    if kind == "quote":
        body = ""
        for q in payload:
            if not q:
                continue
            if q.startswith("- "):  # 引用块内的列表项（参考资料节常见写法）
                body += f"<p>· {md_inline(q[2:])}</p>"
            else:
                body += f"<p>{md_inline(q)}</p>"
        return f"<blockquote>{body}</blockquote>"
    if kind == "ul":
        lis = "".join(f"<li>{md_inline(x)}</li>" for x in payload)
        return f"<ul>{lis}</ul>"
    if kind == "img":
        got = istore.get(payload)
        if not got:
            return ""
        fn = got[0]
        return f'<div class="img"><img src="../images/{fn}" alt=""/></div>'
    if kind == "table":
        rows = payload
        if not rows:
            return ""
        thead = ""
        body = ""
        for idx, r in enumerate(rows):
            tds = "".join(f"<td>{md_inline(c)}</td>" for c in r)
            if idx == 0 and len(rows) > 1:
                thead = f"<thead><tr>{tds}</tr></thead>"
            else:
                body += f"<tr>{tds}</tr>"
        return f"<table>{thead}<tbody>{body}</tbody></table>"
    return ""

XHTML_TPL = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head><meta charset="utf-8"/><title>{title}</title><link rel="stylesheet" type="text/css" href="../style.css"/></head>
<body>
<h1>{title}</h1>
{body}
</body>
</html>
"""

def title_page_xhtml(book_title, subtitle, groups):
    parts = []
    for name, items in groups:
        parts.append(f"<h2>{esc(name)}（{len(items)} 篇）</h2>")
    body = "\n".join(parts)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head><meta charset="utf-8"/><title>{esc(book_title)}</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body class="titlepage">
<h1>{esc(book_title)}</h1>
<p class="sub">{esc(subtitle)}</p>
{body}
<p class="sub">UP主：小约翰可汗 · 依据 Obsidian 归档笔记编制 · 2026-10</p>
</body>
</html>
"""

CSS = """body{font-family:"Songti SC","Noto Serif CJK SC",serif;line-height:1.7;margin:1em}
h1{font-size:1.5em;margin:0 0 .8em}h2{font-size:1.2em;margin:1.2em 0 .5em;border-bottom:1px solid #999;padding-bottom:.2em}
h3{font-size:1.05em;margin:1em 0 .4em;color:#333}
p{margin:.6em 0;text-indent:0}blockquote{margin:.8em 1em;padding:.4em .8em;border-left:3px solid #999;color:#444}
blockquote p{margin:.3em 0;font-size:.92em}
table{border-collapse:collapse;margin:.8em 0;font-size:.9em}td{border:1px solid #aaa;padding:.2em .5em}
ul{margin:.5em 1em}a{color:inherit}
.img{margin:.8em 0;text-align:center}.img img{max-width:100%;height:auto}
.titlepage{text-align:center;margin-top:20%}.titlepage h1{font-size:1.8em}.titlepage .sub{color:#666}
.coverpage{margin:0;padding:0}.coverbox{text-align:center;margin:0}.coverbox img{max-width:100%;height:auto}
"""

def cover_xhtml():
    return """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xml:lang="zh-CN" lang="zh-CN">
<head><meta charset="utf-8"/><title>封面</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body class="coverpage"><div class="coverbox"><img src="images/cover.jpg" alt="封面"/></div></body>
</html>
"""

def build_epub(out_path, book_title, subtitle, groups, cover_path=None):
    uid = str(uuid.uuid4())
    istore = ImageStore()
    if os.path.exists(out_path):
        os.remove(out_path)
    zf = zipfile.ZipFile(out_path, "w")
    zf.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
    zf.writestr("META-INF/container.xml", """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>""")
    zf.writestr("OEBPS/style.css", CSS)

    manifest, spine, nav_lis, ncx_pts = [], [], [], []
    if cover_path and os.path.exists(cover_path):
        zf.writestr("OEBPS/images/cover.jpg", open(cover_path, "rb").read())
        manifest.append('<item id="cover-image" href="images/cover.jpg" media-type="image/jpeg" properties="cover-image"/>')
        zf.writestr("OEBPS/cover.xhtml", cover_xhtml())
        manifest.append('<item id="coverpage" href="cover.xhtml" media-type="application/xhtml+xml"/>')
        spine.append('<itemref idref="coverpage"/>')
    play = 1
    zf.writestr("OEBPS/title.xhtml", title_page_xhtml(book_title, subtitle, groups))
    manifest.append('<item id="title" href="title.xhtml" media-type="application/xhtml+xml"/>')
    spine.append('<itemref idref="title"/>')
    nav_lis.append(f'<li><a href="title.xhtml">{esc(book_title)}</a></li>')
    ncx_pts.append((play, book_title, "title.xhtml")); play += 1

    n = 0
    for part_name, items in groups:
        nav_lis.append(f"<li><span>{esc(part_name)}</span><ol>")
        for label, disp_title, blocks in items:
            n += 1
            cid = f"c{n:04d}"
            href = f"text/{cid}.xhtml"
            body = "\n".join(block_html(k, p, istore) for k, p in blocks)
            xhtml = XHTML_TPL.format(title=esc(disp_title), body=body)
            zf.writestr(f"OEBPS/{href}", xhtml)
            manifest.append(f'<item id="{cid}" href="{href}" media-type="application/xhtml+xml"/>')
            spine.append(f'<itemref idref="{cid}"/>')
            nav_lis.append(f'<li><a href="{href}">{esc(label)}</a></li>')
            ncx_pts.append((play, label, href)); play += 1
        nav_lis.append("</ol></li>")

    # 图片入包
    for src, (fn, media, data) in istore.map.items():
        zf.writestr(f"OEBPS/images/{fn}", data)
        manifest.append(f'<item id="i{fn[3:7]}" href="images/{fn}" media-type="{media}"/>')

    nav = f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head><meta charset="utf-8"/><title>目录</title><link rel="stylesheet" type="text/css" href="style.css"/></head>
<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>
{chr(10).join(nav_lis)}
</ol></nav>
<nav epub:type="landmarks" hidden="hidden"><ol>
<li><a epub:type="toc" href="nav.xhtml">目录</a></li>
<li><a epub:type="bodymatter" href="text/c0001.xhtml">正文开始</a></li>
</ol></nav></body></html>"""
    zf.writestr("OEBPS/nav.xhtml", nav)
    manifest.append('<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>')
    # 目录页进正文流：扉页之后第二页，任何阅读器都能翻到实体目录
    spine.insert(1, '<itemref idref="nav"/>')

    ncx_items = "".join(
        f'<navPoint id="np{i}" playOrder="{i}"><navLabel><text>{esc(t)}</text></navLabel>'
        f'<content src="{h}"/></navPoint>' for i, t, h in ncx_pts)
    ncx = f"""<?xml version="1.0" encoding="utf-8"?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
<head><meta name="dtb:uid" content="urn:uuid:{uid}"/><meta name="dtb:depth" content="1"/>
<meta name="dtb:totalPageCount" content="0"/><meta name="dtb:maxPageNumber" content="0"/></head>
<docTitle><text>{esc(book_title)}</text></docTitle>
<navMap>{ncx_items}</navMap></ncx>"""
    zf.writestr("OEBPS/nav.ncx", ncx)
    manifest.append('<item id="ncx" href="nav.ncx" media-type="application/x-dtbncx+xml"/>')

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" xml:lang="zh-CN">
<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:identifier id="uid">urn:uuid:{uid}</dc:identifier>
<dc:title>{esc(book_title)}</dc:title>
<dc:creator>小约翰可汗</dc:creator>
<dc:language>zh-CN</dc:language>
<meta property="dcterms:modified">{now}</meta>
</metadata>
<manifest>{chr(10).join(manifest)}</manifest>
<spine toc="ncx">{chr(10).join(spine)}</spine>
<guide><reference type="toc" title="目录" href="nav.xhtml"/></guide>
</package>"""
    zf.writestr("OEBPS/content.opf", opf)
    zf.close()
    return n, istore

BOOK1_SERIES = ["硬核狠人"]
BOOK2_SERIES = ["奇葩小国", "神奇组织"]

def main():
    global VAULT, ASSETS, IMG_MAXW, IMG_Q
    ap = argparse.ArgumentParser(description="小约翰可汗笔记 → 双卷 EPUB")
    ap.add_argument("--vault", required=True, help="笔记目录（含 .md）")
    ap.add_argument("--assets", help="素材根目录（素材库/ 所在处），默认取 vault 上上级")
    ap.add_argument("--out-dir", default=".", help="EPUB 输出目录")
    ap.add_argument("--maxw", type=int, default=1024, help="图片限宽像素")
    ap.add_argument("--quality", type=int, default=72, help="JPEG 质量")
    ap.add_argument("--cover1", help="书一封面图路径")
    ap.add_argument("--cover2", help="书二封面图路径")
    args = ap.parse_args()
    VAULT = args.vault
    ASSETS = args.assets or os.path.dirname(os.path.dirname(VAULT))
    IMG_MAXW, IMG_Q = args.maxw, args.quality
    os.makedirs(args.out_dir, exist_ok=True)

    idx = scan_vault(VAULT)
    no_date = [x["f"] for x in idx if not x["series"] and not x["created"]]
    if no_date:
        print(f"⚠ {len(no_date)} 个单集无 created/updated 字段，将排在单集末尾：", no_date[:5])

    def sort_key(x):
        if x["series"]:
            assert x["ep"] is not None, f"系列笔记缺期号: {x['f']}"
            return (0, int(x["ep"]))
        return (1, x["created"] or "9999-99-99", x["f"])

    notes = sorted(idx, key=sort_key)

    def chapter(x):
        h1, cover, blocks = parse_md(os.path.join(VAULT, x["f"]))
        disp = strip_suffix(h1 or x["title"])
        if x["series"]:
            label = f"{x['series']}{x['ep']:02d} · {disp}"
        else:
            label = f"单集 · {disp}"
            if x["created"]:
                d = datetime.fromisoformat(x["created"]).strftime("%Y-%m-%d")
                label = f"单集 · {d} · {disp}"
        return (label, disp, blocks)

    def make_group(series_name):
        return (series_name, [chapter(x) for x in notes if x["series"] == series_name])

    g_singles = ("单集", [chapter(x) for x in notes if not x["series"]])
    groups1 = [make_group(s) for s in BOOK1_SERIES] + [g_singles]
    groups2 = [make_group(s) for s in BOOK2_SERIES]

    out1 = os.path.join(args.out_dir, "小约翰可汗·硬核狠人与单集.epub")
    out2 = os.path.join(args.out_dir, "小约翰可汗·奇葩小国与神奇组织.epub")
    n1, st1 = build_epub(out1, "小约翰可汗 · 硬核狠人与单集",
                         "硬核狠人系列（按编号）+ 单集（按发布时间）", groups1, args.cover1)
    n2, st2 = build_epub(out2, "小约翰可汗 · 奇葩小国与神奇组织",
                         "奇葩小国系列 + 神奇组织系列（均按编号）", groups2, args.cover2)
    print(f"书一 {out1}: {n1} 章, {st1.n} 图, 失败 {len(st1.fail)}")
    print(f"书二 {out2}: {n2} 章, {st2.n} 图, 失败 {len(st2.fail)}")
    for f in (st1.fail + st2.fail)[:10]:
        print("图失败:", f)

    # ---- 校验 ----
    for path, expect, groups in [(out1, sum(len(g[1]) for g in groups1), groups1),
                                 (out2, sum(len(g[1]) for g in groups2), groups2)]:
        zf = zipfile.ZipFile(path)
        names = zf.namelist()
        assert names[0] == "mimetype" and zf.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert len([x for x in names if x.startswith("OEBPS/text/")]) == expect, path
        import xml.etree.ElementTree as ET
        for nm in names:
            if nm.endswith((".xhtml", ".opf", ".ncx", ".xml")):
                ET.fromstring(zf.read(nm))
        g0, g1 = groups
        print(f"{os.path.basename(path)}: 首={g0[1][0][0]} 尾={g1[1][-1][0]} 章={expect}")

if __name__ == "__main__":
    main()
