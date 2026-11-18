"""项目搬家：找本机正在开发的项目 → 用户勾选 → 原地登记、补进度文件 → 可撤回。

只读扫描；搬家只新建缺少的文件，不改、不覆盖、不挪原有文件；撤回把新建的文件移到 ~/归档。
"""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import uuid
from .core import Blocked, atomic, user_home as _home

SKIP_DIRS={'Library','node_modules','.Trash','.cache','.npm','.cargo','.rustup','.venv','venv','__pycache__','Applications','Pictures','Music','Movies','归档','.git'}
TEMP_HINT=re.compile(r'(^|/)[^/]*(tmp|temp|scratch|sandbox|测试|临时|试验|备份|backup|-wt|worktrees?)[^/]*(/|$)|/private/|/T/|\d{8}|[0-9a-f]{32}',re.I)
GENERIC=('repo','main','src','code','app','project')
# 没用 Git 管的项目也要列出来让用户勾（2026-11-16 外部用户：扫描只认 Git，AI 自己到处找、自己 git init 就搬了）
PROJECT_MARKERS=('package.json','pyproject.toml','requirements.txt','setup.py','go.mod','Cargo.toml','docker-compose.yml','docker-compose.yaml','compose.yaml','compose.yml','Dockerfile','pom.xml','build.gradle','Gemfile','composer.json','AGENTS.md','CLAUDE.md')
GITIGNORE='# 随身开发搬家时新建：密钥、运行数据和依赖不进 Git\n.env\n.env.*\n*.pem\n*.key\n*.p12\nsecrets/\n*.log\nlogs/\nnode_modules/\n.venv/\nvenv/\n__pycache__/\n.DS_Store\n'
NEXT=('先停在这里：把上面这张清单原样给用户看（名字、位置、多久没动、是不是 Git），让用户自己说要搬哪几个。'
      '用户回复之前不要 adopt；不要自己再去别的目录找项目；不要替用户 git init。'
      '用户点名后，逐个 move-in adopt <位置> --confirmed "<用户原话>"；清单里「不是Git」的，要先问用户同不同意在原目录新建 Git，同意了再加 --init-git。'
      '清单里没有用户要的项目，就问用户在哪个文件夹，用 move-in scan <那个文件夹> 列出来再让用户确认。')
LAST_SCAN='move-in:last-scan'
CREATED_NOTE='> 搬家时由随身开发自动起草，带「待确认」的请核对后改掉。\n'

def backup_root(store):
    """包外备份目录：后台配置的 backup_dir，否则 ~/随身开发备份。"""
    cfg=store.get('runner:config') or {}
    return Path(cfg.get('backup_dir') or _home()/'随身开发备份')

def _git(path,*args):
    r=subprocess.run(['git','-C',str(path),*args],capture_output=True,text=True)
    return r.stdout.strip() if r.returncode==0 else ''

def _repos(root,depth=4,plain=None):
    found=[]
    def walk(p,d):
        try:entries=list(os.scandir(p))
        except OSError:return
        if any(e.name=='.git' for e in entries):found.append(Path(p));return
        if plain is not None and d<depth and any(e.name in PROJECT_MARKERS for e in entries if e.is_file()):plain.append(Path(p));return
        if d==0:return
        for e in entries:
            if e.is_dir(follow_symlinks=False) and e.name not in SKIP_DIRS and not e.name.startswith('.'):walk(e.path,d-1)
    walk(root,depth);return found

def _agent_paths(home):
    """各 AI 工具留下的工作目录：Codex/Claude Code 会话里的 cwd、T3 worktree。只读文件头。"""
    seen={}
    for f in sorted((home/'.codex/sessions').glob('*/*/*/*.jsonl'))[-400:]:
        try:
            with open(f,encoding='utf-8') as h:row=json.loads(h.readline())
            cwd=row.get('payload',{}).get('cwd')
            if cwd:seen.setdefault(cwd,set()).add('Codex')
        except (OSError,ValueError):continue
    for f in list((home/'.claude/projects').glob('*/*.jsonl'))[-400:]:
        try:
            with open(f,encoding='utf-8') as h:
                for _ in range(20):
                    line=h.readline()
                    if not line:break
                    cwd=json.loads(line).get('cwd')
                    if cwd:seen.setdefault(cwd,set()).add('Claude Code');break
        except (OSError,ValueError):continue
    for d in (home/'.t3/worktrees').glob('*/*'):
        top=_git(d,'rev-parse','--path-format=absolute','--git-common-dir')
        if top:seen.setdefault(str(Path(top).parent),set()).add('T3')
    if (home/'.cursor').exists():
        for f in (home/'Library/Application Support/Cursor/User/workspaceStorage').glob('*/workspace.json'):
            try:
                uri=json.loads(f.read_text()).get('folder','')
                if uri.startswith('file://'):seen.setdefault(uri[7:],set()).add('Cursor')
            except (OSError,ValueError):continue
    return seen

def _repo_root(path):
    top=_git(path,'rev-parse','--show-toplevel')
    return Path(top) if top else None

def scan(store,roots=None):
    """列出本机项目：名字、位置、最近改动、哪些 AI 在用、是否已登记、是否像临时目录。"""
    from .board import load
    home=_home()
    registered={str(Path(e['path']).resolve()) for e in load(store)}
    candidates={}
    own=(store.root.resolve(),)
    def keep(p):
        p=Path(p).resolve()
        if any(p==o or o in p.parents for o in own):return None
        common=_git(p,'rev-parse','--path-format=absolute','--git-common-dir')
        if common and Path(common).resolve()!=(p/'.git').resolve():return None  # linked worktree: counted with its main repo
        return str(p)
    plain={}
    for root in roots or [home]:
        found_plain=[]
        for repo in _repos(Path(root),plain=found_plain):
            k=keep(repo)
            if k:candidates.setdefault(k,set())
        for d in found_plain:
            d=d.resolve()
            if not any(d==o or o in d.parents for o in own):plain.setdefault(str(d),set())
    for cwd,tools in _agent_paths(home).items():
        top=_repo_root(cwd) if Path(cwd).is_dir() else None
        if not top and Path(cwd).is_dir() and Path(cwd).resolve()!=home.resolve():
            d=Path(cwd).resolve()
            if not any(d==o or o in d.parents for o in own):plain.setdefault(str(d),set()).update(tools)
            continue
        if top and top.resolve()!=home.resolve():
            common=_git(top,'rev-parse','--path-format=absolute','--git-common-dir')
            main=Path(common).parent if common else top
            k=keep(main)
            if k:candidates.setdefault(k,set()).update(tools)
    rows=[]
    for path,tools in candidates.items():
        p=Path(path)
        if p.resolve()==store.root.resolve():continue
        last=_git(p,'log','-1','--format=%cs')
        days=None
        if last:days=int((time.time()-time.mktime(time.strptime(last,'%Y-%m-%d')))//86400)
        markers=[n for n in ('AGENTS.md','CLAUDE.md','STATUS.md','.cursorrules') if (p/n).exists()]
        try:shown='/'+str(p.resolve().relative_to(home.resolve()))
        except ValueError:shown=path
        temp=bool(TEMP_HINT.search(shown)) or not last
        label=p.parent.name if p.name.lower() in GENERIC else p.name
        rows.append({'名字':label,'位置':path,'是Git':True,'最近改动':last or '无提交','多久没动':days,'在用的AI':sorted(tools),
                     '已有说明':markers,'已登记':path in registered,'像临时目录':temp,'建议勾选':not temp and path not in registered})
    for path,tools in plain.items():
        p=Path(path)
        try:newest=max((e.stat().st_mtime for e in os.scandir(p) if e.is_file()),default=None)
        except OSError:newest=None
        days=int((time.time()-newest)//86400) if newest else None
        try:shown='/'+str(p.relative_to(home.resolve()))
        except ValueError:shown=path
        temp=bool(TEMP_HINT.search(shown))
        label=p.parent.name if p.name.lower() in GENERIC else p.name
        rows.append({'名字':label,'位置':path,'是Git':False,'最近改动':time.strftime('%Y-%m-%d',time.localtime(newest)) if newest else '不清楚','多久没动':days,'在用的AI':sorted(tools),
                     '已有说明':[n for n in ('AGENTS.md','CLAUDE.md','STATUS.md') if (p/n).exists()],'已登记':path in registered,'像临时目录':temp,
                     '建议勾选':False,'搬之前':'不是 Git 仓库：要先在原目录新建 Git（会多出 .git 和 .gitignore，不改原文件），先问用户同不同意'})
    store.put(LAST_SCAN,{'at':time.time(),'paths':[r['位置'] for r in rows]})
    rows.sort(key=lambda r:(r['已登记'],r['像临时目录'],r['多久没动'] if r['多久没动'] is not None else 10**6))
    return rows

def _slug(name):
    # 编号只能用英文字母、数字、短横线；中文名字照样显示，编号另起
    s=re.sub(r'[^0-9A-Za-z-]+','-',name).strip('-').lower()
    return s or 'project-'+uuid.uuid4().hex[:6]

def _draft_status(path,name):
    log=_git(path,'log','-8','--format=%cs\t%s').splitlines()
    rows=''.join(f'| {l.split(chr(9),1)[0]} | {l.split(chr(9),1)[1].replace("|","/")[:80]} | Git 提交 |\n' for l in log if '\t' in l)
    last=log[0].split('\t')[0] if log else time.strftime('%Y-%m-%d')
    return (f'---\n项目: {name}\n状态: 进行中\n阶段: 待确认\n完成度: 待确认\n长期目标: 待确认（写成：完成 x 块 / 共 y 块；本期：哪几块；卡在：谁）\n最后更新: {last}\n下一步: 待确认\n等谁: 待确认\n卡在: 待确认\n---\n\n'
            f'# {name} 进度\n\n{CREATED_NOTE}\n## 近期动态\n\n| 日期 | 事件 | 出处 |\n|---|---|---|\n{rows}\n## 做到哪了\n\n- 待确认（请 AI 读代码和旧会话后补写）\n')

DRAFTS={
    'STATUS.md':None,
    'docs/工作法.md':lambda name:f'# {name} 工作法\n\n> 换了 AI 接手也照这里做；要改做法先在 docs/DECISIONS.md 写理由。被纠正、踩坑或发现更好的做法，当场记在这里或 docs/避坑.md。\n\n| 日期 | 场景 | 做法 | 为什么 | 出处 |\n|---|---|---|---|---|\n',
    '功能清单.md':lambda name:f'# {name} 功能清单\n\n状态请用 ✅ 完成 / 🔄 进行中 / ⏳ 未开始 / ⛔ 卡住 开头。\n\n{CREATED_NOTE}\n| PRD编号 | 功能 | 状态 | 验收 | 说明 |\n|---|---|---|---|---|\n| 待确认 | 示例功能 | ⏳ 未开始 | 待确认 | 请替换为实际功能 |\n',
    'docs/PRD.md':lambda name:f'# {name} PRD\n\n{CREATED_NOTE}\n## 一句话\n待确认\n\n## 用户\n待确认\n\n## 范围\n待确认\n',
    'docs/避坑.md':lambda name:f'# 避坑本\n\n> 场景｜坑｜经验，三样缺一不记；新的写最上面。\n\n| 日期 | 主题 | 场景 | 坑 | 经验 | 出处 |\n|---|---|---|---|---|---|\n',
    'docs/DECISIONS.md':lambda name:f'# {name} 决定记录\n\n',
}

def adopt(store,path,name=None,protocol=None,confirmed=None,init_git=False,t3=None):
    """原地登记一个项目：先备份 Git，再只补缺少的进度文件，登记注册表。返回撤回号。
    必须先 scan、把清单给用户勾选；confirmed 写用户点名要搬的原话。不是 Git 的目录要用户同意后才 init_git。"""
    from .board import load,apply
    if not (confirmed or '').strip():
        raise Blocked('还没让用户确认：先 move-in scan，把清单给用户看，用户点名要搬这个项目后，用 --confirmed "<用户原话>" 再搬')
    target=Path(path).expanduser().resolve()
    scanned={str(Path(x).resolve()) for x in (store.get(LAST_SCAN) or {}).get('paths',[])}
    repo=_repo_root(target)
    if str(target) not in scanned and not (repo and str(repo.resolve()) in scanned):
        raise Blocked('这个目录不在最近一次扫描清单里：先 move-in scan '+str(target)+'，把结果给用户确认后再搬')
    git_init=False
    if not repo:
        if not init_git:raise Blocked('不是 Git 仓库：'+str(target)+'。要搬得先在原目录新建 Git（会多出 .git 和 .gitignore），先问用户同不同意；同意了加 --init-git')
        if not target.is_dir():raise Blocked('目录不存在：'+str(target))
        subprocess.run(['git','-C',str(target),'init','-q'],check=True,capture_output=True)
        git_init=True;repo=target
    repo=repo.resolve();name=name or repo.name
    entries=load(store)
    if any(Path(e['path']).resolve()==repo for e in entries):raise Blocked('已经登记过：'+str(repo))
    pid=_slug(name)
    if any(e['id']==pid for e in entries):pid+='-'+uuid.uuid4().hex[:4]
    from .core import identifier
    identifier(pid)  # 先校验，失败时项目里一个文件都不写
    move_id=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    backup=backup_root(store)/'搬家'/move_id;backup.mkdir(parents=True)
    # 刚 git init、还没有提交的空仓库没有历史可打包；搬家只新建文件、不改原文件，跳过即可。
    empty=not _git(repo,'for-each-ref','--count=1','--format=%(refname)')
    if not empty:subprocess.run(['git','-C',str(repo),'bundle','create',str(backup/'repo.bundle'),'--all'],check=True,capture_output=True)
    from .settings import hosting_of
    hosting=hosting_of(repo)
    created=[]
    if git_init and not (repo/'.gitignore').exists():atomic(repo/'.gitignore',GITIGNORE);created.append('.gitignore')
    for rel,make in DRAFTS.items():
        target=repo/rel
        if target.exists():continue
        target.parent.mkdir(parents=True,exist_ok=True)
        atomic(target,_draft_status(repo,name) if make is None else make(name));created.append(rel)
    agents=repo/'AGENTS.md';conflict=[]
    line=f'开发规则见 {protocol or store.root/"工具/开发协议.md"}；先读 STATUS.md 和 docs/工作法.md。收到新指令时，当前正在做的和已经排队的继续做完，除非新指令写明停、取消或改做这一件。合并按协议【按风险分级】：低风险按块、CI 通过就合，高风险整件做完给用户看过再问一次合。等检查不改成等合并；先不合或待批准解除监视，留下问题和恢复条件。\n'
    if not agents.exists():atomic(agents,line+'\n# '+name+'\n');created.append('AGENTS.md')
    elif '开发协议' not in agents.read_text(encoding='utf-8',errors='replace'):conflict.append('AGENTS.md 已有自己的规则，未改；请 AI 对照协议列出冲突问一次')
    entry={'id':pid,'name':name,'path':str(repo),'status_path':str(repo/'STATUS.md'),'features_path':str(repo/'功能清单.md'),
           'prd_path':str(repo/'docs/PRD.md'),'moved_in':move_id,'hosting':hosting}
    registered=apply(store,entries+[entry])
    record={'id':move_id,'project':pid,'path':str(repo),'created':created,'backup':str(backup),'at':time.time(),'confirmed':confirmed.strip(),'git_init':git_init}
    store.put('move-in:'+move_id,record)
    window,starter=project_window(repo,name,t3)
    return {'项目窗口':window,'启动语':starter,'提醒':registered.get('提醒',[]),'撤回号':move_id,'项目':name,'编号':pid,'位置':str(repo),'新建':created,'未改':conflict,'代码存放':'连着在线仓库：'+hosting if hosting!='local' else '只存本机','备份':'空仓库，还没有历史可备份' if empty else str(backup/'repo.bundle'),'原文件改动':0,'新建了Git':git_init,'用户确认':confirmed.strip()}

def project_window(repo,name,t3=None):
    """给项目单独开窗口（2026-11-16 外部用户：搬完没开项目对话；用豆包等 AI 时 AI 打不开新对话，只能给启动语）。
    装了 T3 就加进 T3，否则给一句能贴进新对话的启动语。返回 (说明, 启动语)。"""
    repo=Path(repo)
    starter='请先读「'+str(repo/'AGENTS.md')+'」，按里面的规矩当我的开发助手；我不懂代码，用大白话跟你说需求。'
    added=None
    if t3 is not None:
        try:added=t3.add_project(repo,name)
        except Exception:added=False
    window=('已加进 T3：在 T3 左边点「'+name+'」开一个新对话，就是这个项目的窗口' if added else
            'AI 没法替你新开对话窗口，请你自己在用的 AI 里先点「新对话」（一般在左边栏最上面或一个「+」号；能选工作文件夹的，选「'+str(repo)+'」），再把这句贴进那个新对话——一定不要贴在当前这个对话里，否则当前对话会变成这个项目的窗口：'+starter+(
            '（T3 没开着或没加上，打开 T3 后也可以说「把随身开发接进 T3」）' if added is False else ''))
    return window,starter

def undo(store,move_id):
    """撤回搬家：取消登记，把搬家新建且之后没被改过的文件移到 ~/归档/<年-月>/。"""
    from .board import load,apply
    record=store.get('move-in:'+move_id)
    if not record:raise Blocked('没有这个撤回号：'+move_id)
    if record.get('undone'):return {'撤回号':move_id,'已撤回':True}
    repo=Path(record['path'])
    archive=_home()/'归档'/time.strftime('%Y-%m')/('搬家撤回-'+Path(record['path']).name+'-'+move_id)
    moved=[];kept=[]
    for rel in record['created']:
        src=repo/rel
        if not src.exists():continue
        tracked=_git(repo,'log','-1','--format=%H','--',rel)
        if tracked:kept.append(rel+'（已被提交，留着）');continue
        dst=archive/rel;dst.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(src),str(dst));moved.append(rel)
    if record.get('git_init') and (repo/'.git').is_dir():
        if _git(repo,'for-each-ref','--count=1','--format=%(refname)'):kept.append('.git（搬家后已有提交，留着）')
        else:archive.mkdir(parents=True,exist_ok=True);shutil.move(str(repo/'.git'),str(archive/'.git'));moved.append('.git')
    apply(store,[e for e in load(store) if e['id']!=record['project']],remove=[record['project']])
    record['undone']=time.time();store.put('move-in:'+move_id,record)
    return {'撤回号':move_id,'取消登记':record['project'],'移到归档':moved,'保留':kept,'归档位置':str(archive) if moved else None,'永久删除':False}

FIRST_LINE=re.compile(r'^(开发规则见 )(.+?)(/工具/开发协议\.md)')

def _old_registry(source):
    """旧电脑备份文件夹里最新一份每日备份的登记表。"""
    source=Path(source).expanduser().resolve()
    if not source.is_dir():raise Blocked('找不到旧电脑的备份文件夹：'+str(source))
    for daily in sorted(source.glob('daily-*'),reverse=True):
        f=daily/'数据/项目注册表.json'
        if daily.is_symlink() or not f.is_file():continue
        try:data=json.loads(f.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        items=data.get('projects') if isinstance(data,dict) else data
        if isinstance(items,list):return daily.name,[e for e in items if isinstance(e,dict) and e.get('id') and e.get('path')]
    raise Blocked('这个备份文件夹里没有每日备份（daily-…/数据/项目注册表.json）：'+str(source))

def _ignored_patterns(repo):
    try:lines=(Path(repo)/'.gitignore').read_text(encoding='utf-8').splitlines()
    except OSError:return []
    return [l.strip() for l in lines if l.strip() and not l.strip().startswith('#')][:20]

def rejoin(store,source,to=None,confirmed=None,clone=None):
    """换电脑接着做：从旧电脑的备份文件夹把登记过的项目接回来（SD-33，方案 docs/plans/方案-换电脑接着做-20261116.md）。
    不带 confirmed 只给计划；每个项目先恢复到临时文件夹、核对后改名到目标，再改登记表和 AGENTS 第一行（改前留备份）。"""
    from .board import load,apply
    from .assist import restore_project
    from .core import safe
    stamp,old=_old_registry(source)
    current=load(store)
    have_ids={e['id'] for e in current}
    live={e['id'] for e in current if Path(e['path']).exists()}
    base=Path(to).expanduser().resolve() if to else _home()
    plan=[]
    for e in old:
        name=Path(e['path']).name or e['id']
        row={'项目':e.get('name') or e['id'],'编号':e['id'],'旧位置':e['path'],'新位置':str(base/name),
             '来源':'网上仓库：'+e['hosting'] if e.get('hosting') not in (None,'','local') else '备份'}
        if e['id'] in live:row['做法']='新电脑上已登记且目录在，不动'
        elif (base/name).exists():row['做法']='目标文件夹已存在，跳过（换一个 --to 再试）'
        else:row['做法']='接回'
        plan.append(row)
    if not (confirmed or '').strip():
        return {'计划':plan,'备份来自':stamp,'下一步':'先把这张计划给用户看；用户同意后用 move-in rejoin '+str(source)+' --confirmed "<用户原话>" 执行（可加 --to <放项目的文件夹>）'}
    keep=store.root/'数据/接入备份'/('rejoin-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]);keep.mkdir(parents=True)
    reg=store.root/'数据/项目注册表.json'
    if reg.is_file():shutil.copy2(reg,keep/'项目注册表.json')
    clone=clone or (lambda url,dest:subprocess.run(['git','clone','-q','--',url,str(dest)],capture_output=True,stdin=subprocess.DEVNULL).returncode==0)
    done=[];added=[]
    for row,e in zip(plan,old):
        if row['做法']!='接回':done.append(row);continue
        target=Path(row['新位置']);tmp=target.parent/('.接回中-'+target.name+'-'+uuid.uuid4().hex[:6])
        try:
            target.parent.mkdir(parents=True,exist_ok=True)
            try:
                got=restore_project(store,e['id'],to=str(tmp),backup_dir=source);how='备份 '+got.get('备份日期','')
            except Blocked as missing:
                url=e.get('hosting')
                if url in (None,'','local'):raise Blocked('备份里没有这个项目：'+str(missing))
                if tmp.exists():shutil.rmtree(tmp)
                if not clone(url,tmp):raise Blocked('从网上仓库拉不下来（可能要你本人先登录）：'+url)
                how='网上仓库'
            head=_git(tmp,'rev-parse','--verify','-q','HEAD')
            if not head:  # 网上仓库默认分支指向不存在的分支时，克隆下来什么都没检出：改用它的 main（或第一个分支）
                refs=[r for r in _git(tmp,'for-each-ref','--format=%(refname:short)','refs/remotes/origin').splitlines() if r and not r.endswith('/HEAD')]
                pick=next((r for r in refs if r.endswith('/main')),None) or next((r for r in refs if r.endswith('/master')),None) or (refs[0] if refs else None)
                if pick:
                    subprocess.run(['git','-C',str(tmp),'checkout','-q','-B',pick.split('/',1)[1],pick],capture_output=True)
                    head=_git(tmp,'rev-parse','--verify','-q','HEAD')
            if not head:raise Blocked('接回后核对不过：没有提交记录')
            if target.exists():raise Blocked('目标文件夹刚刚被别人建了，没动它')
            os.rename(tmp,target)
            agents=target/'AGENTS.md';note='项目里没有 AGENTS.md'
            if agents.is_file():
                text=agents.read_text(encoding='utf-8');first,_,rest=text.partition('\n')
                if FIRST_LINE.match(first):
                    shutil.copy2(agents,keep/(e['id']+'-AGENTS.md'))
                    atomic(agents,FIRST_LINE.sub(lambda m:m.group(1)+str(store.root)+m.group(3),first,count=1)+'\n'+rest);note='第一行已改成新位置'
                else:note='说明第一行格式不同，没改'
            entry={**e,'path':str(target),'status_path':str(target/'STATUS.md'),'features_path':str(target/'功能清单.md'),'prd_path':str(target/'docs/PRD.md')}
            added.append(entry)
            done.append({**row,'结果':'已接回','来自':how,'最后提交':_git(target,'log','-1','--format=%cs %s'),'项目说明':note,
                         '没带过来（被 .gitignore 忽略，要你自己拷或重新生成）':_ignored_patterns(target)})
        except Exception as error:
            if tmp.exists():
                dest=_home()/'归档'/time.strftime('%Y-%m')/('换电脑接回失败-'+tmp.name);dest.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(tmp),str(dest))
            done.append({**row,'结果':'没接回','原因':str(error)[:200]})
    if added:
        try:apply(store,[x for x in current if x['id'] not in {a['id'] for a in added}]+added)
        except Exception:
            if (keep/'项目注册表.json').is_file():shutil.copy2(keep/'项目注册表.json',reg)
            raise
    return {'结果':done,'接回':len(added),'备份来自':stamp,'改前备份':str(keep),'用户确认':confirmed.strip(),
            '下一步':'说「各项目怎么样」看看项目都在；看板提示后台暂停或没备份目录，就说「帮我恢复后台」「帮我设备份」'}

def run(args):
    from .core import Store
    s=Store(args.root)
    try:
        if args.action=='scan':return {'projects':scan(s,[Path(r) for r in args.value] or None),'下一步':NEXT}
        if args.action=='adopt':
            from .t3_setup import T3Setup
            return adopt(s,args.value[0],args.name,confirmed=args.confirmed,init_git=args.init_git,t3=T3Setup(root=s.root))
        if args.action=='undo':return undo(s,args.value[0])
        if args.action=='rejoin':return rejoin(s,args.value[0],args.to,confirmed=args.confirmed)
    finally:s.db.close()

def register(subparser):
    p=subparser.add_parser('move-in',help='项目搬家：scan [目录…] / adopt <项目目录> --confirmed "<用户原话>" [--init-git] [--name 名字] / undo <撤回号> / rejoin <旧电脑备份文件夹> [--to 放项目的文件夹] --confirmed "<用户原话>"（换电脑接回项目）')
    p.add_argument('action',choices=['scan','adopt','undo','rejoin']);p.add_argument('value',nargs='*');p.add_argument('--name')
    p.add_argument('--confirmed',help='用户点名要搬这个项目的原话（没有就不搬）');p.add_argument('--init-git',action='store_true',help='不是 Git 的目录，用户同意后在原目录新建 Git');p.add_argument('--to',help='rejoin：放项目的文件夹（默认个人文件夹）')
    p.set_defaults(move_in_run=run);return p
