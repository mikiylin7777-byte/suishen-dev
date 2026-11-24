"""Explicit sensitive transport integrated with complete project/product packages."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
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


def _uses_data_filter(stream):
    return 'filter' in getattr(stream.extractall, '__code__').co_varnames

def _destination_stack(name):
    """成员路径必须落在目标目录内。绝对路径、以及 .. 走出目标，都拒绝。

    包内的 a/b/../c 先折成 a/c，和 data filter 一样可以留下。
    """
    if not name or '\x00' in name or os.path.isabs(name):return None
    raw=name.replace('\\','/')
    if raw.startswith('/'):return None
    stack=[]
    for part in raw.split('/'):
        if part in ('','.'):continue
        if part=='..':
            if not stack:return None
            stack.pop()
        else:stack.append(part)
    return stack or None

def _link_target_inside(member_stack, linkname):
    """相对符号链接的解析结果必须仍在目标目录内，和 data filter 对 symlink 的要求一致。"""
    if not linkname or '\x00' in linkname or os.path.isabs(linkname):return False
    raw=linkname.replace('\\','/')
    if raw.startswith('/'):return False
    stack=list(member_stack[:-1])
    parts=[part for part in raw.split('/') if part not in ('','.')]
    if not parts:return False
    for part in parts:
        if part=='..':
            if not stack:return False
            stack.pop()
        else:stack.append(part)
    return bool(stack)

def _resolved_link_inside(root, path):
    """realpath 会顺着链接链走，文字折叠看不到的逃逸在这里露出来。"""
    boundary=os.path.realpath(root)
    try:target=os.path.realpath(path)
    except OSError:return False
    try:return os.path.commonpath([boundary, target])==boundary
    except ValueError:return False

def _reject_escaped_links(root):
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames+filenames:
            path=os.path.join(dirpath, name)
            if os.path.islink(path) and not _resolved_link_inside(root, path):
                raise Blocked('敏感包含链接或特殊文件：'+os.path.relpath(path, root))

def _walk(root, stack, member_name):
    current=root
    for part in stack[:-1]:
        current=current/part
        if current.is_symlink():raise Blocked('敏感包路径越界：'+member_name)
        current.mkdir(exist_ok=True)
    return current/stack[-1]

def _extract_data_legacy(stream, dest):
    """Python 3.9 没有 data filter。允许包内相对符号链接，其余危险成员照旧拒绝。"""
    root=Path(dest).resolve()
    for member in stream.getmembers():
        stack=_destination_stack(member.name)
        if stack is None:raise Blocked('敏感包路径越界：'+member.name)
        if member.issym():
            if not _link_target_inside(stack, member.linkname or ''):
                raise Blocked('敏感包含链接或特殊文件：'+member.name)
            final=_walk(root, stack, member.name)
            if final.is_symlink() or final.exists():raise Blocked('敏感包路径冲突：'+member.name)
            os.symlink(member.linkname.replace('\\','/'), final)
            continue
        if member.islnk() or not (member.isfile() or member.isdir()):
            raise Blocked('敏感包含链接或特殊文件：'+member.name)
        if member.isdir():
            current=root
            for part in stack:
                current=current/part
                if current.is_symlink():raise Blocked('敏感包路径越界：'+member.name)
                current.mkdir(exist_ok=True)
            continue
        final=_walk(root, stack, member.name)
        if final.is_symlink() or final.exists():raise Blocked('敏感包路径冲突：'+member.name)
        source=stream.extractfile(member)
        if source is None:raise Blocked('敏感包文件不可读：'+member.name)
        data=source.read()
        flags=os.O_WRONLY|os.O_CREAT|os.O_EXCL
        if hasattr(os,'O_NOFOLLOW'):flags|=os.O_NOFOLLOW
        fd=os.open(final,flags,0o600)
        try:os.write(fd,data)
        finally:os.close(fd)
        mode=stat.S_IMODE(member.mode or 0)&0o755
        if not mode&0o100:mode&=~0o111
        os.chmod(final,mode|0o600)
    try:_reject_escaped_links(root)
    except Blocked:
        shutil.rmtree(root, ignore_errors=True)
        raise

def _data_filter(member,dest_path):
    """官方 data filter 先做安全检查。3.12.3 接受 a/b/../c 但不改名，随后 os.makedirs('a/b/..') 会 FileExistsError。名字里还留着 .. 时折进目标目录，和较新版本的 normpath 一样。"""
    filtered=tarfile.data_filter(member,dest_path)
    if filtered is None:return None
    raw=filtered.name.replace('\\','/')
    if '..' not in raw.split('/'):return filtered
    stack=_destination_stack(filtered.name)
    if stack is None:raise tarfile.OutsideDestinationError(filtered,filtered.name)
    folded='/'.join(stack)
    if folded==raw.rstrip('/'):return filtered
    return filtered.replace(name=folded,deep=False)

def _extract_data(stream, dest):
    """Unpack like tarfile's data filter. Python 3.9 has no filter argument."""
    dest=Path(dest);dest.mkdir(mode=0o700,exist_ok=True)
    if _uses_data_filter(stream):
        try:stream.extractall(dest,filter=_data_filter)
        except tarfile.FilterError as exc:raise Blocked('敏感包不能按普通数据解开：'+str(exc)) from None
        return
    _extract_data_legacy(stream, dest)


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
            with tarfile.open(archive) as stream:_extract_data(stream,stage/'unpacked')
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
