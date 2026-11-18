"""Read-only SD32 product entry contract. Never creates an execution grant."""
import json
from pathlib import Path
import subprocess
import sqlite3
from .core import Blocked, canonical, digest


def _path(value):
    p=Path(value).absolute()
    if any(x.is_symlink() for x in (p,*p.parents)):
        raise Blocked('工作区路径含符号链接')
    return p.resolve(strict=True)


def _git(root,*args):
    result=subprocess.run(['git','-C',str(root),*args],capture_output=True,timeout=15)
    if result.returncode:raise Blocked('Git身份无法核实')
    return result.stdout.decode().strip()


def _live_worktrees(registered):
    """仓库登记的工作目录里还在的那些。目录已经不在（只剩登记）的跳过：它不可能是要写入的目标，
    以前一碰就报错，把正常的项目也拦了（2026-11-14 回放时两个正常项目被误拦）。
    路径带符号链接、解析不了的也跳过，不影响对目标目录本身的严格校验。"""
    out=[]
    for line in _git(registered,'-c','core.quotePath=false','worktree','list','--porcelain').splitlines():
        if not line.startswith('worktree '):continue
        raw=Path(line[9:])
        if not raw.exists():continue
        try:out.append(_path(raw))
        except (Blocked,OSError):continue
    return out


def _identity(root):
    if _path(_git(root,'rev-parse','--show-toplevel'))!=root:raise Blocked('工作区不是仓库根')
    common=Path(_git(root,'rev-parse','--git-common-dir'))
    return str((root/common).resolve(strict=True))


class ReadOnlyStore:
    """Existing runtime only; no initialization, host adoption, locks or SQL writes."""
    def __init__(self,root):
        try:self._open(root)
        except (OSError,sqlite3.Error) as exc:
            if hasattr(self,'db'):self.db.close()
            raise Blocked('只读运行数据不可用；未初始化或修改') from exc
    def _open(self,root):
        from .core import Store, host_identity
        self.root=_path(root)
        database=_path(self.root/'数据/state.sqlite')
        self.db=sqlite3.connect(database.as_uri()+'?mode=ro',uri=True)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA query_only=ON')
        self.db.execute('BEGIN')
        self.get=lambda key,default=None:Store.get(self,key,default)
        try:
            if self.get('schema')!='1':raise Blocked('只读查询不初始化未知数据库')
            if self.get('host_binding')!=digest((host_identity()+':'+str(self.root)).encode()):raise Blocked('运行根/设备身份变化；只读查询不接管或修改身份')
        except BaseException:
            self.db.close();raise
    @property
    def device(self):return self.get('device')


def candidate_source(store,key,project,registered,binding,target):
    """Only the deterministic product export with its original reviewed request."""
    from .team import request_scope_valid
    row=store.db.execute('SELECT * FROM outbox WHERE id=?',(key,)).fetchone()
    if not row or not row['reference']:raise Blocked('候选缺原任务引用')
    body=json.loads(row['body']);value=store.get('candidate:engineering:'+key,{})
    if body.get('operation') or body.get('project')!=project or value.get('project')!=project or value.get('purpose')!='engineering':raise Blocked('候选与原项目不符')
    request_scope_valid(store,row,body,binding)
    expected=store.root/'代码/候选/engineering'/digest(key.encode())
    baseline=store.root/'代码/基线'/digest(key.encode())
    if target!=_path(expected) or _path(value['workspace'])!=target or _path(value['registered_source'])!=registered or _path(value['source'])!=registered or _path(value['baseline_workspace'])!=_path(baseline):raise Blocked('候选目录或登记来源不符')
    from .workflows import repo_version
    if value.get('baseline')!=repo_version(store,project)['sha256']:raise Blocked('候选原项目基线变化')
    head=_git(baseline,'rev-parse','HEAD')
    _git(target,'merge-base','--is-ancestor',head,'HEAD')
    return row


def _read_task(store,call,task,target,run=None):
    if call is None:
        from .team import transport
        call=transport(store.get('team_config',{}))
    try:view=call('get_task',{'task_id':task})
    except Exception:raise Blocked('task/run为unknown：可信只读任务服务不可用') from None
    if view.get('task_id')!=task or view.get('caller')!='portable:'+store.device or _path(view.get('workspace',''))!=target:raise Blocked('任务服务身份或工作区不符')
    actual=view.get('run_id')
    if not isinstance(actual,str) or not actual or (run is not None and run!=actual):raise Blocked('run引用不符或unknown')
    return actual


def planning_source(store,key,ref,project,registered,binding,target,call):
    """Validate Planner.tick's actual detached planning export, never a clone guess."""
    from .workflows import status,repo_version,repo_fingerprint
    _,rid,revision=key.split(':',2)
    requirement=status(store,rid)
    expected='portable-plan:'+digest(canonical([store.device,rid,revision,ref['repo_sha256'],binding['epoch']]).encode())
    if (requirement['project']!=project or requirement['execution_revision']!=revision or ref.get('project')!=project or ref.get('device')!=store.device or ref.get('binding_epoch')!=binding['epoch'] or ref.get('request')!=expected):raise Blocked('规划任务原绑定失效')
    candidate=store.get('candidate:planning:'+expected,{})
    if candidate.get('purpose')!='planning' or candidate.get('project')!=project or candidate.get('baseline')!=ref['repo_sha256'] or candidate.get('baseline_workspace') is not None:raise Blocked('规划导出来源缺失或不符')
    if target!=_path(store.root/'代码/候选/planning'/digest(expected.encode())) or _path(ref['workspace'])!=target or _path(candidate['workspace'])!=target or _path(candidate['registered_source'])!=registered:raise Blocked('规划导出目录或登记来源不符')
    if repo_version(store,project)['sha256']!=ref['repo_sha256']:raise Blocked('规划原仓基线已变化')
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='engineering_inspection'",(ref.get('inspection'),)).fetchone()
    inspected=json.loads(row[0]) if row else {}
    if any(inspected.get(k)!=v for k,v in {'requirement':rid,'project':project,'epoch':binding['epoch'],'execution_revision':revision,'revision':ref.get('inspection_revision')}.items()) or inspected.get('repo',{}).get('sha256')!=ref['repo_sha256']:raise Blocked('规划缺对应工程检查来源')
    source=registered
    if ref.get('adjust_request'):
        row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='original_intent'",(ref['adjust_request'],)).fetchone()
        original=json.loads(row[0]) if row else {}
        context=inspected.get('candidate_context') or {}
        if original.get('project')!=project or original.get('requirement')!=rid or context.get('request')!=ref['adjust_request'] or context.get('candidate_request')!=original.get('queue_key'):raise Blocked('规划调整缺原工程请求来源')
        source=_path(context['workspace'])
        queued=candidate_source(store,original['queue_key'],project,registered,binding,source)
        prior=inspected.get('entry_binding',{}).get('task',{})
        if prior.get('state')!='verified' or prior.get('task')!=queued['reference'] or not prior.get('run'):raise Blocked('规划调整缺原task/run证明')
        _read_task(store,call,queued['reference'],source,prior['run'])
        if repo_fingerprint(source)['sha256']!=context.get('sha256'):raise Blocked('规划调整源候选已变化')
    elif inspected.get('candidate_context'):raise Blocked('规划缺正式调整引用')
    if _path(candidate['source'])!=source:raise Blocked('规划导出代码来源不符')
    if not candidate.get('development',{}).get('history_detached'):
        _git(target,'merge-base','--is-ancestor',_git(source,'rev-parse','HEAD'),'HEAD')
    return target


def _result(project,task,run):
    return {'schema':'portable.project-binding/v1','project':project,'registration_source':'Store.projects/paths; board.registry when present',
            'repository':None,'allowed_workspaces':[],'readiness_epoch':None,'task':{'state':'unknown','task':task,'run':run},
            'coverage':{'product_entry_checks':True,'arbitrary_tool_writes':False,'path_replacement_atomic':False},
            'coverage_notice':'产品入口校验已覆盖；任意工具写入未受保护；路径替换竞态未覆盖',
            'status':'BLOCKED','reasons':[],'next':'核对现有登记、readiness及原任务服务；不创建授权'}


def query(store,project,workspace=None,task=None,run=None,epoch=None,call=None,*,candidate_request=None):
    # Even reauthorization/resolve branches receive a SQL-enforced read-only view.
    try:readonly=ReadOnlyStore(store.root)
    except Blocked as exc:
        result=_result(project,task,run);result['reasons']=[str(exc)];return result
    try:return _query(readonly,project,workspace,task,run,epoch,call,candidate_request=candidate_request)
    finally:readonly.db.close()


def _query(store,project,workspace=None,task=None,run=None,epoch=None,call=None,*,candidate_request=None):
    """call is an injected read-only get_task service, never user supplied proof JSON."""
    from .board import load
    from .team import validate_binding, request_scope_valid
    result=_result(project,task,run)
    try:
        rows=store.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(project,store.device)).fetchall()
        if len(rows)!=1 or not store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('项目没有唯一当前设备登记')
        registered=_path(rows[0]['path']);target=_path(workspace if workspace is not None else registered)
        entries=load(store)
        if not isinstance(entries,list) or any(not isinstance(e,dict) for e in entries):raise Blocked('注册表格式未知')
        matches=[e for e in entries if e.get('id')==project]
        if entries and (len(matches)!=1 or _path(matches[0]['path'])!=registered):raise Blocked('注册表与当前设备路径冲突')
        common=_identity(registered);result['repository']={'common_dir':common};result['allowed_workspaces']=[str(registered)]
        same_repository=_identity(target)==common
        if not same_repository and not candidate_request and task:
            candidates=[]
            for row in store.db.execute('SELECT id FROM outbox WHERE reference=?',(task,)):
                value=store.get('candidate:engineering:'+row['id'],{})
                if value.get('workspace') and _path(value['workspace'])==target:candidates.append(row['id'])
            if len(candidates)==1:candidate_request=candidates[0]
        planning_refs=[]
        if task:
            for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'planner:%'"):
                ref=json.loads(row['value'])
                if ref.get('task')==task:planning_refs.append((row['key'],ref))
        if not same_repository and not candidate_request and len(planning_refs)!=1:raise Blocked('工作区属于另一仓库')
        worktrees=_live_worktrees(registered)
        if same_repository and target not in worktrees:raise Blocked('工作区未列入Git worktree')
        # Unlike require_readiness, this read never promotes legacy metadata.
        values=store.get('readiness_projects',{});pointer=store.get('readiness',{})
        if not isinstance(values,dict):raise Blocked('项目绑定映射损坏')
        binding=values.get(project,{}) if values else pointer
        if values and not pointer.get('project'):raise Blocked('旧绑定指针缺失')
        if binding.get('project')==project:
            if pointer.get('project')==project and pointer!=binding:raise Blocked('本项目旧绑定已变更')
            validate_binding(store,binding);result['readiness_epoch']=binding['epoch']
        elif values or pointer or store.get('execution')=='active':
            bound=pointer.get('project') or '、'.join(sorted(values)) or '无'
            raise Blocked(f'请求项目与绑定项目不符：{project} 现在不在「可开工」状态（现在可开工的是 {bound}），要开工先按流程登记')
        else:binding=None  # no-team read-only discovery remains available
        if epoch is not None and (not binding or epoch!=binding['epoch']):raise Blocked('readiness epoch已变化')
        if candidate_request:
            if not binding:raise Blocked('候选缺有效readiness')
            original=candidate_source(store,candidate_request,project,registered,binding,target)
            if task is not None and task!=original['reference']:raise Blocked('候选原任务不符')
            task=original['reference']
        proofs=[]
        if task:
            if not binding:raise Blocked('任务缺有效readiness')
            for row in store.db.execute('SELECT * FROM outbox WHERE reference=?',(task,)):
                body=json.loads(row['body'])
                if body.get('operation'):continue
                if body.get('project')!=project:raise Blocked('原任务属于另一项目')
                request_scope_valid(store,row,body,binding)
                candidate=store.get('candidate:engineering:'+row['id'],{})
                proofs.append(_path(candidate.get('workspace') or body['workspace']))
            for key,ref in planning_refs:
                proofs.append(planning_source(store,key,ref,project,registered,binding,target,call))
            if not proofs or set(proofs)!={target}:raise Blocked('原任务缺唯一工作区引用')
            actual=_read_task(store,call,task,target,run)
            result['task']={'state':'verified','task':task,'run':actual}
        elif run is not None:raise Blocked('run缺原task引用')
        if target!=registered:
            if not proofs:
                # Existing candidate metadata is only a workspace source, never
                # proof of task/run identity or a new readiness grant.
                sources=[]
                for row in store.db.execute('SELECT * FROM outbox WHERE reference IS NOT NULL'):
                    body=json.loads(row['body'])
                    candidate=store.get('candidate:engineering:'+row['id'],{})
                    if candidate.get('workspace') and _path(candidate['workspace'])==target:
                        if not binding or body.get('project')!=project:raise Blocked('候选项目不符')
                        request_scope_valid(store,row,body,binding);sources.append(row['id'])
                if len(sources)!=1:raise Blocked('worktree缺唯一既有候选/任务绑定来源')
            result['allowed_workspaces'].append(str(target))
        result['status']='READY';result['next']='沿现有产品入口继续；此结果不授予任意工具写权限'
    except (Blocked,OSError,ValueError,KeyError,TypeError,AttributeError,sqlite3.Error,subprocess.SubprocessError) as exc:
        result['reasons']=[str(exc)]
    return result


def require(store,project,workspace=None,task=None,run=None,epoch=None,call=None,strong_atomic=False,candidate_request=None):
    result=query(store,project,workspace,task,run,epoch,call,candidate_request=candidate_request)
    if strong_atomic:raise Blocked('路径替换原子保护未覆盖，拒绝需要强保证的动作')
    if result['status']!='READY':raise Blocked(canonical(result))
    if result['readiness_epoch'] is None:raise Blocked('只读登记查询不授予工程写权限：缺readiness')
    return result
