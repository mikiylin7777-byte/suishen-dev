"""Read-only Git export and history-aware reconciliation. No source Git writes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from .core import Blocked


def git(root, *args, input=None, check=True):
    env={**os.environ,'GIT_OPTIONAL_LOCKS':'0','GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_TERMINAL_PROMPT':'0','GIT_NO_LAZY_FETCH':'1','GIT_LFS_SKIP_SMUDGE':'1'}
    # gc.auto=0 / maintenance.auto=false: a detached auto-gc spawned by `git commit`
    # repacks loose objects while the next `git fsck --full` is still reading them
    # (observed: 1064 loose objects packed within 0.5s), which fails export randomly.
    p=subprocess.run(['git','-c','core.hooksPath='+os.devnull,'-c','core.fsmonitor=false','-c','gc.auto=0','-c','maintenance.auto=false','-C',str(root),*args],input=input,capture_output=True,env=env)
    if check and p.returncode:raise Blocked('Git '+args[0]+': '+(p.stderr+p.stdout).decode(errors='replace')[:1500])
    return p


def fingerprint(root):
    """Includes metadata and bytes; never follows filesystem links."""
    root=Path(root); h=hashlib.sha256()
    for parent,dirs,files in os.walk(root,followlinks=False):
        for name in sorted(dirs+files):
            p=Path(parent)/name; s=p.lstat()
            h.update(str(p.relative_to(root)).encode());h.update(str((s.st_mode,s.st_size,s.st_mtime_ns)).encode())
            if p.is_symlink():h.update(os.readlink(p).encode())
            elif p.is_file():h.update(p.read_bytes())
    return h.hexdigest()


def locations(source):
    gd=Path(os.fsdecode(git(source,'rev-parse','--absolute-git-dir').stdout).strip()).resolve()
    raw=Path(os.fsdecode(git(source,'rev-parse','--git-common-dir').stdout).strip())
    common=(raw if raw.is_absolute() else Path(source)/raw).resolve()
    return gd,common


def export(source, target, *, ignored='preserve', external_links=None,sensitive_encrypted_only=False,development=False):
    """Export all objects/refs, exact index and working files to standalone repo.

    External symlinks are read only when their relative name and resolved target
    are explicitly registered. External Git object stores are Git dependencies.
    """
    source=Path(source).resolve(strict=True);target=Path(target).absolute()
    if target.exists() or target.is_relative_to(source):raise Blocked('Git export requires a new independent target')
    gd,common=locations(source)
    roots={source,gd,common}
    for directory in {gd,common}:
        if any(p.is_symlink() for p in directory.rglob('*')):raise Blocked('Git metadata symlink requires explicit normalization')
    # Alternates may be transitive; Git validates object closure, source snapshots
    # additionally detect modifications of every declared object store.
    def alternates(obj,seen):
        obj=obj.resolve()
        if obj in seen:return
        seen.add(obj);roots.add(obj)
        f=obj/'info/alternates'
        if f.exists():
            for line in f.read_text().splitlines():
                p=Path(line);alternates(p if p.is_absolute() else obj/p,seen)
    alternates(common/'objects',set())
    before={p:fingerprint(p) for p in roots}
    shallow=git(source,'rev-parse','--is-shallow-repository').stdout.strip()==b'true'
    if shallow:raise Blocked('shallow history boundary: '+(common/'shallow').read_text().strip()+'; use explicit prepare() into a new repository with fetch=True')
    missing=git(source,'rev-list','--objects','--all','--reflog','--missing=print').stdout
    if any(line.startswith(b'?') for line in missing.splitlines()):raise Blocked('missing Git objects: '+os.fsdecode(missing))
    git(source,'fsck','--full')
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent,prefix='.git-export-') as tmp:
        stage=Path(tmp)/'repo';stage.mkdir()
        from .portability import copy_tree
        if development:
            from .development import copy_code
            copied=copy_code(source,stage)
        else:copied=copy_tree(source,stage,skip_git=True,external_links=external_links,ignored=ignored,sensitive_encrypted_only=sensitive_encrypted_only)
        git(stage,'init')
        # Pack every object including reflog-only and unreachable objects, without
        # preserving an alternate or a machine-specific gitdir.
        objects=git(source,'cat-file','--batch-all-objects','--batch-check=%(objectname)').stdout
        from .migration import SECRET_CONTENT, SECRET_NAME
        history=git(source,'rev-list','--objects','--all','--reflog').stdout
        for line in history.splitlines():
            if development and b' ' in line:
                oid,name=line.split(b' ',1)
                if git(source,'cat-file','-t',os.fsdecode(oid)).stdout.strip()==b'blob':
                    from .development import data_asset
                    if data_asset(os.fsdecode(name),git(source,'cat-file','blob',os.fsdecode(oid)).stdout):raise Blocked('data export history remains at source; development needs detached safe snapshot')
            if not sensitive_encrypted_only and b' ' in line and SECRET_NAME.search(os.fsdecode(line.split(b' ',1)[1])):raise Blocked('sensitive Git history filename requires encrypted package')
        for oid in objects.splitlines():
            if git(source,'cat-file','-t',os.fsdecode(oid)).stdout.strip()==b'blob':
                blob=git(source,'cat-file','blob',os.fsdecode(oid)).stdout
                if not sensitive_encrypted_only and SECRET_CONTENT.search(blob):raise Blocked('sensitive Git object requires encrypted package')
        if objects:
            pack=git(source,'pack-objects','--stdout',input=objects).stdout
            git(stage,'index-pack','--stdin',input=pack)
        refs=git(source,'for-each-ref','--format=%(objectname) %(refname)').stdout
        for line in refs.splitlines():
            oid,ref=line.split(b' ',1);git(stage,'update-ref',os.fsdecode(ref),os.fsdecode(oid))
        for name in ('HEAD','index','ORIG_HEAD','MERGE_HEAD','MERGE_MSG','MERGE_MODE','CHERRY_PICK_HEAD','REVERT_HEAD'):
            p=gd/name
            if p.is_file():shutil.copy2(p,stage/'.git'/name)
        for directory in (common,gd):
            for shared in directory.glob('sharedindex.*'):shutil.copy2(shared,stage/'.git'/shared.name)
        # Retain effective harmless config, including worktree-specific values;
        # external executables, includes, credential and path settings are inert
        # evidence rather than active restored configuration.
        cfg=git(source,'config','--local','--null','--list').stdout
        if not sensitive_encrypted_only and (SECRET_CONTENT.search(cfg) or b'credential' in cfg.lower() or __import__('re').search(rb'https?://[^/\s:@]+:[^/\s@]+@',cfg)):raise Blocked('credential-bearing Git config requires explicit sanitization')
        (stage/'.git/portable-source-config').write_bytes(cfg)
        for key in ('core.filemode','core.ignorecase','core.autocrlf','core.eol'):
            val=git(source,'config','--get',key,check=False)
            if val.returncode==0:git(stage,'config',key,os.fsdecode(val.stdout).strip())
        if (gd/'config.worktree').is_file():
            wc=(gd/'config.worktree').read_bytes()
            if not sensitive_encrypted_only and (SECRET_CONTENT.search(wc) or b'credential' in wc.lower()):raise Blocked('sensitive worktree config')
            (stage/'.git/portable-worktree-config').write_bytes(wc)
        # Reflogs preserve identities and dangling histories, but contain no paths
        # interpreted by Git; copy both common and worktree logs.
        for logs in (common/'logs',gd/'logs'):
            if logs.is_dir():shutil.copytree(logs,stage/'.git/logs',dirs_exist_ok=True)
        lfs=[]
        for oid in objects.splitlines():
            kind=git(source,'cat-file','-t',os.fsdecode(oid)).stdout.strip()
            if kind!=b'blob':continue
            size=int(git(source,'cat-file','-s',os.fsdecode(oid)).stdout)
            if size>1024:continue
            data=git(source,'cat-file','blob',os.fsdecode(oid)).stdout
            if data.startswith(b'version https://git-lfs.github.com/spec/v1\n'):
                fields=dict(line.split(' ',1) for line in data.decode().splitlines()[1:] if ' ' in line)
                ident=fields.get('oid','').removeprefix('sha256:')
                if len(ident)!=64 or any(c not in '0123456789abcdef' for c in ident):raise Blocked('invalid LFS pointer')
                p=common/'lfs/objects'/ident[:2]/ident[2:4]/ident
                if not p.is_file():raise Blocked('missing LFS object sha256:'+ident)
                raw=p.read_bytes()
                if not sensitive_encrypted_only and SECRET_CONTENT.search(raw):raise Blocked('sensitive LFS object requires encrypted package')
                if hashlib.sha256(raw).hexdigest()!=ident or len(raw)!=int(fields['size']):raise Blocked('corrupt LFS object '+ident)
                dest=stage/'.git/lfs/objects'/ident[:2]/ident[2:4]/ident;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(raw);lfs.append(ident)
        # Gitlinks at every level must have initialized matching repositories.
        modules=[]
        for line in git(source,'ls-files','--stage','-z').stdout.split(b'\0'):
            if not line:continue
            meta,name=line.split(b'\t',1)
            if meta.startswith(b'160000 '):
                rel=os.fsdecode(name); sub=source/rel
                if not (sub/'.git').exists():raise Blocked('uninitialized submodule: '+rel)
                shutil.rmtree(stage/rel)
                export(sub,stage/rel,ignored=ignored,external_links=None,sensitive_encrypted_only=sensitive_encrypted_only,development=development);modules.append(rel)
        git(stage,'fsck','--full')
        if any(fingerprint(p)!=v for p,v in before.items()):raise Blocked('Git source changed during export')
        os.rename(stage,target)
    return {**copied,'source':str(source),'target':str(target),'lfs':lfs,'submodules':modules,'rebuild':copied['rebuild'],'config_policy':'safe settings active; original config retained as inert evidence'}


def prepare(source,target,*,fetch=False,remote=None,fetch_lfs=False):
    """Explicit history completion, preserving source refs/index/work files.

    Network use requires both fetch=True and an explicit credential-free upstream.
    It operates exclusively on a disposable mirror, never the source repository.
    """
    source=Path(source).resolve(strict=True);target=Path(target).absolute()
    if target.exists() or target.is_relative_to(source):raise Blocked('prepare target must be new and independent')
    gd,common=locations(source);before={p:fingerprint(p) for p in {source,gd,common}}
    if fetch and (not remote or __import__('re').search(r'://[^/]*@',remote)):raise Blocked('explicit credential-free upstream required for fetch')
    if fetch_lfs and not fetch:raise Blocked('LFS fetch requires explicit network preparation')
    if not fetch:return export(source,target)
    target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent,prefix='.git-prepare-') as tmp:
        stage=Path(tmp)/'repo';stage.mkdir()
        git(source,'clone','--mirror','--no-local',str(source),str(stage/'.git'))
        if git(stage/'.git','rev-parse','--is-shallow-repository').stdout.strip()==b'true':
            git(stage/'.git','fetch','--unshallow',remote,'+refs/*:refs/portable-upstream/*')
        else:git(stage/'.git','fetch',remote,'+refs/*:refs/portable-upstream/*')
        objects=git(source,'cat-file','--batch-all-objects','--batch-check=%(objectname)').stdout
        if objects:git(stage/'.git','index-pack','--stdin',input=git(source,'pack-objects','--stdout',input=objects).stdout)
        git(stage/'.git','config','core.bare','false')
        from .portability import copy_tree
        copy_tree(source,stage,skip_git=True)
        for name in ('HEAD','index'):
            if (gd/name).is_file():shutil.copy2(gd/name,stage/'.git'/name)
        for shared in gd.glob('sharedindex.*'):shutil.copy2(shared,stage/'.git'/shared.name)
        if (common/'lfs/objects').is_dir():shutil.copytree(common/'lfs/objects',stage/'.git/lfs/objects',symlinks=True,dirs_exist_ok=True)
        if fetch_lfs:git(stage,'lfs','fetch','--all',remote)
        # Initialized submodule repositories are normalized independently.
        for line in git(source,'ls-files','--stage','-z').stdout.split(b'\0'):
            if line.startswith(b'160000 '):
                rel=os.fsdecode(line.split(b'\t',1)[1]);shutil.rmtree(stage/rel);export(source/rel,stage/rel)
        if any(fingerprint(p)!=v for p,v in before.items()):raise Blocked('source changed during preparation')
        result=export(stage,target)
    return {**result,'source':str(source),'network_requested':True,'upstream':remote,'complete':True}


def reconcile(base,left,right,output):
    """Merge committed histories plus staged/unstaged changes in disposable copies."""
    roots=[Path(p).resolve(strict=True) for p in (base,left,right)]; output=Path(output).absolute()
    if output.exists() or any(output.is_relative_to(p) for p in roots):raise Blocked('merge output must be new and independent')
    before=[fingerprint(p) for p in roots]
    output.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent,prefix='.history-') as tmp:
        work=Path(tmp);copies=[]
        for name,root in zip(('base','left','right'),roots):
            dest=work/name;export(root,dest);copies.append(dest)
            git(dest,'config','user.name','Portable checkpoint');git(dest,'config','user.email','checkpoint@invalid')
            git(dest,'update-ref','refs/portable/original',git(dest,'rev-parse','HEAD').stdout.decode().strip())
            # Exact original index and working tree remain separately recoverable.
            shutil.copy2(dest/'.git/index',dest/'.git/portable-original-index')
            git(dest,'add','--force','-A');git(dest,'commit','--allow-empty','-m','Portable uncommitted checkpoint')
        merged=work/'merged';export(copies[1],merged)
        git(merged,'config','user.name','Portable merge');git(merged,'config','user.email','merge@invalid')
        for name,repo in zip(('base','left','right'),copies):
            git(merged,'fetch',str(repo),'+refs/*:refs/portable/'+name+'/*')
            git(merged,'update-ref','refs/portable/'+name+'/checkpoint',git(repo,'rev-parse','HEAD').stdout.decode().strip())
        # A supplied baseline must be a genuine ancestor of both original tips.
        base_oid=git(roots[0],'rev-parse','HEAD').stdout.decode().strip()
        for ref in ('left','right'):
            if git(merged,'merge-base','--is-ancestor',base_oid,'refs/portable/'+ref+'/portable/original',check=False).returncode:raise Blocked('baseline is not ancestor of both histories')
        p=git(merged,'merge','--no-edit','--no-ff','refs/portable/right/checkpoint',check=False)
        if p.returncode not in (0,1):raise Blocked('history merge failed: '+p.stderr.decode(errors='replace'))
        conflicts=[os.fsdecode(v) for v in git(merged,'diff','--name-only','--diff-filter=U','-z').stdout.split(b'\0') if v]
        if [fingerprint(p) for p in roots]!=before:raise Blocked('merge input changed')
        # Keep complete independent sides, including exact pre-checkpoint indexes.
        stage=work/'result';stage.mkdir();os.rename(merged,stage/'merged')
        for name,repo in zip(('base','left','right'),copies):
            original=git(repo,'rev-parse','refs/portable/original').stdout.decode().strip()
            git(repo,'update-ref','HEAD',original)
            shutil.copy2(repo/'.git/portable-original-index',repo/'.git/index')
            os.rename(repo,stage/name)
        report={'clean':not conflicts,'conflicts':conflicts,'database_policy':'conflicting database blobs remain in Git stages and side copies; no text restore','source_writes':False}
        (stage/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2));os.rename(stage,output)
    return report


def register(subparser):
    p=subparser.add_parser('git-portable');p.add_argument('operation',choices=['export','prepare','reconcile']);p.add_argument('source');p.add_argument('target');p.add_argument('--left');p.add_argument('--right');p.add_argument('--fetch',action='store_true');p.add_argument('--remote');p.add_argument('--fetch-lfs',action='store_true');p.set_defaults(git_portable_run=run);return p

def run(args):
    if args.operation=='export':return export(args.source,args.target)
    if args.operation=='prepare':return prepare(args.source,args.target,fetch=args.fetch,remote=args.remote,fetch_lfs=args.fetch_lfs)
    return reconcile(args.source,args.left,args.right,args.target)

def main(argv=None):
    import argparse
    p=argparse.ArgumentParser();register(p.add_subparsers(required=True))
    try:print(json.dumps(run(p.parse_args(argv)),ensure_ascii=False));return 0
    except (Blocked,OSError,ValueError,KeyError) as e:print(json.dumps({'status':'BLOCKED','reason':str(e)},ensure_ascii=False));return 2

if __name__=='__main__':raise SystemExit(main())
