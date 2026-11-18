"""Transactional project-set transport. Standalone CLI pending main integration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from .core import Blocked
from .migration import SECRET_NAME, SECRET_CONTENT


def safe_path(root,rel):
    p=Path(rel)
    if p.is_absolute() or not p.parts or '..' in p.parts:raise Blocked('unsafe relative path')
    target=Path(root)/p
    for parent in [target,*target.parents]:
        if parent==Path(root).parent:break
        if parent.is_symlink():raise Blocked('symlink traversal')
    return target


def copy_tree(source,target,*,skip_git=False,external_links=None,ignored='preserve',sensitive_encrypted_only=False):
    source=Path(source).resolve();target=Path(target); external_links=external_links or {}
    if ignored not in ('preserve','choose'):raise Blocked('unknown ignored policy')
    from .sensitive import private_assets
    if not sensitive_encrypted_only and private_assets(source):raise Blocked('private assets require explicit sensitive transport choice')
    from .git_portable import git
    trusted_runtime=set()
    if (source/'发行.json').is_file():
        from .distribution import release_verify
        trusted_runtime=set(release_verify(source)['files'])
    caches=set();excluded_files=set();generated={}
    if (source/'发行.json').is_file():
        from .dependencies import generated_environment_dirs
        generated=generated_environment_dirs(source)
    if (source/'.git').exists():
        ignored_files=[os.fsdecode(v) for v in git(source,'ls-files','--others','--ignored','--exclude-standard','-z').stdout.split(b'\0') if v]
        for rel in ignored_files:
            # Only lockfile-backed package caches are disposable.
            if rel.startswith('node_modules/') and any((source/n).is_file() for n in ('package-lock.json','npm-shrinkwrap.json','pnpm-lock.yaml','yarn.lock')):
                caches.add('node_modules');excluded_files.add(rel)
            elif ignored=='choose':raise Blocked('ignored file needs preserve decision: '+rel)
    for parent,dirs,files in os.walk(source,followlinks=False):
        relparent=Path(parent).relative_to(source)
        dirs[:]=[n for n in dirs if not (skip_git and n=='.git') and (relparent/n).as_posix() not in generated]
        for name in dirs+files:
            if skip_git and name=='.git':continue
            p=Path(parent)/name;rel=p.relative_to(source).as_posix(); dest=target/rel;s=p.lstat()
            if rel in excluded_files:continue
            if p.is_dir() and not p.is_symlink() and rel.startswith('node_modules'):
                # Do not create empty disposable cache directories. Still walk
                # them so tracked/staged/necessary untracked assets survive.
                continue
            if not sensitive_encrypted_only and SECRET_NAME.search(rel):raise Blocked('sensitive name requires separate encrypted package: '+rel)
            if p.is_symlink():
                resolved=p.resolve(strict=True)
                if resolved.is_relative_to(source):
                    link=os.path.relpath(target/resolved.relative_to(source),dest.parent)
                    dest.parent.mkdir(parents=True,exist_ok=True);dest.symlink_to(link)
                else:
                    if external_links.get(rel)!=str(resolved):raise Blocked('external link not registered: '+rel)
                    if resolved.is_dir():
                        dest.mkdir(parents=True,exist_ok=True);copy_tree(resolved,dest,sensitive_encrypted_only=sensitive_encrypted_only)
                    else:
                        data=resolved.read_bytes()
                        if not sensitive_encrypted_only and rel not in trusted_runtime and SECRET_CONTENT.search(data):raise Blocked('sensitive external link')
                        dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(data)
                continue
            if stat.S_ISDIR(s.st_mode):dest.mkdir(parents=True,exist_ok=True);continue
            if not stat.S_ISREG(s.st_mode):raise Blocked('special file: '+rel)
            data=p.read_bytes()
            if not sensitive_encrypted_only and rel not in trusted_runtime and SECRET_CONTENT.search(data):raise Blocked('sensitive content requires encrypted package: '+rel)
            if p.suffix.lower() in ('.db','.sqlite','.sqlite3','.mdb','.realm') and not data.startswith(b'SQLite format 3\x00'):raise Blocked('unknown database requires dump adapter: '+rel)
            dest.parent.mkdir(parents=True,exist_ok=True)
            if data.startswith(b'SQLite format 3\x00'):
                a=sqlite3.connect(p.as_uri()+'?mode=ro',uri=True);b=sqlite3.connect(dest)
                try:
                    a.backup(b)
                    if b.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise Blocked('SQLite integrity failure')
                finally:b.close();a.close()
            elif name.endswith(('-wal','-shm','-journal')):
                db=Path(str(p).rsplit('-',1)[0])
                if db.is_file() and db.read_bytes()[:16]==b'SQLite format 3\x00':continue
                raise Blocked('unknown database sidecar')
            else:dest.write_bytes(data)
            os.chmod(dest,stat.S_IMODE(s.st_mode))
    return {'rebuild':sorted(caches), 'generated_environment_rebuild':generated}


def manifest(root):
    result={};root=Path(root)
    for parent,dirs,files in os.walk(root,followlinks=False):
        for n in dirs+files:
            p=Path(parent)/n;rel=p.relative_to(root).as_posix()
            if p.is_symlink():
                if not p.resolve().is_relative_to(root):raise Blocked('package link escapes')
                result[rel]={'link':os.readlink(p)}
            elif p.is_file():result[rel]={'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),'mode':stat.S_IMODE(p.stat().st_mode),'size':p.stat().st_size}
            else:result[rel]={'directory':True}
    return result


def _create(projects,target,*,previous=None,fail_after=None,quiesced=False,database_ddl_hooks=None):
    """projects: [{id, source, dependencies?, external_links?}]. One atomic set.

    Incremental mode reuses unchanged bytes from a verified previous full set;
    each published set is independently restorable, with no fragile delta chain.
    """
    from .git_portable import export,fingerprint
    target=Path(target).absolute()
    if target.exists():raise Blocked('set target exists')
    if not projects:raise Blocked('empty project set')
    ids=[p['id'] for p in projects]
    if len(set(ids))!=len(ids) or any(not i or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_' for c in i) for i in ids):raise Blocked('invalid or duplicate project identity')
    sources=[Path(p['source']).resolve(strict=True) for p in projects]
    if any(target.is_relative_to(p) or p.is_relative_to(target) for p in sources):raise Blocked('source and package overlap')
    if len(set(sources))!=len(sources) or any(a!=b and a.is_relative_to(b) for a in sources for b in sources):raise Blocked('duplicate or overlapping active paths')
    before=[fingerprint(p) for p in sources]
    target.parent.mkdir(parents=True,exist_ok=True)
    required=sum(p.stat().st_size for root in sources for p in root.rglob('*') if p.is_file() and not p.is_symlink())*2+1024*1024
    if shutil.disk_usage(target.parent).free<required:raise Blocked('insufficient space; no publication')
    old=verify(previous) if previous else None
    with tempfile.TemporaryDirectory(dir=target.parent,prefix='.project-set-') as tmp:
        stage=Path(tmp);payload=stage/'payload';payload.mkdir();entries=[]
        for n,(spec,src) in enumerate(zip(projects,sources)):
            if fail_after is not None and n>=fail_after:raise Blocked('interrupted; no complete publication')
            dest=payload/spec['id']
            if (src/'.git').exists():details=export(src,dest,external_links=spec.get('external_links'))
            else:dest.mkdir();details=copy_tree(src,dest,external_links=spec.get('external_links'))
            dependency_results=[]
            if spec.get('dependencies'):
                from .db_adapters import Adapter, PrivateFileCredentials
                backupdir=dest/'.portable-databases'
                if backupdir.exists():raise Blocked('reserved database backup path already exists')
                backupdir.mkdir()
                for index,dep in enumerate(spec['dependencies']):
                    if not {'engine','connection'}<=dep.keys():raise Blocked('database dependency needs executable engine/connection recipe')
                    provider=PrivateFileCredentials.from_reference(dep['credential_reference'],dep['engine']) if dep.get('credential_reference') else None
                    adapter=Adapter(dep['engine'],dep.get('tools'),credential_provider=provider,ddl_hooks=database_ddl_hooks)
                    meta=adapter.backup(dep['connection'],backupdir/str(index),mysql_ddl_hook=dep.get('ddl_hook'))
                    dependency_results.append({'engine':dep['engine'],'package':'.portable-databases/'+str(index),'manifest':meta,'restore':'Adapter.restore into explicitly authorized empty target'})
            entries.append({'id':spec['id'],'source':str(src),'portable':'payload/'+spec['id'],'dependencies':dependency_results,'rebuild':details.get('rebuild',[]),'restore_conditions':['new independent path','dependencies restored and validated','new task identity; no inherited approval']})
        if before!=[fingerprint(p) for p in sources]:raise Blocked('project set changed; no publication')
        files=manifest(payload);reused=0
        if old:
            for rel,meta in files.items():
                if 'sha256' in meta and old['files'].get(rel)==meta:
                    # Copy bytes rather than hardlink: later media edits cannot
                    # mutate both generations through a shared inode.
                    dest=safe_path(payload,rel);dest.unlink();shutil.copyfile(safe_path(Path(previous)/'payload',rel),dest);os.chmod(dest,meta['mode']);reused+=1
        value={'schema':'portable.set/v1','projects':entries,'files':files,'mode':'incremental' if old else 'full','reused_files':reused,'active_paths':{p['id']:p['portable'] for p in entries},'consistency':'executing caller quiescence barrier across set' if quiesced else 'source stable across copy; each database consistent separately; cross-database consistency NOT_GUARANTEED'}
        raw=json.dumps(value,sort_keys=True,ensure_ascii=False).encode();(stage/'manifest.json').write_bytes(raw);(stage/'COMPLETE').write_text(hashlib.sha256(raw).hexdigest())
        verify(stage);os.rename(stage,target)
    return value


def create(projects,target,*,previous=None,fail_after=None,database_barrier=None,database_ddl_hooks=None):
    if database_barrier is None:return _create(projects,target,previous=previous,fail_after=fail_after,database_ddl_hooks=database_ddl_hooks)
    with database_barrier:return _create(projects,target,previous=previous,fail_after=fail_after,quiesced=True,database_ddl_hooks=database_ddl_hooks)


def verify(path):
    path=Path(path)
    if path.is_symlink() or any((path/n).is_symlink() for n in ('COMPLETE','manifest.json','payload')) or not (path/'COMPLETE').is_file():raise Blocked('incomplete package')
    raw=(path/'manifest.json').read_bytes()
    if hashlib.sha256(raw).hexdigest()!=(path/'COMPLETE').read_text():raise Blocked('manifest checksum mismatch')
    value=json.loads(raw)
    if value.get('schema')!='portable.set/v1' or manifest(path/'payload')!=value['files']:raise Blocked('package payload mismatch')
    return value


def restore(path,target):
    value=verify(path);target=Path(target).absolute()
    if target.exists() or target.is_symlink():raise Blocked('restore target exists')
    for p in target.parents:
        if p.is_symlink():raise Blocked('restore parent symlink')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent,prefix='.set-restore-') as tmp:
        stage=Path(tmp)/'restored';shutil.copytree(Path(path)/'payload',stage,symlinks=True)
        if manifest(stage)!=value['files']:raise Blocked('restore readback mismatch')
        from .git_portable import git
        for entry in value['projects']:
            if (stage/entry['id']/'.git').is_dir():git(stage/entry['id'],'fsck','--full')
        from .runtime_identity import reset_restored_tree
        for entry in value['projects']:reset_restored_tree(stage/entry['id'],target/entry['id'])
        os.rename(stage,target)
    return {'restored':str(target),'active_paths':{p['id']:str(target/p['id']) for p in value['projects']},'execution':'new identity required'}


def register(subparser):
    p=subparser.add_parser('portable-set');p.add_argument('operation',choices=['create','verify','restore']);p.add_argument('path');p.add_argument('--spec');p.add_argument('--target');p.set_defaults(portability_run=run)
    return p


def run(args):
    if args.operation=='create':return create(json.loads(Path(args.spec).read_text()),args.path)
    if args.operation=='verify':return verify(args.path)
    return restore(args.path,args.target)


def main(argv=None):
    p=argparse.ArgumentParser();s=p.add_subparsers(required=True);register(s)
    try:print(json.dumps(run(p.parse_args(argv)),ensure_ascii=False));return 0
    except (Blocked,OSError,ValueError,KeyError) as e:print(json.dumps({'status':'BLOCKED','reason':str(e)},ensure_ascii=False));return 2

if __name__=='__main__':raise SystemExit(main())
