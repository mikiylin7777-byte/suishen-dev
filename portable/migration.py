from .core import cooperative
import json
import fcntl
import contextlib
import importlib.util
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import uuid
from .core import Blocked, VERSION, atomic, canonical, digest, identifier, lock, safe

# .env.example / .env.sample / .env.template / .env.dist are documentation, not secrets. Matching them
# refused full-history export for every repo that documents its env vars (seen on a real project), forcing detached snapshots.
SECRET_NAME = re.compile(r'(^|/)(\.env($|\.(?!(example|sample|template|dist)$))|\.ssh|\.aws|\.config|credentials?($|\.)|secrets?($|\.)|.*\.(pem|key|p12|pfx))',re.I)
from .privacy import CredentialContent
SECRET_CONTENT = CredentialContent()
# 普通快照被秘密拦住时，告诉使用者这是升级前检查，不要去手改数据库。
SNAPSHOT_SECRET_HINT = ('这是升级前的安全检查，不要手改数据库。'
    '先用只读方式确认是不是真密钥；'
    '若是自己写进记录的测试样例，报总管或用户定是否脱敏后重试；'
    '真密钥按【密钥】规矩处理。')

def _block_ordinary_snapshot(reason):
    raise Blocked(reason+'。'+SNAPSHOT_SECRET_HINT)

def git(root, *args):
    p=subprocess.run(['git','-C',str(root),*args],capture_output=True)
    if p.returncode:
        raise Blocked('Git 盘点失败：'+args[0])
    return p.stdout

def ignored_classification(root):
    excluded={rel:'仅本机运行锁，恢复时以无活跃状态重建' for rel in ('数据/runner.guard',) if (root/rel).is_file()}
    if not (root/'.git').is_dir():return excluded
    for raw in git(root,'ls-files','--others','--ignored','--exclude-standard','-z').split(b'\0'):
        if not raw:continue
        rel=os.fsdecode(raw);p=root/rel
        if p.is_symlink():raise Blocked('ignored符号链接尚未分类')
        if '__pycache__' in p.parts and p.suffix=='.pyc' and p.read_bytes()[:4]==importlib.util.MAGIC_NUMBER:
            excluded[rel]='可重新生成的当前Python字节码缓存'
        elif p.name=='.DS_Store' and p.read_bytes()[:8]==b'\x00\x00\x00\x01Bud1':
            excluded[rel]='系统目录显示缓存'
        else:raise Blocked('ignored文件未知或敏感，未丢弃：'+rel)
    return excluded

def inventory(root):
    root=Path(root).resolve(strict=True)
    from .sensitive import private_assets
    trusted_runtime=set()
    if (root/'发行.json').is_file():
        from .distribution import release_verify
        trusted_runtime=set(release_verify(root)['files'])
    if private_assets(root):raise Blocked('存在已标私密且未加密素材；请在敏感携带向导选择age另包/登记不携带/取消')
    if (root/'.git').is_file():
        raise Blocked('worktree/外置 gitdir 尚未实现独立恢复')
    if (root/'.git/objects/info/alternates').exists() or (root/'.git/commondir').exists() or (root/'.git/shallow').exists():
        raise Blocked('共享对象库/浅克隆不能证明历史完整')
    if (root/'.gitmodules').exists():
        raise Blocked('submodule 尚未实现独立恢复')
    if (root/'.git').exists():
        excluded=ignored_classification(root)
        git(root,'fsck','--full')
        names=git(root,'rev-list','--objects','--all','--reflog').decode(errors='replace').splitlines()
        if any(SECRET_NAME.search(line.split(' ',1)[1]) for line in names if ' ' in line):
            raise Blocked('Git 历史存在敏感文件名；需单独审阅或加密策略')
        objects=git(root,'cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype)').splitlines()
        for item in objects:
            oid,kind=item.split()
            if kind==b'blob':
                blob=git(root,'cat-file','blob',oid.decode())
                if SECRET_CONTENT.search(blob) or blob.startswith(b'version https://git-lfs.github.com/spec/v1'):
                    raise Blocked('Git 历史/悬空对象包含疑似秘密或LFS指针')
    result={}
    from .dependencies import generated_environment_dirs
    generated=generated_environment_dirs(root)
    excluded=ignored_classification(root)
    for parent, dirs, files in os.walk(root,followlinks=False):
        dirs[:]=[d for d in dirs if (Path(parent)/d).relative_to(root).as_posix() not in generated
                 and (Path(parent)/d).relative_to(root).as_posix() not in ('代码/候选','代码/基线')] # generated project copies: rebuilt from registered sources, never inventoried as user assets
        for name in dirs+files:
            p=Path(parent)/name
            rel=p.relative_to(root).as_posix()
            if rel in excluded:continue
            info=p.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise Blocked('符号链接需独立恢复策略：'+rel)
            if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
                raise Blocked('不支持的特殊文件：'+rel)
            if name=='.git' and p.parent!=root:
                if p.is_file():raise Blocked('嵌套 worktree/外置gitdir不支持独立恢复')
                inventory(p.parent)
            if name=='.gitmodules':raise Blocked('嵌套submodule不支持独立恢复')
            if p.is_dir():
                continue
            if name.endswith(('.lock','-wal','-shm','-journal')):
                known_locks={'数据/team-mcp/.lock','数据/vault.lock','数据/team-submit.lock','数据/snapshot.lock','数据/update.lock','数据/material.lock','工具/runtimes/install.lock'}
                if rel in known_locks and (root/'发行.json').is_file():
                    from .distribution import release_verify
                    release_verify(root)
                    try:
                        with p.open('rb') as handle:
                            fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:
                        raise Blocked('产品正在写入，不能携带活动锁')
                    continue
                # SQLite sidecars handled only if a real matching SQLite DB exists.
                base=re.sub(r'-(wal|shm|journal)$','',str(p))
                if base!=str(p) and Path(base).is_file() and Path(base).read_bytes()[:16]==b'SQLite format 3\x00':
                    continue
                raise Blocked('存在活动锁/未知 sidecar：'+rel)
            if SECRET_NAME.search(rel):
                _block_ordinary_snapshot('敏感文件不得进入普通快照：'+rel)
            data=p.read_bytes()
            if rel not in trusted_runtime and (SECRET_CONTENT.search(data) or re.search(rb'https?://[^/\s:@]+:[^/\s@]+@',data)):
                _block_ordinary_snapshot('疑似秘密内容，普通快照已阻止：'+rel)
            if data.startswith(b'version https://git-lfs.github.com/spec/v1') or '/lfs/' in '/'+rel:
                raise Blocked('LFS 尚未实现完整独立恢复')
            sqlite=data.startswith(b'SQLite format 3\x00')
            if p.suffix.lower() in ('.db','.sqlite','.sqlite3','.mdb','.realm') and not sqlite:
                raise Blocked('数据库缺少一致性备份插件：'+rel)
            result[rel]={'sha256':digest(data),'size':len(data),'mode':stat.S_IMODE(info.st_mode),'sqlite':sqlite}
    return result

@cooperative
def enroll(store, path):
    path=Path(path).absolute()
    if not path.is_dir() or path.is_symlink():
        raise Blocked('介质目录必须存在且不能是链接')
    marker=path/'.portable-medium.json'
    if marker.exists():
        raise Blocked('介质已有标识，不能覆盖；请核实原登记')
    mid=str(uuid.uuid4())
    value=canonical({'schema':1,'id':mid,'nonce':str(uuid.uuid4())})
    atomic(marker,value)
    with store.db:
        store.db.execute('INSERT INTO media VALUES (?,?,?)',(mid,str(path),digest(value.encode())))
    return mid

def medium(store, path):
    path=Path(path).absolute()
    marker=path/'.portable-medium.json'
    if path.is_symlink() or not marker.is_file() or marker.is_symlink():
        raise Blocked('陌生或不安全介质')
    data=marker.read_bytes()
    try:
        mid=json.loads(data)['id']
    except (ValueError,KeyError):
        raise Blocked('介质标识损坏')
    row=store.db.execute('SELECT marker FROM media WHERE id=?',(mid,)).fetchone()
    if not row or row[0]!=digest(data):
        raise Blocked('未登记介质身份；卷名不能授权')
    return path,mid

@contextlib.contextmanager
def normalized_worktree(source):
    source=Path(source).resolve(strict=True)
    gitdir=Path(git(source,'rev-parse','--absolute-git-dir').decode().strip()).resolve(strict=True)
    common_raw=Path(git(source,'rev-parse','--git-common-dir').decode().strip())
    common=(common_raw if common_raw.is_absolute() else source/common_raw).resolve(strict=True)
    if gitdir==common or not (gitdir/'commondir').is_file():
        raise Blocked('仅支持标准linked worktree；未知外置gitdir不猜测')
    listing=git(source,'worktree','list','--porcelain').decode().splitlines()
    if 'worktree '+str(source) not in listing:
        raise Blocked('worktree来源身份无法确认')
    if (common/'objects/info/alternates').exists() or (common/'shallow').exists():
        raise Blocked('worktree共享外部对象/浅克隆不支持')
    def fingerprint():
        files={}
        for parent,dirs,names in os.walk(source,followlinks=False):
            for name in dirs+names:
                p=Path(parent)/name
                if p.is_symlink():raise Blocked('worktree链接须独立恢复策略')
            for name in names:
                p=Path(parent)/name
                if p.name!='.git':files[str(p.relative_to(source))]=digest(p.read_bytes())
        return [files,git(source,'rev-parse','HEAD'),(gitdir/'index').read_bytes() if (gitdir/'index').exists() else None]
    before=fingerprint()
    with tempfile.TemporaryDirectory(prefix='portable-worktree-') as directory:
        staging=Path(directory).resolve()/'repo'
        shutil.copytree(source,staging,symlinks=True,ignore=lambda parent,names:['.git'] if Path(parent)==source else [])
        # The common object/refs store becomes independent; sibling worktree control records are not executable history.
        shutil.copytree(common,staging/'.git',symlinks=True,ignore=lambda parent,names:['worktrees'] if Path(parent)==common else [])
        if (gitdir/'locked').exists() or (gitdir/'config.worktree').exists():raise Blocked('锁定或带独立配置的worktree需单独恢复策略')
        for name in (p.name for p in gitdir.iterdir() if p.name not in ('commondir','gitdir')):
            src=gitdir/name;dst=staging/'.git'/name
            if src.exists():
                if src.is_dir():shutil.copytree(src,dst,dirs_exist_ok=True)
                else:dst.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,dst)
        # Remove only path-dependent candidate configuration, never source configuration.
        for key in ('core.worktree','extensions.worktreeConfig'):
            result=subprocess.run(['git','-C',str(staging),'config','--local','--unset-all',key],capture_output=True)
            if result.returncode not in (0,5):raise Blocked('worktree候选配置规范化失败')
        git(staging,'config','core.bare','false')
        if before!=fingerprint():raise Blocked('worktree复制时发生变化，未发布')
        yield staging

def snapshot(store, source, media_path, project='fixture', fail_after=None):
    project=identifier(project)
    source=Path(source).resolve(strict=True)
    if (source/'.git').is_file():
        with normalized_worktree(source) as standalone:
            return snapshot(store,standalone,media_path,project,fail_after)
    media,mid=medium(store,media_path)
    if media.is_relative_to(source) or source.is_relative_to(media):
        raise Blocked('快照源和介质不能互相包含')
    with lock(store.root.parent/('.portable-snapshot-'+digest(str(store.root).encode())[:16]+'.lock')):
        before=inventory(source)
        required=sum(v['size'] for v in before.values())*2+1024*1024
        if shutil.disk_usage(media).free<required:
            raise Blocked('空间不足，未发布快照')
        sid=str(uuid.uuid4())
        staging=media/('.partial-'+sid)
        staging.mkdir()
        payload=staging/'payload'
        payload.mkdir()
        try:
            from .dependencies import generated_environment_dirs
            generated=generated_environment_dirs(source)
            for parent,dirs,_ in os.walk(source):
                dirs[:]=[d for d in dirs if (Path(parent)/d).relative_to(source).as_posix() not in generated]
                for d in dirs:
                    safe(payload,(Path(parent)/d).relative_to(source)).mkdir(parents=True,exist_ok=True)
            files={}
            for n,(rel,meta) in enumerate(sorted(before.items())):
                if fail_after is not None and n>=fail_after:
                    raise Blocked('模拟断电：快照没有发布')
                dest=safe(payload,rel)
                dest.parent.mkdir(parents=True,exist_ok=True)
                src=safe(source,rel)
                if meta['sqlite']:
                    a=sqlite3.connect(src.as_uri()+'?mode=ro',uri=True)
                    b=sqlite3.connect(dest)
                    try:
                        a.backup(b)
                        # macOS 自带的旧 SQLite 会把目标留在 WAL，并在关闭后留下 -shm。
                        # 快照只能收清单里的那一个库文件，所以先收成独立的 DELETE 库，再删残留。
                        if b.execute('PRAGMA journal_mode=DELETE').fetchone()[0]!='delete':
                            raise Blocked('SQLite 备份未转为独立快照')
                        if b.execute('PRAGMA integrity_check').fetchone()[0]!='ok':
                            raise Blocked('SQLite 一致性备份失败')
                    finally:
                        b.close(); a.close()
                    for suffix in ('-wal','-shm','-journal'):
                        sidecar=Path(str(dest)+suffix)
                        if sidecar.is_symlink() or (sidecar.exists() and not sidecar.is_file()):
                            raise Blocked('SQLite 备份残留不是普通文件：'+sidecar.name)
                        if sidecar.exists():
                            sidecar.unlink()
                else:
                    atomic(dest,src.read_bytes())
                os.chmod(dest,meta['mode'])
                data=dest.read_bytes()
                files[rel]={**meta,'sha256':digest(data),'size':len(data)}
            after=inventory(source)
            if set(before)!=set(after) or any(before[k]!=after[k] for k in before if not before[k]['sqlite']):
                raise Blocked('快照时源发生变化，请停稳后重试')
            manifest={'schema':1,'version':VERSION,'snapshot':sid,'device':store.device,'project':project,'medium':mid,'created':time.time(),'baseline':{k:v['sha256'] for k,v in before.items()},'excluded_derived':{**ignored_classification(source),**generated},'files':files,'dirs':sorted(p.relative_to(payload).as_posix() for p in payload.rglob('*') if p.is_dir())}
            atomic(staging/'manifest.json',canonical(manifest))
            atomic(staging/'COMPLETE',digest((staging/'manifest.json').read_bytes()))
            verify(staging,_candidate=True)
            final=media/('snapshot-'+sid)
            os.rename(staging,final)
            return str(final)
        except BaseException:
            # Preserve interrupted candidates for diagnosis; only COMPLETE + full validation is accepted.
            raise

def verify(path,_candidate=False):
    path=Path(path).absolute()
    if (path.name.startswith('.partial-') and not _candidate) or path.is_symlink() or not (path/'COMPLETE').is_file() or not (path/'manifest.json').is_file():
        raise Blocked('快照未完整发布')
    for p in path.rglob('*'):
        if p.is_symlink():
            raise Blocked('快照包含链接')
    raw=(path/'manifest.json').read_bytes()
    if (path/'COMPLETE').read_text()!=digest(raw):
        raise Blocked('快照 manifest 损坏')
    m=json.loads(raw)
    if m.get('schema')!=1 or m.get('version') not in ('0.1.0','0.1.1','0.1.2','0.1.3','0.1.4','0.1.5','0.2.37','0.2.38','0.2.39',VERSION):
        raise Blocked('快照版本不兼容')
    payload=path/'payload'
    expected=set(m['files'])
    for rel,meta in m['files'].items():
        p=safe(payload,rel)
        if not p.is_file() or p.stat().st_size!=meta['size'] or digest(p.read_bytes())!=meta['sha256']:
            raise Blocked('快照缺失或被篡改：'+rel)
    for rel in m['dirs']:
        if not safe(payload,rel).is_dir():
            raise Blocked('快照目录缺失')
    actual={p.relative_to(payload).as_posix() for p in payload.rglob('*') if p.is_file()}
    actual_dirs={p.relative_to(payload).as_posix() for p in payload.rglob('*') if p.is_dir()}
    if actual!=expected or actual_dirs!=set(m['dirs']):
        raise Blocked('快照存在未知内容')
    return m

def restore(path,target):
    m=verify(path)
    target=Path(target).absolute()
    if target.exists() or target.is_symlink():
        raise Blocked('不能覆盖已有恢复目标')
    for parent in target.parents:
        if parent.is_symlink():
            raise Blocked('恢复目标父路径含链接')
    target.parent.mkdir(parents=True,exist_ok=True)
    staging=Path(tempfile.mkdtemp(prefix='.restore-',dir=target.parent))
    payload=Path(path)/'payload'
    try:
        for rel in m['dirs']:
            safe(staging,rel).mkdir(parents=True,exist_ok=True)
        for rel,meta in m['files'].items():
            p=safe(staging,rel)
            atomic(p,safe(payload,rel).read_bytes())
            os.chmod(p,meta['mode'])
        for rel,meta in m['files'].items():
            if digest(safe(staging,rel).read_bytes())!=meta['sha256']:
                raise Blocked('恢复读回失败')
        if (staging/'.git').is_dir():
            git(staging,'fsck','--full')
        # A restored package always gets a new actual identity; never revive old execution permission.
        db=staging/'数据/state.sqlite'
        if db.exists():
            c=sqlite3.connect(db)
            with c:
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('device',str(uuid.uuid4())))
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('execution','restored-readonly'))
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('team_config','{}'))
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('history_device',m['device']))
                from .runtime_identity import invalidate_database
                invalidate_database(c)
                from .core import host_identity
                c.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('host_binding',digest((host_identity()+':'+str(target)).encode())))
                for row in c.execute('SELECT id FROM projects').fetchall():
                    code=staging/'代码'/row[0]
                    if code.is_dir():
                        new_device=c.execute("SELECT value FROM meta WHERE key='device'").fetchone()[0]
                        c.execute('INSERT OR REPLACE INTO paths VALUES (?,?,?)',(row[0],new_device,str(target/'代码'/row[0])))
            c.close()
        # serialized local restore prevents concurrent instances overwriting one another.
        with lock(target.parent/'.portable-restore.lock'):
            if target.exists() or target.is_symlink():
                raise Blocked('恢复目标并发出现，停止发布')
            os.rename(staging,target)
        return {'restored':str(target),'history_device':m['device'],'execution':'restored-readonly'}
    finally:
        if staging.exists():
            shutil.rmtree(staging)

def reconcile(base,left,right,output):
    roots=[Path(p).resolve(strict=True) for p in (base,left,right)]
    inventories=[inventory(p) for p in roots]
    output=Path(output).absolute()
    if output.exists():
        raise Blocked('合并输出必须为新目录')
    for p in roots:
        if output.is_relative_to(p):
            raise Blocked('合并输出不能在输入内')
    output.mkdir(parents=True)
    conflicts=[]
    for rel in sorted(set().union(*(i.keys() for i in inventories))):
        if rel.startswith('.git/'):
            raise Blocked('Git 历史合并请用独立 Git 分支工具；禁止逐文件合并 .git')
        vals=[safe(p,rel).read_bytes() if rel in inv else None for p,inv in zip(roots,inventories)]
        b,l,r=vals
        if l==r: chosen=l
        elif l==b: chosen=r
        elif r==b: chosen=l
        else:
            conflicts.append(rel)
            for side,value in zip(('base','left','right'),vals):
                if value is not None:
                    atomic(safe(output/'冲突'/side,rel),value)
            continue
        if chosen is not None:
            atomic(safe(output/'合并',rel),chosen)
    atomic(output/'report.json',canonical({'conflicts':conflicts,'automatic_deletion':False}))
    return {'conflicts':conflicts,'output':str(output),'clean':not conflicts}

@cooperative
def sync(store,project,media_path,force=False):
    row=store.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(project,store.device)).fetchone()
    if not row:
        raise Blocked('当前设备没有活动项目路径')
    change=store.db.execute('SELECT changed FROM changes WHERE project=?',(project,)).fetchone()
    if not force and (not change or time.time()-change[0]<30):
        return {'pending':True,'window_seconds':30}
    result=snapshot(store,row[0],media_path,project)
    with store.db:
        store.db.execute('DELETE FROM changes WHERE project=?',(project,))
    return {'snapshot':result}

@cooperative
def finalize(store):
    from .component_install import required_status
    components=required_status(store)
    missing=list(components['missing'])
    if store.get('restore:needs_takeover') or store.get('execution')!='active':missing.append('新设备本机接管/工程绑定未完成')
    proof=store.get('migration:verified',{})
    if proof.get('device')!=store.device or proof.get('root')!=str(store.root) or not proof.get('old_host_unavailable') or not proof.get('official_takeover_verified'):missing.append('缺本机源失联与正式后端接管实测回执')
    from .team import require_readiness
    try:
        from .team import project_bindings
        project=proof.get('project')
        if not project:
            matched=[p for p,b in project_bindings(store).items() if b.get('epoch')==proof.get('binding_epoch')]
            if len(matched)!=1:raise Blocked('迁移回执缺唯一项目绑定')
            project=matched[0]
        binding=require_readiness(store,project=project)
        if proof.get('binding_epoch')!=binding['epoch'] or Path(proof.get('old_source','/')).exists():missing.append('迁移实测绑定已变化或旧源重新可访问')
    except Exception:missing.append('当前正式工程绑定未就绪')
    if missing:raise Blocked('迁移未完成：'+'；'.join(missing))
    return {'complete':True,'device':store.device,'required_components':components,'production_authorization_inherited':False,'scope':'local_development_only','external_effects_enabled':False,'offline_distributed_exclusivity':False}

@cooperative
def observe(store,project,now=None):
    now=time.time() if now is None else now
    row=store.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(project,store.device)).fetchone()
    if not row:raise Blocked('本设备项目路径未登记')
    fingerprint=digest(canonical(inventory(row[0])).encode())
    key='observed:'+project
    if store.get(key)!=fingerprint:
        with store.db:
            store.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,fingerprint))
            store.db.execute('INSERT OR REPLACE INTO changes VALUES (?,?)',(project,now))
        return {'changed':True,'window_seconds':30}
    return {'changed':False}

def watch(store,project,media_path,interval=2):
    medium(store,media_path)
    while True:
        observe(store,project)
        result=sync(store,project,media_path)
        if not result.get('pending'):
            print(canonical(result),flush=True)
        time.sleep(interval)
