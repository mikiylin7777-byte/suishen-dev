"""Local UI for explicit code selections; restored services remain disabled."""
import json
import os
from pathlib import Path
import sys
import uuid
from . import components as c
from .core import Store,Blocked,canonical

KEY='components:local'
def local(store):
    value=store.get(KEY,{})
    if value.get('device')!=store.device or value.get('root')!=str(store.root):return {'device':store.device,'root':str(store.root),'selected':[],'prefixes':{}}
    return value

def required_rows(generation,ids):
    rows={r['id']:r for r in c.plan(generation)} if ids else {}
    if len(set(ids))!=len(ids) or any(cid not in rows for cid in ids):raise Blocked('必需组件ID未知或重复；请先检查恢复目录')
    return [{'generation':str(Path(generation).resolve()),'id':cid,'hash':rows[cid]['hash']} for cid in ids]


def select(store,rows):
    value=local(store);merged={r['id']:r for r in value['selected']}
    for row in rows:merged[row['id']]=row
    value['selected']=list(merged.values());store.put(KEY,value)
    return {'selections':c.doctor(value['selected'])}

def activate(store,root,component,target,scope):
    # Never usable unattended or through an imported approval flag.
    if not sys.stdin.isatty():raise Blocked('实际服务启用须在本人本机终端确认具体新目标')
    owner='local-uid-'+str(os.getuid());task='component-'+uuid.uuid4().hex
    hook=c.activation_hook_for_store(store,task=task,owner=owner)
    proposal=c.prepare_service_activation(root,component,target,device=store.device,scopes=[scope],hook=hook)
    print(canonical({'component':component,'target':proposal['target'],'scope':scope,'component_hash':proposal['hash'],'device':store.device,'effect':'在当前登录会话注册新服务；不证明健康或重启自启'}))
    if input('仅当你批准以上实际启用，输入“批准启用”：').strip()!='批准启用':return {'activated':False}
    token=store.approval(c.service_approval_binding(proposal,task=task,owner=owner))
    return c.activate_service(root,component,target,proposal,token,device=store.device,scopes=[scope],hook=hook)

def wizard(store):
    if not sys.stdin.isatty():raise Blocked('组件选择需要本人本机终端')
    print('工具与服务携带：1 选择CLI代码 · 2 选择App代码 · 3 封存选中项 · 4 恢复容器 · 5 检查恢复状态 · 6 重建CLI入口 · 7 登记配置或服务 · 8 生成停用服务 · 9 本机明确启用服务 · 10 生成停用配置 · 11 批准持久安装服务 · 12 本次迁移必需组件 · 13 检查迁移组件')
    action=input('选择（回车返回）：').strip()
    if action=='13':
        from .component_install import required_status
        return required_status(store)
    if action=='12':
        generation=input('本机已恢复组件目录：').strip();raw=input('必需组件ID（逗号分隔；确实无需工具输入“无”）：').strip()
        if not raw:return {'changed':False}
        ids=[x.strip() for x in raw.split(',')] if raw!='无' else []
        required=required_rows(generation,ids)
        value=local(store);value['required']=required;store.put(KEY,value);return {'required':required}
    if action=='1':return select(store,c.select_installed(input('明确选择 codex/claude/opencode（逗号分隔）：').strip().split(',')))
    if action=='2':return select(store,[c.register('app-'+uuid.uuid4().hex[:8],input('明确选择 .app 路径：').strip(),kind='app',adapter='app-code',selected=True,login_required=True)])
    if action=='3':return c.save(local(store)['selected'],input('全新容器文件路径：').strip(),recipient=input('私密配置/服务必填age公钥，纯代码可空：').strip() or None)
    if action=='4':
        package=input('容器文件：').strip();target=input('全新恢复目录：').strip();identity=input('加密包本机age身份文件（普通包空）：').strip() or None
        return c.restore(package,target,identity_file=identity)
    if action=='7':
        adapter=input('类型 json-config / launchd-plist / launchd-recipe / tunnel-recipe：').strip()
        kinds={'json-config':'config','launchd-plist':'launchd','launchd-recipe':'launchd','tunnel-recipe':'tunnel'}
        if adapter not in kinds:raise Blocked('未知组件类型')
        cid=input('本地组件ID：').strip();source=input('明确原文件路径：').strip();bindings={}
        if adapter=='launchd-plist':
            old=input('原程序绝对路径：').strip();component=input('对应已选CLI组件ID（SSH填system-ssh）：').strip();profile=input('受支持profile：').strip();bindings[old]={'component':component,'profile':profile}
            oldwork=input('原工作目录（无则空）：').strip()
            if oldwork:bindings[oldwork]=input('新绑定资源ID（如workspace）：').strip()
        return select(store,[c.register(cid,source,kind=kinds[adapter],adapter=adapter,selected=True,program_bindings=bindings)])
    if action in ('5','6','8','9','10','11'):
        root=input('已恢复组件目录：').strip()
        if action=='5':return c.status(root,rebuilt_prefixes=local(store).get('prefixes',{}).get(str(Path(root).resolve()),{}))
        cid=input('组件ID：').strip();target=input('全新本机目标路径：').strip()
        if action=='6':
            rows=c.plan(root);row=next((r for r in rows if r['id']==cid),None)
            if row is None:raise Blocked('未知组件')
            print('所选代码hash：'+row['hash'])
            if input('确认信任此来源并仅执行版本探针，输入“信任”：').strip()!='信任':return {'rebuilt':False}
            return materialize(store,root,cid,target,row['hash'])
        if action=='11':
            from .component_install import install
            return install(store,root,cid,target,input('准确范围 service 或 external_connection：').strip())
        if action=='9':return activate(store,root,cid,target,input('准确范围 service 或 external_connection：').strip())
        bindings={}
        while True:
            name=input('新机绑定ID（填完回车）：').strip()
            if not name:break
            bindings[name]=input('对应新机现存目录：').strip()
        if action=='8':
            identity=None
            if input('SSH服务绑定新身份？yes/回车：').strip()=='yes':identity={'identity_file':input('新私钥文件路径（不读取内容）：').strip(),'known_hosts':input('新known_hosts路径：').strip()}
            return c.stage_service(root,cid,target,device=store.device,bindings=bindings,ssh_identity=identity)
        fields={}
        while True:
            field=input('需重绑的JSON顶层路径字段（填完回车）：').strip()
            if not field:break
            fields[field]=input('对应绑定ID：').strip()
        return c.stage_config(root,cid,target,path_fields=fields,bindings=bindings)
    return {'changed':False}

def materialize(store,root,cid,target,trusted):
    result=c.materialize_native(root,cid,target,trusted_component_hash=trusted)
    value=local(store);value['prefixes'].setdefault(str(Path(root).resolve()),{})[cid]=str(Path(target).resolve());store.put(KEY,value)
    return result

def register(subparser):
    p=subparser.add_parser('components');s=p.add_subparsers(dest='component_action',required=True)
    for action in ('menu','doctor','required-status'):s.add_parser(action)
    a=s.add_parser('select-cli');a.add_argument('names',nargs='+',choices=['codex','claude','opencode'])
    a=s.add_parser('select-file');a.add_argument('id');a.add_argument('source');a.add_argument('--adapter',required=True,choices=['app-code','json-config','launchd-plist','launchd-recipe','tunnel-recipe']);a.add_argument('--bindings')
    a=s.add_parser('check-login');a.add_argument('generation');a.add_argument('id');a.add_argument('prefix')
    a=s.add_parser('probe-service');a.add_argument('id')
    a=s.add_parser('require');a.add_argument('generation');a.add_argument('ids',nargs='*');a.add_argument('--confirm-none',action='store_true')
    a=s.add_parser('save');a.add_argument('target');a.add_argument('--recipient')
    a=s.add_parser('restore');a.add_argument('package');a.add_argument('target');a.add_argument('--identity')
    a=s.add_parser('status');a.add_argument('generation')
    a=s.add_parser('materialize');a.add_argument('generation');a.add_argument('id');a.add_argument('target');a.add_argument('--trust-hash',required=True)
    for action in ('stage-service','stage-config','activate','install-service'):
        a=s.add_parser(action);a.add_argument('generation');a.add_argument('id');a.add_argument('target')
        if action in ('activate','install-service'):a.add_argument('--scope',choices=['service','external_connection'],required=True)
        else:a.add_argument('--bindings',required=True);a.add_argument('--fields');a.add_argument('--ssh-identity')
    p.set_defaults(components_run=run)

def run(args):
    s=Store(args.root)
    def data(name):return json.loads(Path(name).read_text()) if name else {}
    try:
        a=args.component_action
        if a=='menu':return wizard(s)
        if a=='check-login':
            from .component_install import check_login
            return check_login(s,args.generation,args.id,args.prefix)
        if a=='probe-service':
            from .component_install import probe
            return probe(s,args.id)
        if a=='required-status':
            from .component_install import required_status
            return required_status(s)
        if a=='require':
            if not args.ids and not args.confirm_none:raise Blocked('无组件迁移须明确--confirm-none')
            required=required_rows(args.generation,args.ids)
            value=local(s);value['required']=required;s.put(KEY,value);return {'required':required}
        if a=='install-service':
            from .component_install import install
            return install(s,args.generation,args.id,args.target,args.scope)
        if a=='doctor':return c.doctor(local(s)['selected'])
        if a=='select-cli':return select(s,c.select_installed(args.names))
        if a=='select-file':return select(s,[c.register(args.id,args.source,kind={'app-code':'app','json-config':'config','launchd-plist':'launchd','launchd-recipe':'launchd','tunnel-recipe':'tunnel'}[args.adapter],adapter=args.adapter,selected=True,program_bindings=data(args.bindings))])
        if a=='save':return c.save(local(s)['selected'],args.target,recipient=args.recipient)
        if a=='restore':return c.restore(args.package,args.target,identity_file=args.identity)
        if a=='status':return c.status(args.generation,rebuilt_prefixes=local(s)['prefixes'].get(str(Path(args.generation).resolve()),{}))
        if a=='materialize':return materialize(s,args.generation,args.id,args.target,args.trust_hash)
        if a=='stage-service':return c.stage_service(args.generation,args.id,args.target,device=s.device,bindings=data(args.bindings),ssh_identity=data(args.ssh_identity) or None)
        if a=='stage-config':return c.stage_config(args.generation,args.id,args.target,bindings=data(args.bindings),path_fields=data(args.fields))
        return activate(s,args.generation,args.id,args.target,args.scope)
    finally:s.db.close()
