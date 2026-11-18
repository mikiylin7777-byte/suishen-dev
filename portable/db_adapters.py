"""Consistent database dumps. Connections contain no inline credentials.

Cross-database consistency requires a caller-provided *real* quiescence context;
without one each dump is consistent independently, never globally advertised.
"""
import argparse
import configparser
from contextlib import ExitStack, nullcontext
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sqlite3
import subprocess
import tempfile
from .core import Blocked


class PrivateFileCredentials:
    """Explicit local selection only. References never contain password bytes.

    A secure snapshot, rather than the caller's mutable file, is given to tools.
    MySQL files contain ONLY [client] password, preventing option-file overrides
    such as force, init-command or lock-all-tables.
    """
    def __init__(self,engine,path):
        if engine not in ('postgres','mysql'):raise Blocked('unsupported credential provider engine')
        self.engine=engine;self.path=Path(path)
        if not self.path.is_absolute() or '..' in self.path.parts:raise Blocked('credential reference must be absolute')

    @classmethod
    def from_reference(cls,reference,engine):
        if not isinstance(reference,dict) or set(reference)!={'kind','engine','path'} or reference.get('kind')!='portable.private-file/v1' or reference.get('engine')!=engine:
            raise Blocked('invalid private credential reference')
        if not isinstance(reference['path'],str) or not reference['path'] or '\x00' in reference['path']:raise Blocked('invalid private credential reference')
        return cls(engine,reference['path'])

    def snapshot(self,directory):
        try:
            # Reject links in all components; do not follow a personal alias.
            if any(part.is_symlink() for part in (self.path,*self.path.parents)):
                raise Blocked('credential file links forbidden')
            directory_fd=os.open('/',os.O_RDONLY|os.O_DIRECTORY)
            try:
                for component in self.path.parts[1:-1]:
                    child_fd=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory_fd)
                    os.close(directory_fd);directory_fd=child_fd
                fd=os.open(self.path.name,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK,dir_fd=directory_fd)
            finally:os.close(directory_fd)
            with os.fdopen(fd,'rb') as src:
                info=os.fstat(src.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1 or info.st_size>1024*1024:
                    raise Blocked('credential file must be owned private regular 0600 file')
                content=src.read(1024*1024+1)
                if len(content)>1024*1024:raise Blocked('credential file too large')
            if self.engine=='mysql':
                parser=configparser.ConfigParser(interpolation=None)
                parser.read_string(content.decode('utf-8'))
                if parser.defaults() or parser.sections()!=['client'] or set(parser['client'])!={'password'}:
                    raise Blocked('MySQL credential file allows only [client] password')
                password=parser['client']['password']
                if '\n' in password or '\r' in password:raise Blocked('multiline credential options forbidden')
                content=f'[client]\npassword={password}\n'.encode('utf-8')
            dest=Path(directory)/'selected-credentials'
            descriptor=os.open(dest,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
            with os.fdopen(descriptor,'wb') as out:out.write(content)
            return dest
        except Blocked:raise
        except (OSError,ValueError,UnicodeError,configparser.Error):
            raise Blocked('private credential provider unavailable or invalid') from None


class ProjectDDLHooks:
    """Trusted local code registers factories; JSON selects a name, never code."""
    def __init__(self):self.factories={}
    def register(self,name,factory):
        if not re.fullmatch(r'[A-Za-z0-9_-]+',name) or name in self.factories or not callable(factory):raise Blocked('invalid project DDL hook registration')
        self.factories[name]=factory
        return self
    def context(self,name,connection):
        if not isinstance(name,str) or name not in self.factories:raise Blocked('project DDL hook is not registered locally')
        context=self.factories[name](dict(connection))
        if not hasattr(context,'__enter__') or not hasattr(context,'__exit__'):raise Blocked('project DDL hook must execute a context manager')
        return context


class Adapter:
    TOOLS={'postgres':('pg_dump','pg_restore','psql'),'mysql':('mysqldump','mysql'),'sqlite':()}
    def __init__(self,engine,tools=None,*,credential_provider=None,ddl_hooks=None):
        if engine not in self.TOOLS:raise Blocked('unknown database engine; raw copy forbidden')
        self.engine=engine;self.tools={};self.credential_provider=credential_provider;self.ddl_hooks=ddl_hooks
        if credential_provider is not None and (not isinstance(credential_provider,PrivateFileCredentials) or credential_provider.engine!=engine):raise Blocked('credential provider engine mismatch')
        for n in self.TOOLS[engine]:
            p=(tools or {}).get(n) or shutil.which(n)
            if not p or not Path(p).is_absolute() or not Path(p).is_file():raise Blocked('missing database tool: '+n)
            self.tools[n]=str(p)

    def call(self,name,args,*,stdin=None,stdout=None):
        # Never expose database output/errors; dumps can contain sensitive data.
        # Ignore ambient PG*/MYSQL* variables and personal option files.
        with tempfile.TemporaryDirectory(prefix='portable-db-home-') as home:
            env={'HOME':home,'PATH':os.defpath,'PGPASSFILE':str(Path(home)/'no-password-file'),'PGSERVICEFILE':str(Path(home)/'no-service-file')}
            selected=self.credential_provider.snapshot(home) if self.credential_provider is not None else None
            if self.engine=='postgres' and selected is not None:env['PGPASSFILE']=str(selected)
            prefix=[('--defaults-file='+str(selected)) if selected is not None else '--no-defaults','--no-login-paths'] if self.engine=='mysql' else []
            p=subprocess.run([self.tools[name],*prefix,*args],stdin=stdin,stdout=stdout or subprocess.PIPE,stderr=subprocess.PIPE,env=env,timeout=3600)
        if p.returncode:raise Blocked(name+' failed; exit '+str(p.returncode))
        return p.stdout if stdout is None else b''

    def versions(self):
        return {n:self.call(n,['--version']).decode(errors='replace').strip() for n in self.tools}

    def connection(self,c):
        allowed={'host','port','database','user','path','service'}|({'socket'} if self.engine=='mysql' else set())
        if set(c)-allowed:raise Blocked('credentials/unknown connection fields forbidden')
        if any(not isinstance(v,(str,int)) or '\x00' in str(v) or str(v).startswith('-') for v in c.values()):raise Blocked('invalid connection value')
        if self.engine=='sqlite':return []
        if self.engine=='postgres' and c.get('service'):
            raise Blocked('ambient PostgreSQL service files forbidden; provide explicit host/database/user')
        result=[]
        for k in ('host','port','user','socket'):
            if k in c:result.append('--'+('username' if k=='user' and self.engine=='postgres' else k)+'='+str(c[k]))
        database=c.get('database','')
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]*',database):raise Blocked('database must be an explicit simple name')
        return result+(['--dbname='+database] if self.engine=='postgres' else [database])

    def backup(self,connection,target,*,mysql_ddl_barrier=None,mysql_ddl_hook=None):
        args=self.connection(connection)
        if mysql_ddl_hook is not None:
            if mysql_ddl_barrier is not None or self.ddl_hooks is None:raise Blocked('DDL hook requires local registration and no second barrier')
            mysql_ddl_barrier=self.ddl_hooks.context(mysql_ddl_hook,connection)
        if self.engine=='mysql' and mysql_ddl_barrier is None:
            raise Blocked('MySQL backup requires an executing project-specific DDL quiescence protocol; global locks forbidden')
        target=Path(target).absolute()
        if target.exists():raise Blocked('dump target exists')
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=target.parent,prefix='.dump-') as tmp, ExitStack() as stack:
            if self.engine=='mysql':stack.enter_context(mysql_ddl_barrier)
            stage=Path(tmp);dump=stage/'dump'
            versions=self.versions();runtime=None
            if self.engine=='sqlite':
                src=Path(connection['path']).resolve(strict=True);a=sqlite3.connect(src.as_uri()+'?mode=ro',uri=True);b=sqlite3.connect(dump)
                try:a.backup(b);self.sqlite_check(b)
                finally:b.close();a.close()
            elif self.engine=='postgres':
                runtime=self.postgres_runtime(args)
                self.write_asset(stage,'runtime.json',json.dumps(runtime).encode())
                with dump.open('wb') as out:self.call('pg_dump',['--format=custom','--no-owner','--no-acl','--no-password','--no-subscriptions',*args],stdout=out)
                self.call('pg_restore',['--list',str(dump)])
            else:
                # The executing caller barrier must prevent project DDL throughout
                # precheck and dump. Engine inspection cannot rule out concurrent DDL.
                engines=self.call('mysql',['--batch','--skip-column-names',*args,
                    "--execute=SELECT DISTINCT COALESCE(ENGINE, 'UNKNOWN') FROM information_schema.tables WHERE table_schema=DATABASE() AND TABLE_TYPE='BASE TABLE'"])
                if any(line.strip().lower()!=b'innodb' for line in engines.splitlines()):
                    raise Blocked('non-InnoDB/unknown table engine; project-specific stopped-write backup protocol required; no global lock fallback')
                with dump.open('wb') as out:self.call('mysqldump',['--single-transaction','--skip-lock-tables','--no-tablespaces','--routines','--skip-events','--triggers','--hex-blob','--set-gtid-purged=OFF',*args],stdout=out)
                if not dump.stat().st_size:raise Blocked('empty MySQL dump')
                events=self.call('mysql',['--batch','--raw','--skip-column-names',*args,
                    "--execute=SELECT JSON_OBJECT('name',EVENT_NAME,'status',STATUS) FROM information_schema.events WHERE event_schema=DATABASE() ORDER BY EVENT_NAME"])
                try:inventory=[json.loads(line) for line in events.splitlines()]
                except (ValueError,UnicodeError):raise Blocked('invalid MySQL event inventory') from None
                if any(not isinstance(item,dict) or set(item)!={'name','status'} for item in inventory):raise Blocked('invalid MySQL event inventory')
                event_asset=stage/'events.sql'
                with event_asset.open('wb') as out:
                    self.call('mysqldump',['--single-transaction','--skip-lock-tables','--no-tablespaces','--no-data','--no-create-info','--skip-triggers','--skip-routines','--events','--set-gtid-purged=OFF',*args],stdout=out)
                os.chmod(event_asset,0o600)
                runtime={'policy':'definitions-held-for-explicit-approval/v1','events':inventory,
                    'pending':['events.sql'] if inventory else [],'recipe':'Review events.sql on an isolated target; explicitly approve definitions and scheduling before executing. Never replay automatically.'}
                self.write_asset(stage,'runtime.json',json.dumps(runtime).encode())
            os.chmod(dump,0o600)
            meta={'schema':'portable.database/v1','engine':self.engine,'versions':versions,'sha256':hashlib.sha256(dump.read_bytes()).hexdigest(),'bytes':dump.stat().st_size,'consistency':'sqlite-online-backup' if self.engine=='sqlite' else 'postgres-snapshot' if self.engine=='postgres' else 'mysql-innodb-snapshot-with-project-ddl-barrier','scope':'one database; cross-database barrier required'}
            if runtime is not None:
                meta['runtime_policy']='definitions-held-for-explicit-approval/v1'
                meta['assets']={name:{'sha256':hashlib.sha256((stage/name).read_bytes()).hexdigest(),'bytes':(stage/name).stat().st_size} for name in (('events.sql','runtime.json') if self.engine=='mysql' else ('runtime.json',))}
            (stage/'manifest.json').write_text(json.dumps(meta));os.rename(stage,target)
        return meta

    @staticmethod
    def write_asset(stage,name,content):
        path=stage/name
        with path.open('xb') as out:out.write(content)
        os.chmod(path,0o600)

    def postgres_runtime(self,args):
        # Deliberately never select subscription conninfo (may contain passwords).
        raw=self.call('psql',['--no-password','--no-psqlrc','--tuples-only','--no-align','--set=ON_ERROR_STOP=1',*args,
            "--command=SELECT json_build_object('subscriptions',COALESCE((SELECT json_agg(json_build_object('name',subname,'enabled',subenabled,'publications',subpublications)) FROM pg_subscription WHERE subdbid=(SELECT oid FROM pg_database WHERE datname=current_database())), '[]'::json),'extensions',COALESCE((SELECT json_agg(extname) FROM pg_extension WHERE extname<>'plpgsql'), '[]'::json))"])
        try:
            value=json.loads(raw)
            if not isinstance(value,dict) or set(value)!={'subscriptions','extensions'} or not isinstance(value['subscriptions'],list) or not isinstance(value['extensions'],list):raise ValueError()
        except (ValueError,UnicodeError):raise Blocked('invalid PostgreSQL runtime inventory') from None
        if value['extensions']:raise Blocked('PostgreSQL extension runtime dependencies require a project-specific safe recipe; migration not complete')
        return {'policy':'definitions-held-for-explicit-approval/v1',**value,
            'pending':['subscriptions require separately selected connection credentials and explicit enable approval'] if value['subscriptions'] else [],
            'recipe':'Subscriptions excluded from dump. Recreate from inventory using newly selected private credentials only after explicit approval. Unknown scheduler/extension dependencies block backup.'}

    @staticmethod
    def sqlite_check(db):
        if db.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise Blocked('SQLite integrity failure')

    def preflight(self,package):
        package=Path(package);meta=json.loads((package/'manifest.json').read_text());dump=package/'dump'
        if package.is_symlink() or dump.is_symlink() or meta.get('schema')!='portable.database/v1' or meta['engine']!=self.engine:raise Blocked('invalid database package')
        if hashlib.sha256(dump.read_bytes()).hexdigest()!=meta['sha256'] or dump.stat().st_size!=meta['bytes']:raise Blocked('database checksum mismatch')
        if self.engine in ('mysql','postgres'):
            expected={'events.sql','runtime.json'} if self.engine=='mysql' else {'runtime.json'}
            if meta.get('runtime_policy')!='definitions-held-for-explicit-approval/v1' or set(meta.get('assets',{}))!=expected:
                raise Blocked('database package lacks safe runtime-definition separation; rebuild backup')
            for name,entry in meta['assets'].items():
                asset=package/name
                if asset.is_symlink() or not asset.is_file() or asset.stat().st_size!=entry['bytes'] or hashlib.sha256(asset.read_bytes()).hexdigest()!=entry['sha256']:
                    raise Blocked('database runtime asset checksum mismatch')
            try:
                runtime=json.loads((package/'runtime.json').read_bytes())
                if not isinstance(runtime,dict) or runtime.get('policy')!=meta['runtime_policy'] or not isinstance(runtime.get('pending'),list) or any(not isinstance(item,str) for item in runtime['pending']):raise ValueError()
                definitions=runtime.get('events' if self.engine=='mysql' else 'subscriptions')
                if not isinstance(definitions,list) or any(not isinstance(item,dict) for item in definitions):raise ValueError()
            except (ValueError,UnicodeError):raise Blocked('invalid database runtime asset inventory') from None
        versions=self.versions()
        for name,old in meta['versions'].items():
            a=re.search(r'\d+',old);b=re.search(r'\d+',versions.get(name,''))
            if not a or not b or a.group()!=b.group():raise Blocked('database tool major version differs; explicit compatible adapter required')
        if self.engine=='postgres':self.call('pg_restore',['--list',str(dump)])
        elif self.engine=='sqlite':
            db=sqlite3.connect(dump.resolve().as_uri()+'?mode=ro',uri=True)
            try:self.sqlite_check(db)
            finally:db.close()
        return meta

    def restore(self,package,connection,*,authorize_write=False):
        self.preflight(package);args=self.connection(connection);dump=Path(package)/'dump'
        held=json.loads((Path(package)/'runtime.json').read_text()) if self.engine in ('mysql','postgres') else {}
        if not authorize_write:raise Blocked('explicit isolated database restore authorization required')
        if self.engine=='sqlite':
            dest=Path(connection['path']).absolute()
            if dest.exists():raise Blocked('database restore never overwrites')
            dest.parent.mkdir(parents=True,exist_ok=True)
            # exclusive create prevents accidental replacement
            with dest.open('xb') as out:out.write(dump.read_bytes())
            os.chmod(dest,0o600)
        elif self.engine=='postgres':
            runtime=self.postgres_runtime(args)
            if runtime['subscriptions']:raise Blocked('restore target has runtime subscriptions')
            # Fail before restore if target contains any user relations.
            count=self.call('psql',['--no-password','--no-psqlrc','--tuples-only','--no-align','--set=ON_ERROR_STOP=1',*args,'--command=SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN (\'pg_catalog\',\'information_schema\') AND n.nspname NOT LIKE \'pg_toast%\';'])
            if count.strip()!=b'0':raise Blocked('restore target is not empty')
            self.call('pg_restore',['--no-password','--exit-on-error','--single-transaction','--no-owner','--no-acl','--no-subscriptions',*args,str(dump)])
        else:
            count=self.call('mysql',['--batch','--skip-column-names',*args,'--execute=SELECT (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema=DATABASE()) + (SELECT COUNT(*) FROM information_schema.events WHERE event_schema=DATABASE()) + (SELECT COUNT(*) FROM information_schema.routines WHERE routine_schema=DATABASE())'])
            if count.strip()!=b'0':raise Blocked('restore target is not empty')
            with dump.open('rb') as inp:self.call('mysql',['--binary-mode','--batch','--skip-reconnect',*args],stdin=inp)
        return {'held_runtime_definitions':held.get('events',held.get('subscriptions',[])),'pending_runtime_definitions':held.get('pending',[]),'runtime_assets_executed':False,'restored':True,'engine':self.engine,'validation':'tool completion; application-level checks remain caller responsibility'}


def backup_group(specs,target,barrier=None,*,ddl_hooks=None):
    """Barrier must be an executing context manager, not a JSON assertion."""
    target=Path(target)
    if target.exists():raise Blocked('database set target exists')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as tmp, ExitStack() as stack:
        if barrier is not None:stack.enter_context(barrier)
        stage=Path(tmp)/'group';stage.mkdir();results=[]
        for n,spec in enumerate(specs):results.append(Adapter(spec['engine'],spec.get('tools'),credential_provider=PrivateFileCredentials.from_reference(spec['credential_reference'],spec['engine']) if spec.get('credential_reference') else None,ddl_hooks=ddl_hooks).backup(spec['connection'],stage/str(n),mysql_ddl_barrier=nullcontext() if barrier is not None and not spec.get('ddl_hook') else None,mysql_ddl_hook=spec.get('ddl_hook')))
        value={'databases':results,'cross_database_consistency':'quiesced-by-executing-barrier' if barrier else 'NOT_GUARANTEED'}
        (stage/'group.json').write_text(json.dumps(value));os.rename(stage,target)
    return value


def credentials_wizard(engine,path):
    import getpass,sys
    if engine not in ('postgres','mysql') or not sys.stdin.isatty():raise Blocked('私密数据库设置须本人在交互终端选择')
    path=Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink() or any(p.is_symlink() for p in path.parents):raise Blocked('私密文件必须为全新非链接路径')
    password=getpass.getpass('数据库密码（隐藏输入，不进入参数或日志）：')
    if any(c in password for c in ('\n','\r','\x00')):raise Blocked('密码格式无效')
    if engine=='mysql':
        if any(c in password for c in ('#',';','"',"'",'\\')):raise Blocked('该私密选项文件不支持此密码形状；请选择已有受支持600文件')
        content='[client]\npassword='+password+'\n'
    else:
        fields=[input(label+'：').strip() for label in ('主机','端口','数据库','用户')]
        def escape(v):return v.replace('\\','\\\\').replace(':','\\:')
        content=':'.join(escape(v) for v in [*fields,password])+'\n'
    path.parent.mkdir(parents=True,exist_ok=True)
    fd=os.open(path,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as handle:handle.write(content)
    return {'kind':'portable.private-file/v1','engine':engine,'path':str(path),'secret_output':False}


def register(subparser,*,ddl_hooks=None):
    p=subparser.add_parser('database');p.add_argument('operation',choices=['backup','preflight','restore','credentials-setup']);p.add_argument('engine');p.add_argument('package');p.add_argument('--connection');p.add_argument('--authorize-isolated-write',action='store_true');p.add_argument('--credential-reference');p.add_argument('--ddl-hook');p.set_defaults(database_run=run,database_ddl_hooks=ddl_hooks);return p

def run(args):
    if args.operation=='credentials-setup':return credentials_wizard(args.engine,args.package)
    reference=getattr(args,'credential_reference',None)
    provider=PrivateFileCredentials.from_reference(json.loads(Path(reference).read_text()),args.engine) if reference else None
    a=Adapter(args.engine,credential_provider=provider,ddl_hooks=getattr(args,'database_ddl_hooks',None))
    if args.operation=='preflight':return a.preflight(args.package)
    c=json.loads(Path(args.connection).read_text())
    if args.operation=='backup':return a.backup(c,args.package,mysql_ddl_hook=getattr(args,'ddl_hook',None))
    return a.restore(args.package,c,authorize_write=args.authorize_isolated_write)

def main(argv=None,*,ddl_hooks=None):
    p=argparse.ArgumentParser();register(p.add_subparsers(required=True),ddl_hooks=ddl_hooks)
    try:print(json.dumps(run(p.parse_args(argv))));return 0
    except (Blocked,OSError,ValueError,KeyError) as e:print(json.dumps({'status':'BLOCKED','reason':str(e)}));return 2

if __name__=='__main__':raise SystemExit(main())
