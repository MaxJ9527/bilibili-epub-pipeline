"""批次终检：标点率 / 段均 / 段末断词 / 数字空格 / 控制符 / 叠标点 / 实义字符。

单篇版原来是 /tmp/chk.py（临时文件，每次重写，且踩过两次 STOP 边界的坑：
只找第一个 `##` → 把评论区观众口语当成正文统计，报出一堆假问题）。
这里固化成可复用的批量版。

用法：
  python3 verify_batch.py /tmp/batch1.txt
  python3 verify_batch.py /tmp/batch1.txt --dup-check   # 额外比对备份的实义字符
"""
import argparse
import collections
import io
import os
import re
import sys

from model_punc import core, is_para, STOP, count_dup  # 实义字符/段落判定/叠标点：唯一实现

VAULT = os.environ.get("BILI_VAULT", "")  # 指向你的 Obsidian 笔记目录
STOP = ('热门评论', '相关篇目', '视频中出现的音乐', '字幕误识别',
        '归档说明', '参考资料', '图片说明')
PUNC = set('，。、：；？！“”‘’（）「」『』…—')
CTRL_RE = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]')
NUMSP = re.compile(r'(?<=\d)\s+(?=\d)')
# 段末合法终止：句末标点 + 各类闭引号。
# `"` 和 `'` 是 **Whisper 稿的 ASCII 直引号**（B站无字幕、本地 Whisper 转写的
# 篇目引号全是直的）——short10 校验时 13 处「断词」全是它们，不是真断词。
TAIL_OK = re.compile(r'[。！？…：！”"」』\']$')


def is_para(l):
    s = l.strip()
    return bool(s) and not s.startswith(('#', '!', '>', '|', '-', '<!--', '*')) \
        and len(s) > 20


def body_lines(text):
    """正文区 = 首个 `##` 之后、STOP 章节之前的行。

    必须两个边界都算：只找第一个 `##` 会把 STOP 之后的评论区也算进正文，
    评论是观众口语（「你是。。。。。。大大怪将军」），叠标点全是假阳性。
    """
    ls = text.split('\n')
    si = next((i for i, l in enumerate(ls) if l.strip().startswith('## ')), 0)
    stop = len(ls)
    for i, l in enumerate(ls):
        s = l.strip()
        if s.startswith('## ') and s[3:].strip().startswith(STOP):
            stop = i
            break
    return [l.strip() for l in ls[si:stop] if is_para(l)]


def check(fn, backup=None):
    p = os.path.join(VAULT, fn)
    t = io.open(p, encoding='utf-8').read()
    ps = body_lines(t)
    body = ''.join(ps)
    n = len(body)
    pn = sum(1 for c in body if c in PUNC)
    lens = [len(x) for x in ps]
    brk = [x for x in ps if not TAIL_OK.search(re.sub(r'\*+\s*$', '', x))]
    ctrl = sum(len(CTRL_RE.findall(x)) for x in ps)
    # 叠标点/数字空格必须**按行**统计：把段落拼成一串会让上段末尾的「。」
    # 和下段开头的「，」拼成「。，」，凭空多出假阳性（实测 39 处全是这种）。
    dup = sum(count_dup(x) for x in ps)
    nsp = sum(len(NUMSP.findall(x)) for x in ps)
    ad = 'has_ad: true' in t
    ok_core = None
    if backup and os.path.exists(backup):
        old = io.open(backup, encoding='utf-8').read()
        ok_core = (collections.Counter(core(''.join(body_lines(old))))
                   == collections.Counter(core(body)))
    return {
        'fn': fn, 'chars': n, 'paras': len(ps), 'punc': pn,
        'rate': pn / n if n else 0,
        'avg': sum(lens) // len(lens) if lens else 0,
        'min': min(lens) if lens else 0, 'max': max(lens) if lens else 0,
        'brk': len(brk), 'ctrl': ctrl, 'dup': dup, 'nsp': nsp,
        'ad': ad, 'core': ok_core, 'brk_samples': brk[:3],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('listfile')
    ap.add_argument('--backup-dir')
    ap.add_argument('--prefix', default='c1_')
    a = ap.parse_args()
    fns = [l.strip() for l in io.open(a.listfile, encoding='utf-8') if l.strip()]
    rows = []
    for i, fn in enumerate(fns, 1):
        b = os.path.join(a.backup_dir, f'{a.prefix}{i:02d}.md') if a.backup_dir else None
        rows.append(check(fn, b))
    print(f'{"篇":34s} 字符   段  标点率  段均  最短 最长 断词 控制 叠点 数空 广告 字符守恒')
    for r in rows:
        print(f'{r["fn"][:32]:34s} {r["chars"]:6d} {r["paras"]:4d} '
              f'{r["rate"]:.4f} {r["avg"]:4d} {r["min"]:4d} {r["max"]:4d} '
              f'{r["brk"]:4d} {r["ctrl"]:4d} {r["dup"]:4d} {r["nsp"]:4d} '
              f'{"Y" if r["ad"] else "-":>4s} '
              f'{"✓" if r["core"] else ("-" if r["core"] is None else "✗")}')
    bad = [r for r in rows
           if r['ctrl'] or r['dup'] or r['nsp'] or r['core'] is False
           or not (0.05 <= r['rate'] <= 0.13) or not (85 <= r['avg'] <= 145)]
    print(f'\n异常 {len(bad)} / {len(rows)}')
    for r in bad:
        print('  ⚠', r['fn'])
        for s in r['brk_samples']:
            print('     断词:', s[-46:])


if __name__ == '__main__':
    main()
