"""模型出标点初稿 → 机械修复 → 写回笔记。

分工（2026-10-07 确定）：
  - **ct-punc 模型**：只加标点，不改实义字符（已实测 245 段 0 改动）
  - **机械修复**：英文标点→中文、被拆开的数字、段末多余的句号
  - **人工**：只做错字校正（用 fix_typos.py），不再手写标点

模型三个已知缺陷，这里逐一修：
  1. 破坏数字：1912年 → 1 912年
  2. 英文标点混入：IP,叫做007.
  3. 段末无条件补句号：原文段末常是残句（被 cue 边界切断），
     模型 241/241 全补了句号 → 凡是原文段末无终止标点的，删掉模型补的那个

用法：
  python3 model_punc.py --in /tmp/mp_in.json --out /tmp/mp_out.json
  python3 model_punc.py --apply    # 写回库
"""
import os, re, json, sys, time, difflib

os.environ['MODELSCOPE_CACHE'] = '/tmp/ms_cache'

VAULT = os.environ.get("BILI_VAULT", "")  # 指向你的 Obsidian 笔记目录
STOP = ('热门评论', '相关篇目', '视频中���现的音乐', '字幕误识别',
        '归档说明', '参考资料', '图片说明')
CN = set("，。、：；？！“”‘’（）「」『』…—")
EN2CN = {',': '，', ':': '：', ';': '；', '?': '？', '!': '！', '(': '（', ')': '）'}
EN_ALL = set(",.:;?!\"'()<>[]·")


def core(s):
    """实义字符：去空白 + 去中英标点。

    拉丁字母统一转大写后再比对——ct-punc 会把 `FBI` 写成 `Fbi`、`BBC` 写成 `Bbc`，
    这是标点模型的副产品（它只擅长断句，不该改词），但铁律本来是拦「编内容」，
    因大小写误报会把整篇卡死。折中：字母大小写归一，其余实义字符仍须一字不差。
    """
    t = re.sub(r'\s+', '', s)
    t = t.replace('**', '')          # markdown 强调标记是格式符号，不算实义字符
    t = ''.join(c for c in t if c not in CN and c not in EN_ALL)
    return re.sub(r'[a-z]', lambda m: m.group(0).upper(), t)


def is_para(l):
    s = l.strip()
    return s and not s.startswith(('#', '!', '>', '|', '-', '<!--', '*')) and len(s) > 20


# ---- 连续标点收敛（唯一实现） ----
# 修复端（batch_punc.collapse_dup）和检测端（verify_batch）**必须 import 同一份**。
# 教训：早先两端各写一条正则，检测端漏了「，。」这一类，于是终检报 0
# 而全库实际 1690 处，靠 Max 肉眼才发现。规则只准有一处实现。
DUP_RUN = re.compile('[，。、；：？！]{2,}')
DUP_NONFINAL = '，、'


def collapse_dup(s):
    """连续标点收敛成一个。C 档原文已有标点，模型再补一层就叠上了。

    【2026-10-08 修正】旧版拆成两条：`(.)\\1+`（同型）+
    `([。？！；：])[，、。；：？！]`（异型）。后者的第一个字符类只含句末类，
    **漏掉了 `，。`**——而它是最主要的一类（1690 处里占 1598 处，95%）。

    保留哪一个：串里有非终结标点（，、）就留第一个非终结的，
    终结标点夹在句中不合语法；否则留第一个。
    `？？` / `！！` 是合法语气加强，原样保留。
    """
    def rep(m):
        run = m.group(0)
        nf = [c for c in run if c in DUP_NONFINAL]
        if nf:
            return nf[0]
        if len(set(run)) == 1 and run[0] in '？！':
            return run
        return run[0]
    return DUP_RUN.sub(rep, s)


def count_dup(s):
    """检测端：连续标点串的个数。与 collapse_dup 同源，不会各说各话。"""
    return sum(1 for m in DUP_RUN.finditer(s)
               if collapse_dup(m.group(0)) != m.group(0))


def split_fm(t):
    lines = t.split('\n')
    if lines[0].strip() != '---':
        raise SystemExit('没有 frontmatter')
    for i in range(1, len(lines)):
        if lines[i].strip() == '---':
            return '\n'.join(lines[:i + 1]) + '\n', '\n'.join(lines[i + 1:])
    raise SystemExit('frontmatter 没有结束标记')


def fix_en_punc(s):
    """英文标点→中文。但 . 要小心：只在明显是句末时才转。"""
    out = []
    for i, ch in enumerate(s):
        if ch in EN2CN:
            prev = s[i - 1] if i else ''
            nxt = s[i + 1] if i + 1 < len(s) else ''
            near_cn = re.match(r'[\u4e00-\u9fff]', prev or ' ') or \
                      re.match(r'[\u4e00-\u9fff]', nxt or ' ')
            if near_cn:
                out.append(EN2CN[ch])
                continue
        if ch == '.':
            prev = s[i - 1] if i else ''
            nxt = s[i + 1] if i + 1 < len(s) else ''
            # 句号：前是中文/数字后是空白或结尾 → 转中文句号
            if (re.match(r'[\u4e00-\u9fff0-9]', prev or ' ')
                    and (nxt == '' or re.match(r'\s', nxt))):
                out.append('。')
                continue
        out.append(ch)
    return ''.join(out)


def fix_numbers(s):
    r"""被拆开的数字：'1 912年' → '1912年'，'1 96 3年' → '1963年'，'19 69年' → '1969年'。

    注意：早先只匹配 `(?<=\\d)\\s+(?=\\d{3}\\b)`（空格后必须紧跟 3 位数），
    于是 3+1 / 1+2+1 / 2+2 这些拆法全部漏掉——实测「苏联最好的商店」27 处。

    实现要点：必须**先贪婪吃完整串再判断长度**。若用 `(?:\s+\d){1,4}` 直接替换，
    正则在 `1 96 3` 上只吃到 `1 96` 就停（后一组已是最后），留下 `1 963`。
    所以改成 `(?<=\\d)(?:\\s+\\d)+` 整体匹配，回调里数位数：
    合并后 <= 4 位才认（年份/金额），否则原样返回。
    """
    def rep(m):
        raw = m.group(0)
        merged = re.sub(r'\s+', '', raw)
        digits = sum(c.isdigit() for c in merged)
        return merged if digits <= 4 else raw
    return re.sub(r'(?<=\d)(?:\s+\d)+', rep, s)


def incomplete_tail(orig):
    """原文段末是否停在「没说完」的位置。"""
    return not re.search(r'[。！？：…”」』%]$', orig.rstrip())


def post(model_out, orig):
    s = fix_numbers(model_out)
    s = fix_en_punc(s)
    # 段末：原文是残句就删掉模型补的终止标点
    if incomplete_tail(orig):
        s = re.sub(r'[。！？]+$', '', s)
    # 铁律：实义字符不能变
    assert core(s) == core(orig), f'实义字符被改动！\n原: {core(orig)[:80]}\n新: {core(s)[:80]}'
    return s


def run_model():
    from funasr import AutoModel
    data = json.load(open('/tmp/mp_in.json'))
    m = AutoModel(model="ct-punc", device="cpu")
    t0 = time.time()
    for k, d in enumerate(data):
        r = m.generate(input=d)
        raw = r[0]['text']
        data[k] = {'raw': raw, 'fixed': post(raw, d), 'orig': d}
        if (k + 1) % 40 == 0:
            print(f'  {k+1}/{len(data)}  {time.time()-t0:.0f}s', flush=True)
    json.dump(data, open('/tmp/mp_out.json', 'w'), ensure_ascii=False)
    print(f'完成 {len(data)} 段，{time.time()-t0:.0f}s')
    n = sum(1 for d in data if d['raw'] == d['fixed'])
    print(f'机械修复生效 {n} 段')


def apply():
    import io
    data = json.load(open('/tmp/mp_out.json'))
    fn = json.load(open('/tmp/ep_name.json'))
    p = os.path.join(V, fn)
    t = io.open(p, encoding='utf-8').read()
    fm, body = split_fm(t)
    ls = body.split('\n')
    stop = len(ls)
    for i, l in enumerate(ls):
        s = l.strip()
        if s.startswith('## ') and s[3:].strip().startswith(STOP):
            stop = i
            break
    idx = [i for i, l in enumerate(ls[:stop]) if is_para(l)]
    assert len(idx) == len(data), f'段落数 {len(idx)} vs {len(data)}'
    for k, i in enumerate(idx):
        ls[i] = data[k]['fixed']
    res = fm + '\n'.join(ls)
    # 写盘前校验
    assert core(split_fm(res)[1]) == core(body), '整篇实义字符变化'
    assert re.findall(r'^## .+$', res, re.M) == re.findall(r'^## .+$', body, re.M), '标题变化'
    assert re.findall(r'!\[\[[^\]]+\]\]', res) == re.findall(r'!\[\[[^\]]+\]\]', body), '图片变化'
    assert re.findall(r'^> .+$', res, re.M) == re.findall(r'^> .+$', body, re.M), '引用变化'
    if '--apply' not in sys.argv:
        print('✓ 预演通过')
        return
    with io.open(p, 'w', encoding='utf-8') as f:
        f.write(res)
    assert io.open(p, encoding='utf-8').read() == res, '回读不一致'
    n = sum(res.count(c) for c in CN)
    print(f'✓ 已写入 {fn}  {len(t):,} → {len(res):,} 字符，标点 {n}')


if __name__ == '__main__':
    if os.path.exists('/tmp/mp_out.json') and '--fresh' not in sys.argv:
        apply()
    else:
        run_model()
