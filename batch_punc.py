"""C 档批量标点管道：一次抽取 N 篇 → 一次跑模型 → 批量写回。

为什么不用 model_punc.py 逐篇跑：187 篇每篇都要重新加载 ct-punc 模型，
光模型加载就吃掉大半时间。这里把 N 篇的段落平铺成一个大数组送模型，
靠 /tmp/mp_meta.json 记录分篇边界，写回时按偏移还原。

复用 model_punc 的 post() / core() / split_fm() / is_para()，
不另造一套校验逻辑——实义字符不变这条铁律只有一个实现。

用法：
  python3 batch_punc.py extract /tmp/batch_list.txt   # 每行一个完整文件名
  python3 batch_punc.py model
  python3 batch_punc.py apply [--yes]
"""
import os, re, sys, json, time, io

sys.path.insert(0, '/Users/max_j/WorkBuddy/2026-09-26-22-27-56/work')
os.environ['MODELSCOPE_CACHE'] = '/tmp/ms_cache'
from model_punc import post, core, is_para, split_fm, CN, STOP, collapse_dup, count_dup

V = "/Users/max_j/Library/Mobile Documents/iCloud~md~obsidian/Documents/库/知识库/bilibili/小约翰可汗"


def extract_one(fn):
    """返回 (段落列表, 全文)。段落抽取逻辑与 ep_extract.py 逐字一致。"""
    t = io.open(os.path.join(V, fn), encoding='utf-8').read()
    fm, body = split_fm(t)
    ls = body.split('\n')
    stop = len(ls)
    for i, l in enumerate(ls):
        s = l.strip()
        if s.startswith('## ') and s[3:].strip().startswith(STOP):
            stop = i
            break
    first_h2 = next((i for i, l in enumerate(ls) if l.strip().startswith('## ')), stop)
    ps, prev_quote = [], False
    for idx, l in enumerate(ls[:stop]):
        s = l.strip()
        if idx < first_h2 and prev_quote and s and \
                not s.startswith(('>', '#', '!', '|', '-', '<!--')) and len(s) > 20:
            ps.append(s.replace('**', '').strip())
            prev_quote = False
            continue
        prev_quote = s.startswith('> ')
        if is_para(s):
            ps.append(s)
    return ps, t


def cmd_extract(listfile):
    fns = [l.strip() for l in io.open(listfile, encoding='utf-8') if l.strip()]
    flat, meta = [], []
    for fn in fns:
        assert os.path.exists(os.path.join(V, fn)), f'文件不存在: {fn}'
        ps, t = extract_one(fn)
        assert len(ps) >= 5, f'{fn} 段落过少 ({len(ps)})，可能是格式问题'
        meta.append({'fn': fn, 'n': len(ps), 'chars': sum(len(x) for x in ps)})
        flat.extend(ps)
    json.dump(flat, open('/tmp/mp_in.json', 'w'), ensure_ascii=False)
    json.dump(meta, open('/tmp/mp_meta.json', 'w'), ensure_ascii=False)
    tot = sum(m['chars'] for m in meta)
    print(f'抽取 {len(meta)} 篇  共 {len(flat)} 段  {tot:,} 字')
    for m in meta:
        print(f"  {m['n']:4d} 段 {m['chars']:6,d} 字  {m['fn']}")


def cmd_model():
    from funasr import AutoModel
    data = json.load(open('/tmp/mp_in.json'))
    m = AutoModel(model="ct-punc", device="cpu")
    t0 = time.time()
    for k, d in enumerate(data):
        r = m.generate(input=d)
        raw = r[0]['text']
        data[k] = {'raw': raw, 'fixed': post(raw, d), 'orig': d}
        if (k + 1) % 200 == 0:
            print(f'  {k+1}/{len(data)}  {time.time()-t0:.0f}s', flush=True)
    json.dump(data, open('/tmp/mp_out.json', 'w'), ensure_ascii=False)
    print(f'完成 {len(data)} 段，{time.time()-t0:.0f}s')


def latin_ratio(s):
    lat = sum(1 for c in s if c.isascii() and c.isalpha())
    cn = sum(1 for c in s if '\u4e00' <= c <= '\u9fff')
    return lat / (lat + cn) if (lat + cn) else 0.0


def pick(d):
    """决定这一段用模型输出还是原文。

    ct-punc 是**中文**标点模型，喂纯英文段它会把单词劈开：
    `Which`→`W hich`、`How`→`H ow`、`That`→`T ha`（实测 27 段全中）。
    这些段是 ASR 转写的英文演讲残片，本来就逐词粘连，模型只会让它更糟。
    所以拉丁占比 > 35% 的段一律保留原文。中英混排段（0.5~0.6）仍走模型，
    中文部分的断句它是对的。
    """
    if latin_ratio(d['orig']) > 0.35:
        return d['orig'], True
    return collapse_dup(d['fixed']), False


def cmd_apply():
    data = json.load(open('/tmp/mp_out.json'))
    meta = json.load(open('/tmp/mp_meta.json'))
    assert sum(m['n'] for m in meta) == len(data), '段数与 meta 不符'
    # 1) 逐篇预演，全部通过才写盘
    plans = []
    off = 0
    for m in meta:
        fn = m['fn']
        chunk = data[off:off + m['n']]
        off += m['n']
        t = io.open(os.path.join(V, fn), encoding='utf-8').read()
        fm, body = split_fm(t)
        ls = body.split('\n')
        stop = len(ls)
        for i, l in enumerate(ls):
            s = l.strip()
            if s.startswith('## ') and s[3:].strip().startswith(STOP):
                stop = i
                break
        idx = [i for i, l in enumerate(ls[:stop]) if is_para(l)]
        assert len(idx) == len(chunk), f'{fn} 段落数 {len(idx)} vs {len(chunk)}'
        # 输入一致性：模型拿到的原文必须与当前磁盘上的段落逐字相同
        for k, i in enumerate(idx):
            assert chunk[k]['orig'] == ls[i].strip(), \
                f'{fn} 第 {k} 段输入与磁盘不一致，可能已被改过'
        new = list(ls)
        kept = 0
        for k, i in enumerate(idx):
            text, is_orig = pick(chunk[k])
            kept += is_orig
            new[i] = text
        res = fm + '\n'.join(new)
        assert core(split_fm(res)[1]) == core(body), f'{fn} 整篇实义字符变化'
        assert re.findall(r'^## .+$', res, re.M) == re.findall(r'^## .+$', body, re.M), f'{fn} 标题变化'
        assert re.findall(r'!\[\[[^\]]+\]\]', res) == re.findall(r'!\[\[[^\]]+\]\]', body), f'{fn} 图片变化'
        assert re.findall(r'^> .+$', res, re.M) == re.findall(r'^> .+$', body, re.M), f'{fn} 引用变化'
        joined = ''.join(new[i] for i in idx)
        ns = len(re.findall(r'(?<=\d)\s+(?=\d)', joined))
        dup = count_dup(joined)
        plans.append((fn, res, ns, dup, kept))
    if '--yes' not in sys.argv:
        print('预演全部通过，加 --yes 写盘')
        for fn, res, ns, dup, kept in plans:
            print(f'  {fn}  数字空格 {ns}  叠标点 {dup}  保留原文段 {kept}')
        return
    for fn, res, ns, dup, kept in plans:
        p = os.path.join(V, fn)
        with io.open(p, 'w', encoding='utf-8') as f:
            f.write(res)
        assert io.open(p, encoding='utf-8').read() == res, f'{fn} 回读不一致'
        print(f'✓ {fn}  {len(res):,} 字符  数字空格 {ns}  叠标点 {dup}  保留原文段 {kept}')


if __name__ == '__main__':
    cmd = sys.argv[1]
    arg = sys.argv[2] if len(sys.argv) > 2 else None
    fn = {'extract': cmd_extract, 'model': cmd_model, 'apply': cmd_apply}[cmd]
    if cmd == 'extract':
        fn(arg)
    else:
        fn()
