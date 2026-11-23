from .core import cooperative
import json
import os
from pathlib import Path
import shutil
import subprocess
import sqlite3
import tempfile
import time
import uuid
import zipapp
from .core import Blocked, VERSION, Store, atomic, canonical, digest, identifier, lock, safe, maintenance, user_home

RUNTIME='工具/portable.pyz'

def build(source,target,edition='full',markers=(),repo=''):
    """edition='friend' 是给朋友的版本：打完检查包里没有打包人的个人信息。两种版本都不再带自带战队后台（11-17 停用）。"""
    if edition not in ('full','friend'):raise Blocked('版本只能是 full 或 friend')
    source,target=Path(source).absolute(),Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise Blocked('发行目标已存在，禁止覆盖')
    for p in target.parents:
        if p.is_symlink():raise Blocked('发行目标父目录为链接')
    target.parent.mkdir(parents=True,exist_ok=True)
    candidate=Path(tempfile.mkdtemp(prefix='.release-',dir=target.parent))
    try:
        stage=Path(tempfile.mkdtemp())
        try:
            protocol=json.loads((source/'portable/protocol.json').read_text())
            files=protocol.get('distribution',{}).get('runtime_files')
            actual={p.name for p in (source/'portable').iterdir() if p.is_file() and p.suffix in ('.py','.json')}
            if not files or set(files)!=actual:raise Blocked('运行模块白名单漂移，未构建')
            from .privacy import CredentialContent
            (stage/'portable').mkdir()
            for name in files:
                if Path(name).name!=name:raise Blocked('非法运行模块路径')
                item=source/'portable'/name
                if item.is_symlink() or CredentialContent().search(item.read_bytes()):raise Blocked('运行源码含未知链接或疑似凭据，未构建')
                shutil.copy2(item,stage/'portable'/name)
            (candidate/'工具').mkdir()
            atomic(stage/'__main__.py','import sys\nfrom portable.__main__ import main\nsys.exit(main())\n')
            zipapp.create_archive(stage,candidate/RUNTIME,interpreter='/usr/bin/env python3')
        finally:shutil.rmtree(stage)
        # Build must not bind an end-user identity in the temporary directory.
        for directory in ('知识库/项目','知识库/素材','知识库/记忆','数据','代码','会话档案','任务记录','迁移'):
            (candidate/directory).mkdir(parents=True,exist_ok=True)
        atomic(candidate/'知识库/首页.md','# 随身开发\n\n<!-- AUTO BEGIN -->\n尚无项目\n<!-- AUTO END -->\n\n## 手写区\n在这里保留你的笔记。\n')
        from .core import PROTOCOL
        atomic(candidate/'工具/协议.json',canonical(PROTOCOL))
        atomic(candidate/'工具/开发协议.md','# 随身开发公共协议\n\n'+'\n'.join('- '+rule for rule in PROTOCOL['public_rules'])+'\n')
        entry=('# 随身开发入口\n\n先读 [公共协议](工具/开发协议.md)（机器单源 [协议](工具/协议.json)）和 [本机附加规则](数据/本机规则.md)（存在时），再读 看板/全部项目.md。\n'
               '用户在对话里用大白话说话；命令都由 AI 代为执行：python3 工具/portable.pyz --root <本目录> <命令>。\n'
               '常用：overview（问进度）、move-in scan/adopt/undo（项目搬家）、assist undo-last/screenshot/help-bundle/example/window、upgrade check/apply/rollback（升级与退回）。\n'
               '用户说「升级随身开发」或「看看随身开发有没有新版」：跑 upgrade check 看有没有新版，跑 upgrade apply 自动从 GitHub 下载最新版、核对 SHA256 并升级（用户给了新版 .zip 或文件夹路径时跑 upgrade apply <路径>）；要退回上一版跑 upgrade rollback。\n'
               '用户贴来的启动语里还写着 <随身开发目录> 时，它就是本文件所在的文件夹，AI 自己替换，不要让用户改。\n'
               '用户说换了电脑、要把项目接回来：跑 move-in rejoin <旧电脑备份文件夹>，先把计划给用户看，用户同意后带 --confirmed "<用户原话>" 执行；不要只拷文件夹、不要自己写脚本。\n'
               '用户要给某个项目开会话窗口：跑 assist window <项目>，把结果原样告诉用户（AI 没法替用户新开对话，只能给一句启动语让用户自己新开对话贴进去）；不要去读程序源码、不要解压 portable.pyz、不要自己写脚本。\n'
               '开始使用.command 菜单只是备用。\n\n'
               '## 在本目录打开的对话 = 开发总管（总入口）\n'
               '- 问「各项目怎么样」：跑 overview。先说「待处理」：待验收、等总管和等回复留在桌上，点出项目、任务号、对话和在等什么；待验收没读到要说明没读到，不要当成没有。「分档」先说等你和堵住别人的，现在能做的停得久的在前，先别动的只报个数。谁等谁、同一件、撞车另列；互相等的停下来问。可继续的项目里，写明先不合、等总管、等批准的不要做，等你拍板的留下；下一步里不用人的部分，能向该项目对话发消息就让它当回合继续，不能发就告诉用户去哪个项目对话说。发出去的同一回合用 reply expect 登记对话、请求号和问了什么。后台每分钟把还没读回的写到看板/等回复.md，不发消息。看到这一页或待处理里的等回复，就读那些对话的最后一条，读到就 reply read。不能停在「下一步看回复」。每周进度定时只读项目卡，这一页才是读回。对方很快能完的，同一回合等到那次运行结束再读。等机器和等用户决定分开。监视要写目标、结束条件、回传位置，跑 wait-check；等检查不改成等合并。先不合或待批准就解除这次监视，PR 留着，写上问题和恢复条件。一件堵住只停依赖它的，已经授权且不依赖它的继续。短的等到结束再读回。长的先核实回传；没有自动唤醒就写未接通，不说后台会继续，也不把再催一次当成做完。再按协议【问进度】用 2–4 句收尾。工作记忆只带对得上的坑和做法，对不上的旧坑不要念。还没记下的项目，做完新需求时把场景、坑或做法写回那个仓库。\n'
               '- 说某个项目的需求：总管不改项目代码。能向别的对话发消息（如 T3）时，转给该项目自己的对话，并说明「转给了谁」；做完把结果带回来。不能转时，告诉用户去哪个项目对话说。\n'
               '- 「今天请你定」：把各项目 STATUS 里明确等用户的事汇成选择题（项目 · 一句话问题 · 选项与默认 · 不答的后果），最多 7 条；只汇总已写的，不编、不替用户答。\n'
               '- 不在这里改随身开发本身；改随身开发去它的代码仓库对话。\n'
               '- 本目录是安装目录：不许在这里手改代码、协议或本文件冒充发版；产品改动回源码仓改、测、打包，再升级安装。\n'
               '\n'
               '### 装好后第一件事\n'
               '- 用户刚装好随身开发时：先跑首次配置 wizard；再跑 t3-setup 看缺什么，用户同意后用 t3-setup --install 安装 T3。\n'
               '- 提醒用户本人登录 Codex 或 Claude；登录好后用 t3-setup --connect，把随身开发（标题「开发总管」）和已登记项目加进 T3 并打开。\n'
               '- 缺 T3 命令行时，先说明要运行官方远程安装脚本；用户说「同意装 T3 命令行」后才用 t3-setup --connect --allow-cli-install。\n'
               '- 接入成功后对用户说：「装好了，以后打开 T3，点左边『开发总管』说话就行」。没加上的项目如实说明，待确认后重试。\n'
)
        if edition!='friend':entry+=(
               '\n'
               '### 总管工作法\n'
               '- 授权范围内自己拍板：直接定、直接派、事后汇报；不列 A/B 让用户选。只有四件要点头的事和执行方的硬门禁（写在本机附加规则里）才提。\n'
               '- 用户写过「不擅自合并」时，任何合并前先问，包括为了打包而合并。整件事没做完不问合，做完一步只留在分支上。\n'
               '- 总管不改项目代码：需求转给项目自己的对话（能发消息时用 t3_thread_send，带 clientRequestId）；要求对方 CI 绿、回报提交号、给出部署命令。发出后同一回合运行 reply expect。后台每分钟把还没读回的写到看板/等回复.md。读到最后一条就 reply read。对方很快能完的用 t3_thread_wait。没有登记，这回合就不算发完。先不合或待批准就解除对应监视，不关闭 PR，留下问题和恢复条件。等 CI 的监视在检查结束后收尾，不改去等合并。没有自动唤醒就写未接通，不说后台会继续。\n'
               '- 发给执行机的指令固定格式：项目 / 提交号 / 命令 / 读回 / 回滚 / 是否需用户同意；要求的回报格式：做了什么 / 读回 / 回滚文件 / 遗留。\n'
               '- 生产变更：先备份、先 dry-run、一台机器一次一件、读回通过再做下一件；出故障可先按回滚说明回滚再报。用户已宣布「正式使用」的项目，才避开本机附加规则里写的禁止部署时段、等定时运行再验证；其他项目部署后直接手动触发验证、当场读回（审批、先备份、凭据不进聊天照旧）。\n'
               '- 不信自报：执行方说成功，要看读回证据；dry-run 必须真只读（用只读事务，不靠「执行后再回滚」）。\n'
               '- 先查清事实再下结论，不凭印象判断数据从哪来、任务在哪跑。\n'
               '- 回复用户：大白话，结果 → 证据 → 一个明确下一步；需要转达的，给一段可原样转发的话。\n'
               '- 密钥只在机器之间直传，聊天里只说存在/不存在。\n'
               '- 旧服务只停不删，留 7 天；清理只移到 ~/归档。\n')
        for name in ('AGENTS.md','CLAUDE.md'):atomic(candidate/name,entry)
        atomic(candidate/'启动语.md','# 启动语\n\n把下面这句话整段贴进你常用的、能读写本机文件的 AI（T3、Codex、Claude Code、WorkBuddy……）：\n\n'
               '> 请先读「<随身开发目录>/AGENTS.md」，按里面的规矩当我的开发助手；我不懂代码，用大白话跟你说需求。\n\n'
               '把 <随身开发目录> 换成本文件所在的文件夹路径。也可以直接把这个文件拖进对话发给 AI，不用自己改。第一次用接着说「帮我配置随身开发」，AI 会问你几件事；然后说「帮我把电脑上正在做的项目搬进来」或「建一个示例项目带我走一遍」。\n\n'
               '> 给 AI 的话：如果你拿到的是这个文件本身，<随身开发目录> 就是这个文件所在的文件夹，直接去读那里的 AGENTS.md，不要让用户改路径；配置完把 wizard 结果里「你的启动语」原样告诉用户。\n')
        shutil.copy2(source/'使用说明.md',candidate/'使用说明.md')
        botdir=candidate/'工具/adapters/bots';botdir.mkdir(parents=True)
        for name in protocol['distribution']['bot_files']:
            item=source/'adapters/bots'/name
            if Path(name).name!=name or item.is_symlink() or CredentialContent().search(item.read_bytes()):raise Blocked('机器人源码不满足发行白名单或含疑似秘密')
            shutil.copy2(item,botdir/name)
        shutil.copy2(source/'scripts/bot_bridge.py',candidate/'工具/bot_bridge.py')
        entry='#!/bin/bash\nset -euo pipefail\nROOT="$(cd "$(dirname "$0")" && pwd)"\nif [ "$#" -eq 0 ]; then\n  python3 "$ROOT/工具/portable.pyz" --root "$ROOT" doctor\n  python3 "$ROOT/工具/portable.pyz" --root "$ROOT" wizard --if-needed || true\n  python3 "$ROOT/工具/portable.pyz" --root "$ROOT" t3-setup --if-needed || true\n  python3 "$ROOT/工具/portable.pyz" --root "$ROOT" menu\nelse\n  exec python3 "$ROOT/工具/portable.pyz" --root "$ROOT" "$@"\nfi\n'
        atomic(candidate/'开始使用.command',entry)
        os.chmod(candidate/'开始使用.command',0o755)
        if repo:
            import re
            if not re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+',repo):raise Blocked('--repo 要写成 账号/仓库名')
        runtime={p.relative_to(candidate).as_posix():digest(p.read_bytes()) for p in candidate.rglob('*') if p.is_file() and not p.relative_to(candidate).as_posix().startswith(('知识库/','数据/'))}
        atomic(candidate/'发行.json',canonical({'schema':1,'version':VERSION,'files':runtime,**({'edition':'friend'} if edition=='friend' else {}),**({'repo':repo} if repo else {})}))
        if edition=='friend':
            hits=personal_hits(candidate,markers,repo=repo)
            if hits:raise Blocked('朋友版里有打包人的个人信息，未打包：'+'；'.join(hits[:10]))
        with lock(target.parent/'.portable-install.lock'):
            if target.exists():raise Blocked('目标并发出现')
            os.rename(candidate,target)
        return {'installed':str(target),'version':VERSION}
    finally:
        if candidate.exists():shutil.rmtree(candidate)

def personal_markers(extra=(),local=True):
    """打包人的个人信息：家目录路径、路径和邮箱里的用户名（local=False 时不加，CI 机器的用户名不算），再加上调用方给的词（名字、项目名、内网地址等）。
    用户名不单独整词比对：像 runner 这种用户名本身是代码里的普通单词，会把整个程序误判成个人信息（见 DECISIONS 2026-11-18）。"""
    import getpass
    found=[]
    if local:
        found.append(str(Path.home()))
        user=getpass.getuser()
        if len(user)>=3:found+=['/'+user+'/',user+'@']
    return [m for m in dict.fromkeys([*found,*extra]) if m and m.strip()]

def personal_hits(root,markers=(),local=True,repo=''):
    """逐个文件（含运行包里的每个模块）找个人信息，返回「文件：命中的词」。
    纯英文字母的词按整词找（短词不会误中更长的英文单词），其余按原文找；发行.json 里的升级仓库账号名放行。"""
    import re,zipfile
    owner=repo.split('/')[0] if repo and '/' in repo else ''
    words=[]
    for m in personal_markers(markers,local):
        b=m.encode()
        words.append((m,re.compile(rb'(?<![A-Za-z])'+re.escape(b)+rb'(?![A-Za-z])') if m.isascii() and m.isalpha() else None,b))
    hits=[]
    def look(name,data):
        for m,rx,b in words:
            if name=='发行.json' and owner and m in owner:continue
            if (rx.search(data) if rx else b in data):hits.append(name+'：'+m)
    for p in sorted(Path(root).rglob('*')):
        if not p.is_file():continue
        rel=p.relative_to(root).as_posix()
        if rel==RUNTIME:
            with zipfile.ZipFile(p) as z:
                for n in z.namelist():look(rel+'!'+n,z.read(n))
        else:look(rel,p.read_bytes())
    return hits

def release_verify(path):
    root=Path(path).absolute()
    manifest=json.loads((root/'发行.json').read_text())
    if manifest.get('schema')!=1 or manifest.get('version') not in ('0.1.0','0.1.1','0.1.2','0.1.3','0.1.4','0.1.5','0.2.0','0.2.1','0.2.2','0.2.3','0.2.4','0.2.5','0.2.6','0.2.7','0.2.8','0.2.9','0.2.10','0.2.11','0.2.12','0.2.13','0.2.14','0.2.15','0.2.16','0.2.17','0.2.18','0.2.19','0.2.20','0.2.21','0.2.22','0.2.23','0.2.24','0.2.25','0.2.26','0.2.27','0.2.28','0.2.29','0.2.30','0.2.31','0.2.32','0.2.33','0.2.34','0.2.35','0.2.36','0.2.37','0.2.38','0.2.39','0.2.40','0.2.41','0.2.42','0.2.43','0.2.44','0.2.45','0.2.46','0.2.47','0.2.48','0.2.49','0.2.50','0.2.51','0.2.52','0.2.53','0.2.54','0.2.55','0.2.56','0.2.57','0.2.58','0.2.59','0.2.60','0.2.61',VERSION):
        raise Blocked('发行版本不兼容')
    for rel,h in manifest['files'].items():
        if rel not in ('开始使用.command','AGENTS.md','CLAUDE.md','使用说明.md','启动语.md',RUNTIME,'工具/bot_bridge.py','工具/协议.json','工具/开发协议.md') and not rel.startswith(('工具/adapters/','工具/team-host-release/')):
            raise Blocked('发行包含非运行时路径')
        p=safe(root,rel)
        if not p.is_file() or digest(p.read_bytes())!=h:
            raise Blocked('发行校验失败：'+rel)
    if RUNTIME not in manifest['files']:
        raise Blocked('发行缺少运行时')
    return manifest

RETAIN={'daily':7,'weekly':4,'upgrade':2}


def expire_backups(destination,olds,marker):
    """删掉过期的自动备份（规矩【只移不删】：程序自己生成、别处有原件的东西按保留期自动删除，不进归档）。
    只删备份目录里、名字对得上、里面有备份清单文件、本身不是链接的目录；不符合的一律不动。"""
    destination=Path(destination).absolute();gone=[]
    for old in olds:
        old=Path(old)
        if old.is_symlink() or not old.is_dir() or old.parent.absolute()!=destination or not (old/marker).is_file():continue
        shutil.rmtree(old);gone.append(old.name)
    return gone


@cooperative
def backup(store,destination,kind='daily'):
    destination=Path(destination).absolute()
    if destination.is_relative_to(store.root) or store.root.is_relative_to(destination):
        raise Blocked('备份必须在包外独立目录')
    destination.mkdir(parents=True,exist_ok=True)
    dest=destination/(kind+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+str(uuid.uuid4())[:8])
    stage=Path(tempfile.mkdtemp(prefix='.backup-',dir=destination))
    # 自带战队后台已移除（见 DECISIONS 2026-11-18），不再等它停稳；旧版没挪走的 工具/hosts 只跳过它的锁文件
    guarded_hosts={h.relative_to(store.root).as_posix() for h in (store.root/'工具/hosts').glob('*') if h.is_dir() and (h/'runtime.json').is_file()}
    try:
        from .dependencies import generated_environment_dirs
        generated=generated_environment_dirs(store.root)
        for p in store.root.rglob('*'):
            rel=p.relative_to(store.root)
            if any(rel==Path(prefix) or Path(prefix) in rel.parents for prefix in generated):continue
            if p.is_symlink():raise Blocked('备份存在待处理符号链接')
            if project_copy(rel):continue # product-made project copies; see project_copy
            if p.is_dir():safe(stage,rel).mkdir(parents=True,exist_ok=True)
            if p.is_file():
                owned_lock=any(rel.as_posix().startswith(prefix+'/') for prefix in guarded_hosts) and (rel.name=='.ownership.lock' or rel.parts[-2:]==('mcp-state','.lock'))
                if owned_lock:continue
                if str(rel)=='数据/team-mcp/.lock':continue
                if str(rel) in ('数据/state.sqlite-wal','数据/state.sqlite-shm','数据/runner.guard','工具/runtimes/install.lock') or (rel.parent==Path('数据') and p.suffix=='.lock') or rel.parent==Path('数据/复盘/.锁'):
                    continue
                target=safe(stage,rel)
                target.parent.mkdir(parents=True,exist_ok=True)
                if str(rel)=='数据/state.sqlite':
                    c=sqlite3.connect(target)
                    try:
                        # Only this unpublished destination is exclusive; source stays WAL.
                        c.execute('PRAGMA locking_mode=EXCLUSIVE')
                        store.db.backup(c)
                        # Publish a standalone snapshot, never a WAL-mode file without sidecars.
                        # macOS Python 3.9.6 cannot open that incomplete layout read-only.
                        if c.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete':raise Blocked('SQLite 备份未转为独立快照')
                        if c.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise Blocked('SQLite 备份完整性检查失败')
                    finally:c.close()
                    os.chmod(target,p.stat().st_mode & 0o777)
                else:shutil.copy2(p,target)
        from .migration import inventory
        files=inventory(stage)
        atomic(stage/'BACKUP.json',canonical({'schema':1,'kind':kind,'created':time.time(),'excluded_generated':generated,'files':files}))
        os.rename(stage,dest)
        # 过期的自动备份直接删（只删带 BACKUP.json 的本程序备份目录），不再挪进恢复区越攒越多。
        limit=RETAIN[kind]
        existing=sorted((p for p in destination.glob(kind+'-*') if p.is_dir() and not p.is_symlink() and (p/'BACKUP.json').is_file() and p.name[len(kind)+1:len(kind)+9].isdigit()),key=lambda p:(p.name[:len(kind)+16],p.stat().st_mtime_ns),reverse=True)  # 同一秒内的多份按实际时间，不按随机后缀
        gone=expire_backups(destination,existing[limit:],'BACKUP.json')
        result={'backup':str(dest),'retained':limit,'删除过期':gone,'permanent_delete':bool(gone)}
        today_local=time.strftime('%Y-%m-%d')
        from .board import backup_external
        try:
            external=backup_external(store,destination,kind)
            if external:result['external']=external
        except (OSError,subprocess.CalledProcessError,Blocked) as error:
            result['external_error']=str(error)
        # 手动跑成的每日备份也算当天有备份：不然 overview 会一直报「今天还没有完整备份」，让人不信这个提示
        if kind=='daily' and not result.get('external_error') and not (result.get('external') or {}).get('failed'):
            previous=store.get('runner:daily')
            if previous and previous!=today_local:
                try:
                    import datetime as _d
                    gap=(_d.date.fromisoformat(today_local)-_d.date.fromisoformat(previous)).days
                    if gap>1:store.put('runner:backup-gap',{'last_ok':previous,'catch_up':today_local,'missed_days':gap-1,'at':time.time()})
                except ValueError:pass
            store.put('runner:daily',today_local)
        return result
    finally:
        if stage.exists():shutil.rmtree(stage)

def update(store,candidate,backup_dir=None):
    manifest=release_verify(candidate)
    current=release_verify(store.root)
    preview={'current':current['version'],'candidate':manifest['version'],'files':sorted(manifest['files']),'compatibility':'schema-1','backup_required':True}
    if backup_dir is None:return preview
    result=atomic_upgrade(store.root,candidate,backup_dir)
    return {**preview,**result,'applied':True}

@cooperative
def material_move(store,rid,recover=False):
    identifier(rid)
    source=store.root/'知识库/素材'/(rid+'.md')
    recovery=store.root/'数据/素材恢复区'/(rid+'.md')
    src,dest=(recovery,source) if recover else (source,recovery)
    with lock(store.root/'数据/material.lock'):
        if not src.is_file() or dest.exists():raise Blocked('素材不存在或目标冲突')
        dest.parent.mkdir(parents=True,exist_ok=True)
        os.rename(src,dest)
    return {'path':str(dest),'recoverable_days':30,'permanent_delete':False}

def prepare_update(store):
    from .team import transport
    # Freeze new submissions before checking remote state; failure leaves a safe local pause.
    from .automation import pause
    pause(store,by='update',reason='更新前停写')
    store.put('runner:shutdown_requested',{'requested':True})
    # Wait outside the maintenance lock so the owned runner can stop its child
    # and release its guard. Never signal a PID inferred from copied history.
    import fcntl
    guard=store.root/'数据/runner.guard'
    deadline=time.monotonic()+12
    while guard.exists():
        with guard.open('a') as handle:
            try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB);break
            except BlockingIOError:
                if time.monotonic()>=deadline:raise Blocked('本产品运行器尚未停稳；保持暂停，未升级')
        time.sleep(.1)
    store.put('execution','update-paused')
    from .team import reconcile_outbox
    reconcile_outbox(store)
    cfg=store.get('team_config',{})
    if cfg:
        call=transport(cfg);offset=0
        while True:
            result=call('list_tasks',{'offset':offset,'limit':50})
            if not isinstance(result.get('tasks'),list):raise Blocked('无法确认远端任务状态，未升级')
            if any(row.get('status') not in ('completed','cancelled','interrupted') for row in result['tasks']):
                raise Blocked('仍有未收口任务，保持本端暂停，不停止真实任务')
            following=result.get('next_offset')
            if following is None:break
            if not isinstance(following,int) or following<=offset:raise Blocked('任务分页无效，未升级')
            offset=following
    store.put('readiness_disabled:upgrade',{'projects':store.get('readiness_projects',{}),'legacy':store.get('readiness',{})})
    store.put('readiness_projects',{});store.put('readiness',{})
    return {'execution':'update-paused','remote_tasks_checked':bool(cfg),'services_stopped':False}

def exchange_directories(left,right):
    """One kernel namespace operation; never emulate exchange with two renames."""
    import ctypes
    import platform
    libc=ctypes.CDLL(None,use_errno=True)
    if platform.system()=='Darwin':
        fn=libc.renameatx_np;fd=-2
    elif hasattr(libc,'renameat2'):
        fn=libc.renameat2;fd=-100
    else:raise Blocked('此系统不支持原子目录交换，未升级')
    fn.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    fn.restype=ctypes.c_int
    if fn(fd,os.fsencode(left),fd,os.fsencode(right),2)!=0:
        raise Blocked('原子目录交换失败，未采用非原子替代')

def conflict_path(root):
    return Path(root).parent/('.portable-conflict-'+digest(str(Path(root).absolute()).encode())[:20]+'.json')

def user_assets(root,excluded):
    """Logical SQLite content plus every user path; never log database contents."""
    from .dependencies import generated_environment_dirs
    root=Path(root);generated=generated_environment_dirs(root)
    ignored_dirs={str(Path(x).parent) for x in excluded}
    ignored_dirs |= {str(p) for x in excluded for p in Path(x).parents if str(p)!='.'}
    result={}
    for p in sorted(root.rglob('*')):
        rel=p.relative_to(root).as_posix()
        if any(rel==g or rel.startswith(g+'/') for g in generated):continue
        if rel in excluded or rel in ('数据/team-mcp/.lock','数据/state.sqlite-wal','数据/state.sqlite-shm','数据/runner.guard','工具/runtimes/install.lock'):continue
        if project_copy(rel):continue # product-made project copies, carried over after exchange
        if p.parent==root/'数据' and p.suffix=='.lock':continue
        if p.is_symlink():raise Blocked('用户资产包含链接，未升级')
        if p.is_dir():
            if rel not in ignored_dirs:result[rel]={'directory':True}
            continue
        if rel=='数据/state.sqlite':
            c=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True)
            try:
                tables=c.execute("SELECT name,sql FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
                contents=[]
                for name,sql in tables:
                    quoted='"'+name.replace('"','""')+'"'
                    rows=c.execute('SELECT * FROM '+quoted).fetchall()
                    contents.append((name,sql,sorted(rows,key=repr)))
                value=digest(repr(contents).encode())
            finally:c.close()
        else:value=digest(p.read_bytes())
        result[rel]={'sha256':value,'mode':p.stat().st_mode & 0o777}
    return result

import re as _re
# Project copies made by the product: candidate/baseline workspaces and the full copy inside a
# delivery (its patch and notes stay), plus the base/new trees copied only to diff an external
# reception (a managed reception's exchanged generation is kept and backed up). They duplicate registered project repos, may carry those
# repos' ignored files or secret-looking fixtures, and are never product user assets.
_PROJECT_COPY=_re.compile(r'^(代码/候选|代码/基线|任务记录/候选交付/[0-9a-f]{64}/完整候选|代码/接收事务/[0-9a-f]{64}-[0-9a-f]{32}/(base|new))(/|$)')

def project_copy(rel):
    return bool(_PROJECT_COPY.match(Path(rel).as_posix()))

# 隔离实测副本只在开发者本机用，由环境变量指定；没设就不允许恢复修补。
ISOLATED_RECOVERY_TARGET=Path(os.environ['PORTABLE_ISOLATED_RECOVERY_TARGET']).absolute() if os.environ.get('PORTABLE_ISOLATED_RECOVERY_TARGET') else None

def atomic_upgrade(target,candidate,backup_dir,*,isolated_recovery=False):
    target,candidate=Path(target).absolute(),Path(candidate).absolute()
    if isolated_recovery and (ISOLATED_RECOVERY_TARGET is None or target!=ISOLATED_RECOVERY_TARGET):
        raise Blocked('恢复修补仅限OWNER指定的隔离实测副本，禁止用于实际安装或其他目录')
    incoming=release_verify(candidate);current=release_verify(target)
    if target==candidate or candidate.is_relative_to(target) or target.is_relative_to(candidate):raise Blocked('升级源与目标不能重叠')
    if any(p.is_symlink() for p in (target,*target.parents)):raise Blocked('目标路径存在链接')
    with maintenance(target), lock(target.parent/'.portable-upgrade.lock'):
        store=Store(target)
        try:
            if isolated_recovery:
                # Read-only recovery may need the repaired adapter before unknown
                # can be reviewed. Keep uncertainty verbatim, freeze mutations.
                store.put('execution','recovery-paused')
            else:
                if store.get('execution')=='active':
                    raise Blocked('先使用prepare-update验证停稳并暂停本端提交；不能直接改active标记')
                if store.get('team_config',{}) and store.get('execution')!='update-paused':
                    raise Blocked('已接入战队的包必须先执行prepare-update')
                from .team import reconcile_outbox
                reconcile_outbox(store)
            guard=target/'数据/runner.guard'
            if guard.exists():
                import fcntl
                with guard.open('a') as handle:
                    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:raise Blocked('本产品后台仍在运行，先prepare-update停稳')
            excluded=set(current['files']) | set(incoming['files']) | {'发行.json'}
            baseline=user_assets(target,excluded)
            saved=backup(store,backup_dir,'upgrade')['backup']
            if user_assets(target,excluded)!=baseline:raise Blocked('备份期间用户资产变化，保持原包并保留备份；未升级')
        finally:store.db.close()
        staging=Path(tempfile.mkdtemp(prefix='.upgrade-',dir=target.parent))
        # Never initialize a Store at the staging path: retain target identity and all user assets.
        shutil.copytree(saved,staging,dirs_exist_ok=True)
        (staging/'BACKUP.json').unlink()
        try:
            for rel in incoming['files']:
                dest=safe(staging,rel)
                if rel not in current['files'] and dest.exists():raise Blocked('升级将覆盖未知文件，停止')
                atomic(dest,safe(candidate,rel).read_bytes())
                os.chmod(dest,safe(candidate,rel).stat().st_mode & 0o777)
            atomic(staging/'发行.json',canonical(incoming))
            release_verify(staging)
            # Keep a durable receipt OUTSIDE the package before the exchange. This also identifies rollback after interruption.
            receipt=Path(backup_dir)/('upgrade-receipt-'+str(uuid.uuid4())+'.json')
            info={'target':str(target),'previous_package':str(staging),'backup':saved,'from':current['version'],'to':incoming['version'],'phase':'prepared','isolated_recovery':isolated_recovery}
            atomic(receipt,canonical(info))
            if user_assets(target,excluded)!=baseline or user_assets(staging,excluded)!=baseline:
                info['phase']='conflict-before-exchange';atomic(receipt,canonical(info))
                raise Blocked('交换前用户资产变化，原包保持活动；备份与候选均保留：'+str(receipt))
            exchange_directories(target,staging)
            # Generated project copies are not backed up; carry the live ones over so open requests keep their candidates.
            carried=['代码/候选','代码/基线']+[p.relative_to(staging).as_posix() for pattern in ('任务记录/候选交付/*/完整候选','代码/接收事务/*/base','代码/接收事务/*/new') for p in staging.glob(pattern) if project_copy(p.relative_to(staging))]
            for gen in carried:
                src=staging/gen
                if src.is_dir() and not (target/gen).exists():
                    (target/gen).parent.mkdir(parents=True,exist_ok=True);os.rename(src,target/gen)
            # Check both generations. External editors need not participate in our lock.
            if user_assets(target,excluded)!=baseline or user_assets(staging,excluded)!=baseline:
                if user_assets(target,excluded)==baseline:
                    exchange_directories(target,staging)
                info['phase']='conflict-after-exchange';atomic(receipt,canonical(info))
                atomic(conflict_path(target),canonical(info))
                raise Blocked('交换期间资产冲突，双方已保全并冻结合作写入；请按回执对账：'+str(receipt))
            try:
                release_verify(target)
                check=Store(target)
                try:check.doctor()
                finally:check.db.close()
                if user_assets(target,excluded)!=baseline or user_assets(staging,excluded)!=baseline:
                    raise Blocked('读回期间用户资产变化')
            except BaseException:
                # Roll back only when the active generation still matches the baseline.
                if user_assets(target,excluded)!=baseline:
                    info['phase']='conflict-after-exchange';atomic(receipt,canonical(info))
                    atomic(conflict_path(target),canonical(info))
                    raise Blocked('活动新包已有外部修改；双方保留且合作写入冻结：'+str(receipt))
                exchange_directories(target,staging)
                info['phase']='rolled-back';atomic(receipt,canonical(info));raise
            info['phase']='applied';atomic(receipt,canonical(info))
            return {**info,'receipt':str(receipt),'user_assets_preserved':True,'execution_resumed':False}
        except BaseException:
            # Keep candidates and backups for readback, never delete user data or rollback material.
            raise


def archive_residue(destination, now=None):
    """恢复区里放满 30 天的残留（备份失败留下的半份、旧版留下的过期备份）和第 3 个以后的旧版程序：直接删除。
    它们都是程序自己生成、别处有原件的东西（规矩【只移不删】），不再挪进归档。链接只删链接本身。"""
    from datetime import datetime
    now=time.time() if now is None else now
    home=user_home()
    recovery=Path(destination)/'恢复区'
    items=[]
    if recovery.is_dir() and not recovery.is_symlink():
        items.extend((p,'随身开发备份恢复区') for p in recovery.iterdir() if now-p.lstat().st_mtime>30*86400)
    # 只认本程序留下的旧版程序包（里面有 发行.json、本身不是链接）；别的 .upgrade-* 一概不动
    upgrades=sorted((p for p in home.glob('.upgrade-*') if p.is_dir() and not p.is_symlink() and (p/'发行.json').is_file()),key=lambda p:p.lstat().st_mtime,reverse=True)
    items.extend((p,'随身开发旧版程序') for p in upgrades[2:])
    gone=[]
    for src,category in items:
        if src.is_symlink() or src.is_file():src.unlink()
        else:shutil.rmtree(src)
        gone.append(category+'/'+src.name)
    return {'删除':gone,'移到归档':[],'永久删除':bool(gone)}
