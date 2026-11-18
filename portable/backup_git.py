"""Dedicated local Git vault: exact knowledge allowlist, no inherited Git config."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import re
from .core import Blocked, Store, atomic, canonical, cooperative, digest, safe
from .migration import SECRET_NAME, SECRET_CONTENT

MARKER='portable-private-knowledge/v1'
IGNORE='*\n!*/\n!知识库/**/*.md\n!知识库/*.md\n!config-summary.json\n!BACKUP-OWNER\n!.gitignore\n'

def suspicious(data):
    return (SECRET_CONTENT.search(data) or b'PRIVATE KEY' in data or
            re.search(rb'https?://[^/\s:@]+:[^/\s@]+@',data) or
            re.search(rb'(?:api[_-]?key|password|appsecret|secret|token)[\"\']?\s*[=:]\s*[\"\']?[A-Za-z0-9_+/=-]{8,}',data,re.I) or
            re.search(rb'\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{16,}|Bearer\s+[A-Za-z0-9_.-]{16,})',data))

def git(repo,*args):
    env={'PATH':os.environ.get('PATH','/usr/bin:/bin'),'HOME':str(repo),
         'GIT_CONFIG_NOSYSTEM':'1','GIT_CONFIG_GLOBAL':os.devnull,'GIT_TERMINAL_PROMPT':'0',
         'GIT_AUTHOR_NAME':'Portable backup','GIT_AUTHOR_EMAIL':'backup@localhost',
         'GIT_COMMITTER_NAME':'Portable backup','GIT_COMMITTER_EMAIL':'backup@localhost'}
    result=subprocess.run(['git','-c','core.quotepath=false','-c','core.fsmonitor=false','-c','core.hooksPath=/dev/null','-c','commit.gpgsign=false',
        '-C',str(repo),*args],env=env,capture_output=True,timeout=60)
    if result.returncode:raise Blocked('私有知识备份Git操作失败，敏感详情已隐藏')
    return result.stdout

def permitted(rel):
    p=Path(rel)
    return not p.is_absolute() and '..' not in p.parts and len(p.parts)>1 and p.parts[0]=='知识库' and p.suffix=='.md' and not any(x.startswith('.') for x in p.parts) and not SECRET_NAME.search(rel) and not re.search(r'secret|credential|password|token|private|密钥|凭据|密码',p.name,re.I)

@cooperative
def backup(store, destination, allowlist, remote=None):
    repo=Path(destination).absolute()
    if repo.is_relative_to(store.root) or store.root.is_relative_to(repo):raise Blocked('Git备份必须为包外专用目录')
    for p in (repo,*repo.parents):
        if p.is_symlink():raise Blocked('Git备份路径不能是链接')
    if not allowlist or len(set(allowlist))!=len(allowlist):raise Blocked('需要明确非空文件白名单')
    files={}
    for rel in allowlist:
        if not permitted(rel):raise Blocked('白名单仅允许知识库Markdown')
        data=safe(store.root,rel).read_bytes()
        if suspicious(data):raise Blocked('疑似秘密，提交前拒绝')
        files[rel]=data
    summary={'schema':1,'projects':sorted(r['id'] for r in store.db.execute('SELECT id FROM projects'))}
    files['config-summary.json']=canonical(summary).encode()
    # Validate every selected byte before initializing/staging; ignored rules never bypass scans.
    if repo.exists():
        if not (repo/'.git').is_dir() or (repo/'.git').is_symlink() or (repo/'BACKUP-OWNER').read_text()!=MARKER:raise Blocked('不是本产品专用Git备份')
        if any(p.name=='.gitattributes' for p in repo.rglob('.gitattributes')):raise Blocked('备份不接受属性过滤器')
        if any(p.is_symlink() for p in (repo/'.git').rglob('*')) or any((repo/'.git'/name).exists() for name in ('commondir','shallow','objects/info/alternates')):raise Blocked('专用备份包含未知Git布局')
        if safe(repo,'.gitignore').read_text()!=IGNORE:raise Blocked('备份忽略规则已变化')
        if any(line.split('=',1)[0] not in {'core.repositoryformatversion','core.filemode','core.bare','core.logallrefupdates','core.ignorecase','core.precomposeunicode'} for line in git(repo,'config','--local','--list').decode().splitlines()):raise Blocked('专用备份不接受已有remote')
        tracked=git(repo,'ls-files','-z').decode().split('\0')
        if any(n and n not in files and n not in ('BACKUP-OWNER','.gitignore') for n in tracked):raise Blocked('备份白名单变化或存在未知跟踪文件，请显式协调')
        # Refuse unexpected history/config: never amend an arbitrary repo or its origin.
        for raw in git(repo,'rev-list','--objects','--all').decode().splitlines():
            parts=raw.split(' ',1)
            if len(parts)==2 and parts[1] and parts[1] not in files and parts[1] not in ('知识库','知识库/项目','知识库/素材','知识库/记忆','BACKUP-OWNER','.gitignore'):
                # Tree paths are allowed only as parents of selected files.
                if not any(n.startswith(parts[1]+'/') for n in files):raise Blocked('历史白名单不匹配')
        for item in git(repo,'cat-file','--batch-all-objects','--batch-check=%(objectname) %(objecttype)').splitlines():
            oid,kind=item.split()
            if kind==b'blob' and suspicious(git(repo,'cat-file','blob',oid.decode())):raise Blocked('备份历史含疑似秘密')
    else:
        repo.mkdir(parents=True);git(repo,'init','--template=')
        atomic(repo/'BACKUP-OWNER',MARKER)
        atomic(repo/'.gitignore',IGNORE)
    for rel,data in files.items():atomic(safe(repo,rel),data)
    git(repo,'add','-f','--',*sorted(files),'BACKUP-OWNER','.gitignore')
    staged=git(repo,'diff','--cached','--name-only','-z').decode().split('\0')
    if any(x and x not in files and x not in ('BACKUP-OWNER','.gitignore') for x in staged):raise Blocked('暂存区包含未知文件')
    for rel in staged:
        if rel and suspicious(git(repo,'show',':'+rel)):raise Blocked('暂存内容含疑似秘密')
    if any(staged):git(repo,'commit','-m','Knowledge backup')
    result={'commit':git(repo,'rev-parse','HEAD').decode().strip(),'network':False,'files':sorted(files)}
    if remote:
        # Explicit private target assertion is required; URL alone does not prove visibility.
        if set(remote)!={'url','private','push'} or remote['private'] is not True or remote['push'] is not True:raise Blocked('push需要显式私有目标及启用')
        if not re.fullmatch(r'ssh://git@[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+',remote['url']):raise Blocked('仅接受无凭据SSH私有目标')
        git(repo,'push',remote['url'],'HEAD:refs/heads/portable-backup');result['network']=True
    return result

def register(subparser):
    p=subparser.add_parser('backup-git');p.add_argument('--destination',required=True);p.add_argument('--allow',action='append',required=True);p.add_argument('--private-target')
    p.set_defaults(backup_git_run=run);return p

def run(args):
    store=Store(args.root)
    try:return backup(store,args.destination,args.allow,json.loads(Path(args.private_target).read_text()) if args.private_target else None)
    finally:store.db.close()

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True))
    try:print(canonical(run(p.parse_args(argv))));return 0
    except Exception:print('{"status":"BLOCKED","reason":"知识Git备份未确认"}');return 2

if __name__=='__main__':raise SystemExit(main())
