"""Explicit local full-project assets; historical callers are never executable."""
import json
from pathlib import Path
import sys
import uuid
from . import legacy_assets as legacy
from .core import Store,Blocked,canonical
from .privacy import sanitize


def selection(store,reference):
    value=store.get('legacy:selection:'+reference,{})
    if value.get('device')!=store.device or value.get('root')!=str(store.root):raise Blocked('旧设备选择只作历史；请在本机重新选择')
    return value

def preview(projects):
    report=legacy.preflight(projects)
    for item in report['projects']:
        selected={str(Path(p).resolve()) for p in item['contexts']}
        for suggestion in item['suggestions']:suggestion['selected']=str(Path(suggestion['path']).resolve()) in selected
    return report


def choose(store,projects,contexts=(),homes=(),histories=(),evidence=(),archives=(),exclusions=(),scope_reviewed=False):
    def pairs(values):
        result={}
        for raw in values:
            key,sep,value=raw.partition('=')
            if not sep or not key or not value:raise Blocked('选择格式为 ID=路径或相对证据')
            result.setdefault(key,[]).append(value)
        return result
    contexts=pairs(contexts);homes=pairs(homes);rows=[]
    for project in projects:
        row=store.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(project,store.device)).fetchone()
        if not row:raise Blocked('项目未在本设备登记')
        item={'id':project,'path':row[0],'context_roots':contexts.get(project,[])}
        if homes.get(project):item['project_home']=homes[project][0]
        rows.append(item)
    selected=pairs(evidence);history=[]
    for ident,roots in pairs(histories).items():
        if len(roots)!=1:raise Blocked('同历史ID须唯一state根')
        history.append({'id':ident,'state_root':roots[0],'selections':selected.get(ident,[])})
    if set(selected)-{h['id'] for h in history}:raise Blocked('历史证据缺明确state根')
    omitted=[{'source':source,'reason':reason} for source,values in pairs(exclusions).items() for reason in values]
    report=preview(rows)
    for project in report['projects']:
        for suggestion in project['suggestions']:
            if not suggestion['selected']:omitted.append({'source':suggestion['path'],'reason':'外围上下文未明确选择，未静默携带'})
    if not scope_reviewed:omitted.append({'source':'未逐项选择的旧任务/原生会话/旧资料','reason':'未选择或未export；本包不声明包含全部机器资料'})
    value={'device':store.device,'root':str(store.root),'projects':rows,'histories':history,'archives':[{'id':ident,'path':path} for ident,paths in pairs(archives).items() for path in paths],'exclusions':omitted}
    reference=uuid.uuid4().hex;store.put('legacy:selection:'+reference,value)
    return {'selection':reference,'preflight':report,'exclusions':omitted,'complete':False}


def capture(store,reference,target,*,sensitive=False,recipient=None,history_plaintext=False):
    value=selection(store,reference)
    from .git_portable import export
    normalize=(lambda source,dest:export(source,dest,sensitive_encrypted_only=True)) if sensitive else None
    header=legacy.capture(value['projects'],target,histories=value['histories'],archives=value['archives'],exclusions=value['exclusions'],sensitive=sensitive,recipient=recipient,normalize=normalize,history_plaintext=history_plaintext)
    return {'container':str(target),'selection_captured':True,'complete':False,'scope':'explicit selected assets only','format':'portable.legacy-assets/v1','header':header,'old_authority_imported':False}


def restore(store,package,target,identity=None):
    manifest=legacy.restore(package,target,identity_file=identity)
    from .runtime_identity import reset_restored_tree
    for db in Path(target).rglob('数据/state.sqlite'):reset_restored_tree(db.parent.parent)
    reference=uuid.uuid4().hex
    store.put('legacy:restore:'+reference,{'device':store.device,'root':str(store.root),'target':str(Path(target).resolve()),'manifest':manifest})
    return {'restored':reference,'target':str(target),'selection_captured':manifest['selection_captured'],'complete':manifest['complete'],'projects':len(manifest['projects']),'history_entries':len(manifest['histories']),'active_projects_registered':0,'old_authority_imported':False,'unresolved':manifest['unresolved'],'exclusions':manifest['exclusions']}


def draft(store,reference,task,checkpoint=None):
    value=store.get('legacy:restore:'+reference,{})
    if value.get('device')!=store.device or value.get('root')!=str(store.root):raise Blocked('历史恢复引用不属于当前设备')
    result=legacy.continuation_draft(value['manifest'],task,new_caller='portable:'+store.device,checkpoint=sanitize(checkpoint) if checkpoint else None)
    if checkpoint:result.update(historical_checkpoint=sanitize(checkpoint),current_execution_grants={},historical_restrictions_remain_binding=True)
    return sanitize(result)


def wizard(store):
    if not sys.stdin.isatty():raise Blocked('旧资产明选须本人本机终端；无人值守使用已登记选择引用')
    print('完整旧资产：1 明确选择并预检全部附属工作树 · 2 封存选择 · 3 恢复到新目录 · 4 草拟旧任务续接')
    action=input('选择（回车返回）：').strip()
    if action=='1':
        projects=input('登记项目ID，逗号分隔：').strip().split(',');contexts=[];homes=[]
        for project in projects:
            home=input(project+'明确外围项目目录（只建议，不递归复制，回车空）：').strip()
            if home:homes.append(project+'='+home)
            while True:
                path=input(project+'要携带的外围规则/决策文件或目录（填完回车）：').strip()
                if not path:break
                contexts.append(project+'='+path)
        histories=[];evidence=[];archives=[]
        while True:
            ident=input('旧战队历史选择ID（不选历史回车）：').strip()
            if not ident:break
            root=input('旧state根（不读账号目录）：').strip();histories.append(ident+'='+root);print(canonical(legacy.history_index(root)))
            while True:
                item=input('明确tasks/<id>.json或tasks/dispatches内证据相对路径（填完回车）：').strip()
                if not item:break
                evidence.append(ident+'='+item)
        while True:
            ident=input('旧资料归档ID（不选资料回车）：').strip()
            if not ident:break
            archives.append(ident+'='+input('明确旧资料文件/目录：').strip())
        return choose(store,projects,contexts,homes,histories,evidence,archives,scope_reviewed=input('已逐项明选本次必要范围，其他无必需资产，输入“已审范围”确认（否则记未选清单）：').strip()=='已审范围')
    if action=='2':
        reference=input('已确认选择引用：').strip();target=input('全新容器文件：').strip();sensitive=input('完整私密age包输入“加密”，普通安全包回车：').strip()=='加密';recipient=input('age公钥：').strip() if sensitive else None
        public=False
        if not sensitive and selection(store,reference)['histories']:public=input('确认所选历史可明文公开？输入“公开”：').strip()=='公开'
        return capture(store,reference,target,sensitive=sensitive,recipient=recipient,history_plaintext=public)
    if action=='3':
        package=input('容器文件：').strip();target=input('全新恢复目录：').strip();identity=input('本机age身份文件（普通包回车）：').strip() or None
        return restore(store,package,target,identity)
    if action=='4':return draft(store,input('已恢复引用：').strip(),input('原任务ID：').strip())
    return {'changed':False}


def register(subparser):
    p=subparser.add_parser('legacy');s=p.add_subparsers(dest='legacy_action',required=True)
    s.add_parser('menu')
    a=s.add_parser('select');a.add_argument('projects',nargs='+');a.add_argument('--scope-reviewed',action='store_true')
    for field in ('context','home','history','evidence','archive','exclude'):a.add_argument('--'+field,action='append',default=[])
    a=s.add_parser('preflight');a.add_argument('selection')
    a=s.add_parser('history-index');a.add_argument('state')
    a=s.add_parser('capture');a.add_argument('selection');a.add_argument('target');a.add_argument('--sensitive',action='store_true');a.add_argument('--recipient');a.add_argument('--public-history',action='store_true')
    a=s.add_parser('restore');a.add_argument('package');a.add_argument('target');a.add_argument('--identity')
    a=s.add_parser('draft');a.add_argument('restored');a.add_argument('task')
    p.set_defaults(legacy_run=run)


def run(args):
    store=Store(args.root)
    try:
        a=args.legacy_action
        if a=='menu':return wizard(store)
        if a=='select':return choose(store,args.projects,args.context,args.home,args.history,args.evidence,args.archive,args.exclude,args.scope_reviewed)
        if a=='preflight':return preview(selection(store,args.selection)['projects'])
        if a=='history-index':return legacy.history_index(args.state)
        if a=='capture':return capture(store,args.selection,args.target,sensitive=args.sensitive,recipient=args.recipient,history_plaintext=args.public_history)
        if a=='restore':return restore(store,args.package,args.target,args.identity)
        return draft(store,args.restored,args.task)
    finally:store.db.close()
