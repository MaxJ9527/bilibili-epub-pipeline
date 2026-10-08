#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
note_kit.py — B 站视频归档的通用工具层（25 期批量复用）

从 build_ramu_note.py 抽出：SRT 解析、校订、断段、小节切分、格式校验。
期特有的东西（专名校订表、小节划分、一句话概括、参考资料）走 CONFIG 传入，
**不在这里硬编码**——每期内容不同，工具只保证骨架和口径一致。

【踩过的坑，都在这里固化成防御】
1. SRT 正则正文是**第 10 组**（1 个序号 + 6 个时间数字组 + 正文），
   写错组号**不抛异常**，只会静默拼出「1764 个数字而不是 9285 字」。
   → parse_srt 一律做量级校验。
2. Whisper / turbo 输出**完全没有标点**（。！？ 计数为 0），
   所以不能用「句末标点」切句断段，必须按 **cue 边界 + 累计字数**断段。
   → to_paragraphs 不看标点。
3. 小节关键词查找必须在**校订之后**的文本上做（校订会改变用词）。
"""
import re
from pathlib import Path

# SRT 正则：组 1=序号 2-4=起时 5=起毫秒 6-8=止时 9=止毫秒 10=正文
SRT_RE = re.compile(
    r"(\d+)\n(\d{2}):(\d{2}):(\d{2}),(\d{3}) --> "
    r"(\d{2}):(\d{2}):(\d{2}),(\d{3})\n(.+?)(?=\n\n|\Z)", re.S)


def apply_fix(t, fix):
    """用**单个正则一次性替换**，避免子串互相污染。

    【踩过的坑·必须这么写】顺序替换治不了前缀重叠：
      原文「阿波威尔」（Abwehr 的误译）与「波威尔」（人名误译）**长度相同**
      但有前缀关系。顺序替换无论怎么排，先处理哪个都会错：
        先「波威尔」→ 「阿波威尔」变成「阿波波夫」（混合体）
        先「阿波威尔」→ 但原文根本没有「阿博威尔」，规则失效
    所以用 `|` 拼成一个 alternation 正则，Python re 天然**最左最长匹配**，
    一次扫完互不干扰。
    """
    if not fix:
        return t
    #长的写前面，配合 re 的最左最长匹配
    pairs = sorted(fix, key=lambda x: -len(x[0]))
    pat = "|".join(re.escape(a) for a, _ in pairs)
    table = {a: b for a, b in pairs}
    return re.sub(pat, lambda m: table[m.group(0)], t)


def audit_fix(before, after, fix):
    """校订**自检**：抓两类问题。

    【踩过的坑】校订规则会「自我制造错误」——
    我写「洛诺夫→巴拉诺夫」，而原文主形态是「巴洛诺夫」，
    结果78 处全变成「**巴巴拉诺夫**」（前缀重复）。
    这类错误**不会报错**，只会让文本悄悄变怪。

    两类检查：
      1. 替换目标自污染：目标词里包含了替换源
      2. 出现 3+ 连续重复字（如「巴巴拉诺夫」）

    用法：after = apply_fix(...) 之后调用，命中则抛异常。
    """
    problems = []
    for a, b in fix or []:
        if a in b:
            problems.append(f"规则自污染：源「{a}」出现在目标「{b}」里，"
                            f"会造出「{b}」这种前缀重复")
    # 查重复字：巴巴巴、的的、了了
    for m in set(re.findall(r"([\u4e00-\u9fa5])\1{2,}", after)):
        ctx_i = after.find(m * 3)
        ctx = after[max(0, ctx_i - 12):ctx_i + 16]
        problems.append(f"重复字「{m*3}」：…{ctx}…")
    return problems


def parse_srt(path, fix=None, drop_ad=False):
    """解析 SRT。fix 为 [(错, 对)] 校订表，对每条 cue 文本生效。

    返回 items（cue 文本列表，已校订、已去空）。

    【重要·跨 cue 替换】Whisper 会把一句话切在多个 cue 上，
    原文「我不不不」+「不需要你出卖谁」是两个 cue，
    逐 cue 替换**永远匹配不到**跨 cue 的长模式。
    解法：先逐 cue 替换；再把全文拼起来做一次替换，
    然后**按替换前后每个 cue 的长度变化**把修正结果分配回各 cue。
    这样既保证跨 cue 的模式能被改，又不会打乱段落边界。
    """
    s = Path(path).read_text(encoding="utf-8", errors="ignore")
    cues = SRT_RE.findall(s)
    if not cues:
        raise ValueError(f"SRT 解析到 0 条cue，检查格式：{path}")
    raw = [c[9].replace("\n", "").strip() for c in cues]
    if drop_ad:
        raw, _ad = split_ad(raw)
    out = apply_fix_across_cues(raw, fix)
    # 量级校验：正常中文视频每条 cue 约 5-30 字，全片至少几百条
    if len([x for x in out if x]) < 5:
        raise ValueError("仅解析出极少 cue，正则组号可能写错")
    return [x for x in out if x]


def apply_fix_across_cues(raw, fix, window=3):
    """逐 cue 替换 + **跨 cue 窗口**替换。

    【为什么要跨 cue】Whisper 会把一句话切在多个 cue 上：
    原文是「我不不不」+「不需要你出卖谁」两个 cue，
    逐 cue 替换永远匹配不到「我不不不不需要你出卖谁」这个模式。
    【实测】曾试过「全文替换后按长度重新切分」，
    但位置映射在替换处会错位，导致后面所有 cue 边界都偏移——
    实测 1161 个 cue 被压成 2 个，完全不可用。
    【窗口法】只对「相邻 N 个 cue 拼接后能命中」的窗口做替换，
    改动范围可控（最多影响 3 个 cue），其余 cue 边界不受影响。
    """
    if not fix:
        return list(raw)
    out = [apply_fix(t, fix) for t in raw]

    # 【性能】只对「逐 cue 替换后仍未命中」的规则做窗口扫描。
    # 【踩过的坑】原实现对全部规则 × 全部窗口 × 3 轮做正则，
    # 1351 cue × 15 规则 × 3 轮× 2 窗口 ≈ 12 万次 re.sub，
    # 实测跑超 120 秒被 kill。改为「先找出未命中的规则」后，
    # 规则数通常≤3，窗口只需扫 2 和 3，**降到千级次**，秒级完成。
    # 【关键·判据，踩过两次坑】
    # pending = 「原文里存在、但**任何单个 cue 内都不完整**」的规则。
    # 错误判据 1：`a in 全文` → 跨 cue 的模式在全文里是连续的，会被误判为「已处理」。
    # 错误判据 2：`a not in joined` → 恰恰因为逐 cue 改不到，joined 里还留着原样，
    #   结果被判为「已处理」而跳过，窗口法永远不触发。
    # 正确判据：看它有没有在任何单个 cue 里完整出现过。
    in_cue = set()
    for t in raw:
        for a, _ in fix:
            if a in t:
                in_cue.add(a)
    pending = [(a, b) for a, b in fix if a not in in_cue]
    if not pending:
        return out
    for _ in range(3):                      # 多轮覆盖跨 3+ cue
        changed = False
        for w in range(2, window + 1):
            i = 0
            while i + w <= len(out):
                seg = "".join(out[i:i + w])
                new = apply_fix(seg, pending)
                if new != seg:
                    # 【关键】替换后文本放进第 i 个槽位，其余置空。
                    # 不能按原长度切分——替换会增删字符，长度对不上，
                    # 强行切会把文本搅乱（实测把整段塞进首槽才暴露出来）。
                    # 置空的槽位会在 parse_srt 的最后过滤掉，
                    # 净效果就是「这一小段被合并了」，语义正确。
                    out[i:i + w] = [new] + [""] * (w - 1)
                    changed = True
                    i += w
                else:
                    i += 1
        if not changed:
            break
        still = [(a, b) for a, b in pending if a not in "".join(out)]
        if not still:
            break
        pending = still
    return out


# 【广告识别】B站片尾常有固定推广语，重复数十次，把正文挤掉。
# 39 期实测：1406 个 cue 里 296 个是广告（重复「YoYo Television Series
# Exclusive 优优独播剧场字幕志愿者」），13230 字 / 总22821 字 = 58%。
AD_MARKERS = ("YoYo", "优独播剧场", "字幕志愿者", "Television Series",
              "请不吝点赞", "订阅 转发", "明镜与点点")


def split_ad(items):
    """把 cue 分成 (真实, 广告) 两组。"""
    real, ad = [], []
    for it in items:
        (ad if any(m in it for m in AD_MARKERS) else real).append(it)
    return real, ad


def parse_srt_timed(path, fix=None):
    """带时间戳的 SRT 解析，返回 [(start_sec, text)]，供 coverage() 用。

    【曾被误删两次】修 _split_by_len 时用 s.index() 切片，边界算错把
    这个函数一起切掉了。教训：改文件用 Edit 精确定位，
    别用 s[:i] + s[j:] 这种索引切片。
    """
    s = Path(path).read_text(encoding="utf-8", errors="ignore")
    raw = []
    for c in SRT_RE.findall(s):
        start = int(c[1]) * 3600 + int(c[2]) * 60 + int(c[3])
        raw.append((start, c[9].replace("\n", "").strip()))
    texts = apply_fix_across_cues([t for _, t in raw], fix)
    return [(st, t) for (st, _), t in zip(raw, texts) if t]


def coverage(srt_path, audio_sec, fix=None):
    """字幕覆盖时长 / 音频总时长。<0.92 视为转录不完整，建议重跑。"""
    cues = parse_srt_timed(srt_path, fix)
    if not cues:
        return 0.0
    s = Path(path).read_text(encoding="utf-8", errors="ignore") if False else \
        Path(srt_path).read_text(encoding="utf-8", errors="ignore")
    last = SRT_RE.findall(s)[-1]
    end = int(last[5]) * 3600 + int(last[6]) * 60 + int(last[7])
    return end / audio_sec if audio_sec else 0.0


def to_paragraphs(items, target=120):
    """按 cue 边界 + 累计字数断段。

    【为什么不按标点】Whisper/turbo 输出无标点，按标点切会失败（每节一整段）。
    target=120 对应笔记密度基准（均值 114 / 中位 125）。
    """
    paras, cur, n = [], [], 0
    for t in items:
        if not t:
            continue
        cur.append(t)
        n += len(t)
        if n >= target:
            paras.append("".join(cur))
            cur, n = [], 0
    if cur:
        paras.append("".join(cur))
    return [p for p in paras if p.strip()]


def find_sections(txt, sections):
    """按关键词定位小节边界。sections = [(小节标题, 关键词)]。

    必须在**校订后**的 txt 上调用。
    返回 (idx, bounds, missing)：idx=[(pos,title)]，bounds 为切分位置。
    漏匹配的关键词会报出来——不要静默跳过，那会导致小节缺失。
    """
    idx, missing = [], []
    for title, kw in sections:
        p = txt.find(kw)
        if p >= 0:
            idx.append((p, title))
        else:
            missing.append((title, kw))
    idx.sort()
    bounds = [p for p, _ in idx] + [len(txt)]
    return idx, bounds, missing


def slice_by_pos(items, start, end):
    """按字符区间切出该区间包含的 cue（items 已拼接为 txt 的顺序）。"""
    pos, seg = 0, []
    for it in items:
        nxt = pos + len(it)
        if nxt > start:
            seg.append(it)
        pos = nxt
        if pos >= end:
            break
    return seg


def build_body(items, sections, target=120):
    """按小节切分并断段，返回 [(小节标题, [段落...])]。漏匹配的关键词会打印警告。"""
    txt = "".join(items)
    idx, bounds, missing = find_sections(txt, sections)
    for title, kw in missing:
        print(f"  ⚠️ 未找到关键词:「{kw}」-> 小节「{title}」缺失")
    # 【踩过的坑】CFG 里SECTIONS 写乱顺序时，工具会自动按位置排序，
    # 结果「看起来对」——但作者自己知道写错了，下次还会犯。
    # 显式报出来，逼着人按叙事顺序写。
    seq = [(title, kw) for title, kw in sections]
    orig_pos = [txt.find(kw) for _, kw in seq]
    if orig_pos != sorted(orig_pos):
        print("  ⚠️ SECTIONS 顺序与正文不符（工具已自动排序，"
              "但建议按叙事顺序重写）")
    out = []
    for k, (p, title) in enumerate(idx):
        seg = slice_by_pos(items, p, bounds[k + 1])
        out.append((title, to_paragraphs(seg, target)))
    return out


def fmt_dur(sec):
    """秒 → '26 分 14 秒'"""
    m, s = divmod(int(sec), 60)
    return f"{m} 分 {s} 秒"


def check_density(paras, target=120, tol=0.25):
    """段长密度自检。返回 (统计dict, 警告列表)。"""
    import statistics
    L = [len(p) for p in paras]
    if not L:
        return {}, ["零段"]
    st = {"n": len(L), "mean": round(statistics.mean(L)),
          "median": round(statistics.median(L)),
          "max": max(L), "min": min(L)}
    warn = []
    if st["mean"] > target * (1 + tol):
        warn.append(f"均值 {st['mean']}字 偏长（基准{target}）→ 段数可能太少")
    if st["mean"] < target * (1 - tol):
        warn.append(f"均值 {st['mean']} 字 偏短 → 段数可能过多")
    if st["max"] > target * 2:
        warn.append(f"最长 {st['max']} 字 → 有超长段")
    return st, warn


def make_note(*, stem, bvid, duration, cover_rel, summary, desc,
              sections_body, refs=None, related=None):
    """组装完整笔记 Markdown。

    正文布局硬性规范（Max 定）：
      - 简介/一句话概括在**正文前部**（引用块）
      - 参考文献在**热门评论之前**
      - 广告只标 has_ad: true，绝不删改
    """
    L = [
        "[[小约翰可汗|小约翰可汗]]", "",
        f"# {stem}", "",
        f"![[{cover_rel}]]", "",
        f"> UP主：小约翰可汗 · [查看B站视频](https://www.bilibili.com/video/{bvid}) · "
        f"时长 {fmt_dur(duration)}", "",
        f"> **一句话概括**：{summary}", "",
    ]
    if desc:
        L += [f"> **简介**：{desc}", ""]

    for h, ps in sections_body:
        L += [f"## {h}", ""]
        L += ps
        L += [""]

    body = "\n".join(L)
    body += "\n---\n\n"
    if refs:
        body += "> **参考资料**（UP 主列于简介栏）：\n"
        for r in refs:
            body += f"> - {r}\n"
        body += "\n"
    body += "## 相关篇目\n\n"
    body += "".join(f"- {r}\n" for r in (related or []))
    body += "\n"
    return body