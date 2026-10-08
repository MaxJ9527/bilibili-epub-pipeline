"""按语义重新切分段落。

**问题**：段落边界沿用 ASR 的 cue 边界——按**时间轴**切的，不是按语义。
于是出现「…钻进了自己的汽车。吩咐司机赶紧出发…」（两句挤一段）
和段末断在词中间（「…就在洛马金闭目养」＋ 下一段接「神时」）。

**修法**（Max 2026-10-07 定）：把正文拼成一条流，按 target 在**句末标点**处切。

**关键设计：图片说明块是隔板**。
B 档笔记正文里散布着 `> 图片说明` + `> <small>图片来源…</small>` 的配对块，
它们**必须原样保留、位置不动**（Max 口径：图片只打标记不改）。
所以把它们当分隔符：正文流在隔板处断开，各自独立重排，块本身原位插入。

**为什么不按小节分别处理**：小节多的篇目（神经病枪手 9 节、小塔乔 11 节）
每节只有 5–10 段，段内凑不出 0.70*target 的断点 → 段末仍断词（实测 96/100 处）。
所以「连续正文区」= 两个隔板之间的全部正文（可跨小节）。

用法：
  python reflow.py --file "篇目标题" [--target 118] [--apply]
"""
import argparse
import io
import os
import sys

def split_fm(t):
    """拆 frontmatter，返回 (含分隔符的 fm, body)。"""
    lines = t.split('\n')
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == '---'), None)
    if end is None:
        return '', t
    return '\n'.join(lines[:end + 1]) + '\n', '\n'.join(lines[end + 1:])


STOP = ('热门评论', '相关篇目', '视频中出现的音乐', '字幕误识别',
        '归档说明', '参考资料', '图片说明')

VAULT = os.environ.get("BILI_VAULT", "")  # 指向你的 Obsidian 笔记目录
SOFT = '，、；：'
HARD = '。！？…'


def is_para(l):
    s = l.strip()
    return bool(s) and not s.startswith(('#', '!', '>', '|', '-', '<!--', '*')) and len(s) > 20


def is_block(l):
    """图片说明块 / 空行等不可动内容。"""
    s = l.strip()
    return bool(s) and (s.startswith('>') or s.startswith('|') or s.startswith('<!--'))


def split_points(text):
    pts = []
    for i, c in enumerate(text):
        if c in HARD:
            pts.append((i + 1, 'hard'))
        elif c in SOFT:
            pts.append((i + 1, 'soft'))
    out = []
    for pos, kind in pts:
        j = pos
        while j < len(text) and text[j] in '」』"\')）】》':
            j += 1
        out.append((j if j > pos else pos, kind))
    return out


def reflow_stream(text, target=118, sent=None, floor=0.70):
    if not text:
        return []
    pts = split_points(text)
    if sent:
        # 落在哨兵区间内、或紧贴哨兵的切点一律丢弃
        pts = [p for p in pts if not inside_sent(text, p[0], sent)]
    if not pts:
        return [text]
    # 切点选择：**先攒够 floor，再继续往后找最贴近 target 的那一个**。
    #
    # 踩坑（第 5 次）：早先写成「取第一个 >= floor*target 的句末点就切」，
    # floor=0.70 时段均被压到 94（目标 118）——因为每个段都在刚够 83 字时就断了，
    # 正文被切得比 cue 原文还碎（81 段 → 109 段）。
    # 正确做法是向前多看几步：越过 target 之前一直推进 best，
    # 越过之后回退到最后一个未越界的切点。这样段均才贴得住 target。
    paras, start, i, n = [], 0, 0, len(pts)
    while i < n:
        pos, kind = pts[i]
        if kind != 'hard' or pos - start < target * floor:
            i += 1
            continue
        # best = 该区间里**离 target 最近**的句末点（可略超，但不超过 1.25 倍）。
        # 只许回退不许越界的话，段长被压在「最后一个未越界的点」上，
        # 均长只有 105——句末点间隔约 25 字时，回退点常落在 100 附近。
        best = i
        j = i + 1
        while j < n:
            p2, k2 = pts[j]
            if k2 != 'hard':
                j += 1
                continue
            if abs((p2 - start) - target) < abs((pts[best][0] - start) - target):
                best = j
            if p2 - start > target * 1.25:
                break
            j += 1
        # 加粗闭合修正：【实测 short10 两篇踩坑】切点若落在 `**加粗**` 的
        # 闭合标记之前，`**` 会被切到下一段行首——该行以 `**` 开头后被
        # is_para 判成格式行，从此所有统计口径都看不见它（看起来就像丢了
        # 262 字），Markdown 渲染也会错位。修法：piece 内 `**` 计数为奇数
        # （有开无闭）时，把切点后紧随的 `**` 吞进来闭合。
        p = pts[best][0]
        if text[start:p].count('**') % 2 == 1:
            while p < len(text) and text[p] == '*':
                p += 1
        paras.append(text[start:p].strip())
        start = p
        i = best + 1
    if start < len(text):
        rest = text[start:].strip()
        if rest:
            paras.append(rest)
    if len(paras) >= 2 and len(paras[-1]) < target * 0.45:
        # 注意：必须先pop 再 += ——写成 paras[-1] += paras.pop() 时，
        # pop() 已改变列表，paras[-1] 指向新的末项，导致丢字（实测丢 88~135 字）
        tail = paras.pop()
        paras[-1] = paras[-1] + tail
    return [p for p in paras if p]


def inside_sent(text, pos, sent):
    """pos 是否落在哨兵区间内或紧贴哨兵。"""
    a = text.rfind(sent, 0, pos)
    b = text.find(sent, 0, pos)
    if a != -1 and (b == -1 or a > b):
        return True
    return text[max(0, pos - len(sent)):pos] == sent or text[pos:pos + len(sent)] == sent


def acc_of(mid, upto):
    """mid[0:upto] 中正文段的累计字符数。"""
    return sum(len(x.strip()) for x in mid[:upto] if is_para(x))


def process(title, target, apply_=False):
    fns = [f for f in os.listdir(VAULT) if title in f and f.endswith('.md')]
    if len(fns) != 1:
        raise SystemExit(f'匹配 {len(fns)} 篇：{fns}')
    fn = fns[0]
    path = os.path.join(VAULT, fn)
    t = io.open(path, encoding='utf-8').read()
    fm, body = split_fm(t)
    ls = body.split('\n')

    start_i = next((i for i, l in enumerate(ls) if l.strip().startswith('## ')), None)
    if start_i is None:
        raise SystemExit('未找到正文起点（## 小节标题）')
    stop_i = len(ls)
    for i, l in enumerate(ls):
        s = l.strip()
        if s.startswith('## ') and s[3:].strip().startswith(STOP):
            stop_i = i
            break

    pre = ls[:start_i + 1]
    mid = ls[start_i + 1:stop_i]
    post = ls[stop_i:]

    # 把mid 切成：连续正文区（可重排） 与 隔板块（原样保留）
    # 极简稳健实现（踩坑 4 次后的定型版）：
    # ① 先把 mid 拆成两类行——「正文段」参与重排；其余（标题/图片说明/空行）**原位保留**
    # ② 正文全部拼成一条流，按 target 在句末标点处切
    # ③ 按**段落序号比例**把非正文行插回：新段 i 对应原位置 = round(i /新段数 * 原正文段数)
    #    —— 用序号比例而非字符累计，避免 reflow_stream 内部跳切导致锚点漂移
    #    （早期用字符累计定位，7 篇各丢 88–135 字，字符多重集比对查出）
    others= []      # (原正文段序号位置, 行)
    paras_src = []  # 原正文段
    npara = 0
    for l in mid:
        if is_para(l):
            paras_src.append(l.strip())
            npara += 1
        else:
            others.append((npara, l))
    pieces = reflow_stream(''.join(paras_src), target)
    N = len(pieces)
    # 每个 others 行应插在哪个新段之前
    slots = [[] for _ in range(N + 1)]
    for npara_before, l in others:
        if N == 0:
            idx = 0
        else:
            idx = round(npara_before / len(paras_src) * N) if paras_src else 0
        idx = max(0, min(N, idx))
        slots[idx].append(l)
    new_mid = []
    for i in range(N):
        new_mid.extend(slots[i])
        new_mid.append(pieces[i])
    new_mid.extend(slots[N])

    res = fm + '\n'.join(pre + new_mid + post)
    n_before = len([l for l in mid if is_para(l)])
    lens = [len(x.strip()) for x in new_mid if is_para(x)]
    print(f'▶ {fn[:44]}')
    print(f'  正文段 {n_before} → {len(lens)} | 段长 均{sum(lens)//len(lens)} '
          f'短{min(lens)} 长{max(lens)}')
    if not apply_:
        print('  （dry）')
        return None
    with io.open(path, 'w', encoding='utf-8') as f:
        f.write(res)
    print(f'  ✓ 已写入 {fn}')
    return res


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--file', required=True)
    ap.add_argument('--target', type=int, default=118)
    ap.add_argument('--apply', action='store_true')
    a = ap.parse_args()
    process(a.file, a.target, a.apply)


if __name__ == '__main__':
    main()