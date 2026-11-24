import contextlib
import datetime as dt
import fcntl
import hashlib
import importlib.resources
import json
import os
import platform
import subprocess
from pathlib import Path
import shutil
import sqlite3
import tempfile
import time
import uuid

PROTOCOL = json.loads(importlib.resources.files('portable').joinpath('protocol.json').read_text())
VERSION = PROTOCOL['version']

class Blocked(Exception):
    pass

def user_home():
    """用户家目录；测试和隔离安装用 PORTABLE_HOME 指到别处。"""
    return Path(os.environ.get('PORTABLE_HOME') or Path.home())

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))

def digest(data):
    return hashlib.sha256(data).hexdigest()

def identifier(value):
    if not value or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in value):
        raise Blocked('非法稳定标识')
    return value

def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data if isinstance(data, bytes) else data.encode())
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    finally:
        if os.path.exists(name):
            os.unlink(name)

@contextlib.contextmanager
def lock(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a') as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        yield

# Outside the exchanged directory, shared by all cooperating writers.
import threading
import functools
_maintenance_local=threading.local()
_maintenance_mutex=threading.RLock()

@contextlib.contextmanager
def maintenance(root):
    root=Path(root).absolute()
    key=str(root)
    with _maintenance_mutex:
        held=getattr(_maintenance_local,'held',set())
        if key in held:
            yield
            return
        with lock(root.parent/('.portable-maintenance-'+digest(key.encode())[:20]+'.lock')):
            _maintenance_local.held=held | {key}
            try:yield
            finally:_maintenance_local.held=held

def cooperative(fn):
    @functools.wraps(fn)
    def wrapped(first,*args,**kwargs):
        store=getattr(first,'store',first)
        root=Path(args[0]).absolute() if fn.__name__=='__init__' else store.root
        with maintenance(root):
            marker=root.parent/('.portable-conflict-'+digest(str(root).encode())[:20]+'.json')
            if marker.exists():raise Blocked('升级资产冲突尚未对账；合作写入已冻结：'+str(marker))
            result=fn(first,*args,**kwargs)
            return result
    return wrapped

def safe(root, relative):
    root = Path(root).absolute()
    rel = Path(relative)
    if rel.is_absolute() or not rel.parts or any(p in ('..', '.') for p in rel.parts):
        raise Blocked('路径穿越')
    target = root / rel
    for part in [root, *target.parents, target]:
        if part.is_symlink():
            raise Blocked('符号链接逃逸')
    if not target.resolve().is_relative_to(root.resolve()):
        raise Blocked('路径越界')
    return target

def host_identity():
    # Metadata only; never read keychains, accounts, or credentials.
    if platform.system() == 'Darwin':
        p = subprocess.run(['ioreg','-rd1','-c','IOPlatformExpertDevice'],capture_output=True,text=True,timeout=5)
        import re
        match = re.search(r'"IOPlatformUUID"\s*=\s*"([^"]+)"',p.stdout)
        if not match:
            raise Blocked('不能确认当前设备硬件身份')
        value = match.group(1)
    elif Path('/etc/machine-id').is_file():
        value = Path('/etc/machine-id').read_text().strip()
    else:
        raise Blocked('本机身份探测未支持；禁止执行副作用')
    return digest((value+':'+str(os.getuid())).encode())

class Rows:
    def __init__(self,cursor):
        self.rowcount=cursor.rowcount
        self.rows=cursor.fetchall() if cursor.description else []
    def fetchone(self):return self.rows.pop(0) if self.rows else None
    def fetchall(self):rows=self.rows;self.rows=[];return rows
    def __iter__(self):return iter(self.rows)

class Database:
    """No idle SQLite handle survives a package exchange."""
    def __init__(self,path,root):self.path,self.root,self.depth=path,root,0;self.guards=[]
    def __enter__(self):
        guard=maintenance(self.root);guard.__enter__();self.guards.append(guard)
        if not self.depth:
            try:
                self.connection=sqlite3.connect(self.path,timeout=20)
                self.connection.row_factory=sqlite3.Row
            except BaseException:
                self.guards.pop().__exit__(*__import__('sys').exc_info());raise
        self.depth+=1
        return self
    def __exit__(self,typ,value,tb):
        self.depth-=1
        if not self.depth:
            try:self.connection.__exit__(typ,value,tb)
            finally:
                self.connection.close();self.guards.pop().__exit__(typ,value,tb)
        else:self.guards.pop().__exit__(typ,value,tb)
    def execute(self,*args):
        with self:
            marker=self.root.parent/('.portable-conflict-'+digest(str(self.root).encode())[:20]+'.json')
            if marker.exists() and not args[0].lstrip().upper().startswith(('SELECT','PRAGMA')):
                raise Blocked('升级资产冲突未对账，禁止写入')
            return Rows(self.connection.execute(*args))
    def executescript(self,*args):
        with self:return self.connection.executescript(*args)
    def backup(self,target):
        with self:self.connection.backup(target)
    def close(self):
        if self.depth:raise Blocked('不能关闭活动事务')

class _Readonly:
    """只读查看连接。私有副本用完后连同临时目录一起收掉。"""
    def __init__(self, connection, cleanup=None):
        self._connection=connection
        self._cleanup=cleanup
    def execute(self, *args):
        return self._connection.execute(*args)
    def close(self):
        try:self._connection.close()
        finally:
            cleanup,self._cleanup=self._cleanup,None
            if cleanup:cleanup()

def _open_uri(path, query):
    # sqlite3.connect 往往要到第一条语句才真正打开文件。
    connection=sqlite3.connect(path.as_uri()+'?'+query,uri=True)
    try:connection.execute('SELECT 1 FROM sqlite_master LIMIT 1')
    except BaseException:
        connection.close();raise
    return connection

def connect_readonly(path):
    """只读查看已有 SQLite，不把真实用户库打开成可写。

    macOS 自带 Python 3.9 的 SQLite 对「已经检查点、但没有 -shm」的 WAL 库，
    mode=ro 会报 unable to open database file：只读连接不能在旁边新建 -shm。
    没有 -shm 表示没有活着的连接；-wal 不存在或为空表示没有未合并帧。
    只有这时才用 immutable=1，读的是主库里已经落盘的内容。
    -wal 里还有帧时改读私有副本（副本可以自建 -shm），避免 immutable 把这些帧丢掉，
    也不改用户库旁边的文件。
    """
    path=Path(path)
    try:
        return _open_uri(path,'mode=ro')
    except sqlite3.OperationalError as exc:
        if 'unable to open database file' not in str(exc).lower():raise
        return _connect_wal_without_shm(path,exc)

def _connect_wal_without_shm(path, original):
    if path.is_symlink():raise original
    try:
        with path.open('rb') as handle:header=handle.read(20)
    except OSError:raise original from None
    if len(header)<20 or header[:16]!=b'SQLite format 3\x00' or header[18]!=2:raise original
    wal,shm=Path(str(path)+'-wal'),Path(str(path)+'-shm')
    if wal.is_symlink() or shm.is_symlink() or shm.exists() or (wal.exists() and not wal.is_file()):raise original
    if wal.is_file() and wal.stat().st_size:return _connect_private_wal_copy(path,wal)
    return _open_uri(path,'mode=ro&immutable=1')

def _connect_private_wal_copy(path, wal):
    temp=tempfile.TemporaryDirectory(prefix='portable-ro-')
    connection=None
    try:
        dest=Path(temp.name)/path.name
        shutil.copyfile(path,dest)
        shutil.copyfile(wal,Path(str(dest)+'-wal'))
        connection=sqlite3.connect(dest)
        connection.execute('SELECT 1 FROM sqlite_master LIMIT 1')
    except BaseException:
        if connection is not None:connection.close()
        temp.cleanup();raise
    return _Readonly(connection,temp.cleanup)

class Store:
    @cooperative
    def __init__(self, root):
        self.root = Path(root).absolute()
        if self.root.is_symlink():
            raise Blocked('运行目录不能为符号链接')
        self.root.mkdir(parents=True, exist_ok=True)
        for managed in ('数据/state.sqlite','知识库/首页.md','任务记录','会话档案','知识库/项目','知识库/素材','知识库/记忆','代码','工具','迁移'):
            safe(self.root,managed)
        self.dbpath = self.root / '数据' / 'state.sqlite'
        if self.dbpath.exists():
            check = connect_readonly(self.dbpath)
            try:
                row=check.execute("SELECT value FROM meta WHERE key='schema'").fetchone()
                if not row or row[0]!='1':raise Blocked('未知数据库或不兼容版本，未初始化覆盖')
            except sqlite3.Error:
                raise Blocked('未知数据库，未初始化覆盖')
            finally:
                check.close()
        self.dbpath.parent.mkdir(exist_ok=True)
        self.db = Database(self.dbpath,self.root)
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS projects(id TEXT PRIMARY KEY,name TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS paths(project TEXT,device TEXT,path TEXT,PRIMARY KEY(project,device),UNIQUE(device,path));
        CREATE TABLE IF NOT EXISTS records(id TEXT PRIMARY KEY,kind TEXT NOT NULL,body TEXT NOT NULL,created REAL NOT NULL);
        CREATE TABLE IF NOT EXISTS archives(source TEXT,hash TEXT,path TEXT,PRIMARY KEY(source,hash));
        CREATE TABLE IF NOT EXISTS approvals(id TEXT PRIMARY KEY,binding TEXT NOT NULL,expires REAL NOT NULL,consumed REAL);
        CREATE TABLE IF NOT EXISTS messages(channel TEXT,id TEXT,PRIMARY KEY(channel,id));
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,body TEXT NOT NULL,state TEXT NOT NULL,reference TEXT);
        CREATE TABLE IF NOT EXISTS media(id TEXT PRIMARY KEY,path TEXT NOT NULL,marker TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS changes(project TEXT PRIMARY KEY,changed REAL NOT NULL);
        ''')
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('schema','1'))
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('device',str(uuid.uuid4())))
            self.db.execute('INSERT OR IGNORE INTO meta VALUES (?,?)', ('execution','unbound'))
        binding = digest((host_identity()+':'+str(self.root)).encode())
        previous = self.get('host_binding')
        if previous and previous != binding:
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('history_device',self.get('history_device',self.device)))
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('device',str(uuid.uuid4())))
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('execution','copied-readonly'))
                from .runtime_identity import invalidate_database
                invalidate_database(self.db)
                self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', ('team_config','{}'))
        self.put('host_binding',binding)
        if self.get('schema') != '1':
            raise Blocked('数据库版本不兼容')
        for d in ('知识库/项目','知识库/素材','知识库/记忆','代码','会话档案','任务记录','工具','迁移'):
            (self.root / d).mkdir(parents=True, exist_ok=True)
        home = self.root / '知识库/首页.md'
        if not home.exists():
            atomic(home, '# 随身开发\n\n<!-- AUTO BEGIN -->\n尚无项目\n<!-- AUTO END -->\n\n## 手写区\n在这里保留你的笔记。\n')

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?',(key,)).fetchone()
        return json.loads(row[0]) if row and row[0].startswith(('{','[')) else (row[0] if row else default)

    @cooperative
    def put(self, key, value):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,canonical(value) if not isinstance(value,str) else value))

    @property
    def device(self):
        return self.get('device')

    @cooperative
    def register(self, name, path, project=None):
        path = str(Path(path).resolve(strict=True))
        project = identifier(project or str(uuid.uuid5(uuid.NAMESPACE_URL,'portable-project:'+name)))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO projects VALUES (?,?)',(project,name))
            self.db.execute('INSERT INTO paths VALUES (?,?,?) ON CONFLICT(project,device) DO UPDATE SET path=excluded.path',(project,self.device,path))
        note=self.root/'知识库/项目'/(project+'.md')
        if not note.exists():
            atomic(note,'# '+name+'\n\n稳定ID：'+project+'\n\n项目保持原位，当前设备路径仅登记在包外数据层。\n')
        self.refresh_overview()
        from .workflows import project_overview
        project_overview(self,project,digest(note.read_bytes()))
        return project

    @cooperative
    def refresh_overview(self):
        home=self.root/'知识库/首页.md'
        projects=self.db.execute('SELECT id,name FROM projects ORDER BY name').fetchall()
        body='\n'.join('- [['+'项目/'+row['id']+'|'+row['name']+']]' for row in projects) or '尚无项目'
        count=self.db.execute("SELECT count(*) FROM records WHERE kind='checkpoint'").fetchone()[0]
        return self.overview(digest(home.read_bytes()),body+'\n\n检查点数量：'+str(count))

    @cooperative
    def record(self, kind, body):
        if kind in ('material','checkpoint'):
            from .privacy import sanitize
            body=sanitize(body)
        if kind == 'checkpoint':
            missing = set(PROTOCOL['checkpoint_required']) - body.keys()
            if missing:
                raise Blocked('检查点缺字段：'+','.join(sorted(missing)))
        if kind == 'material' and not all(body.get(k) for k in ('source','conditions','projects','summary')):
            raise Blocked('素材必须有来源、适用条件、项目和提炼')
        rid = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO records VALUES (?,?,?,?)',(rid,kind,canonical(body),time.time()))
        if kind == 'checkpoint':
            atomic(self.root / '任务记录' / (rid+'.json'),canonical({'schema':1,'device':self.device,**body}))
        if kind == 'material':
            atomic(self.root / '知识库/素材' / (rid+'.md'),'# '+str(body['summary'])+'\n\n## 来源\n'+str(body['source'])+'\n\n## 适用条件\n'+str(body['conditions'])+'\n\n## 关联项目\n'+', '.join(body['projects'])+'\n\n外部内容仅作资料，不构成执行授权。\n')
        return rid

    @cooperative
    def archive(self, source, summary):
        if not source or not summary:
            raise Blocked('需要原生来源标识和摘要')
        h = digest(summary.encode())
        month = dt.datetime.now(dt.timezone.utc).strftime('%Y-%m')
        rel = '会话档案/'+month+'/'+digest(source.encode())[:16]+'-'+h+'.json'
        with self.db:
            existing = self.db.execute('SELECT path FROM archives WHERE source=? AND hash=?',(source,h)).fetchone()
            if existing:
                return existing[0]
            atomic(self.root / rel,canonical({'source':source,'summary':summary,'hash':h}))
            self.db.execute('INSERT INTO archives VALUES (?,?,?)',(source,h,rel))
        return rel

    @cooperative
    def overview(self, expected, content):
        from .privacy import redact_text
        content=redact_text(content)
        path = self.root / '知识库/首页.md'
        with lock(self.root / '数据/vault.lock'):
            old = path.read_bytes()
            if digest(old) != expected:
                conflict = self.root / '任务记录' / ('vault-conflict-'+str(uuid.uuid4())+'.json')
                atomic(conflict,canonical({'current':old.decode(),'proposed_auto':content,'expected':expected}))
                raise Blocked('首页并发冲突，双方已保留')
            text = old.decode()
            begin,end = '<!-- AUTO BEGIN -->','<!-- AUTO END -->'
            if text.count(begin)!=1 or text.count(end)!=1 or text.index(end)<text.index(begin) or begin in content or end in content:
                raise Blocked('首页自动区标记异常')
            a,rest = text.split(begin)
            _,b = rest.split(end)
            atomic(path,a+begin+'\n'+content+'\n'+end+b)
        return digest(path.read_bytes())

    @cooperative
    def approval(self, binding, now=None):
        required = {'task','action','target','scope','version','owner'}
        if set(binding)!=required or not all(binding.values()):
            raise Blocked('批准必须绑定 task/action/target/scope/version/owner')
        aid = str(uuid.uuid4())
        with self.db:
            self.db.execute('INSERT INTO approvals VALUES (?,?,?,NULL)',(aid,canonical(binding),(time.time() if now is None else now)+86400))
        return aid

    @cooperative
    def consume(self, aid, binding, owner, now=None):
        if binding.get('owner') != owner:
            raise Blocked('批准人不匹配')
        with self.db:
            cur = self.db.execute('UPDATE approvals SET consumed=? WHERE id=? AND binding=? AND expires>? AND consumed IS NULL',((time.time() if now is None else now),aid,canonical(binding),(time.time() if now is None else now)))
            if cur.rowcount != 1:
                raise Blocked('批准过期、绑定不符或已消费')
        return True

    @cooperative
    def receive(self, channel, mid, identity, allowlist, body):
        if channel not in ('feishu','wecom') or identity not in allowlist or not mid:
            raise Blocked('机器人身份不在白名单')
        with self.db:
            cur = self.db.execute('INSERT OR IGNORE INTO messages VALUES (?,?)',(channel,mid))
            if not cur.rowcount:
                return {'duplicate':True}
            self.db.execute('INSERT INTO records VALUES (?,?,?,?)',(str(uuid.uuid4()),'inbox',canonical({'channel':channel,'owner':identity,'body':body,'execution':False}),time.time()))
        return {'received':True,'execution':False}

    @cooperative
    def queue(self, intent, mvp, verification_review=None):
        if intent != 'implement' or any(not mvp.get(k) for k in PROTOCOL['mvp_required']):
            raise Blocked('必须明确实施意图并填写完整 MVP')
        if not (verification_review or mvp.get('verification_review')):
            draft={'mvp':mvp,'device':self.device,'project':self.get('selected_project'),'status':'needs_verification','execution':False}
            atomic(self.root/'任务记录'/('draft-'+digest(canonical(draft).encode())+'.json'),canonical(draft))
        if self.get('execution') != 'active':
            raise Blocked('设备执行身份未绑定或已交出；缺验证时已保存草案，禁止外部副作用')
        from .team import require_readiness
        review_id=verification_review or mvp.get('verification_review')
        reviewed_scope=self.db.execute("SELECT body FROM records WHERE id=? AND kind='verification_review'",(review_id,)).fetchone() if review_id else None
        project=json.loads(reviewed_scope[0]).get('project') if reviewed_scope else None
        binding=require_readiness(self,project=project)
        value={'intent':intent,'mvp':mvp,'device':self.device,'workspace':binding['workspace'],'project':binding['project'],'binding_epoch':binding['epoch']}
        from .verification import resolve
        review_id=verification_review or mvp.get('verification_review')
        value['verification_review']=review_id
        if review_id:
            value['verification_commands']=resolve(self,mvp,review_id,binding)
            reviewed=self.db.execute("SELECT body FROM records WHERE id=? AND kind='verification_review'",(review_id,)).fetchone()
            source_version=json.loads(reviewed[0]).get('repo_sha256') if reviewed else None
            if source_version:value['source_version']=source_version
            state='pending'
        else:
            state='needs_verification'
        from .verification import requirements
        identity={k:v for k,v in value.items() if k!='verification_review'}
        identity['mvp']=requirements(mvp)
        key=digest(canonical(identity).encode())
        for prior in self.db.execute("SELECT id,body FROM outbox WHERE state IN ('sending','unknown')"):
            existing=json.loads(prior['body'])
            if prior['id']!=key and existing.get('project')==binding['project'] and existing.get('workspace')==binding['workspace'] and isinstance(existing.get('mvp'),dict) and requirements(existing['mvp'])==requirements(mvp):
                raise Blocked('同一目标/MVP已有未知提交；先查询原请求，不能换审阅或验证命令重复派工')
        if state=='needs_verification':
            atomic(self.root/'任务记录'/('spec-'+key+'.json'),canonical({'status':state,**value}))
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO outbox VALUES (?,?,?,NULL)',(key,canonical(value),state))
        return key

    @cooperative
    def profile(self, value):
        required = ('user','goals','projects','workflow','tools','backup','notifications','sensitive_rules_confirmed')
        if any(k not in value for k in required) or value['sensitive_rules_confirmed'] is not True:
            raise Blocked('首次访谈不完整或敏感规则未确认')
        self.put('profile', value)
        return value

    def doctor(self):
        integrity=self.db.execute('PRAGMA integrity_check').fetchone()[0]
        if integrity!='ok':raise Blocked('数据库完整性校验失败')
        runtime='development'
        if (self.root/'发行.json').is_file():
            from .distribution import release_verify
            release_verify(self.root)
            runtime='verified'
        from .board import backup_reminders
        known={'数据','知识库','看板','任务记录','会话档案','代码','迁移','工具','.obsidian','.git','portable','tests','docs','scripts','adapters','.github','__pycache__'}
        manifest=self.root/'发行.json'
        if manifest.is_file():known.update(Path(rel).parts[0] for rel in json.loads(manifest.read_text()).get('files',{}))
        unknown=sorted(p.name for p in self.root.iterdir() if p.is_dir() and p.name not in known)
        reminders=backup_reminders(self)
        if unknown:reminders.append('安装目录里有不认识的顶层目录：'+'、'.join(unknown)+'；未移动或删除')
        return {'不认识的顶层目录':unknown,'提醒':reminders,'version':VERSION,'schema':1,'device':self.device,'database':integrity,'runtime':runtime,'execution':self.get('execution'),'vault':str(self.root/'知识库'),'projects':self.db.execute('SELECT count(*) FROM projects').fetchone()[0],'external':'LIVE_UNVERIFIED','retention':_retention()}


def _retention():
    """自检里显示的备份保留规则，取程序实际执行的那套（distribution.RETAIN），不再显示协议里写死的旧数字（11-14 0.2.44 自检曾显示 upgrade 1、permanent_delete false）。"""
    from .distribution import RETAIN
    return {**RETAIN,'过期备份':'直接删除（只删带清单文件的本程序备份目录）','恢复区残留':'放满 30 天直接删除','旧版程序':'只留最新 2 份','归档':'满 30 天每周提醒，用户点头才删','permanent_delete':True}

