"""User-facing complete transport: safety classification -> normalization -> sealed file."""
import argparse
import json
import os
from pathlib import Path
import tempfile
from .core import Blocked,canonical,atomic
from .sensitive import private_assets,stable_source
from . import sealed_bundle


def pack(source,target,choice='ordinary',recipient=None):
    if choice=='cancel':return {'cancelled':True,'source_preserved':True}
    if choice not in ('ordinary','encrypted','omit'):raise Blocked('未知携带选择')
    source=Path(source).resolve(strict=True)
    if (source/'.git').exists():
        from .legacy_assets import preflight,capture
        from .git_portable import export
        project={'id':'project','path':str(source)};report=preflight([project])
        if len(report['projects'][0]['worktrees'])>1:
            if choice=='omit':raise Blocked('完整附属工作树的私密历史不能靠删工作文件排除；请选择完整age包，或用旧资产明选范围并保留排除记录')
            omissions=[{'source':s['path'],'reason':'外围上下文尚未明确选择；菜单17可逐项加入'} for s in report['projects'][0]['suggestions'] if not s['selected']]
            omissions.append({'source':'旧任务/会话/外围资料','reason':'此快捷包只含代码与全部已发现附属WT；完整选择请用菜单17'})
            target=Path(target).absolute();target.parent.mkdir(parents=True,exist_ok=True)
            normalize=(lambda src,dest:export(src,dest,sensitive_encrypted_only=True)) if choice=='encrypted' else None
            header=capture([project],target,sensitive=choice=='encrypted',recipient=recipient,normalize=normalize,exclusions=omissions)
            return {'container':str(target),'format':'portable.legacy-assets/v1','selection_captured':True,'complete':False,'all_discovered_worktrees_captured':True,'exclusions':omissions,'header':header,'source_preserved':True}
    # 自带战队后台已移除（见 DECISIONS 2026-11-18），不再等它停稳；旧版没挪走的 工具/hosts 只跳过它的锁文件
    hosts=list((source/'工具/hosts').glob('*/runtime.json'))
    return _pack_quiescent(source,target,choice,recipient,guarded_hosts=[r.parent.relative_to(source) for r in hosts])

def _pack_quiescent(source,target,choice='ordinary',recipient=None,guarded_hosts=()):
    if choice=='cancel':return {'cancelled':True,'source_preserved':True}
    if choice not in ('ordinary','encrypted','omit'):raise Blocked('未知携带选择')
    source=Path(source).resolve(strict=True);rawtarget=Path(target).absolute();rawtarget.parent.mkdir(parents=True,exist_ok=True)
    target=rawtarget.parent.resolve()/rawtarget.name
    if target.exists() or target.is_symlink() or target.is_relative_to(source):raise Blocked('容器必须为全新包外文件')
    before=stable_source(source)
    with tempfile.TemporaryDirectory(prefix='portable-transfer-') as tmp:
        root=Path(tmp).resolve();root.chmod(0o700);tree=root/'tree'
        from .git_portable import export
        from .portability import copy_tree
        private=private_assets(source)
        if private and choice=='ordinary':raise Blocked('私密原件未加密；请选完整age另包、不携带并登记或取消')
        allowed=choice in ('encrypted','omit')
        if (source/'.git').exists():details=export(source,tree,sensitive_encrypted_only=allowed)
        else:tree.mkdir(mode=0o700);details=copy_tree(source,tree,sensitive_encrypted_only=allowed)
        # 旧版留下的后台锁文件不携带
        omitted_locks=[]
        for host in guarded_hosts:
            for name in ('.ownership.lock','mcp-state/.lock'):
                rel=host/name;lockfile=tree/rel
                if lockfile.is_file() and not lockfile.is_symlink():lockfile.unlink();omitted_locks.append(rel.as_posix())
        excluded=[]
        if choice=='omit':
            excluded=private_assets(tree)
            if not excluded:raise Blocked('无声明的私密素材可排除；含秘密的完整repo请选加密')
            from .core import safe
            if (tree/'.git').exists():
                from .git_portable import git
                historical={__import__('os').fsdecode(line.split(b' ',1)[1]) for line in git(tree,'rev-list','--objects','--all','--reflog').stdout.splitlines() if b' ' in line}
                if any(rel in historical for rel in excluded):raise Blocked('私密原件仍在完整Git历史，不能明文排除后发布；请选择完整age包或取消，原历史保留')
            for rel in excluded:safe(tree,rel).unlink() # only private generated candidate
        if choice!='encrypted':
            # The normalized Git exporter already checked all history objects;
            # migration inventory additionally validates complete product roots.
            from .migration import inventory
            if (tree/'manifest.json').is_file() and json.loads((tree/'manifest.json').read_text()).get('schema')=='portable.set/v1':
                from .portability import verify
                verify(tree)
            elif not (tree/'.git').exists():inventory(tree)
            elif choice=='omit':
                from .git_portable import export as checked_export
                check=root/'checked';checked_export(tree,check)
        atomic(tree/'携带选择.json',canonical({'schema':'portable.transport-selection/v1','choice':choice,'excluded_private_assets':excluded,'rebuild':details.get('rebuild',[]),'generated_environment_rebuild':details.get('generated_environment_rebuild',{}),'omitted_owned_locks':omitted_locks,'source_preserved':True,'restore_authority':'new local identity only'}))
        if stable_source(source)!=before:raise Blocked('源变化，未发布')
        try:result=sealed_bundle.pack(tree,target,sensitive=choice=='encrypted',recipient=recipient)
        except sealed_bundle.BundleError:raise Blocked('介质容器未发布；源和已有目标保留，检查本机age/介质/目标条件') from None
    return {**result,'container':str(target),'choice':choice,'excluded_count':len(excluded),'source_preserved':True,'format':'portable.sealed/v1'}


def restore(package,target,identity=None):
    raw=Path(target).absolute();raw.parent.mkdir(parents=True,exist_ok=True);target=raw.parent.resolve()/raw.name
    try:result=sealed_bundle.restore(package,target,identity_file=identity)
    except sealed_bundle.BundleError:raise Blocked('容器恢复未完成；原包、已有目标和并发内容保留') from None
    from .runtime_identity import reset_restored_tree
    reset_restored_tree(target)
    # Nested project-set payloads can also carry product state; reset each such tree.
    for path in target.rglob('数据/state.sqlite'):
        if path.parent.parent!=target:reset_restored_tree(path.parent.parent)
    return {**result,'restored':str(target),'old_execution_authority_restored':False}


def wizard(store):
    import sys
    if not sys.stdin.isatty():raise Blocked('携带选择须本人本机终端')
    source=input('要携带的产品/项目/已校验项目集合目录：').strip()
    target=input('介质上的全新容器文件路径：').strip()
    value=input('1 普通安全包 · 2 完整age加密另包 · 3 私密素材不携带并登记 · 回车取消：').strip()
    choice={'1':'ordinary','2':'encrypted','3':'omit'}.get(value,'cancel')
    recipient=(input('可选age接收公钥（回车为本机私密密码）：').strip() or None) if choice=='encrypted' else None
    return pack(source,target,choice,recipient)


def register(subparser):
    p=subparser.add_parser('transfer');p.add_argument('action',choices=['pack','inspect','restore']);p.add_argument('source');p.add_argument('target',nargs='?');p.add_argument('--choice',choices=['ordinary','encrypted','omit','cancel'],default='ordinary');p.add_argument('--recipient');p.add_argument('--identity');p.set_defaults(transfer_run=run);return p

def run(args):
    if args.action=='pack':return pack(args.source,args.target,args.choice,args.recipient)
    if args.action=='inspect':return sealed_bundle.inspect(args.source)
    return restore(args.source,args.target,args.identity)
