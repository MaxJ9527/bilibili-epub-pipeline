"""C 档单批次一键流水线。

把每批要手工敲的 7 条命令串起来（之前每批都是手敲，容易漏步骤、漏备份）：
  备份 → extract → model → apply → reflow 前快照 → reflow → 终检

model 需要 funasr（ct-punc 标点模型），用 PY_MODEL 指向装了 funasr 的解释器；
其余步骤用当前 python3 即可。

用法：
  export BILI_VAULT="/path/to/你的笔记目录"
  python3 run_batch.py /tmp/batch5.txt c_batch5 c5_
"""
import os
import shutil
import subprocess
import sys
import io

VAULT = os.environ.get('BILI_VAULT', '')
PY = sys.executable
PY_MODEL = os.environ.get('PY_MODEL', sys.executable)


def sh(cmd, cwd=None):
    here = os.path.dirname(os.path.abspath(__file__))
    print(f'$ {" ".join(cmd)}', flush=True)
    r = subprocess.run(cmd, cwd=here, capture_output=True, text=True)
    if r.stdout:
        print(r.stdout.rstrip())
    if r.returncode != 0:
        print('!! STDERR:', r.stderr[-3000:])
        raise SystemExit(f'步骤失败（exit {r.returncode}）: {cmd[0]}')
    return r.stdout


def main():
    assert VAULT, '请先设置环境变量 BILI_VAULT 指向你的笔记目录'
    listfile, bdir, prefix = sys.argv[1], sys.argv[2], sys.argv[3]
    fns = [l.strip() for l in io.open(listfile, encoding='utf-8') if l.strip()]
    n = len(fns)
    print(f'=== 批次 {listfile}  {n} 篇 ===')

    # 1 备份（改前）
    here = os.path.dirname(os.path.abspath(__file__))
    bpath = os.path.join(here, 'backup', bdir)
    os.makedirs(bpath, exist_ok=True)
    for i, f in enumerate(fns, 1):
        shutil.copy2(os.path.join(VAULT, f),
                     os.path.join(bpath, f'{prefix}{i:02d}.md'))
    print(f'✓ 备份 {n} 篇 → backup/{bdir}/{prefix}NN.md')

    # 2-4 标点
    sh([PY, 'batch_punc.py', 'extract', listfile])
    sh([PY_MODEL, 'batch_punc.py', 'model'])
    sh([PY, 'batch_punc.py', 'apply', '--yes'])

    # 5 reflow 前快照
    for i, f in enumerate(fns, 1):
        shutil.copy2(os.path.join(VAULT, f),
                     os.path.join(bpath, f'r{prefix[1:]}{i:02d}.md'))
    print(f'✓ reflow 前快照 → backup/{bdir}/r{prefix[1:]}NN.md')

    # 6 reflow
    for f in fns:
        sh([PY, 'reflow.py', '--file', f, '--apply'])

    # 7 终检
    sh([PY, 'verify_batch.py', listfile, '--backup-dir',
        os.path.join('backup', bdir), '--prefix', prefix])


if __name__ == '__main__':
    main()
