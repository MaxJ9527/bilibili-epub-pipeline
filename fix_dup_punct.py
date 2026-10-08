"""全库连续标点收敛（正文区）。

**背景**：2026-10-08 Max 肉眼发现 `1945年，。人类正式进入了核武器失败` ——
`DUP_MIX` 正则的第一个字符类只含句末类，漏掉 `，。` 这一类，
而它占全库 1690 处的 1598 处（95%）。终检用的是同一条错正则，所以一直报 0。

**范围**：正文区 = 首个 `##` 之后、STOP 章节之前的段落行。
  - 导语块（`> **一句话概括**`）是人工写的，在首个 `##` 之前，不动；
  - 评论区是观众口语（`你是。。。。。。大大怪将军`），那是**原样**不是错误，不动；
  - 图片说明块（`>` 开头）与表格（`|`）不是段落行，不动。

**断言**：改后行去掉全部标点必须与原行逐字相同——保证只删标点、不动实义字符。

用法：
  python3 fix_dup_punct.py            # 预演
  python3 fix_dup_punct.py --apply
"""
import io
import os
import re
import sys
import shutil
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from model_punc import collapse_dup, count_dup, split_fm

VAULT = os.environ.get("BILI_VAULT", "")  # 指向你的 Obsidian 笔记目录
BACKUP = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'backup')
STOP = ('热门评论', '相关篇目', '视频中出现的音乐', '字幕误识别',
        '归档说明', '参考资料', '图片说明')
STRIP = re.compile(r'[，。、；：？！]')


def is_para(l):
    s = l.strip()
    return bool(s) and not s.startswith(('#', '!', '>', '|', '-', '<!--', '*')) \
        and len(s) > 20


def body_range(ls):
    si = next((i for i, l in enumerate(ls) if l.strip().startswith('## ')), 0)
    stop = len(ls)
    for i, l in enumerate(ls):
        s = l.strip()
        if s.startswith('## ') and s[3:].strip().startswith(STOP):
            stop = i
            break
    return si, stop


def process(fn, apply_=False):
    p = os.path.join(VAULT, fn)
    t = io.open(p, encoding='utf-8').read()
    fm, body = split_fm(t)
    ls = body.split('\n')
    si, stop = body_range(ls)
    n, changed = 0, []
    for i in range(si, stop):
        if not is_para(ls[i]):
            continue
        new = collapse_dup(ls[i])
        if new == ls[i]:
            continue
        assert STRIP.sub('', new) == STRIP.sub('', ls[i]), \
            f'{fn} 第{i}行实义字符被动过\n原: {ls[i][:70]}\n新: {new[:70]}'
        n += count_dup(ls[i])
        changed.append((i, ls[i], new))
        ls[i] = new
    if not changed:
        return 0, 0
    res = fm + '\n'.join(ls)
    # 结构性断言
    assert re.findall(r'^## .+$', res, re.M) == re.findall(r'^## .+$', body, re.M), f'{fn} 标题变化'
    assert re.findall(r'!\[\[[^\]]+\]\]', res) == re.findall(r'!\[\[[^\]]+\]\]', body), f'{fn} 图片变化'
    assert re.findall(r'^> .+$', res, re.M) == re.findall(r'^> .+$', body, re.M), f'{fn} 引用变化'
    if apply_:
        with io.open(p, 'w', encoding='utf-8') as f:
            f.write(res)
        assert io.open(p, encoding='utf-8').read() == res, f'{fn} 回读不一致'
    return n, len(changed)


def main():
    apply_ = '--apply' in sys.argv
    fns = sorted(f for f in os.listdir(VAULT)
                 if f.endswith('.md') and not f.startswith('小约翰可汗'))
    if apply_:
        tag = datetime.now().strftime('%Y%m%d_%H%M')
        dst = os.path.join(BACKUP, f'vault_{tag}')
        os.makedirs(dst, exist_ok=True)
        for f in fns:
            shutil.copy2(os.path.join(VAULT, f), os.path.join(dst, f))
        print(f'全库已备份 → {dst}（{len(fns)} 篇）')
    tot_n = tot_l = 0
    hit = []
    for f in fns:
        n, l = process(f, apply_)
        if n:
            hit.append((n, l, f))
            tot_n += n
            tot_l += l
    for n, l, f in sorted(hit, reverse=True)[:15]:
        print(f'  {n:4d} 处 / {l:4d} 行  {f}')
    print(f'\n合计 {tot_n} 处叠标点，涉及 {len(hit)} 篇 / {tot_l} 行')
    print('（预演，加 --apply 写盘）' if not apply_ else '✓ 已写盘')


if __name__ == '__main__':
    main()
