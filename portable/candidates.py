"""Independent engineering candidates. Never writes source Git/index or active paths."""
from pathlib import Path
from .core import Blocked,canonical,digest,cooperative
from .git_portable import export
from .portability import copy_tree

@cooperative
def prepare(store,key,project,expected,purpose='engineering',source_override=None,expected_override=None):
    from .workflows import repo_version
    current=repo_version(store,project)
    if current['sha256']!=expected:raise Blocked('原项目版本变化，候选未创建')
    record_key='candidate:'+purpose+':'+key
    prior=store.get(record_key)
    if prior:
        if prior['baseline']!=expected or not Path(prior['workspace']).is_dir():raise Blocked('候选引用失效')
        return prior
    registered_source=Path(current['workspace']);source=Path(source_override or registered_source)
    if source_override is not None:
        from .workflows import repo_fingerprint
        if repo_fingerprint(source)['sha256']!=expected_override:raise Blocked('原任务代码候选已变化，请重新检查')
    target=store.root/'代码/候选'/purpose/digest(key.encode())
    target.parent.mkdir(parents=True,exist_ok=True)
    from .development import export as development_export
    details=development_export(source,target)
    if source_override is not None and repo_fingerprint(source)['sha256']!=expected_override:raise Blocked('原任务候选在复制时变化，未派工')
    if repo_version(store,project)['sha256']!=expected:raise Blocked('复制期间原项目变化；候选保留，禁止派工')
    baseline=None
    if purpose=='engineering':
        baseline=store.root/'代码/基线'/digest(key.encode());baseline.parent.mkdir(parents=True,exist_ok=True)
        if not baseline.exists():export(target,baseline)
    value={'development':details,'baseline_workspace':str(baseline) if baseline else None,'registered_source':str(registered_source),'source':str(source),'workspace':str(target),'baseline':expected,'project':project,'purpose':purpose,'merge_authorized':False}
    if purpose=='engineering':
        from . import remote_publish as rp
        from .git_portable import git
        url=rp.remote(registered_source)
        value['publish_binding']={'source':str(registered_source.resolve()),'remote':url,
            'base':rp.local_base(registered_source) if url else None,
            'candidate_base':git(target,'rev-parse','HEAD').stdout.decode().strip(),
            'source_head':git(registered_source,'rev-parse','HEAD').stdout.decode().strip()}
    store.put(record_key,value);return value

def runtime_assets(root,paths):
    # Local CAS metadata only; never include credential bytes in model or receipts.
    import os,stat
    root=Path(root);values={}
    if (root/'.git').exists():
        from .git_portable import git
        paths=set(paths)|{os.fsdecode(p) for p in git(root,'ls-files','--others','--ignored','--exclude-standard','-z').stdout.split(b'\0') if p}
    for rel in paths:
        p=root/rel
        if p.is_symlink():values[rel]={'link':os.readlink(p)}
        elif p.is_file():values[rel]={'sha256':digest(p.read_bytes()),'mode':stat.S_IMODE(p.stat().st_mode)}
        elif p.exists():values[rel]={'directory':True}
        else:values[rel]={'missing':True}
    return values


def reception_staging(store,request):
    import contextlib,uuid
    @contextlib.contextmanager
    def kept():
        root=store.root/'代码/接收事务'/(request+'-'+uuid.uuid4().hex)
        root.mkdir(parents=True,mode=0o700)
        yield root # Deliberately retained: exceptions must never delete an exchanged generation.
    return kept()


def recover_reception(store,key,value):
    from .workflows import repo_fingerprint
    from .core import atomic
    import shutil
    receipt=store.get('reception-pending:'+key)
    if not receipt:return None
    source=Path(value['source']);stage=Path(receipt['stage']);rollback=Path(receipt['rollback'])
    current=repo_fingerprint(source)['sha256']
    if current==receipt['baseline'] and runtime_assets(source,receipt['paths'])==receipt['runtime_before']:
        return None # No exchange, or a completed safe rollback; original remains authoritative.
    if current==receipt['incoming'] and runtime_assets(source,receipt['paths'])==receipt['incoming_runtime']:
        old=stage if stage.is_dir() else rollback
        if old.is_dir() and repo_fingerprint(old)['sha256']==receipt['baseline'] and runtime_assets(old,receipt['paths'])==receipt['runtime_before']:
            if old==stage:
                if rollback.exists():raise Blocked('接收回退目录冲突，双方保留')
                rollback.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(stage),rollback)
            result={'state':'received','authoritative_path':str(source),'rollback':str(rollback),'candidate':value['workspace'],'recovered_transaction':True,'production_deployed':False}
            value['reception']=result;store.put('candidate:engineering:'+key,value);return result
    result={'state':'conflict','source':str(source),'preserved_transaction':str(stage.parent),'candidate':value['workspace'],'production_deployed':False}
    value['reception']=result;store.put('candidate:engineering:'+key,value);return result


@cooperative
def create_managed(store,name,project):
    from .core import identifier,atomic
    from .git_portable import git
    if not isinstance(name,str) or not name.strip():raise Blocked('新项目名称不能为空')
    project=identifier(project)
    if store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('项目ID已登记，禁止覆盖')
    root=store.root/'代码'/project
    if root.exists():raise Blocked('新项目目录已存在，禁止覆盖')
    root.mkdir(parents=True);atomic(root/'README.md','# '+name+'\n')
    git(root,'init','-q');git(root,'add','README.md');git(root,'-c','user.name=portable','-c','user.email=portable@localhost','commit','-qm','new managed project')
    store.register(name,root,project)
    store.put('project-policy:'+project,{'ownership':'managed','created_by_product':True,'root':str(root),'device':store.device})
    return {'project':project,'path':str(root),'managed':True}


def pending(store,project):
    return [json_value for row in store.db.execute("SELECT value FROM meta WHERE key LIKE 'candidate:engineering:%'") if (json_value:=__import__('json').loads(row[0])).get('project')==project and json_value.get('reception',{}).get('state') in ('awaiting_external_owner','awaiting_private_reception','conflict')]

@cooperative
def receive(store,request):
    import json,shutil,tempfile,uuid
    from .core import atomic
    from .workflows import repo_version,repo_fingerprint
    from .git_portable import git
    from .distribution import exchange_directories
    key='candidate:engineering:'+request;value=store.get(key)
    if not value: return {'state':'legacy_request_without_candidate'}
    if value.get('reception'):return value['reception']
    recovered=recover_reception(store,request,value)
    if recovered:return recovered
    candidate=Path(value['workspace']);policy=store.get('project-policy:'+value['project'],{})
    source=Path(value['source']);delivery=store.root/'任务记录/候选交付'/request;delivery.mkdir(parents=True,exist_ok=True)
    with reception_staging(store,request) as tmp:
        stage=Path(tmp)/'candidate';export(candidate,stage) # scan new history/working files before any acceptance
        if policy.get('ownership')=='managed' and policy.get('created_by_product') is True and policy.get('device')==store.device and policy.get('root')==str(source) and source==store.root/'代码'/value['project'] and not value.get('development',{}).get('redacted'):
            if repo_version(store,value['project'])['sha256']!=value['baseline']:raise Blocked('自管项目自基线后变化；保留候选，不覆盖')
            if value.get('development',{}).get('history_detached'):
                if not (source/'.git').is_dir() or (source/'.git').is_symlink():raise Blocked('自管原Git布局变化；保留候选等待审阅')
                shutil.rmtree(stage/'.git');shutil.copytree(source/'.git',stage/'.git')
            runtime_paths=[item['path'] for item in value.get('development',{}).get('excluded',[])]
            runtime_before=runtime_assets(source,runtime_paths)
            for excluded in value.get('development',{}).get('excluded',[]):
                rel=excluded['path'];original=source/rel;dest=stage/rel
                if original.is_file() and not original.is_symlink():
                    if dest.exists():
                        saved=delivery/'候选dev配置'/rel;saved.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(dest,saved)
                    dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(original,dest)
            incoming=repo_fingerprint(stage)['sha256'];incoming_runtime=runtime_assets(stage,runtime_paths)
            if runtime_assets(source,runtime_paths)!=runtime_before:raise Blocked('本机运行配置在接收前变化，保持源与候选，未交换')
            rollback_target=store.root/'代码/回退'/request
            receipt={'stage':str(stage),'rollback':str(rollback_target),'baseline':value['baseline'],'incoming':incoming,'paths':runtime_paths,'runtime_before':runtime_before,'incoming_runtime':incoming_runtime}
            store.put('reception-pending:'+request,receipt)
            atomic(Path(tmp)/'RECEPTION.json',canonical(receipt))
            exchange_directories(source,stage)
            if repo_fingerprint(stage)['sha256']!=value['baseline'] or runtime_assets(stage,runtime_paths)!=runtime_before:
                # Never erase a concurrent editor's generation.
                if repo_fingerprint(source)['sha256']==incoming and runtime_assets(source,runtime_paths)==incoming_runtime:exchange_directories(source,stage)
                preservation=store.root/'代码/回退'/('conflict-'+uuid.uuid4().hex);preservation.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(stage),preservation)
                result={'state':'conflict','preserved':str(preservation),'candidate':str(candidate)}
            else:
                rollback=store.root/'代码/回退'/request;rollback.parent.mkdir(parents=True,exist_ok=True);shutil.move(str(stage),rollback)
                result={'state':'received','authoritative_path':str(source),'rollback':str(rollback),'candidate':str(candidate),'production_deployed':False}
        else:
            patch=delivery/'候选.patch';baseline=Path(value.get('baseline_workspace',''))
            if baseline.is_dir() and baseline!=Path('.'):
                from .portability import copy_tree
                left=Path(tmp)/'base';right=Path(tmp)/'new';left.mkdir();right.mkdir();copy_tree(baseline,left,skip_git=True);copy_tree(stage,right,skip_git=True)
                diff=git(Path(tmp),'diff','--no-index','--binary','--src-prefix=a/','--dst-prefix=b/','base','new',check=False)
                if diff.returncode not in (0,1):raise Blocked('候选补丁生成失败，完整双方仍保留')
                atomic(patch,diff.stdout)
            delivered=delivery/'完整候选'
            if delivered.exists():raise Blocked('交付目录已有未知候选，保留双方')
            shutil.move(str(stage),delivered)
            result={'delivered':str(delivered),'delivered_sha256':repo_fingerprint(delivered)['sha256'],'state':'awaiting_private_reception' if policy.get('ownership')=='managed' else 'awaiting_external_owner','source':str(source),'baseline':value['baseline'],'baseline_workspace':value.get('baseline_workspace'),'candidate':str(candidate),'patch':str(patch) if patch.exists() else None,'source_unchanged':True,'production_deployed':False}
        atomic(delivery/'交付说明.md','# 候选交付\n\n'+canonical(result)+'\n\n外部项目须由现有主窗口审阅基线并接收；补丁可用 git apply -p2 --check 检查。未自动合回或上线。\n')
        value['reception']=result;store.put(key,value)
    return result

@cooperative
def acknowledge_external(store,request):
    from .workflows import repo_version,repo_fingerprint
    key='candidate:engineering:'+request;value=store.get(key,{})
    if value.get('reception',{}).get('state')not in ('awaiting_external_owner','awaiting_private_reception'):raise Blocked('无待接收外部候选')
    current=repo_version(store,value['project']);candidate=repo_fingerprint(Path(value['reception']['delivered']))
    if candidate['sha256']!=value['reception']['delivered_sha256']:raise Blocked('交付候选已变化，须重新验收')
    if current['files']!=candidate['files']:raise Blocked('原项目尚未接收候选完整代码；继续保留待现有主窗口接收')
    value['reception'].update(state='received',current_baseline=current['sha256']);store.put(key,value)
    return value['reception']


def _remote_git(root,*args,check=True):
    from .remote_publish import network
    return network(root,*args,check=check)

@cooperative
def publish(store,request,base=None,transport=None):
    """Replay a delivered candidate onto the remote base and open a platform PR. Never touches the source checkout."""
    import tempfile
    from .git_portable import git
    key='candidate:engineering:'+request;value=store.get(key,{});reception=value.get('reception',{})
    from . import remote_publish as rp
    from .workflows import repo_version
    binding=value.get('publish_binding')
    if not binding:raise Blocked('候选缺少发布身份绑定；保留候选等待重新准备')
    if reception.get('state') not in ('awaiting_external_owner','received'):raise Blocked('无待接收的外部候选，不能开 PR')
    source=Path(reception['source'])
    def verify_binding():
        registered=repo_version(store,value['project'])
        current=store.get(key,{})
        if current.get('publish_binding')!=binding or current.get('registered_source')!=str(source) or current.get('reception',{}).get('source')!=str(source):raise Blocked('候选绑定已变化；未写入')
        if str(Path(registered['workspace']).resolve())!=binding['source'] or str(source.resolve())!=binding['source'] or value.get('registered_source')!=str(source):raise Blocked('注册项目与候选源身份冲突')
        if git(source,'cat-file','-e',binding['source_head']+'^{commit}',check=False).returncode:raise Blocked('候选登记的原仓基线已失效')
        if rp.remote(source)!=binding['remote']:raise Blocked('原项目远端已变化')
    verify_binding()
    url=binding['remote']
    if not url:raise Blocked('原项目没有远端；候选保留')
    rp.repository(url)
    if base and binding['base'] and base!=binding['base']:raise Blocked('显式 base 与候选登记冲突')
    operation='publish-pending:'+request
    original=store.get(operation)
    if original and not reception.get('publication'):
        if not binding['base']:raise Blocked('原发布操作缺少已登记 base，禁止重建')
        verify_binding()
        pr=rp.open_pr(source,url,binding['base'],'portable-'+request[:12],'','',transport,(store,operation),verify_binding,recover_only=True)
        if pr:
            verify_binding()
            result={'state':'pr_opened','pr_url':pr,'remote':url,'base':original['base'],'topic':original['topic'],'head':original['head'],'production_deployed':False}
            reception['publication']=result;value['reception']=reception;store.put(key,value)
            return result
    base=rp.base_branch(source,url,binding['base'] or base)
    if not binding['base']:
        binding['base']=base;store.put(key,value)
    published=reception.get('publication')
    if published and published.get('state') in ('pr_opened','pushed') and reception.get('state')=='awaiting_external_owner':
        # Re-running after the PR merged records reception, so the project is no longer held.
        import tempfile
        with tempfile.TemporaryDirectory(prefix='portable-publish-') as tmp:
            work=Path(tmp)/'repo'
            _remote_git(Path(tmp),'clone','-q','--no-tags','--single-branch','--branch',published['base'],published['remote'],str(work))
            if _remote_git(work,'merge-base','--is-ancestor',published['head'],'HEAD',check=False).returncode==0:
                published={**published,'state':'merged','merged_into':_remote_git(work,'rev-parse','HEAD').stdout.decode().strip()}
                reception.update(state='received',publication=published);value['reception']=reception;store.put(key,value)
        return published
    if published:return published
    if reception.get('state')!='awaiting_external_owner':raise Blocked('无待接收的外部候选，不能开 PR')
    delivered=Path(reception['delivered'])
    if not (delivered/'.git').is_dir():raise Blocked('交付候选缺少 Git 历史，不能开 PR')
    from .workflows import repo_fingerprint
    if repo_fingerprint(delivered)['sha256']!=reception['delivered_sha256']:raise Blocked('交付候选已变化，须重新验收')
    if not git(delivered,'rev-parse','--verify','-q','candidate',check=False).stdout.strip():raise Blocked('交付候选没有 candidate 分支')
    if git(delivered,'status','--porcelain').stdout.strip():raise Blocked('交付候选有未提交改动，不能开 PR')
    if git(delivered,'merge-base','--is-ancestor',binding['candidate_base'],'candidate',check=False).returncode:raise Blocked('候选分支偏离已登记基线，禁止发布')
    patches=git(delivered,'format-patch','--stdout','--binary',binding['candidate_base']+'..candidate').stdout
    if not patches.strip():raise Blocked('候选相对 '+base+' 没有提交，不开 PR')
    subject=git(delivered,'log','-1','--format=%s','candidate').stdout.decode().strip() or '随身开发候选'
    source=Path(reception['source'])
    topic='portable-'+request[:12]
    with tempfile.TemporaryDirectory(prefix='portable-publish-') as tmp:
        work=Path(tmp)/'repo'
        _remote_git(Path(tmp),'clone','-q','--no-tags','--single-branch','--branch',base,url,str(work))
        remote_base=_remote_git(work,'rev-parse','HEAD').stdout.decode().strip()
        ident=['-c','user.name=随身开发','-c','user.email=portable@localhost']
        import subprocess,os
        p=subprocess.run(['git','-c','core.hooksPath='+os.devnull,*ident,'-C',str(work),'am','--3way','--committer-date-is-author-date'],input=patches,capture_output=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0'})
        if p.returncode:raise Blocked('候选与远端 '+base+' 最新版冲突，未开 PR，候选保留')
        head=_remote_git(work,'rev-parse','HEAD').stdout.decode().strip()
        if head==remote_base or not _remote_git(work,'diff','--name-only',remote_base,head).stdout.strip():
            # Already merged upstream (e.g. by hand): record reception so the project is no longer held.
            result={'state':'already_merged','remote':url,'base':base,'remote_head':remote_base,'production_deployed':False}
            reception.update(state='received',publication=result,current_baseline=None);value['reception']=reception;store.put(key,value)
            return result
        body='随身开发候选交付 '+request+'：已由战队完成并经后台复验；合并前请等 CI 全绿。未上线。'
        pr=rp.open_pr(work,url,base,topic,subject,body,transport,(store,'publish-pending:'+request),verify_binding)
        head=store.get('publish-pending:'+request,{}).get('head',head)
    result={'state':'pr_opened','pr_url':pr,'remote':url,'base':base,'topic':topic,'head':head,'production_deployed':False}
    reception['publication']=result;value['reception']=reception;store.put(key,value)
    return result


def register(subparser):
    p=subparser.add_parser('candidates');p.add_argument('action',choices=['new','status','ack','publish']);p.add_argument('value');p.add_argument('--name');p.add_argument('--base');p.set_defaults(candidates_run=run);return p

def run(args):
    from .core import Store
    store=Store(args.root)
    try:
        if args.action=='new':return create_managed(store,args.name or args.value,args.value)
        if args.action=='status':return {'pending':pending(store,args.value)}
        if args.action=='publish':return publish(store,args.value,args.base)
        return acknowledge_external(store,args.value)
    finally:store.db.close()
