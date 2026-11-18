"""Credential-free current-code candidates, distinct from complete encrypted transport."""
import os
from pathlib import Path
import shutil
import stat
import re
from .core import Blocked
from .privacy import CredentialContent,redact_text


def data_asset(rel,raw):
    suffix=Path(rel).suffix.lower()
    if suffix in ('.sqlite','.sqlite3','.db','.dump') or raw.startswith(b'SQLite format 3\x00'):return True
    if suffix!='.sql':return False
    text=raw.decode('utf-8','replace')
    dump=bool(re.search(r'(?im)(?:^-- (?:MySQL dump|PostgreSQL database dump)|^COPY\s+.+\s+FROM stdin;|^LOCK TABLES |^/\*!\d{5} SET)',text))
    fixture=any(part.lower() in ('test','tests','fixtures','fixture','migrations','migration','queries','sql','schema') for part in Path(rel).parts[:-1])
    return dump or bool(re.search(r'(?im)^\s*(?:INSERT\s+(?:IGNORE\s+)?INTO|REPLACE\s+INTO)',text) and not fixture)


def copy_code(source,target):
    from .git_portable import git
    from .migration import SECRET_NAME
    source=Path(source).resolve();target=Path(target);target.mkdir(parents=True,exist_ok=True)
    records={'excluded':[],'redacted':[],'rebuild':[]}
    tracked=git(source,'ls-files','-c','-o','--exclude-standard','-z').stdout.split(b'\0')
    paths=sorted(set(os.fsdecode(p) for p in tracked if p))
    ignored=[os.fsdecode(p) for p in git(source,'ls-files','--others','--ignored','--exclude-standard','-z').stdout.split(b'\0') if p]
    records['excluded']=[{'path':rel,'reason':'本机忽略的运行资产；原件保留，未提供模型'} for rel in ignored]
    def excluded(rel,reason):records['excluded'].append({'path':rel,'reason':reason})
    for rel in paths:
        p=source/rel;dest=target/rel
        if SECRET_NAME.search(rel) or ((source/'发行.json').is_file() and any(part in ('数据','会话档案','任务记录') for part in Path(rel).parts)):excluded(rel,'敏感或运行资产，原件保留');continue
        if p.is_symlink():
            resolved=p.resolve()
            if not resolved.is_relative_to(source) or not resolved.exists() or SECRET_NAME.search(resolved.relative_to(source).as_posix()):excluded(rel,'边界外/私密链接，只记录不读取目标');continue
            dest.parent.mkdir(parents=True,exist_ok=True);dest.symlink_to(os.path.relpath(target/resolved.relative_to(source),dest.parent));continue
        if not p.exists():continue
        if p.is_dir():
            if (p/'.git').exists():
                nested=copy_code(p,dest)
                for field in ('excluded','redacted'):
                    records[field]+=[{**v,'path':rel+'/'+v['path']} for v in nested[field]]
            else:excluded(rel,'未初始化gitlink或非代码目录，未递归读取')
            continue
        if not p.is_file():raise Blocked('工程候选遇到未知特殊文件，未丢弃原件')
        raw=p.read_bytes()
        if data_asset(rel,raw):excluded(rel,'数据库/数据导出不提供给模型；请使用明确合成dev数据，原件保留');continue
        dest.parent.mkdir(parents=True,exist_ok=True)
        if CredentialContent().search(raw):
            try:text=raw.decode('utf-8')
            except UnicodeError:excluded(rel,'二进制含疑似秘密，原件保留');continue
            cleaned=redact_text(text)
            if cleaned==text:excluded(rel,'疑似秘密无法可靠脱敏，原件保留');continue
            dest.write_text(cleaned);records['redacted'].append({'path':rel,'reason':'只提供脱敏代码，原字节保留在源'})
        else:dest.write_bytes(raw)
        os.chmod(dest,stat.S_IMODE(p.stat().st_mode)&0o777)
    # Do not expose an internal link whose target was excluded by classification.
    for p in list(target.rglob('*')):
        if p.is_symlink() and not p.exists():excluded(p.relative_to(target).as_posix(),'目标未进入安全候选');p.unlink()
    return records


def export(source,target):
    from .git_portable import export as complete_export,git
    source=Path(source);target=Path(target)
    # Safe histories retain their full Git lineage. A sensitive/unsupported history
    # stays on the original host; only a declared detached current-code snapshot is used.
    try:
        result=complete_export(source,target,development=True)
        return {**result,'history_detached':False}
    except Blocked:
        if target.exists():raise Blocked('不完整候选已存在，保持现场；未覆盖')
    target.mkdir(parents=True)
    details=copy_code(source,target)
    # The snapshot is branch `main` (the baseline) and work happens on `candidate`, so the team's
    # standard verification `git diff main...HEAD` isolates exactly the task's changes.
    git(target,'init','-q','-b','main');git(target,'add','-A');git(target,'-c','user.name=portable','-c','user.email=portable@localhost','commit','--allow-empty','-qm','credential-free development snapshot; original history retained at source')
    git(target,'checkout','-q','-b','candidate')
    return {**details,'history_detached':True,'source_history_preserved':True}
