"""给小白的四个帮手：撤回上一个改动、截图看成品、求助包、示例项目。"""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid
import zipfile
from .core import Blocked, atomic, user_home as _home

CHROME=('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome','/Applications/Chromium.app/Contents/MacOS/Chromium','google-chrome','chromium','chromium-browser')

def _entry(store,project):
    from .board import load
    for e in load(store):
        if project in (e['id'],e['name']):return e
    raise Blocked('没登记这个项目：'+project)

def _git(root,*args,check=True,extra_env=None):
    p=subprocess.run(['git','-c','core.hooksPath='+os.devnull,'-c','user.name=随身开发','-c','user.email=portable@localhost','-C',str(root),*args],
                     capture_output=True,text=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0',**(extra_env or {})},timeout=300)
    if check and p.returncode:raise Blocked('Git '+args[0]+'：'+(p.stderr or p.stdout)[:800])
    return p.stdout.strip()

def undo_last(store,project,base=None,transport=None):
    """把 base 上最近一次改动反着做一遍，开成新 PR（不改历史、不碰工作区）；没有远端时留在本地分支。"""
    entry=_entry(store,project);repo=Path(entry['path'])
    if Path(_git(repo,'rev-parse','--show-toplevel')).resolve()!=repo.resolve():raise Blocked('登记目录不是独立仓库根，禁止撤回上层项目')
    from . import remote_publish as rp
    url=rp.remote(repo)
    if url:rp.repository(url)
    base=rp.base_branch(repo,url,base)
    remotes=bool(url)
    def verify_binding():
        if Path(_entry(store,entry['id'])['path']).resolve()!=repo.resolve() or rp.remote(repo)!=url:raise Blocked('撤回目标登记或远端已变化；未写入')
        if Path(_git(repo,'rev-parse','--show-toplevel')).resolve()!=repo.resolve():raise Blocked('撤回仓库身份已变化')
    with tempfile.TemporaryDirectory(prefix='portable-undo-') as tmp:
        work=Path(tmp)/'repo'
        if remotes:
            rp.network(Path(tmp),'clone','-q','--no-tags','--single-branch','--branch',base,url,str(work))
        else:
            url=None;_git(Path(tmp),'clone','-q','--no-local','--branch',base,str(repo),str(work))
        last=_git(work,'rev-parse','HEAD');subject=_git(work,'log','-1','--format=%s')
        if subject.startswith('撤回：'):raise Blocked('最近一次已经是撤回，不再撤回撤回；请说清要撤哪一个')
        parents=_git(work,'log','-1','--format=%P').split()
        _git(work,'revert','--no-edit',*(['-m','1'] if len(parents)>1 else []),last)
        _git(work,'commit','--amend','-q','--date='+_git(work,'show','-s','--format=%cI',last),'-m','撤回：'+subject,'-m','撤回 '+last[:12]+'（随身开发「撤回上一个改动」）',extra_env={'GIT_COMMITTER_DATE':_git(work,'show','-s','--format=%cI',last)})
        topic='undo-'+last[:12]
        verify_binding()
        if url:
            operation='undo:'+__import__('hashlib').sha256((str(repo)+url+base+last).encode()).hexdigest()
            prior=store.get(operation)
            if prior:return prior
            pr=rp.open_pr(work,url,base,topic,'撤回：'+subject[:120],'撤回 '+last,transport,(store,operation+':pending'),verify_binding)
            result={'撤回的改动':subject,'提交':last[:12],'PR':pr,'base':base,'合并后生效':True,'改历史':False}
            store.put(operation,result)
            return result
        existing=_git(repo,'rev-parse','--verify','-q','refs/heads/'+topic,check=False)
        if existing and existing!=_git(work,'rev-parse','HEAD'):raise Blocked('本地撤回分支已有不同提交，保留双方')
        if not existing:_git(work,'push','-q','--force-with-lease=refs/heads/'+topic+':',str(repo),'HEAD:refs/heads/'+topic)
        return {'撤回的改动':subject,'提交':last[:12],'本地分支':topic,'合并后生效':True,'改历史':False}

def _chrome():
    for c in CHROME:
        path=c if os.path.isabs(c) else shutil.which(c)
        if path and os.path.exists(path):return path
    return None

# Chrome 的窗口最窄只有 500 像素：要更窄（比如手机 390）时改走 DevTools 管道，把视口设成要的宽度再截图。
NARROW_BELOW=500

def _shoot_narrow(browser,url,out,width,height,profile,timeout=120):
    """用 --remote-debugging-pipe 打开页面：视口设成 width×height，等页面加载完再截图写到 out。只用标准库。"""
    import base64,fcntl,select
    to_r,to_w=os.pipe();from_r,from_w=os.pipe()
    # 先挪到 10 以上，子进程里再放到 3、4 号（Chrome 约定从 3 读、往 4 写），免得互相覆盖
    hi_r=fcntl.fcntl(to_r,fcntl.F_DUPFD,10);hi_w=fcntl.fcntl(from_w,fcntl.F_DUPFD,10)
    def child():
        os.dup2(hi_r,3);os.dup2(hi_w,4)
    proc=subprocess.Popen([browser,'--headless=new','--disable-gpu','--hide-scrollbars','--no-first-run','--user-data-dir='+profile,
                           '--remote-debugging-pipe',f'--window-size={NARROW_BELOW},{height}','about:blank'],
                          stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,close_fds=False,preexec_fn=child)
    for fd in (to_r,from_w,hi_r,hi_w):os.close(fd)
    deadline=time.monotonic()+timeout;buf=b'';seq=[0];events=[]
    def send(method,params=None,session=None):
        seq[0]+=1;msg={'id':seq[0],'method':method,'params':params or {}}
        if session:msg['sessionId']=session
        os.write(to_w,json.dumps(msg).encode()+b'\0');return seq[0]
    def messages(wait):
        nonlocal buf
        end=min(deadline,time.monotonic()+wait)
        while True:
            while b'\0' in buf:
                raw,buf=buf.split(b'\0',1)
                if raw:yield json.loads(raw)
            left=end-time.monotonic()
            if left<=0:return
            ready,_,_=select.select([from_r],[],[],left)
            if not ready:return
            chunk=os.read(from_r,1<<20)
            if not chunk:raise Blocked('截图失败：浏览器提前退出了')
            buf+=chunk
    def call(method,params=None,session=None):
        want=send(method,params,session)
        for m in messages(deadline-time.monotonic()):
            if m.get('id')==want:
                if 'error' in m:raise Blocked('截图失败：'+method+' '+str(m['error'].get('message',''))[:200])
                return m.get('result') or {}
            events.append(m)
        raise Blocked('截图失败：浏览器 '+str(timeout)+' 秒没响应（'+url+'）')
    try:
        target=call('Target.createTarget',{'url':'about:blank'})['targetId']
        session=call('Target.attachToTarget',{'targetId':target,'flatten':True})['sessionId']
        call('Emulation.setDeviceMetricsOverride',{'width':width,'height':height,'deviceScaleFactor':1,'mobile':False},session)
        call('Page.enable',None,session)
        nav=call('Page.navigate',{'url':url},session)
        if nav.get('errorText'):raise Blocked('截图失败：页面打不开（'+url+'）：'+nav['errorText'])
        loaded=any(e.get('method')=='Page.loadEventFired' for e in events)
        if not loaded:
            for m in messages(30):
                if m.get('method')=='Page.loadEventFired':loaded=True;break
        for _ in messages(1):pass  # 加载完再等一秒，让脚本把页面画完
        data=call('Page.captureScreenshot',{'format':'png'},session)['data']
        out.write_bytes(base64.b64decode(data))
        try:send('Browser.close')
        except OSError:pass
    finally:
        for fd in (to_w,from_r):
            try:os.close(fd)
            except OSError:pass
        try:proc.wait(timeout=10)
        except subprocess.TimeoutExpired:proc.kill();proc.wait(timeout=10)

def screenshot(store,url,name='成品',width=1280,height=900):
    """用本机浏览器无界面打开页面并截图，存到 看板/截图/。打不开就说明原因。
    width/height 是窗口大小（像素），默认 1280×900；看手机样子用 --width 390。"""
    try:width,height=int(width),int(height)
    except (TypeError,ValueError):raise Blocked('截图宽高要写整数，比如 --width 390')
    if not (100<=width<=8000 and 100<=height<=8000):raise Blocked('截图宽高要在 100 到 8000 像素之间')
    browser=_chrome()
    if not browser:raise Blocked('本机没有 Chrome/Chromium，无法自动截图；请装 Chrome 或手动截图')
    folder=store.root/'看板/截图';folder.mkdir(parents=True,exist_ok=True)
    out=folder/(time.strftime('%Y%m%d-%H%M%S')+'-'+''.join(ch if ch.isalnum() or ch in '-_' else '_' for ch in name)[:40]+'.png')
    stderr=''
    with tempfile.TemporaryDirectory(prefix='portable-shot-') as profile:
        if width<NARROW_BELOW:_shoot_narrow(browser,url,out,width,height,profile)
        else:
            p=subprocess.run([browser,'--headless=new','--disable-gpu','--hide-scrollbars','--no-first-run','--user-data-dir='+profile,
                              f'--window-size={width},{height}','--virtual-time-budget=5000','--screenshot='+str(out),url],capture_output=True,text=True,timeout=120)
            stderr=p.stderr or ''
    if not out.is_file() or out.stat().st_size<1000:
        raise Blocked('截图失败：页面可能没跑起来（'+url+'）。'+stderr[-300:])
    return {'截图':str(out),'页面':url,'宽':width,'高':height}

def help_bundle(store,out_dir=None):
    """出问题时一键打包诊断信息：版本、自检、后台事件、看板；全部去掉密钥后再打包。"""
    from .privacy import sanitize,redact_text
    out_dir=Path(out_dir) if out_dir else (_home()/'Desktop' if (_home()/'Desktop').is_dir() else store.root.parent)
    bundle=out_dir/('随身开发求助包-'+time.strftime('%Y%m%d-%H%M%S')+'.zip')
    parts={}
    try:parts['自检.json']=json.dumps(sanitize(store.doctor()),ensure_ascii=False,indent=2,default=str)
    except Exception as e:parts['自检.json']=json.dumps({'自检失败':redact_text(repr(e))},ensure_ascii=False)
    try:
        release=json.loads((store.root/'发行.json').read_text());parts['版本.json']=json.dumps({'version':release.get('version')},ensure_ascii=False)
    except (OSError,ValueError):parts['版本.json']='{"version":"待确认"}'
    rows=store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'runner:%' OR key LIKE 'automation%' OR key='registry'").fetchall()
    parts['后台状态.json']=json.dumps(sanitize({r['key']:r['value'] for r in rows}),ensure_ascii=False,indent=2,default=str)
    for name in ('全部项目.md','提醒.md'):
        p=store.root/'看板'/name
        if p.is_file():parts['看板-'+name]=redact_text(p.read_text(encoding='utf-8',errors='replace'))
    parts['说明.txt']='这是随身开发的求助包，已去掉密码、令牌等密钥。把它发给帮你的人即可。\n生成时间：'+time.strftime('%Y-%m-%d %H:%M')+'\n'
    from .privacy import CredentialContent
    leaked=[k for k,v in parts.items() if CredentialContent().search(v.encode())]
    for k in leaked:parts[k]='（此项疑似含密钥，已整项去掉）'
    out_dir.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(bundle,'w',zipfile.ZIP_DEFLATED) as z:
        for k,v in parts.items():z.writestr(k,v)
    return {'求助包':str(bundle),'内容':sorted(parts),'去掉的项':leaked,'含密钥':False}

EXAMPLE_FILES={
    'docs/工作法.md':'# 示例·我的待办 工作法\n\n> 换了 AI 接手也照这里做；要改做法先在 docs/DECISIONS.md 写理由。被纠正、踩坑或发现更好的做法，当场记在这里或 docs/避坑.md。\n\n| 日期 | 场景 | 做法 | 为什么 | 出处 |\n|---|---|---|---|---|\n',
    'index.html':'<!doctype html>\n<meta charset="utf-8">\n<title>我的待办</title>\n<h1>我的待办</h1>\n<ul>\n  <li>试一试随身开发</li>\n</ul>\n',
    'STATUS.md':'---\n项目: 示例·我的待办\n状态: 进行中\n阶段: 示例\n完成度: 10\n长期目标: 完成 0 块 / 共 3 块；本期：添加待办；卡在：无\n最后更新: {today}\n下一步: 在对话里说「给示例加一个添加待办的输入框」\n等谁: 你（说一个需求试试）\n卡在: 无\n---\n\n# 示例·我的待办 进度\n\n## 近期动态\n\n| 日期 | 事件 | 出处 |\n|---|---|---|\n| {today} | 建好示例项目 | 随身开发 |\n\n## 做到哪了\n- 一个静态网页，用浏览器打开 index.html 就能看到。\n',
    '功能清单.md':'# 示例·我的待办 功能清单\n\n| PRD编号 | 功能 | 状态 | 验收 | 说明 |\n|---|---|---|---|---|\n| EX-1 | 显示待办列表 | ✅完成 | 打开页面能看到 | |\n| EX-2 | 添加待办 | ⏳未开始 | 输入后列表多一行 | 留给你第一次试用 |\n',
    'docs/PRD.md':'# 示例·我的待办 PRD\n\n## 一句话\n一个最小的待办网页，用来五分钟走一遍随身开发。\n\n## 范围\nEX-1 显示、EX-2 添加。\n',
    'docs/避坑.md':'# 避坑本\n\n> 场景｜坑｜经验，三样缺一不记；新的写最上面。\n\n| 日期 | 主题 | 场景 | 坑 | 经验 | 出处 |\n|---|---|---|---|---|---|\n',
    'docs/DECISIONS.md':'# 示例·我的待办 决定记录\n\n',
    'AGENTS.md':'开发规则见 {protocol}；先读 STATUS.md 和 docs/工作法.md。收到新指令时，当前正在做的和已经排队的继续做完，除非新指令写明停、取消或改做这一件。合并按协议【按风险分级】：低风险按块、CI 通过就合，高风险整件做完给用户看过再问一次合。等检查不改成等合并；先不合或待批准解除监视，留下问题和恢复条件。\n\n# 示例·我的待办\n- 怎么跑：用浏览器打开 index.html（截图：随身开发 assist screenshot file://<本目录>/index.html）\n- 怎么测：打开页面肉眼看；无自动测试\n',
}

def example(store,where=None):
    """建一个五分钟就能走完全流程的小示例项目并登记。"""
    from .board import load,apply
    if any(e['id']=='example-todo' for e in load(store)):raise Blocked('示例项目已经建过')
    path=Path(where) if where else _home()/'随身开发示例'/'我的待办'
    if path.exists() and any(path.iterdir()):raise Blocked('目录已存在且不为空：'+str(path))
    path.mkdir(parents=True,exist_ok=True)
    today=time.strftime('%Y-%m-%d')
    for rel,text in EXAMPLE_FILES.items():
        atomic(path/rel,text.replace('{today}',today).replace('{protocol}',str(store.root/'工具/开发协议.md')))
    _git(path,'init','-q','-b','main');_git(path,'add','.');_git(path,'commit','-qm','建好示例项目')
    apply(store,load(store)+[{'id':'example-todo','name':'示例·我的待办','path':str(path),'status_path':str(path/'STATUS.md'),
                              'features_path':str(path/'功能清单.md'),'prd_path':str(path/'docs/PRD.md')}])
    return {'示例项目':str(path),'下一句可以说':'给示例加一个添加待办的输入框'}

def window(store,project,t3=None):
    """给已登记的项目开会话窗口：按编号或名字找项目，交给 move_in.project_window。"""
    from .board import load
    from .move_in import project_window
    entries=load(store)
    hit=[e for e in entries if project in (e.get('id'),e.get('name'))] or [e for e in entries if project and project in (e.get('name') or '')]
    if len(hit)!=1:raise Blocked('没找到唯一的项目「'+str(project)+'」；已登记的有：'+'、'.join(e.get('name') or e['id'] for e in entries))
    e=hit[0];text,starter=project_window(e['path'],e.get('name') or e['id'],t3)
    return {'项目':e.get('name') or e['id'],'位置':e['path'],'项目窗口':text,'启动语':starter}

def run(args):
    from .core import Store
    s=Store(args.root)
    try:
        if args.action=='restore-project':return restore_project(s,args.value[0],args.value[1] if len(args.value)>1 else None,args.to)
        if args.action=='undo-last':return undo_last(s,args.value[0],args.base)
        if args.action=='screenshot':return screenshot(s,args.value[0],args.name or '成品',args.width,args.height)
        if args.action=='help-bundle':return help_bundle(s,args.value[0] if args.value else None)
        if args.action=='example':return example(s,args.value[0] if args.value else None)
        if args.action=='window':
            from .t3_setup import T3Setup
            return window(s,' '.join(args.value),T3Setup(root=s.root))
    finally:s.db.close()

def register(subparser):
    p=subparser.add_parser('assist',help='restore-project <项目> [日期] [--to 新目录] / undo-last <项目> / screenshot <网址> [--name] [--width 1280] [--height 900] / help-bundle [目录] / example [目录] / window <项目>（给项目开会话窗口）')
    p.add_argument('action',choices=['restore-project','undo-last','screenshot','help-bundle','example','window']);p.add_argument('value',nargs='*');p.add_argument('--name');p.add_argument('--to');p.add_argument('--base')
    p.add_argument('--width',type=int,default=1280,help='截图宽度（像素），手机样子用 390');p.add_argument('--height',type=int,default=900,help='截图高度（像素）')
    p.set_defaults(assist_run=run);return p


def restore_project(store,project,date=None,to=None,backup_dir=None):
    """从每日备份恢复到全新目录；不会覆盖现有文件。"""
    from .move_in import backup_root
    from .core import safe, identifier
    if date:
        import re
        if not re.fullmatch(r'\d{4}-?\d{2}-?\d{2}',date):raise Blocked('日期请写成 2026-11-10')
    def made(folder):
        # 同一秒里的几份按备份清单里的实际时间排，不按随机后缀
        try:return (folder.name[len('外部-daily-'):len('外部-daily-')+15],json.loads((folder/'外部备份.json').read_text()).get('created') or 0)
        except (OSError,ValueError):return (folder.name[len('外部-daily-'):len('外部-daily-')+15],0)
    root=Path(backup_dir) if backup_dir else backup_root(store)  # 换电脑时指向从旧电脑带来的备份文件夹
    candidates=sorted(root.glob('外部-daily-*'),key=made,reverse=True)
    selected=None
    for folder in candidates:
        if date and not folder.name.startswith('外部-daily-'+date.replace('-','')):continue
        if folder.is_symlink():raise Blocked('备份目录不能是链接')
        report=json.loads((folder/'外部备份.json').read_text())
        for item in report['projects']:
            if project not in (item['项目'],item.get('编号')):continue
            pid=item.get('编号')
            if not pid:
                try:pid=_entry(store,project)['id']
                except Blocked:continue
            identifier(pid)
            source=safe(folder,pid)
            if item.get('失败') or not any((source/n).exists() for n in ('repo.bundle','未推送提交.bundle','全部已推送.txt')):continue
            selected=(folder,source,item,pid);break
        if selected:break
    if not selected:raise Blocked('没有找到这个项目可恢复的每日备份')
    folder,source,item,pid=selected
    stamp=folder.name[len('外部-daily-'):].split('-')[0]
    name=Path(project).name
    if name!=project or name in ('.','..'):raise Blocked('项目名字不合法')
    target=Path(to).expanduser().absolute() if to else _home()/'恢复'/(name+'-'+stamp)
    if target.exists() or target.is_symlink():raise Blocked('恢复目标已存在，请换一个新目录，不会覆盖')
    if any(p.is_symlink() for p in target.parents):raise Blocked('恢复目标父目录不能是链接')
    # Validate backup paths before invoking Git or copying anything.
    for p in source.rglob('*'):
        if p.is_symlink():raise Blocked('备份里有链接，停止恢复')
    target.parent.mkdir(parents=True,exist_ok=True)
    base=_restore_history(store,folder,source,item,pid,target,root)
    patch=source/'未提交改动.patch'
    if patch.is_file() and patch.stat().st_size:
        _git(target,'apply','--check',str(patch))
        _git(target,'apply',str(patch))
    untracked=source/'未跟踪文件'
    if untracked.is_dir():
        for p in untracked.rglob('*'):
            if not p.is_file():continue
            rel=p.relative_to(untracked)
            if '.git' in rel.parts:raise Blocked('未跟踪文件含 Git 内部目录，停止恢复')
            dest=safe(target,rel.as_posix())
            if dest.exists():raise Blocked('备份文件冲突，停止恢复')
            dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(p,dest)
    return {'恢复到':str(target),'备份日期':stamp,'完整历史来自':base,'覆盖原项目':False}


def _restore_history(store,folder,source,item,pid,target,root=None):
    """先拿完整历史，再叠上当天没推上去的提交。
    每天的备份只存在线仓库没有的东西，所以完整历史按顺序找：当天备份里有完整的 → 在线仓库 → 最近一次每周备份。"""
    from .move_in import backup_root
    from .core import safe
    def clone(src):
        return subprocess.run(['git','clone','--',str(src),str(target)],capture_output=True).returncode==0
    if (source/'repo.bundle').is_file():
        if not clone(source/'repo.bundle'):raise Blocked('备份仓库未能恢复，已保留现场')
        return '当天备份'
    base=None
    if item.get('在线仓库') and clone(item['在线仓库']):base='在线仓库'
    if base is None:
        for weekly in sorted((root or backup_root(store)).glob('外部-weekly-*'),reverse=True):
            if weekly.is_symlink():continue
            bundle=safe(weekly,pid)/'repo.bundle'
            if bundle.is_file() and clone(bundle):base='每周备份 '+weekly.name;break
    if base is None:raise Blocked('当天备份只存了在线仓库没有的部分，但在线仓库和每周备份都拿不到完整历史，没法恢复')
    extra=source/'未推送提交.bundle'
    if extra.is_file():
        got=subprocess.run(['git','-C',str(target),'fetch','--update-head-ok','--',str(extra),'+refs/heads/*:refs/heads/*'],capture_output=True)
        if got.returncode:raise Blocked('完整历史比没推上去的提交还旧，叠不上；换一个更新的完整历史来源再试')
        _git(target,'reset','-q','--hard','HEAD')
    return base
