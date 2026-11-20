"""项目注册表、全局进度页与外部备份（0.2.1）。

注册表是随身开发唯一项目源：登记到 state.sqlite 的 projects/paths，
附加字段存 meta.registry，并导出 数据/项目注册表.json 供其他工具读取。
"""
import hashlib
import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from .core import Blocked, atomic, canonical

FIELDS=('status_path','features_path','prd_path')
STATES=('✅','🔄','⏳','⛔','🚫')
SYNC_NAMES={'status_path':'STATUS.md','features_path':'功能清单.md','prd_path':'PRD.md','pitfalls_path':'避坑.md'}
BOARD='看板'
STALE_DAYS=14
WAIT_DAYS=7
PRD_STALE_DAYS=30
PRD_STALE_COMMITS=20

def load(store):
    raw=store.get('registry')
    if not raw:return []
    return json.loads(raw) if isinstance(raw,str) else raw

def apply(store,entries=None,remove=()):
    if entries is None:
        from .settings import projects
        entries=projects(store)
    ids=[];missing=[]
    for e in entries:
        for key in ('id','name','path'):
            if not e.get(key):raise Blocked('注册表条目缺少'+key)
        if not Path(e['path']).is_dir():
            if e['id'] not in {x['id'] for x in load(store)}:raise Blocked('项目目录不存在：'+e['path'])
            missing.append(e['name']+' 的目录找不到了：说『帮我处理找不到的项目』')
        ids.append(e['id'])
    if len(set(ids))!=len(ids):raise Blocked('注册表编号重复')
    # Projects registered only on this machine are never dropped silently: stop and report, unless named in remove.
    local_only=[(r['id'],r['name']) for r in store.db.execute('SELECT id,name FROM projects').fetchall() if r['id'] not in ids and r['id'] not in remove]
    if local_only:
        raise Blocked('以下项目只在本机登记、源码名单里没有，已停下未做任何改动：'+'、'.join(f'{n}（{i}）' for i,n in local_only)+'；要保留请写进本机设置的项目名单（settings set projects），确认要去掉请加 --remove 编号')
    for e in entries:
        if not Path(e['path']).is_dir():continue
        with store.db:
            store.db.execute('UPDATE projects SET name=? WHERE id=?',(e['name'],e['id']))
        store.register(e['name'],e['path'],e['id'])
    removed=[]
    for row in store.db.execute('SELECT id FROM projects').fetchall():
        if row['id'] not in ids:
            with store.db:
                store.db.execute('DELETE FROM paths WHERE project=?',(row['id'],))
                store.db.execute('DELETE FROM projects WHERE id=?',(row['id'],))
            removed.append(row['id'])
    store.put('registry',canonical(entries))
    exported=store.root/'数据/项目注册表.json'
    atomic(exported,json.dumps({'schema':1,'projects':entries},ensure_ascii=False,indent=2)+'\n')
    store.refresh_overview()
    return {'提醒':missing,'registered':ids,'removed_from_registry':removed,'files_deleted':False,'exported':str(exported)}

def frontmatter(text):
    if not text.startswith('---'):return {}
    end=text.find('\n---',3)
    if end<0:return {}
    out={}
    for line in text[3:end].splitlines():
        if ':' in line:
            k,v=line.split(':',1);out[k.strip()]=v.strip().strip('"\'')
    return out

def count_features(text):
    counts={s:0 for s in STATES}
    for line in text.splitlines():
        cells=[c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells)<3 or not line.lstrip().startswith('|'):continue
        for s in STATES:
            if cells[2].startswith(s):counts[s]+=1;break
    return counts

FALLBACK_REFS=('gitea/main','origin/main','main','chore/status-prd')

REMOTE_MAIN=('gitea/main','origin/main')

# 很多项目先把进度写在开发分支上，过几天才合进主线；看板要一起比，取最新的那份。
REMOTE_DEV=('gitea/development','origin/development')
NEWER_REFS=REMOTE_MAIN+REMOTE_DEV

def _git_text(result):
    """git 成功但没有输出时按空串，避免把 None 当成路径或时间。"""
    if result.returncode!=0 or result.stdout is None:return ''
    text=result.stdout
    if isinstance(text,bytes):text=text.decode('utf-8','replace')
    return text.strip()

def _git_show(root,ref,rel):
    r=subprocess.run(['git','-C',str(root),'show',ref+':'+rel],capture_output=True)
    return None if r.returncode!=0 or r.stdout is None else r.stdout.decode('utf-8','replace')

def _rel(entry,key):
    path=entry.get(key)
    if not path:return None,None
    p=Path(path);root=Path(entry['path'])
    try:return p,p.relative_to(root).as_posix()
    except ValueError:return p,None

def _newer_remote(entry):
    """工作区、已合并主线、开发分支三处的 STATUS.md 比一比，取最新的：先比「最后更新」日期，同一天比改动时间。
    工作区最新（或和某处一字不差）时返回 None，照旧读工作区。"""
    p,rel=_rel(entry,'status_path');root=Path(entry['path'])
    if not rel or not (root/'.git').exists() or not p.is_file():return None
    local_text=p.read_text(encoding='utf-8')
    best=((frontmatter(local_text).get('最后更新',''),p.stat().st_mtime),None)
    for ref in NEWER_REFS:
        text=_git_show(root,ref,rel)
        if text is None or text==local_text:continue
        when=_git_text(subprocess.run(['git','-C',str(root),'log','-1','--format=%ct',ref,'--',rel],capture_output=True,text=True))
        key=(frontmatter(text).get('最后更新',''),int(when) if when.isdigit() else 0)
        if key>best[0]:best=(key,ref)
    return best[1]

_DECIDE=object()

def _read_with_source(entry,key,newer=_DECIDE):
    """默认读工作区；工作区落后于主线或开发分支时读那一处；工作区没有时依次读主线或待合并分支（只读 git show）。
    newer 是这个项目按 STATUS.md 定下的来源（_newer_remote 的结果）；同一个项目的几份文件传同一个，保证出自同一处。
    那一处没有这份文件时退回工作区，并在来源里写明。"""
    p,rel=_rel(entry,key)
    if p is None:return None,None
    root=Path(entry['path'])
    if newer is _DECIDE:newer=_newer_remote(entry) if rel else None
    if newer and rel:
        text=_git_show(root,newer,rel)
        if text is not None:return text,newer
        if p.is_file():return p.read_text(encoding='utf-8'),'工作区（'+newer+' 上没有这份）'
    if p.is_file():return p.read_text(encoding='utf-8'),'工作区'
    if not rel or not (root/'.git').exists():return None,None
    for ref in ([entry['status_ref']] if entry.get('status_ref') else [])+list(FALLBACK_REFS):
        text=_git_show(root,ref,rel)
        if text is not None:return text,ref
    return None,None

def fetch_remotes(store):
    """只更新远端跟踪分支（git fetch），不动任何工作区和本地分支。"""
    done=[]
    for e in load(store):
        root=Path(e['path'])
        if not (root/'.git').exists():continue
        remotes=subprocess.run(['git','-C',str(root),'remote'],capture_output=True,text=True).stdout.split()
        for name in ('gitea','origin'):
            if name in remotes:
                env={**os.environ,'GIT_TERMINAL_PROMPT':'0'}
                r=subprocess.run(['git','-C',str(root),'fetch','-q','--no-tags',name,'main'],capture_output=True,timeout=120,env=env)
                if r.returncode==0:done.append(e['id'])
                # 开发分支不是每个项目都有，取不到不算失败
                subprocess.run(['git','-C',str(root),'fetch','-q','--no-tags',name,'development'],capture_output=True,timeout=120,env=env)
                break
    return done

def _read(entry,key,newer=_DECIDE):
    return _read_with_source(entry,key,newer)[0]

def _run_git(root,*args,timeout=30):
    """只读 git；不拿可选锁（git status 不顺手改 index）。失败返回空串。"""
    try:
        r=subprocess.run(['git','--no-optional-locks','-C',str(root),*args],capture_output=True,text=True,timeout=timeout,
                         env={**os.environ,'GIT_OPTIONAL_LOCKS':'0','GIT_TERMINAL_PROMPT':'0'})
    except (OSError,subprocess.SubprocessError):return ''
    return _git_text(r)

def _is_ancestor(root,commit,target):
    try:
        return subprocess.run(['git','-C',str(root),'merge-base','--is-ancestor',commit,target],capture_output=True,timeout=30).returncode==0
    except (OSError,subprocess.SubprocessError):return False

def _main_refs(root):
    """这台机器上已有的主线：远端主线优先，没有远端时用本地 main。"""
    found=[ref for ref in REMOTE_MAIN if _run_git(root,'rev-parse','-q','--verify',ref+'^{commit}')]
    return found or [ref for ref in ('main',) if _run_git(root,'rev-parse','-q','--verify','refs/heads/'+ref+'^{commit}')]

def dev_source(entry,source):
    """开发进度来源：看板这一行的进度出自哪个分支的哪个提交。
    source 是 STATUS.md 的来源；工作区时给 HEAD，再看有没有未提交改动。"""
    root=Path(entry['path'])
    ref=source if source and not str(source).startswith('工作区') else None
    out={'分支':ref or '工作区','提交':'','日期':''}
    if not (root/'.git').exists():return out
    line=_run_git(root,'log','-1','--format=%h %H %cd','--date=format:%Y-%m-%d %H:%M',ref or 'HEAD')
    parts=line.split(' ',2)
    if len(parts)==3:out.update(提交=parts[0],完整提交=parts[1],日期=parts[2])
    if not ref:
        branch=_run_git(root,'rev-parse','--abbrev-ref','HEAD')
        if branch and branch!='HEAD':out['工作区分支']=branch
        out['有未提交改动']=bool(_run_git(root,'status','--porcelain'))
    mains=_main_refs(root)
    commit=out.get('完整提交')
    # 进度走在主线前面：出自开发分支或别的分支、且那个提交还没进主线
    out['领先主线']=bool(commit and mains and (ref not in REMOTE_MAIN+('main',)) and not any(_is_ancestor(root,commit,m) for m in mains))
    return out

def source_text(src):
    """「gitea/development · edf873a · 2026-11-14 15:08」这样一句；工作区再写分支和有没有未提交改动。"""
    if not src:return '待确认'
    head=src.get('分支') or '工作区'
    if head=='工作区' and src.get('工作区分支'):head+='（'+src['工作区分支']+'）'
    bits=[head]+[x for x in (src.get('提交'),src.get('日期')) if x]
    if src.get('有未提交改动'):bits.append('有未提交改动')
    return ' · '.join(bits)

# 运行版本只认项目自己写下的记录，不猜。
RUN_KEYS=('运行版本','线上版本','部署版本','已部署','已安装版本')

def _is_tool_repo(root):
    return (root/'portable/protocol.json').is_file() and (root/'portable/board.py').is_file()

def running_version(store,entry,fm,src):
    """实际装着或部署着的版本：随身开发自己的仓库看安装目录的 发行.json；其余看 STATUS.md 里写明的版本；都没有写「待确认」。
    同时判断开发进度有没有上线：记录里写了提交号就对提交号；随身开发对安装时间；只写了版本号的，进度领先主线就算没上线。"""
    import re
    root=Path(entry['path']);version='';where='';installed_at=None
    release=store.root/'发行.json' if store is not None else None
    if release is not None and _is_tool_repo(root) and release.is_file():
        try:
            version=str(json.loads(release.read_text(encoding='utf-8')).get('version') or '')
            where='安装目录 发行.json';installed_at=release.stat().st_mtime
        except (OSError,ValueError):version=''
    if not version:
        for k in RUN_KEYS:
            if fm.get(k):version=fm[k];where='STATUS.md「'+k+'」';break
    src=src or {}
    if not version:
        return {'版本':'待确认','出处':'','开发进度未上线':bool(src.get('领先主线'))}
    commit=src.get('完整提交') or ''
    shas=[x.lower() for x in re.findall(r'(?<![0-9a-fA-F])[0-9a-fA-F]{7,40}(?![0-9a-fA-F])',version)]
    if shas and commit:behind=not any(commit.lower().startswith(x) for x in shas)
    elif installed_at is not None and commit:
        when=_run_git(root,'log','-1','--format=%ct',commit)
        behind=bool(when.isdigit() and int(when)>installed_at)
    else:behind=bool(src.get('领先主线'))
    return {'版本':version,'出处':where,'开发进度未上线':behind}

UNRELEASED='⚠ 开发进度未上线'

def _pitfalls_entry(e):
    return dict(e,pitfalls_path=e.get('pitfalls_path') or str(Path(e['path'])/'docs/避坑.md'))

def pitfalls(text):
    """docs/避坑.md 表格行：日期｜主题｜场景｜坑｜经验｜出处（缺场景不收）。"""
    out=[]
    for line in (text or '').splitlines():
        if not line.lstrip().startswith('|'):continue
        cells=[c.strip() for c in line.strip().strip('|').split('|')]
        if len(cells)<5 or set(cells[0])<=set('-: ') or cells[0] in ('日期',):continue
        if not cells[2]:continue
        out.append(cells[:6]+['']*(6-len(cells[:6])))
    return out

def _days(value,today=None):
    try:
        then=time.mktime(time.strptime(value[:10],'%Y-%m-%d'))
    except (TypeError,ValueError):return None
    return int(((today or time.time())-then)//86400)

_STOP=frozenset('进行 行中 开发 发中 暂缓 待确 确认 项目 完成 用户 下一 一步 没有 已更 更新'.split())

def _pieces(text):
    """中文按两字滑动，英文按词；太泛的两字不参与对上。"""
    import re
    found=set()
    for part in re.findall(r'[\u4e00-\u9fff]+|[A-Za-z][A-Za-z0-9_-]*', text or ''):
        if part[:1].isascii():
            if len(part)>=2:found.add(part.lower())
        else:
            found.update(part[i:i+2] for i in range(len(part)-1) if part[i:i+2] not in _STOP)
    return found

def focus_text(rows):
    return ' '.join(str(r.get(k) or '') for r in rows for k in ('下一步','等谁','卡在'))

def _lesson_hit(terms,lesson,focus,current):
    """两字只碰一处不算对上。两处重叠，或教训里的整词出现在当前下一步、等谁、卡在里，才带出。"""
    import re
    if len(terms & focus)>=2:return True
    for word in re.findall(r'[\u4e00-\u9fff]{3,}|[A-Za-z][A-Za-z0-9_-]{3,}', lesson or ''):
        if (word.lower() if word[:1].isascii() else word) in (current or ''):return True
    return False

def _mentioned(blob,names,skip=''):
    """项目名按整段出现才算点到。长名先认；名字嵌在更长的中文词里不算。"""
    text=blob or ''
    found=[];occupied=[]
    def cjk(ch):return '\u4e00'<=ch<='\u9fff'
    for name in sorted((n for n in names if n and n!=skip),key=len,reverse=True):
        start=0
        while True:
            i=text.find(name,start)
            if i<0:break
            end=i+len(name)
            before=text[i-1] if i else ''
            after=text[end] if end<len(text) else ''
            inside=any(a<=i and end<=b for a,b in occupied)
            if not inside and (not before or not cjk(before)) and (not after or not cjk(after)):
                if name not in found:found.append(name)
                occupied.append((i,end))
            start=i+1
    return found

def _clip(text,limit=80):
    text=' '.join(str(text or '').split())
    return text if len(text)<=limit else text[:limit]

def practices(text):
    """工作法表格认场景和做法；决定记录收短横行。对不上当前项目的不进工作记忆。"""
    out=[];header=None
    for line in (text or '').splitlines():
        raw=line.strip()
        if raw.startswith('|'):
            cells=[c.strip() for c in raw.strip('|').split('|')]
            if not cells or not cells[0] or set(cells[0])<=set('-: '):continue
            if header is None and '做法' in cells and '场景' in cells:
                header=cells;continue
            if header:
                row=dict(zip(header,cells+['']*(len(header)-len(cells))))
                if row.get('场景') and row.get('做法'):out.append({'场景':row['场景'],'做法':row['做法']})
            continue
        if raw.startswith('- ') and len(raw)>4:out.append({'场景':'','做法':raw[2:]})
    return out

def working_memory(rows,traps,learned,limit=5):
    """总管同时看很多项目时用的一小份记忆：在做的事、对得上的坑、可复用做法。"""
    active=[r for r in rows if not (r.get('状态') or '').startswith(('暂缓','待启动'))]
    focus=set()
    doing=[]
    for r in active:
        doing.append({k:r.get(k,'') for k in ('项目','下一步','等谁','卡在','停了几天')})
        focus |= _pieces(' '.join(str(r.get(k) or '') for k in ('下一步','等谁','卡在')))
    avoid=[]
    remembered=set()
    for t in traps:
        name,date,topic,scene,pit,experience=(list(t)+['']*7)[:6]
        remembered.add(name)
        terms=_pieces(' '.join(str(x or '') for x in (topic,scene,pit,experience)))
        if not _lesson_hit(terms,' '.join(str(x or '') for x in (topic,scene,pit,experience)),focus,focus_text(active)):continue
        avoid.append((len(terms & focus),str(date),{'项目':name,'场景':scene or topic,'坑':_clip(pit),'经验':_clip(experience)}))
    reuse=[]
    for name,items in learned:
        if items:remembered.add(name)
        for item in items:
            terms=_pieces(item.get('场景','')+' '+item.get('做法',''))
            if not _lesson_hit(terms,item.get('场景','')+' '+item.get('做法',''),focus,focus_text(active)):continue
            reuse.append((len(terms & focus),{'项目':name,'场景':item.get('场景') or '','做法':_clip(item.get('做法'))}))
    blank=[r['项目'] for r in active if r['项目'] not in remembered and r.get('阶段')!='缺STATUS.md']
    return {
        '在做':doing[:12],
        '要避开':[item for _,_,item in sorted(avoid,key=lambda x:(x[0],x[1]),reverse=True)[:limit]],
        '可复用':[item for _,item in sorted(reuse,key=lambda x:x[0],reverse=True)[:limit]],
        '还没记下':blank[:8],
    }

def _learned(entry):
    root=Path(entry.get('path',''))
    found=[]
    for rel in ('docs/工作法.md','docs/DECISIONS.md'):
        path=root/rel
        if path.is_symlink() or not path.is_file():continue
        try:found+=practices(path.read_text(encoding='utf-8'))
        except OSError:continue
    return found

def backup_reminders(store,now=None):
    """只读保护状态；没有证据时不把备份当成已完成。"""
    from .supervisor import service
    now=time.time() if now is None else now
    import datetime
    reminders=[]
    if not store.get('runner:config',{}).get('backup_dir'):
        reminders.append('还没设备份目录，所以没有每日备份：说「帮我设备份」即可')
    try:installed=service(store.root,Path.home()/'Library/LaunchAgents','status')['installed']
    except Exception:installed=False
    heartbeat=store.get('runner:heartbeat',{})
    if not installed or store.get('runner:service',{}).get('running') is False or not heartbeat:
        reminders.append('还没开后台或后台没在跑，所以没有每日备份，看板也不会自动更新：说「帮我开后台」即可')
    elif now-heartbeat.get('at',0)>600:
        reminders.append('后台已经超过十分钟没报平安，自动备份可能停了：说「帮我检查后台」即可')
    from .automation import state as automation_state
    if automation_state(store)['paused']:
        why=store.get('runner:pause-reason') or {}
        when=time.strftime('%m-%d %H:%M',time.localtime(why['at'])) if why.get('at') else '时间未知'
        if why.get('by') or why.get('reason'):
            reminders.insert(0,f'后台处于暂停，什么都不做（谁停的：{why.get("by") or "未写明"}；{when}；原因：{why.get("reason") or "未写明"}）：不是你让停的就说「帮我恢复后台」')
        else:  # 没记原因：多半是换了电脑或挪了位置，为防两台电脑重复干活先停下（2026-11-16 U 盘实测）
            reminders.insert(0,'后台处于暂停，什么都不做（没记原因，常见于换了电脑或挪了位置：为防两台电脑重复干活先停下）：换了电脑就说「我换电脑了，把项目接回来」，否则说「帮我恢复后台」')
    if (store.root/'发行.json').is_file():
        try:
            from .distribution import release_verify
            release_verify(store.root)
        except Exception as e:
            reminders.insert(0,'安装目录文件被手改过（'+str(e)+'）：不许在安装目录手改代码或协议，请用 upgrade apply 重新安装或退回上一版')
    from .wizard import risky_location
    try:place=risky_location(store.root)
    except Exception:place=''
    if place:reminders.insert(0,place)
    today=datetime.datetime.fromtimestamp(now).date()  # 本地日期，和后台判「今天」一致
    last=store.get('runner:daily')
    try:days=(today-datetime.date.fromisoformat(last)).days if last else None
    except ValueError:days=1  # 记录读不懂时只说今天还没备份
    if store.get('runner:config',{}).get('backup_dir'):
        if days is None:reminders.insert(0,'每日备份还没成功过一次：说「帮我立刻备份」即可')
        elif days>=2:reminders.insert(0,f'每日备份已 {days-1} 天没成功（最后一次 {last}），这是安全网出了洞：说「帮我检查备份」即可')
        elif days==1:reminders.append('今天还没有完整备份：说「帮我立刻备份」即可')
    last_error=store.get('runner:backup-last-error') or {}
    # 一次都没成功过（days 为 None）时更要说原因：2026-11-16 外部用户只看到「还没成功过」，看不到为什么
    if last_error.get('at') and (days is None or days>=1):
        reminders.append('最近一次自动备份失败的原因：'+(last_error.get('type') or '')+'：'+(last_error.get('detail') or '')[:120])
    if days is None or days>=1:
        for problem in (store.get('runner:backup-problems') or [])[:3]:
            if isinstance(problem,str) and problem not in ' '.join(reminders):reminders.append('自动备份没完成：'+problem[:160])
    gap=store.get('runner:backup-gap') or {}
    if gap.get('missed_days') and gap.get('catch_up')==today.isoformat():
        reminders.append(f'今天补跑了备份：上次成功是 {gap.get("last_ok")}，中间漏了 {gap.get("missed_days")} 天')
    auto=store.get('runner:auto-resumed') or {}
    if auto.get('at') and now-auto['at']<7*86400:
        reminders.append('后台曾因旧版关机留下的暂停而停住，开机时已自动恢复')
    return reminders

def overview(store,sync=False,today=None):
    entries=load(store)
    if not entries:raise Blocked('注册表为空；先运行 registry-apply')
    rows=[];traps=[]
    for e in entries:
        e=_pitfalls_entry(e)
        newer=_newer_remote(e)  # 一个项目只定一次来源，几份文件都从这里读
        status,source=_read_with_source(e,'status_path',newer)
        features,features_src=_read_with_source(e,'features_path',newer)
        prd_src=_read_with_source(e,'prd_path',newer)[1]
        trap_text,trap_src=_read_with_source(e,'pitfalls_path',newer)
        fm=frontmatter(status) if status else {}
        counts=count_features(features) if features else None
        src=dev_source(e,source)
        run=running_version(store,e,fm,src)
        rows.append({'项目':e['name'],'_id':e.get('id') or e['name'],'状态':fm.get('状态',''),'阶段':fm.get('阶段','缺STATUS.md' if status is None else '待确认'),'完成度':fm.get('完成度',''),
                     '下一步':fm.get('下一步',''),'等谁':fm.get('等谁',''),'卡在':fm.get('卡在',''),'功能':counts,'最后更新':fm.get('最后更新',''),
                     '停了几天':_days(fm.get('最后更新'),today),'来源':source,'长期目标':fm.get('长期目标',''),
                     '开发进度来源':src,'开发进度来源说明':source_text(src),'文件来源':{'STATUS.md':source,'功能清单.md':features_src,'PRD.md':prd_src,'避坑.md':trap_src},
                     '运行版本':run['版本'],'运行版本出处':run['出处'],'开发进度未上线':run['开发进度未上线']})
        traps+=[[e['name']]+row for row in pitfalls(trap_text)]
    learned=[(e['name'],_learned(e)) for e in entries]
    from .settings import load as settings_load
    for row in rows:row['_owner_name']=settings_load(store).get('owner_name','')
    goals=track_goals(store,rows,today,write=sync)
    reminders=backup_reminders(store,today)
    page=render(rows)
    if reminders:
        # 看板页面（Obsidian 里看的）最上面也要有安全网这一行：以前只出现在 overview 输出里，用户在看板上看不到
        head,_,rest=page.partition('\n\n')
        page=head+'\n\n> ⚠ **安全网（备份和后台）**：'+'；'.join(reminders)+'\n\n'+rest
    try:
        from .nudge import board_lines
        nudged=board_lines(store)
    except Exception:nudged=[]
    if nudged:
        # 后台叫了几次还不动、或者叫不醒的项目，点名放在看板最上面（见 DECISIONS 2026-11-18）
        head,_,rest=page.partition('\n\n')
        page=head+'\n\n> ⚠ **自动接着做**：'+'；'.join(nudged)+'\n\n'+rest
    try:waiting_tasks=awaiting_team_tasks(store,hours=0.0001)
    except Exception:waiting_tasks=None
    # 安全网（备份、后台）出问题排在最前面：2026-11-16 备份停了两天，提醒排在摘要末尾没人看到
    result={'projects':rows,'page':page,'brief':('⚠ '+'；'.join(reminders)+'。' if reminders else '')+brief(rows),'pitfalls':len(traps),'提醒':reminders,'工作记忆':working_memory(rows,traps,learned),'待处理':pending_desk(rows,waiting_tasks,entries,replies=open_replies(store),waits=open_waits(store)),'分档':attention(rows,entries,store),'长期目标':goals}
    if reminders:result['待处理']={'安全网':reminders,**(result['待处理'] if isinstance(result['待处理'],dict) else {'其余':result['待处理']})}
    if sync:
        result['sync']=write_board(store,entries,page,traps)
        result['knowledge']=sync_knowledge(store,entries,rows)
    return result

KNOW_BEGIN='<!-- 进度 BEGIN -->';KNOW_END='<!-- 进度 END -->'

def knowledge_card(row):
    """知识库项目页顶部的进度卡：取自项目自己的 STATUS.md，并指路到看板。"""
    lines=[KNOW_BEGIN,'## 进度（来自项目的 STATUS.md）']
    if row['阶段']=='缺STATUS.md':
        lines.append('这个项目还没有进度卡（STATUS.md）。对 AI 说「帮我给这个项目补上进度卡」，这里就会显示进度。')
    else:
        for k in ('长期目标','状态','阶段','下一步','等谁','卡在','最后更新'):
            if row.get(k):lines.append(f'- **{k}**：'+str(row[k]))
    lines+=['','全部项目的进度看板在「随身开发/看板/全部项目.md」，它在知识库文件夹外面：用 Obsidian 打开「随身开发/看板」文件夹就能看到。','下面「当前目标、当前任务、最近交付」只记通过随身开发派出去的任务，刚搬进来的项目是空的属正常。',KNOW_END]
    return '\n'.join(lines)

def sync_knowledge(store,entries,rows):
    """把进度卡写进 知识库/项目/<id>.md（只改进度卡那一段；页面不存在就不建）。"""
    done=[]
    for e,row in zip(entries,rows):
        note=store.root/'知识库/项目'/(str(e.get('id') or '')+'.md')
        if not e.get('id') or not note.is_file():continue
        text=note.read_text(encoding='utf-8');card=knowledge_card(row)
        if text.count(KNOW_BEGIN)==1 and text.count(KNOW_END)==1:
            head,rest=text.split(KNOW_BEGIN,1);tail=rest.split(KNOW_END,1)[1]
            new=head+card+tail
        elif KNOW_BEGIN in text or KNOW_END in text:continue  # 标记被手改坏了，不覆盖
        elif text.count('<!-- AUTO BEGIN -->')==1:
            head,tail=text.split('<!-- AUTO BEGIN -->',1);new=head+card+'\n\n<!-- AUTO BEGIN -->'+tail
        else:new=text.rstrip('\n')+'\n\n'+card+'\n'
        if new!=text:atomic(note,new);done.append(str(note))
    return done

def attention(rows,entries=None,store=None,limit=8):
    """一百个项目只留四档，并列出写明的交叉：谁等谁、同一件、同一个仓库。"""
    import re
    names=[r.get('项目') or '' for r in rows if r.get('项目')]
    waits=[]
    mentioned={}
    for r in rows:
        name=r.get('项目') or ''
        blob=' '.join(str(r.get(k) or '') for k in ('等谁','卡在'))
        for other in _mentioned(blob,names,name):
            waits.append((name,other));mentioned.setdefault(other,[]).append(name)
    pair=set(waits);seen=set();mutual=[]
    for a,b in waits:
        key=tuple(sorted((a,b)))
        if (b,a) in pair and key not in seen:
            seen.add(key);mutual.append(sorted((a,b)))
    def age(r):
        return r.get('停了几天') if isinstance(r.get('停了几天'),int) else -1
    piles={k:[] for k in ('等你','堵住别人','现在能做','先别动')}
    for r in rows:
        name=r.get('项目') or ''
        nxt=' '.join(str(r.get('下一步') or '').split())
        item={'项目':name,'下一步':_clip(nxt,120),'等谁':r.get('等谁') or '','卡在':_clip(r.get('卡在') or '',80),'停了几天':r.get('停了几天')}
        who=' '.join(str(r.get('等谁') or '').split())
        blob=' '.join(str(r.get(k) or '') for k in ('等谁','卡在'))
        held=any(mark in nxt or mark in who for mark in HOLD_MARKS)
        waiting_other=bool(_mentioned(blob,names,name))
        if (r.get('状态') or '').startswith(('暂缓','待启动')):pile='先别动'
        elif waiting_on_owner(r,store):pile='等你'
        elif owner_quiet(r,store):pile='先别动'
        elif name in mentioned:pile='堵住别人'
        elif waiting_other or held or not nxt or nxt in ('无','待确认'):pile='先别动'
        else:pile='现在能做'
        piles[pile].append((age(r),item))
    out={}
    for key,items in piles.items():
        ordered=[item for _,item in sorted(items,key=lambda x:x[0],reverse=True)]
        out[key]=ordered[:limit];out[key+'数']=len(ordered)
    marks={}
    for r in rows:
        text=' '.join(str(r.get(k) or '') for k in ('下一步','等谁','卡在','阶段'))
        for m in re.finditer(r'PR\s*#?\s*(\d+)|(task-[0-9a-fA-F]{8,})',text):
            mark='PR'+m.group(1) if m.group(1) else m.group(2).lower()
            found=marks.setdefault(mark,[])
            if r.get('项目') and r['项目'] not in found:found.append(r['项目'])
    same=[{'标记':mark,'项目':projects} for mark,projects in sorted(marks.items()) if len(projects)>=2]
    tops={}
    for e in entries or []:
        path=e.get('path')
        if not path:continue
        try:
            ran=subprocess.run(['git','-C',str(path),'rev-parse','--show-toplevel'],capture_output=True,text=True,timeout=5)
            text=_git_text(ran)
            top=str(Path(text).resolve()) if text else ''
        except (OSError,subprocess.SubprocessError,ValueError):top=''
        if not top:continue
        found=tops.setdefault(top,[])
        if e.get('name') and e['name'] not in found:found.append(e['name'])
    clash=[{'项目':projects} for projects in tops.values() if len(projects)>=2]
    out['谁等谁']=[{'等':a,'被等':b} for a,b in waits[:limit]];out['谁等谁数']=len(waits)
    out['互相等']=mutual[:limit];out['互相等数']=len(mutual)
    out['同一件']=same[:limit];out['同一件数']=len(same);out['撞车']=clash[:limit];out['撞车数']=len(clash)
    return out

# 「等谁」里点到用户、但写明用户已经答过或不用催的，不算等用户拍板。
QUIET_MARKS=('不催','不用催','已答','先不','日后','明确恢复','不再等','不用等')

def _owner_parts(row,store=None):
    """把「等谁」按分号、句号切成几件事，挑出点到用户的那几件。"""
    import re
    name=row.get('_owner_name','')
    if store is not None:
        from .settings import load as settings_load
        name=settings_load(store).get('owner_name','')
    aliases=[name]+re.findall(r'[（(]([^）)]+)[）)]',name)
    keys=[k for k in ['OWNER','你','用户',*aliases] if k]
    parts=[x for x in re.split(r'[；;。\n]',row.get('等谁') or '') if x.strip()]
    return [x for x in parts if any(k in x for k in keys)]

def waiting_on_owner(row,store=None):
    """真在等用户拍板：点到用户的那件事里没写「已答、不催、先不」之类。"""
    return any(not any(m in x for m in QUIET_MARKS) for x in _owner_parts(row,store))

def owner_quiet(row,store=None):
    """点到了用户，但每件都写明已答或不用催：单独列出来，不算等你拍板，也不催。"""
    parts=_owner_parts(row,store)
    return bool(parts) and not waiting_on_owner(row,store)

def brief(rows):
    """2–4 句大白话：几个在动、几件等用户、哪个停得最久。"""
    active=[r for r in rows if not (r['状态'] or '').startswith(('暂缓','待启动'))]
    paused=[r for r in rows if (r['状态'] or '').startswith('暂缓')]
    waiting=[r['项目'] for r in active if waiting_on_owner(r)]
    quiet=[r['项目'] for r in active if owner_quiet(r)]
    s=[f'{len(rows)} 个项目，{len(active)} 个在动'+(f'、{len(paused)} 个暂缓' if paused else '')+'。']
    note=('（你答过、不用催的：'+'、'.join(quiet)+'）') if quiet else ''
    s.append(('等你拍板的：'+'、'.join(waiting)+note+'。') if waiting else '眼下没有等你拍板的事'+note+'。')
    stalled=goal_stalled(active)
    if stalled:s.append('长期目标两天以上没前进：'+'、'.join(stalled)+'，要说清原因。')
    known=[r for r in active if r['停了几天'] is not None]
    if known:
        r=max(known,key=lambda r:r['停了几天'])
        if r['停了几天']>=STALE_DAYS:s.append(f'{r["项目"]} 已经 {r["停了几天"]} 天没更新。')
    missing=[r['项目'] for r in rows if r['阶段'] in ('缺STATUS.md','待确认')]
    if missing:s.append('进度待确认：'+'、'.join(missing)+'。')
    unreleased=[r['项目'] for r in rows if r.get('开发进度未上线')]
    # 另起一句，不挤掉前面几句：开发分支上做完的不等于能用了
    tail=('开发进度还没上线（看板里标了 ⚠，以「运行版本」为准）：'+'、'.join(unreleased)+'。') if unreleased else ''
    return ''.join(s[:4])+tail

GOAL_LOG='goal:progress'
GOAL_STALL_DAYS=2

def goal_numbers(text):
    """「长期目标：完成 x 块 / 共 y 块；本期…；卡在…」里取出 x、y；没写成这个样子的返回 None。"""
    import re
    m=re.search(r'完成\s*(\d+)\s*块?\s*[/／]\s*共\s*(\d+)\s*块?',text or '')
    return (int(m[1]),int(m[2])) if m else None

def track_goals(store,rows,today=None,write=False):
    """记下每个项目长期目标的「完成块数」从哪天起没变；write=True（后台刷新看板）时才写记录。
    按项目 id 记（不按名字，免得同名项目互相覆盖）；只看完成数，只改总块数不算前进也不重新计时；
    已经全部完成的不算停滞；暂缓、待启动时记下「暂停」，第一次看到恢复就从那天重新算（不靠暂停期间天天刷新）。
    给每行补「目标没前进天数」，返回在动项目的长期目标一览。"""
    history=dict(store.get(GOAL_LOG,{}) or {})
    day=time.strftime('%Y-%m-%d',time.localtime(today or time.time()))
    changed=False;out=[]
    for r in rows:
        name=r.get('项目') or '';key=r.get('_id') or name;n=goal_numbers(r.get('长期目标'))
        paused=(r.get('状态') or '').startswith(('暂缓','待启动'))
        r['目标没前进天数']=None
        if n:
            h=history.get(key)
            if paused:
                if write and (not h or not h.get('暂停') or h.get('完成')!=n[0] or h.get('共')!=n[1]):
                    history[key]={'完成':n[0],'共':n[1],'since':day,'暂停':True};changed=True
            elif not h or h.get('完成')!=n[0] or h.get('暂停'):
                # 新项目、完成数变了、或者刚从暂缓恢复：都从今天重新算
                if write:history[key]={'完成':n[0],'共':n[1],'since':day};changed=True
                r['目标没前进天数']=0
            else:
                if write and h.get('共')!=n[1]:history[key]=dict(h,共=n[1]);changed=True
                r['目标没前进天数']=None if n[1] and n[0]>=n[1] else _days(h.get('since'),today)
        if paused:continue
        out.append({'项目':name,'长期目标':r.get('长期目标') or '没写','没前进天数':r['目标没前进天数']})
    if write and changed:store.put(GOAL_LOG,history)
    return out

def goal_stalled(rows):
    return [r['项目'] for r in rows if not (r.get('状态') or '').startswith(('暂缓','待启动'))
            and isinstance(r.get('目标没前进天数'),int) and r['目标没前进天数']>=GOAL_STALL_DAYS]

def render(rows):
    lines=['# 全部项目进度（'+time.strftime('%Y-%m-%d %H:%M')+'）','','> 本页由随身开发自动生成，手改会被覆盖；改进度请改各项目的 STATUS.md。','',
           brief(rows),'',
           '> 「开发进度来源」是这一行进度出自哪个分支的哪个提交；「运行版本」是实际装着或部署着的版本（只认记录，没记录写待确认）。标 '+UNRELEASED+' 的：开发分支上做完了，还不能当成已经能用。','',
           '| 项目 | 状态 | 长期目标 | 阶段 | 完成度 | 下一步 | 等谁 | 卡在 | 最后更新 | 功能 ✅/🔄/⏳/⛔ | 开发进度来源 | 运行版本 |','|---|---|---|---|---|---|---|---|---|---|---|---|']
    for r in rows:
        c=r['功能'];f='/'.join(str(c[s]) for s in STATES[:4]) if c else '无功能清单'
        v=(r['完成度'] or '').strip();done=v+'%' if v[:1].isdigit() and not v.endswith(('%','％')) else v
        src=r.get('来源')
        if src in REMOTE_DEV:stage=r['阶段']+'（开发分支上的进度，还没合进主线）'
        else:stage=r['阶段']+('（分支 '+src+'，未合并）' if src not in (None,'工作区')+REMOTE_MAIN else '')
        name=r['项目']+('（'+UNRELEASED+'）' if r.get('开发进度未上线') else '')
        run=r.get('运行版本') or '待确认'
        if r.get('运行版本出处'):run+='（'+r['运行版本出处']+'）'
        if r.get('开发进度未上线'):run=UNRELEASED+'：'+run
        lines.append('| '+' | '.join(x.replace('|','/') for x in [name,r['状态'] or '-',_goal_cell(r),stage,done,r['下一步'] or '-',r['等谁'] or '-',r['卡在'] or '-',r['最后更新'] or '-',f,
                                                                    r.get('开发进度来源说明') or source_text(r.get('开发进度来源')),run])+' |')
    return '\n'.join(lines)+'\n'

def _goal_cell(r):
    text=r.get('长期目标') or '-'
    days=r.get('目标没前进天数')
    return text+(f'（{days} 天没前进）' if isinstance(days,int) and days>=GOAL_STALL_DAYS else '')

def render_pitfalls(traps):
    lines=['# 跨项目避坑（'+time.strftime('%Y-%m-%d %H:%M')+'）','','> 自动汇总各项目 docs/避坑.md；做新需求、开新项目前先看这里。','',
           '| 项目 | 日期 | 主题 | 场景 | 坑 | 经验 | 出处 |','|---|---|---|---|---|---|---|']
    for t in sorted(traps,key=lambda t:t[1],reverse=True):lines.append('| '+' | '.join(t)+' |')
    if not traps:lines.append('| - | - | - | 还没有记录 | - | - | - |')
    return '\n'.join(lines)+'\n'

def _safe_name(name):
    return ''.join('_' if ch in '/\\:' else ch for ch in name).strip() or 'project'

def write_board(store,entries,page,traps):
    """看板写到随身开发自己的 看板/ 目录（Obsidian 直接打开）；整目录由本工具生成。"""
    folder=store.root/BOARD;folder.mkdir(exist_ok=True)
    atomic(folder/'全部项目.md',page);atomic(folder/'跨项目避坑.md',render_pitfalls(traps))
    done=[str(folder/'全部项目.md'),str(folder/'跨项目避坑.md')]
    for e in entries:
        e=_pitfalls_entry(e);sub=folder/_safe_name(e['name']);newer=_newer_remote(e)
        for key,name in SYNC_NAMES.items():
            content=_read(e,key,newer)
            if content is None:
                if (sub/name).exists():os.replace(sub/name,sub/('.'+name+'.已失效'))
                continue
            sub.mkdir(exist_ok=True);atomic(sub/name,content);done.append(str(sub/name))
    return {'board':str(folder),'written':done}

def board_fingerprint(store):
    parts=[]
    for e in load(store):
        e=_pitfalls_entry(e)
        for key in SYNC_NAMES:
            p=e.get(key)
            try:st=Path(p).stat();parts.append(f'{p}:{st.st_mtime_ns}:{st.st_size}')
            except (OSError,TypeError):parts.append(f'{p}:-')
        for group in (REMOTE_MAIN,REMOTE_DEV):
            for ref in group:
                r=subprocess.run(['git','-C',str(e.get('path','.')),'rev-parse','-q','--verify',ref],capture_output=True,text=True)
                text=_git_text(r)
                if text:parts.append(ref+':'+text);break
    return hashlib.sha256('\n'.join(parts).encode()).hexdigest()

def awaiting_team_tasks(store,timeout=60,hours=None):
    """从登记的战队仓库入口只读查「做完待验收」的任务。没有入口时返回空列表；入口失败返回 None。"""
    if hours is None:hours=WAIT_DAYS*24
    for e in load(store):
        if not isinstance(e,dict) or not e.get('path'):continue
        entry=Path(e['path'])/'bin/codex-task'
        if not entry.is_file():continue
        try:
            out=subprocess.run([str(entry),'--task-stale','--hours',str(hours),'--json'],capture_output=True,text=True,timeout=timeout,check=True).stdout
            data=json.loads(out)
        except (OSError,subprocess.SubprocessError,ValueError,TypeError):return None
        tasks=[]
        for owner,group in (data.get('groups') or {}).items():
            for t in group:
                if t.get('status')=='awaiting_acceptance' and t.get('task_id'):
                    tasks.append({**t,'caller':t.get('caller') or owner})
        for t in data.get('unknown_time') or []:
            if t.get('status')=='awaiting_acceptance' and t.get('task_id'):tasks.append(t)
        return tasks
    return []

HOLD_MARKS=('先不合','等总管','等验收','待验收','等批准','等你点头')

def pending_desk(rows,tasks,entries,limit=8,replies=None,waits=None):
    """当天要处理的三堆：刚做完待验收、等总管核、窗口还能继续的下一步。"""
    names={}
    for e in entries or []:
        try:names[str(Path(e.get('path') or '').resolve())]=e.get('name') or ''
        except OSError:continue
    waiting=[]
    for t in tasks or []:
        if t.get('status')!='awaiting_acceptance' or not t.get('task_id'):continue
        workspace=str(t.get('workspace') or '')
        try:project=names.get(str(Path(workspace).resolve()),'') if workspace else ''
        except OSError:project=''
        waiting.append({'项目':project,'任务':t['task_id'],'标题':_clip(t.get('title') or ''),'调用方':t.get('caller') or '','闲置小时':t.get('idle_hours'),'工作区':workspace})
    review=[]
    carry=[]
    for r in rows:
        if (r.get('状态') or '').startswith(('暂缓','待启动')):continue
        blob=' '.join(str(r.get(k) or '') for k in ('下一步','等谁','卡在'))
        if '等总管' in blob:review.append((r.get('停了几天') if isinstance(r.get('停了几天'),int) else -1,{k:r.get(k,'') for k in ('项目','下一步','等谁','卡在')}))
        nxt=' '.join(str(r.get('下一步') or '').split())
        who=' '.join(str(r.get('等谁') or '').split())
        held=any(mark in nxt or mark in who for mark in HOLD_MARKS)
        if nxt and nxt not in ('无','待确认') and not held and not waiting_on_owner(r) and not owner_quiet(r):
            carry.append((r.get('停了几天') if isinstance(r.get('停了几天'),int) else -1,{'项目':r.get('项目',''),'下一步':_clip(nxt,120),'等谁':r.get('等谁') or ''}))
    review=[item for _,item in sorted(review,key=lambda x:x[0],reverse=True)]
    carry=[item for _,item in sorted(carry,key=lambda x:x[0],reverse=True)]
    def idle(item):
        hours=item.get('闲置小时')
        return hours if isinstance(hours,(int,float)) and hours>=0 else 10**9
    fresh=sorted((item for item in waiting if idle(item)<24),key=idle)
    older=sorted((item for item in waiting if idle(item)>=24),key=idle)
    waiting_replies=[item for item in (replies or []) if not item.get('读到')]
    return {'待验收':(fresh+older)[:limit],'待验收数':len(waiting),'待验收没读到':tasks is None,
            '等总管':review[:limit],'等总管数':len(review),'可继续':carry[:limit],'可继续数':len(carry),
            '等回复':waiting_replies[:limit],'等回复数':len(waiting_replies),'等待':(waits or [])[:limit]}

REPLIES='supervisor:replies'

def _reply_view(request,item):
    return {'请求号':request,'对话':item.get('thread') or '','项目':item.get('project') or '','问了什么':item.get('asked') or '','读到':(item.get('read') or {}).get('summary')}

def expect_reply(store,thread,request,project,asked,now=None):
    """登记一条已经发给别的对话、还要读回的消息。同一请求号不重复登记。"""
    thread=' '.join(str(thread or '').split());request=' '.join(str(request or '').split());asked=' '.join(str(asked or '').split())
    if not thread or not request or not asked:raise Blocked('读回要有对话、请求号和问了什么')
    book=store.get(REPLIES,{'items':{}});items=book.setdefault('items',{})
    if request in items and not items[request].get('read'):return {'已有':True,**_reply_view(request,items[request])}
    items[request]={'thread':thread,'project':' '.join(str(project or '').split()),'asked':_clip(asked,200),'at':time.time() if now is None else now,'read':None}
    store.put(REPLIES,book)
    return {'已有':False,**_reply_view(request,items[request])}

def record_reply(store,request,summary,now=None):
    """记下已经读到的回复。没有登记过的请求号不能写成已读。"""
    book=store.get(REPLIES,{'items':{}});request=' '.join(str(request or '').split());item=(book.get('items') or {}).get(request)
    if not item:raise Blocked('没有这笔记回')
    if not str(summary or '').strip():raise Blocked('读回要写下读到了什么')
    item['read']={'at':time.time() if now is None else now,'summary':_clip(summary,200)}
    store.put(REPLIES,book)
    return _reply_view(request,item)

def open_replies(store):
    items=(store.get(REPLIES,{}) or {}).get('items') or {}
    rows=[(item.get('at') or 0,_reply_view(request,item)) for request,item in items.items() if not item.get('read')]
    return [item for _,item in sorted(rows,key=lambda x:x[0])]

def write_reply_desk(store,replies=None):
    """后台每分钟覆盖看板/等回复.md。只列出还没读回的，不发消息，也不读对方对话。"""
    if replies is None:replies=open_replies(store)
    folder=store.root/BOARD;folder.mkdir(exist_ok=True)
    lines=['# 等回复（'+time.strftime('%Y-%m-%d %H:%M')+'）','',
           '> 后台每分钟覆盖这一页。总管窗口读列出的对话最后一条，读到就 reply read。这一页不代替发送。','']
    if not replies:lines.append('没有还没读回的消息。')
    else:
        for item in replies:
            lines.append('- '+(item.get('项目') or '未填项目')+'｜对话 '+(item.get('对话') or '')+'｜请求号 '+(item.get('请求号') or '')+'｜问了：'+(item.get('问了什么') or ''))
    atomic(folder/'等回复.md','\n'.join(lines)+'\n')
    return {'open':len(replies),'path':str(folder/'等回复.md')}

WAITS='supervisor:waits'
CI_GOALS=('等CI','等检查')
HOLD_STATES=('先不合','待批准','等批准')
USER_MARKS=('等合并','问合','等批准','待批准','先不合','等你点头')

def _wait_text(value):
    return ' '.join(str(value or '').split())

def assess_waits(items):
    """判定一份等待清单。不发消息，不关 PR，也不把缺的宿主能力写成已经接通。"""
    if not isinstance(items,list) or not items:raise Blocked('等待清单要有条目')
    prepared=[];blocked=set()
    for raw in items:
        if not isinstance(raw,dict):raise Blocked('等待条目格式不对')
        number,goal,done,back=_wait_text(raw.get('编号')),_wait_text(raw.get('目标')),_wait_text(raw.get('结束条件')),_wait_text(raw.get('回传位置'))
        if not number or not goal or not done or not back:raise Blocked('监视要有编号、目标、结束条件和回传位置')
        state=_wait_text(raw.get('状态'));span=_wait_text(raw.get('跨度') or '短')
        if span not in ('短','长'):raise Blocked('跨度只分短任务和长任务')
        depends=[_wait_text(item) for item in (raw.get('依赖') or [])]
        if any(not item for item in depends):raise Blocked('依赖编号不能空')
        hold=state in HOLD_STATES or goal in HOLD_STATES
        machine=goal in CI_GOALS or goal.startswith('等CI')
        if machine and any(mark in done for mark in ('合并','问合')):done='CI 通过'
        who='机器' if machine else '用户' if hold or any(mark in goal for mark in USER_MARKS) else _wait_text(raw.get('等谁'))
        if who not in ('机器','用户'):raise Blocked('要写明等机器还是等用户决定')
        if hold:
            problem=_wait_text(raw.get('问题')) or state or goal
            resume=_wait_text(raw.get('恢复条件'))
            if not problem or not resume:raise Blocked('解除监视要写下问题和恢复条件')
            watch='解除'
        elif machine and state in ('CI通过','检查通过','CI 通过'):watch,problem,resume='可收尾','',''
        else:
            watch='监视中';problem=_wait_text(raw.get('问题'));resume=_wait_text(raw.get('恢复条件'))
        if span=='短':returned='已读回' if raw.get('已读') is True else '等读回'
        else:returned='已接通' if raw.get('能唤醒') is True else '未接通'
        if raw.get('阻塞') is True:blocked.add(number)
        prepared.append({'编号':number,'等谁':who,'监视':watch,'目标':goal,'结束条件':done,'回传位置':back,
                         '改成等合并':False,'保留PR':bool(_wait_text(raw.get('pr'))),'关闭PR':False,
                         '问题':problem,'恢复条件':resume,'回传':returned,'后台继续':False,'用户催算完成':False,
                         '依赖':depends,'已授权':raw.get('已授权') is True})
    known={row['编号'] for row in prepared}
    if len(known)!=len(prepared):raise Blocked('等待编号不能重复')
    for row in prepared:
        if any(dep not in known for dep in row['依赖']):raise Blocked('依赖的编号不在这份清单里')
    held=set(blocked);changed=True
    while changed:
        changed=False
        for row in prepared:
            if row['编号'] not in held and any(dep in held for dep in row['依赖']):
                held.add(row['编号']);changed=True
    actionable=[]
    for row in prepared:
        row['被拖住']=row['编号'] in held and row['编号'] not in blocked
        if row['已授权'] and row['编号'] not in held and row['监视']!='解除':actionable.append(row['编号'])
    return {'项':prepared,'可做':actionable,'未接通':[row['编号'] for row in prepared if row['回传']=='未接通']}

def save_waits(store,items):
    found=assess_waits(items)
    store.put(WAITS,found)
    return found

def open_waits(store):
    saved=store.get(WAITS) or {}
    return list(saved.get('项') or [])

ARCHIVE_DAYS=30

def archive_overdue(now=None):
    """~/归档/<年-月>/ 下放满 30 天的东西有几样、多大。只读；删不删由用户点头（规矩【只移不删】）。"""
    home=Path(os.environ.get('PORTABLE_HOME') or Path.home());root=home/'归档'
    now=time.time() if now is None else now
    if not root.is_dir() or root.is_symlink():return None
    count=0;size=0
    for month in root.iterdir():
        if month.is_symlink() or not month.is_dir():continue
        for item in month.iterdir():
            try:
                if now-item.lstat().st_mtime<ARCHIVE_DAYS*86400:continue
                count+=1
                if item.is_symlink() or item.is_file():size+=item.lstat().st_size;continue
                for base,_,files in os.walk(item):
                    for f in files:
                        try:size+=os.lstat(os.path.join(base,f)).st_size
                        except OSError:pass
            except OSError:continue
    return (count,size) if count else None

WORKTREE_STALE_DAYS=14
WORKTREE_SHOW=3

def _worktrees(root):
    """git worktree list --porcelain 拆成一条条登记（只读）。"""
    items=[];cur=None
    for line in _run_git(root,'worktree','list','--porcelain').splitlines():
        if line.startswith('worktree '):cur={'path':line[9:]};items.append(cur)
        elif cur is not None and line.strip():
            k,_,v=line.partition(' ');cur[k]=v or True
    return items

def prune_missing_worktrees(root):
    """清掉「目录已经没了」的工作树登记：只动 .git/worktrees 里的登记，不碰任何文件和分支；锁住的不动。
    清之前、清之后各读一遍登记，只许标着 prunable 的消失。返回 (清掉几条, 问题说明或 None)。"""
    before=_worktrees(root)
    prunable=[w for w in before if w.get('prunable') and not w.get('locked')]
    if not prunable:return 0,None
    if any(Path(w['path']).exists() for w in prunable):return 0,'有登记标着可清但目录还在，没动'
    # 外接盘上的目录可能只是暂时没插：git 的 prune 一清一整批，有一条在外接盘就整仓跳过，只提醒（清理计划 v3.1 第三节）
    if any(str(w['path']).startswith('/Volumes/') for w in prunable):return 0,'有登记在外接盘上（可能只是没插），这次整仓没清'
    try:
        r=subprocess.run(['git','-C',str(root),'worktree','prune','--expire=now'],capture_output=True,text=True,timeout=60)
    except (OSError,subprocess.SubprocessError) as e:return 0,'清理没跑成：'+type(e).__name__
    if r.returncode:return 0,'清理没跑成：'+(r.stderr or '').strip()[-120:]
    after={w['path'] for w in _worktrees(root)}
    gone={w['path'] for w in before}-after
    allowed={w['path'] for w in prunable}
    unexpected=gone-allowed
    return len(gone&allowed),('意外少了登记：'+'、'.join(sorted(unexpected)) if unexpected else None)

def _merge_targets(root):
    """拿来判断「合进去没有」的线：远端和本地的主线、开发分支里这台机器上有的。"""
    refs=list(NEWER_REFS)+['refs/heads/main','refs/heads/development']
    return [ref for ref in refs if _run_git(root,'rev-parse','-q','--verify',ref+'^{commit}')]

def worktree_lists(root,registered,now):
    """只读：已合并且干净的工作树、超过 14 天没动又没合并的分支或工作树。看不出主线时返回 None。"""
    targets=_merge_targets(root)
    if not targets:return None
    merged=lambda commit:any(_is_ancestor(root,commit,t) for t in targets)
    try:here=Path(registered).resolve()
    except OSError:here=Path(registered)
    trees=_worktrees(root)
    done=[];stale=[]
    for w in trees[1:]:  # 第一条是主工作树，不算
        if w.get('bare') or w.get('prunable') or w.get('locked') or not w.get('HEAD'):continue
        path=Path(w['path'])
        if not path.is_dir():continue
        try:
            if path.resolve()==here:continue  # 登记的项目目录本身不列
        except OSError:pass
        if merged(w['HEAD']):
            if not _run_git(path,'status','--porcelain',timeout=60):done.append(w['path'])
        elif w.get('detached'):
            when=_run_git(root,'log','-1','--format=%ct',w['HEAD'])
            if when.isdigit() and now-int(when)>=WORKTREE_STALE_DAYS*86400:stale.append(w['path']+'（没挂分支）')
    for line in _run_git(root,'for-each-ref','--format=%(refname:short)%09%(objectname)%09%(committerdate:unix)','refs/heads/').splitlines():
        parts=line.split('\t')
        if len(parts)!=3 or parts[0] in ('main','development') or not parts[2].isdigit():continue
        if now-int(parts[2])<WORKTREE_STALE_DAYS*86400 or merged(parts[1]):continue
        stale.append(parts[0])
    return done,stale

def _few(items):
    return '、'.join(items[:WORKTREE_SHOW])+(' 等' if len(items)>WORKTREE_SHOW else '')

def worktree_reminders(store,rows,now,week):
    """每周体检里的工作树三件事，返回 [(键, 提醒)]；同一周每个项目每件只提醒一次。
    只有第一件会写：清掉目录已经没了的登记（git worktree prune）；同一周已经清过、提醒过的不再跑。"""
    told=store.get('weekly:told') or {}
    found=[];seen=set()
    for e,r in zip(load(store),rows):
        root=Path(e.get('path') or '')
        if not (root/'.git').exists():continue
        common=_run_git(root,'rev-parse','--git-common-dir')
        if not common:continue
        common=str((root/common).resolve())
        if common in seen:continue  # 几个项目共用一个仓库时只看一次
        seen.add(common)
        key=e.get('id') or e['name']
        prune_key=f'wt-prune:{key}:{week}'
        if prune_key not in told:
            count,problem=prune_missing_worktrees(root)
            if count or problem:
                found.append((prune_key,f'{e["name"]}：自动清掉了 {count} 条目录已经不在的工作树登记（锁住的没动，没碰任何文件和分支）'+('；'+problem if problem else '')))
        if (r.get('状态') or '').startswith('暂缓'):continue
        lists=worktree_lists(root,e['path'],now)
        if not lists:continue
        done,stale=lists
        if done:found.append((f'wt-merged:{key}:{week}',f'{e["name"]}有 {len(done)} 个工作树已经合并、也没有未提交改动（如 {_few(done)}）：要收掉说「帮我收已合并的工作树」，只移不删'))
        if stale:found.append((f'wt-stale:{key}:{week}',f'{e["name"]}有 {len(stale)} 处超过 {WORKTREE_STALE_DAYS} 天没动、也没合进主线或开发分支（如 {_few(stale)}）：还要不要，回一句即可'))
    return found

def _check_ref(root):
    for ref in REMOTE_DEV+REMOTE_MAIN:
        if not subprocess.run(['git','-C',str(root),'rev-parse','-q','--verify',ref+'^{commit}'],capture_output=True,timeout=30).returncode:return ref
    return None

def progress_lag(entry,now):
    """开发分支（没有就主线）上，最后一次改 STATUS.md 之后合进了几个「不只改文档」的提交；最早那个超过 1 天才算。只看 Git 历史，不靠本机记录。"""
    from .ci_docs_only import _is_doc_path
    root=Path(entry['path'])
    if not (root/'.git').exists():return None
    try:
        ref=_check_ref(root)
        if not ref:return None
        last=_git(root,'log','-1','--format=%H',ref,'--','STATUS.md',timeout=30).stdout.decode().strip()
        if not last:return None
        # 中文文件名不能让 git 转义（避坑 2026-11-14「列文件名要加 -z」同一类）
        out=_git(root,'-c','core.quotepath=off','log','--no-merges','--format=%x01%ct','--name-only',last+'..'+ref,timeout=60).stdout.decode('utf-8','replace')
    except (subprocess.SubprocessError,OSError,ValueError):return None
    n=0;oldest=None
    for block in out.split('\x01')[1:]:
        lines=[x for x in block.splitlines() if x.strip()]
        if not lines:continue
        when=int(lines[0]);files=lines[1:]
        if files and any(not _is_doc_path(f) for f in files):
            n+=1;oldest=when if oldest is None else min(oldest,when)
    if n and oldest is not None and now-oldest>86400:return n,ref
    return None

def docs_gate_drift(entry):
    """仓库里照抄的 tools/ci/docs_only.py 和随身开发原件不一致就返回 True；仓库没接轻量 CI 返回 False。"""
    root=Path(entry['path'])
    if not (root/'.git').exists():return False
    try:
        ref=_check_ref(root) or 'HEAD'
        r=subprocess.run(['git','-C',str(root),'show',ref+':tools/ci/docs_only.py'],capture_output=True,timeout=30)
        if r.returncode:return False
        from importlib import resources
        original=resources.files('portable').joinpath('ci_docs_only.py').read_bytes()
        return r.stdout!=original
    except Exception:return False

MAX_RECENT_UPDATES=20
MAX_AGENTS_LINES=40

def count_recent_updates(text):
    """数 STATUS.md 里「## 近期动态」下的顶层条目数（以 '- ' 开头、无前导缩进的行）。"""
    in_sec=False;count=0
    for line in (text or '').splitlines():
        if line.startswith('## '):
            if line.strip()=='## 近期动态':in_sec=True;continue
            if in_sec:break
        if not in_sec:continue
        if line.startswith(('较早记录见','较早动态')):break
        if line.startswith('- '):count+=1
    return count

def trim_status_updates(repo,max_entries=MAX_RECENT_UPDATES,stamp=None):
    """当 STATUS.md 的「## 近期动态」超过 max_entries 条时，自动把第 max_entries+1 条起移进 docs/历史/（只移不删）。"""
    repo=Path(repo);status_path=repo/'STATUS.md'
    if not status_path.is_file():raise Blocked('找不到 STATUS.md：'+str(status_path))
    text=status_path.read_text(encoding='utf-8')
    lines=text.splitlines()
    sec_start=None;sec_end=len(lines)
    for i,line in enumerate(lines):
        if line.strip()=='## 近期动态':sec_start=i+1;continue
        if sec_start is not None and (line.startswith('## ') or line.startswith(('较早记录见','较早动态'))):
            sec_end=i;break
    if sec_start is None:return {'total':0,'kept':0,'moved':0,'archive_path':None}
    body=lines[sec_start:sec_end]
    entries=[];cur=None
    leading=[]
    for line in body:
        if line.startswith('- '):
            cur=[line];entries.append(cur)
        elif cur is not None:
            cur.append(line)
        else:
            leading.append(line)
    total=len(entries)
    if total<=max_entries:return {'total':total,'kept':total,'moved':0,'archive_path':None}
    kept=entries[:max_entries];moved=entries[max_entries:]
    while kept and kept[-1] and not kept[-1][-1].strip():kept[-1].pop()
    stamp=stamp or time.strftime('%Y%m%d')
    hist_dir=repo/'docs/历史';hist_dir.mkdir(parents=True,exist_ok=True)
    rel_hist=f'docs/历史/STATUS-{stamp}-动态归档.md'
    hist_path=repo/rel_hist
    moved_text='\n'.join('\n'.join(item).rstrip() for item in moved)+'\n'
    header=f'# STATUS 历史动态归档（{stamp} 移出，原样保留）\n\n> 由随身开发按【收工必写】「近期动态最多 {max_entries} 条」从 STATUS.md 移出。\n\n'
    if hist_path.is_file():
        old_hist=hist_path.read_text(encoding='utf-8').rstrip()+'\n\n'+moved_text
        atomic(hist_path,old_hist)
    else:
        atomic(hist_path,header+moved_text)
    new_body=leading+[line for item in kept for line in item]+['']
    tail=lines[sec_end:]
    link_note=f'[{stamp} 动态归档]({rel_hist})'
    if tail and tail[0].startswith(('较早记录见','较早动态')):
        if rel_hist not in tail[0]:
            tail[0]=tail[0].rstrip('。.')+'、'+link_note+'。'
    else:
        new_body+=[f'较早记录见{link_note}。','']
    new_lines=lines[:sec_start]+new_body+tail
    atomic(status_path,'\n'.join(new_lines).rstrip()+'\n')
    return {'total':total,'kept':max_entries,'moved':len(moved),'archive_path':rel_hist}

def weekly_check(store,today=None,tasks=None):
    """每周体检：超 14 天没更新、等用户超 7 天、工作法空了满 14 天、归档里放满 30 天的（每周提醒一次）、需求稿 30 天没改而代码改了 20 次以上、战队任务做完超 7 天没人验收；
    工作树三件（每周每项目各一次）：自动清掉目录已不在的登记、列出已合并且干净的工作树、列出超 14 天没动又没合并的分支或工作树。每条只提醒一次（记住已提醒的）。

    tasks 是待验收任务列表（每项含 task_id、title、status、idle_hours）；不传则不检查这一项。"""
    rows=overview(store,today=today)['projects']
    told=store.get('weekly:told') or {}
    found=[]
    for r in rows:
        if (r['状态'] or '').startswith('暂缓') or r['停了几天'] is None:continue
        if r['停了几天']>=STALE_DAYS:found.append((r['项目']+':stale:'+r['最后更新'],f'{r["项目"]} 已 {r["停了几天"]} 天没更新进度'))
        elif waiting_on_owner(r,store) and r['停了几天']>=WAIT_DAYS:found.append((r['项目']+':wait:'+r['最后更新'],f'{r["项目"]} 等你 {r["停了几天"]} 天了：{r["等谁"]}'))
    now=time.time() if today is None else today
    for e,r in zip(load(store),rows):
        if (r['状态'] or '').startswith('暂缓'):continue
        notebook=Path(e['path'])/'docs/工作法.md'
        if not notebook.is_file():continue
        age=int((now-notebook.stat().st_mtime)//86400)
        if age<STALE_DAYS:continue
        separator=False;has_data=False
        for line in notebook.read_text(encoding='utf-8').splitlines():
            line=line.strip()
            if not line.startswith('|'):continue
            cells=line.strip('|').split('|')
            if all(cell.strip().strip(':').strip('-')=='' and '-' in cell for cell in cells):
                separator=True
            elif separator:
                has_data=True;break
        if not has_data:
            found.append((e['name']+':learn-empty',f'{e["name"]}的工作法本子建了 {age} 天还是空的：被纠正或踩坑时记一笔'))
    for e,r in zip(load(store),rows):
        if (r['状态'] or '').startswith('暂缓'):continue
        drift=prd_drift(e,now)
        if drift:
            age,commits,mark,ref=drift
            found.append((e['name']+':prd-stale:'+mark,f'{e["name"]}的需求稿 {age} 天没改，之后仓库有 {commits} 次提交（按{ref}）：对一遍，过期的改掉（错的需求稿比没有更害人）'))
    for t in tasks or ():
        if t.get('status')!='awaiting_acceptance':continue
        days=int((t.get('idle_hours') or 0)//24)
        if days<WAIT_DAYS:continue
        found.append(('task:'+t['task_id']+':awaiting',f'战队任务「{t.get("title") or t["task_id"]}」做完 {days} 天了还没人验收'))
    from .review import weekly_summary
    reviews=weekly_summary(store,now)
    if reviews:found.append(('review:'+time.strftime('%G-%V',time.localtime(now)),reviews))
    for e,r in zip(load(store),rows):
        if (r['状态'] or '').startswith('暂缓'):continue
        week=time.strftime('%G-%V',time.localtime(now))
        lag=progress_lag(e,now)
        if lag:
            n,ref=lag
            found.append((e['name']+':progress-lag:'+week,f'{e["name"]}：{ref} 上最后一次改 STATUS.md 之后，又合进了 {n} 个功能改动，超过 1 天进度表还没更新；另开文档 PR 补上（见【收工必写】）'))
        if docs_gate_drift(e):
            found.append((e['name']+':docs-gate-drift:'+week,f'{e["name"]}：仓库里的 tools/ci/docs_only.py 和随身开发的原件不一致，「只改文档」两边判法可能不同；照原件改回'))
        root_p=Path(e['path'])
        agents_f=root_p/'AGENTS.md'
        if agents_f.is_file() and not (root_p/'发行.json').is_file():
            try:
                alines=agents_f.read_text(encoding='utf-8',errors='replace').splitlines()
                if len(alines)>MAX_AGENTS_LINES:
                    found.append((e['name']+':agents-lines:'+week,f'{e["name"]}：AGENTS.md 有 {len(alines)} 行（规矩上限 {MAX_AGENTS_LINES} 行），请精简到 {MAX_AGENTS_LINES} 行以内'))
            except OSError:pass
        status_f=Path(e.get('status_path') or (root_p/'STATUS.md'))
        if status_f.is_file():
            try:
                ucount=count_recent_updates(status_f.read_text(encoding='utf-8',errors='replace'))
                if ucount>MAX_RECENT_UPDATES:
                    found.append((e['name']+':status-over20:'+week,f'{e["name"]}：STATUS.md 近期动态有 {ucount} 条（规矩上限 {MAX_RECENT_UPDATES} 条），旧记录请移进 docs/历史/'))
            except OSError:pass
    old_archive=archive_overdue(now)
    if old_archive:
        n,size=old_archive
        found.append(('archive-old:'+time.strftime('%G-%V',time.localtime(now)),f'归档里有 {n} 样东西放满 30 天了（约 {size/1024**3:.1f}G），回「删归档」我先列清单给你确认，确认后才删'))
    found+=worktree_reminders(store,rows,now,time.strftime('%G-%V',time.localtime(now)))
    new=[(k,msg) for k,msg in found if k not in told]
    for k,_ in new:told[k]=time.strftime('%Y-%m-%d')
    store.put('weekly:told',told)
    return [msg for _,msg in new]

def prd_drift(entry,now):
    """需求稿（docs/PRD.md）最后一次提交距今几天、之后仓库又有几次提交；够不上「30 天没改且之后 20 次以上提交」就返回 None。
    在工作区 HEAD、开发分支、主线里挑需求稿最新的那条线算，免得远端已经改了还报过期；浅克隆、需求稿没进 Git 的判断不了，返回 None。"""
    root=Path(entry['path']);prd=Path(entry.get('prd_path') or root/'docs/PRD.md')
    if not prd.is_file() or not (root/'.git').exists():return None
    try:
        rel=str(prd.relative_to(root))
        if _git(root,'rev-parse','--is-shallow-repository',timeout=30).stdout.decode().strip()=='true':return None
        lines=[]
        for ref in ('HEAD',)+REMOTE_DEV+REMOTE_MAIN:
            if subprocess.run(['git','-C',str(root),'rev-parse','-q','--verify',ref+'^{commit}'],capture_output=True,timeout=30).returncode:continue
            last=_git(root,'log','-1','--format=%H %ct',ref,'--',rel,timeout=30).stdout.decode().split()
            if len(last)!=2:continue
            count=int(_git(root,'rev-list','--count','--no-merges',last[0]+'..'+ref,timeout=30).stdout.decode().strip() or 0)
            lines.append((int(last[1]),count,ref,last[0]))
        if not lines:return None
        # 需求稿最新的那一版为准；同一版在几条线上都有时，取之后提交最多（走得最远）的那条
        newest=max(x[0] for x in lines)
        when,commits,ref,sha=max(x for x in lines if x[0]==newest)
    except (subprocess.SubprocessError,ValueError,OSError):return None
    age=int((now-when)//86400)
    if age<PRD_STALE_DAYS or commits<PRD_STALE_COMMITS:return None
    return age,commits,sha[:12],('工作区' if ref=='HEAD' else ref)

def _git(path,*args,out=None,timeout=None):
    return subprocess.run(['git','-C',str(path),*args],check=True,stdout=out or subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)

def _safe_remote_url(src):
    """项目连着的在线仓库地址（恢复时从这里拉完整历史）。地址里带账号密码的不记，免得把凭据写进备份。"""
    from urllib.parse import urlsplit
    try:
        names=_git(src,'remote').stdout.decode().split()
        if not names:return None
        url=_git(src,'remote','get-url','origin' if 'origin' in names else names[0]).stdout.decode().strip()
    except Exception:return None
    if '://' in url:
        p=urlsplit(url)
        if p.password or (p.username and p.scheme in ('http','https')):return None
    return url or None


def backup_external(store,destination,kind='daily',untracked_limit=20*1024*1024):
    """备份各项目仓库在线仓库里没有的东西：未提交改动、未跟踪文件，以及没推上去的提交（git bundle）。
    连着在线仓库的项目，每天只打「没推上去的提交」；每周（以及没连在线仓库的项目每次）打完整历史，防在线仓库出事。
    登记 backup=full 的整目录复制。只读源目录。"""
    entries=load(store)
    if not entries:return None
    destination=Path(destination).absolute()
    dest=destination/('外部-'+kind+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:8])
    stage=destination/('.外部-'+uuid.uuid4().hex[:8])
    stage.mkdir(parents=True)
    report=[]
    def recover(path):
        if path.exists():
            recovery=destination/'恢复区';recovery.mkdir(exist_ok=True)
            os.rename(path,recovery/(path.name+'-'+uuid.uuid4().hex[:8]))
    try:
        for e in entries:
            src=Path(e['path']);name=e['id'];target=stage/name
            omitted={'忽略文件数':0,'超过20MB跳过的文件':[],'非Git项目':False}
            try:
                if src.is_dir() and (src/'.git').exists():
                    omitted['忽略文件数']=len(list(filter(None,_git(src,'ls-files','-o','-i','--exclude-standard','-z').stdout.split(b'\0'))))
                if e.get('backup')=='full':
                    shutil.copytree(src,target,symlinks=True,ignore=shutil.ignore_patterns('.DS_Store'))
                    report.append({'项目':e['name'],'方式':'整个目录'});continue
                if not src.is_dir():raise Blocked('登记目录不存在')
                if not (src/'.git').exists():
                    omitted['非Git项目']=True
                    report.append({'项目':e['name'],'方式':'跳过：不是 Git 仓库','没备的':omitted});continue
                # Validate Git before classifying an unborn repository; corrupt Git must fail.
                top=_git(src,'rev-parse','--show-toplevel').stdout.decode().strip()
                if Path(top).resolve()!=src.resolve():raise Blocked('Git 仓库位置不符')
                # Daily backup stays lightweight; weekly validates all historical objects.
                checked=_git(src,'fsck','--full',timeout=300) if kind=='weekly' else None
                refs=_git(src,'for-each-ref','--format=%(objectname)').stdout.strip()
                head=_git(src,'symbolic-ref','-q','HEAD').stdout.strip() if not refs else b''
                if not refs and head:
                    if checked is not None and checked.stdout.strip():raise Blocked('空仓库仍有待恢复的历史对象')
                    shutil.copytree(src,target,symlinks=True)
                    report.append({'项目':e['name'],'方式':'整个目录（空仓库）'});continue
                target.mkdir(parents=True)
                remotes=_git(src,'for-each-ref','--format=%(refname)','refs/remotes').stdout.strip()
                online=_safe_remote_url(src)
                if kind=='daily' and remotes:
                    # 在线仓库已有的历史不再每天复制一遍：只打没推上去的提交，一个都没有就记一笔
                    ahead=int(_git(src,'rev-list','--count','--all','--not','--remotes').stdout.strip() or 0)
                    if ahead:_git(src,'bundle','create',str(target/'未推送提交.bundle'),'--all','--not','--remotes');way=f'只备在线仓库没有的：{ahead} 个没推上去的提交 + 未提交改动'
                    else:(target/'全部已推送.txt').write_text('这天所有提交都已在在线仓库里；完整历史见最近一次每周备份或在线仓库。\n',encoding='utf-8');way='只备在线仓库没有的：提交全部已推送，只备未提交改动'
                else:
                    _git(src,'bundle','create',str(target/'repo.bundle'),'--all');way='git bundle（完整历史）+ 未提交改动'

                with open(target/'未提交改动.patch','wb') as handle:_git(src,'diff','HEAD','--binary',out=handle)
                listed=_git(src,'ls-files','-o','--exclude-standard','-z').stdout.split(b'\0')
                skipped=[]
                for raw in filter(None,listed):
                    rel=raw.decode('utf-8','surrogateescape');p=src/rel
                    if p.is_symlink() or not p.is_file():continue
                    if p.stat().st_size>untracked_limit:skipped.append(rel);continue
                    q=target/'未跟踪文件'/rel;q.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,q)
                omitted['超过20MB跳过的文件']=skipped
                report.append({'项目':e['name'],'编号':name,'方式':way,'在线仓库':online,'超限未备份':skipped,'没备的':omitted})
            except subprocess.TimeoutExpired:
                report.append({'项目':e['name'],'方式':'失败：检查超时，已跳过本周全量检查','失败':True})
                recover(target)
            except Exception:
                # Keep diagnostics plain and avoid leaking paths or command output.
                reason='登记目录不存在' if not src.is_dir() else '项目仓库无法读取，请帮我检查这个项目后重试'
                report.append({'项目':e['name'],'方式':'失败：'+reason,'失败':True})
                recover(target)
        for item in report:item.setdefault('没备的',{'忽略文件数':0,'超过20MB跳过的文件':[],'非Git项目':False})
        atomic(stage/'外部备份.json',json.dumps({'schema':1,'kind':kind,'created':time.time(),'projects':report},ensure_ascii=False,indent=2))
        os.rename(stage,dest)
    finally:
        recover(stage)
    from .distribution import RETAIN,expire_backups
    olds=sorted((p for p in destination.glob('外部-'+kind+'-*') if p.is_dir() and not p.is_symlink() and (p/'外部备份.json').is_file()),key=lambda p:p.name,reverse=True)[RETAIN[kind]:]
    gone=expire_backups(destination,olds,'外部备份.json')
    return {'backup':str(dest),'projects':report,'failed':[p['项目'] for p in report if p.get('失败')],'删除过期':gone,'permanent_delete':bool(gone)}
