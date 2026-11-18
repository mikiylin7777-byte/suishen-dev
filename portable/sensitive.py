"""Explicit sensitive transport integrated with complete project/product packages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from .core import Blocked,atomic,canonical,digest,safe


def private_assets(root):
    root=Path(root).resolve();paths=[]
    databases=sorted(set([root/'数据/state.sqlite',*root.rglob('数据/state.sqlite')]))
    for db in databases:
        if not db.is_file():continue
        if db.is_symlink():raise Blocked('私密资产状态不能是链接')
        connection=sqlite3.connect(db.as_uri()+'?mode=ro',uri=True)
        try:
            if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone():continue
            rows=connection.execute("SELECT value FROM meta WHERE key LIKE 'material_asset:%'").fetchall()
            base=db.parent.parent
            for row in rows:
                value=json.loads(row[0])
                if value.get('private') and not value.get('encrypted'):
                    for rel in [value['path'],*value.get('previous_copies',[])]:
                        path=safe(base,rel)
                        if path.is_file():paths.append(path.relative_to(root).as_posix())
        finally:connection.close()
    return sorted(set(paths))


def stable_source(root):
    # SQLite SHM reader marks can change merely from a consistent backup read.
    # WAL/database bytes remain part of the source comparison.
    value={}
    for p in Path(root).rglob('*'):
        rel=p.relative_to(root).as_posix()
        if p.name.endswith('-shm'):
            db=Path(str(p)[:-4])
            if db.is_file() and db.read_bytes()[:16]==b'SQLite format 3\x00':continue
        if p.is_symlink():value[rel]={'link':os.readlink(p)}
        elif p.is_file():value[rel]={'sha256':digest(p.read_bytes()),'mode':p.stat().st_mode&0o777}
    return digest(canonical(value).encode())


def pack(source,target,choice='cancel',recipient=None):
    source=Path(source).resolve(strict=True);target=Path(target).absolute()
    if choice=='cancel':return {'cancelled':True,'source_preserved':True}
    if target.exists() or target.is_relative_to(source) or source.is_relative_to(target):raise Blocked('敏感包目标必须为新包外目录')
    if choice not in ('encrypted','omit'):raise Blocked('请选择加密另包、不携带并登记或取消')
    if any(p.is_symlink() for p in target.parents):raise Blocked('目标路径不能穿越链接')
    if choice=='encrypted' and (not shutil.which('age') or (not recipient and not sys.stdin.isatty())):raise Blocked('age加密需本人TTY私密密码或明确公钥；不接收密码参数')
    from .git_portable import export,fingerprint
    from .portability import copy_tree,manifest
    before=stable_source(source);target.parent.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.sensitive-',dir=target.parent) as tmp:
        stage=Path(tmp);os.chmod(stage,0o700);normalized=stage/'normalized'
        if (source/'.git').exists():normalization=export(source,normalized,sensitive_encrypted_only=True)
        else:normalized.mkdir(mode=0o700);normalization=copy_tree(source,normalized,sensitive_encrypted_only=True)
        publication=stage/'package';publication.mkdir(mode=0o700)
        if choice=='omit':
            excluded=private_assets(normalized)
            if not excluded:raise Blocked('没有可按声明排除的私密素材；疑似敏感repo须整体加密或取消')
            for rel in excluded:safe(normalized,rel).unlink() # generated candidate only; originals untouched
            # Remaining credentials still block ordinary publication.
            from .migration import inventory
            inventory(normalized)
            shutil.move(str(normalized),publication/'payload')
            value={'schema':'portable.sensitive/v1','choice':'omit','excluded_private_assets':excluded,'source_preserved':True,'files':manifest(publication/'payload')}
        else:
            encrypted=publication/'payload.tar.age'
            argv=[shutil.which('age'),'-r',recipient] if recipient else [shutil.which('age'),'-p']
            if recipient and not recipient.startswith('age1'):raise Blocked('无效age公钥')
            with encrypted.open('wb') as output:
                process=subprocess.Popen(argv,stdin=subprocess.PIPE,stdout=output,stderr=subprocess.DEVNULL if recipient else None)
                try:
                    with tarfile.open(fileobj=process.stdin,mode='w|') as archive:archive.add(normalized,arcname='payload',recursive=True)
                    process.stdin.close()
                    if process.wait(timeout=120):raise Blocked('age加密失败，未发布')
                except BaseException:
                    if process.poll() is None:process.terminate();process.wait(timeout=5)
                    raise
            encrypted.chmod(0o600)
            value={'schema':'portable.sensitive/v1','choice':'encrypted','sha256':digest(encrypted.read_bytes()),'size':encrypted.stat().st_size,'source_preserved':True,'contents':'complete normalized source, Git history/index/working files and selected private assets','unlock':'local age identity/passphrase; never inherited authority'}
        value['rebuild']=normalization.get('rebuild',[])
        if stable_source(source)!=before:raise Blocked('源发生变化；未发布敏感包')
        atomic(publication/'manifest.json',canonical(value));os.rename(publication,target)
    return value


def unpack(package,target,identity=None):
    package=Path(package);value=json.loads((package/'manifest.json').read_text());target=Path(target).absolute()
    if target.exists() or target.is_symlink() or any(p.is_symlink() for p in target.parents):raise Blocked('解锁目标必须为全新非链接目录')
    if value.get('schema')!='portable.sensitive/v1':raise Blocked('未知敏感包')
    if value['choice']=='omit':
        from .portability import manifest
        if manifest(package/'payload')!=value['files']:raise Blocked('普通载荷损坏')
        shutil.copytree(package/'payload',target,symlinks=True)
    else:
        payload=package/'payload.tar.age'
        if digest(payload.read_bytes())!=value['sha256']:raise Blocked('加密载荷损坏')
        if not identity and not sys.stdin.isatty():raise Blocked('解锁须本人本机私密TTY或明确age identity文件')
        target.parent.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='.unlock-',dir=target.parent) as tmp:
            stage=Path(tmp);stage.chmod(0o700);archive=stage/'archive.tar'
            argv=[shutil.which('age') or 'age','-d','-o',str(archive)]
            if identity:argv+=['-i',str(identity)]
            argv.append(str(payload))
            result=subprocess.run(argv,stderr=subprocess.DEVNULL if identity else None)
            if result.returncode:raise Blocked('age解锁失败，原包保留')
            archive.chmod(0o600)
            with tarfile.open(archive) as stream:stream.extractall(stage/'unpacked',filter='data')
            os.rename(stage/'unpacked/payload',target)
    from .runtime_identity import reset_restored_tree
    reset_restored_tree(target)
    return {'restored':str(target),'execution':'restored-readonly','excluded':value.get('excluded_private_assets',[])}


def wizard(store):
    if not sys.stdin.isatty():raise Blocked('敏感携带选择须本人本机交互')
    source=input('要携带的产品或项目目录：').strip();target=input('全新包外输出目录：').strip()
    choice=input('1 完整另入age加密包 · 2 私密素材不携带并登记 · 回车取消：').strip()
    recipient=input('可选age接收公钥（回车用私密密码）：').strip() if choice=='1' else None
    return pack(source,target,{'1':'encrypted','2':'omit'}.get(choice,'cancel'),recipient or None)


def register(subparser):
    p=subparser.add_parser('sensitive');p.add_argument('action',choices=['pack','unpack']);p.add_argument('source');p.add_argument('target');p.add_argument('--choice',choices=['encrypted','omit','cancel'],default='cancel');p.add_argument('--recipient');p.add_argument('--identity');p.set_defaults(sensitive_run=run);return p

def run(args):return pack(args.source,args.target,args.choice,args.recipient) if args.action=='pack' else unpack(args.source,args.target,args.identity)
