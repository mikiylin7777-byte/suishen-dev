"""判定一个 PR 是不是「只改文档」（方案见随身开发仓库 docs/plans/方案-文档改动轻量CI与进度表单独更新-20261115.md）。

同一份文件有两种用法：
- CI 里：各仓库照抄到 tools/ci/docs_only.py，CI 从**基线分支**取出来跑，输出 `mode=docs` 或 `mode=full`；
- 合并时：随身开发的 ci merge 调用这里的 classify，按 PR 的真实改动文件再判一次。

只用 Python 标准库。出任何错一律判 full（最坏只是慢，不会漏测）。
"""
import argparse
import subprocess
import sys

DOC_FILES=('STATUS.md','功能清单.md')
DOC_DIR='docs/'
DOC_EXT=('.md','.txt','.png','.jpg','.jpeg','.svg','.csv')
# 协议规定的项目记忆文件：随身开发等工具会泛指这些名字去读各项目的同名文件，测试用的是临时样例，所以不做引用搜索
MEMORY_FILES=('STATUS.md','功能清单.md','docs/PRD.md','docs/DECISIONS.md','docs/避坑.md','docs/工作法.md')
# 太常见的文件名不按文件名搜（项目代码会泛指这些名字，比如看板读各项目的 STATUS.md），只按完整路径搜
COMMON_NAMES=('STATUS.md','功能清单.md','README.md','PRD.md','DECISIONS.md','避坑.md','工作法.md','AGENTS.md','CLAUDE.md','index.md')


def _is_doc_path(path):
    path=path or ''
    if path.startswith('/') or '..' in path.split('/'):return False
    if not (path in DOC_FILES or path.startswith(DOC_DIR)):return False
    return path.lower().endswith(DOC_EXT)


def classify(files,exclude=(),referenced=None):
    """files：[(路径, 状态, 改名前路径)]。exclude：人工兜底名单。referenced(path)->bool：这个文件有没有被程序或测试引用（可不传）。
    返回 (模式, 理由列表)；模式只有 'docs' 和 'full'。"""
    reasons=[]
    if not files:return 'full',['一个文件都没改，按全量']
    excl={x.strip() for x in exclude if x and x.strip() and not x.strip().startswith('#')}
    for item in files:
        name,status,old=(tuple(item)+('',''))[:3]
        for path in [p for p in (name,old) if p]:
            if not _is_doc_path(path):reasons.append('不是文档：'+path)
            elif path in excl:reasons.append('在人工排除名单里：'+path)
            elif referenced is not None and path not in MEMORY_FILES:
                try:hit=referenced(path)
                except Exception:hit=True
                if hit:reasons.append('被程序或测试引用：'+path)
    return ('full',reasons) if reasons else ('docs',['全部是文档，且没被程序或测试引用'])


def _git(*args):
    return subprocess.run(['git',*args],capture_output=True,text=True,timeout=120)


def changed_files(base,head='HEAD'):
    """base...head 之间改了哪些文件（含改名前路径）；读不到返回 None。"""
    r=_git('diff','--name-status','-z','-M',base+'...'+head)
    if r.returncode:return None
    parts=r.stdout.split('\0');out=[];i=0
    while i<len(parts):
        code=parts[i];i+=1
        if not code:continue
        if code[:1] in ('R','C'):
            old,new=parts[i],parts[i+1];i+=2;out.append((new,'renamed',old))
        else:
            path=parts[i];i+=1;out.append((path,{'A':'added','D':'deleted','M':'modified'}.get(code[:1],code),''))
    return out


def make_referenced(head='HEAD'):
    """在 head 的程序、测试、配置里（docs/ 和 .md 以外的所有受管文件）搜：完整路径一律搜，不太常见的文件名也搜。"""
    def referenced(path):
        needles=[path]
        base=path.rsplit('/',1)[-1]
        if base not in COMMON_NAMES:needles.append(base)
        args=['grep','-F','-l','-I']
        for n in needles:args+=['-e',n]
        r=_git(*args,head,'--',':(exclude)docs/**',':(exclude)*.md',':(exclude)tools/ci/**',':(exclude)portable/ci_docs_only.py')
        if r.returncode==1:return False  # 没搜到
        if r.returncode!=0:return True   # 搜索出错：按引用处理，走全量
        return bool(r.stdout.strip())
    return referenced


def main(argv=None):
    p=argparse.ArgumentParser(description='判定 PR 是否只改文档；输出 mode=docs 或 mode=full')
    p.add_argument('--base',default='');p.add_argument('--head',default='HEAD')
    p.add_argument('--exclude',default='',help='人工兜底名单文件，一行一个路径')
    p.add_argument('--explain',action='store_true',help='把理由打印出来')
    a=p.parse_args(argv)
    mode,reasons='full',['不是 PR 或没给基线，按全量']
    try:
        if a.base and a.base not in ('origin/','gitea/'):
            files=changed_files(a.base,a.head)
            if files is None:reasons=['读不到改动文件，按全量']
            else:
                exclude=[]
                if a.exclude:
                    try:exclude=open(a.exclude,encoding='utf-8').read().splitlines()
                    except OSError:exclude=[]
                mode,reasons=classify(files,exclude,make_referenced(a.head))
    except Exception as e:
        mode,reasons='full',['判定出错，按全量：'+type(e).__name__]
    print('mode='+mode)
    if a.explain:
        for r in reasons:print('  '+r,file=sys.stderr)
    return 0


if __name__=='__main__':sys.exit(main())
