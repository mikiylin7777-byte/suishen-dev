"""Recoverable installer, reversible preferences, isolated client configuration checks."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from .privacy import sanitize
from .core import Store, Blocked, PROTOCOL, atomic, canonical, cooperative

PROMPTS={'name':'希望如何称呼你？','tools':'你常用什么工具？可选 Codex、Claude、T3 或其他','goal':'最想先解决什么问题？','notifications':'需要完成通知吗？默认不启用机器人，可稍后设置'}

def preferences(store):
    legacy=store.get('profile',{})
    mapping={'name':'user','goal':'goals','tools':'tools','notifications':'notifications'}
    revoked=store.get('onboarding_revoked',[])
    values={key:legacy[old] for key,old in mapping.items() if legacy.get(old) and key not in revoked}
    values.update(store.get('onboarding',{}))
    return sanitize(values)

@cooperative
def preference(store,key,value,*,persistent=True,confirmed=False):
    if key not in (*PROMPTS,'important_rules'):raise Blocked('未知偏好')
    if not persistent:return {'temporary':{key:value},'saved':False}
    if key=='important_rules' and not confirmed:raise Blocked('重要规则必须本人明确确认后保存')
    value=sanitize(value)
    value_state=preferences(store)
    revoked=store.get('onboarding_revoked',[])
    if value is None:
        value_state.pop(key,None)
        if key not in revoked:revoked.append(key)
    else:
        value_state[key]=value
        revoked=[k for k in revoked if k!=key]
    store.put('onboarding_revoked',revoked)
    store.put('onboarding',value_state)
    note='# 个人偏好\n\n'+''.join('- '+PROMPTS.get(k,'已确认的重要规则')+'：'+str(v)+'\n' for k,v in value_state.items())+'\n可在设置中撤回；临时要求不自动保存。\n'
    atomic(store.root/'知识库/记忆/个人偏好.md',note)
    return {'saved':True,'preferences':value_state,'execution_enabled':False}

def interview(store):
    state=preferences(store)
    missing=next((k for k in PROMPTS if not state.get(k)),None)
    return {'preferences':state,'question':{'field':missing,'text':PROMPTS[missing]} if missing else None,'complete':missing is None,'defaults':{'tools':'自行选择','notifications':'off'},'execution_enabled':False}

def dependency_check(store):
    return {'python':sys.version.split()[0],'standard_library_ready':True,'optional':{name:bool(shutil.which(name)) for name in ('codex','claude','pdftotext','tesseract')},'global_install':False}

@cooperative
def configure_context(store,client,python=None,package=None):
    from .context_server import enroll
    python=str(Path(python or sys.executable).absolute())
    package=Path(package or Path(__file__).resolve().parents[1]).absolute()
    if package.is_file():
        import zipfile
        try:
            with zipfile.ZipFile(package) as archive:archive.getinfo('portable/context_server.py')
        except (OSError,KeyError,zipfile.BadZipFile) as e:raise Blocked('发行缺少共享上下文模块') from e
    elif not (package/'portable/context_server.py').is_file():raise Blocked('发行缺少共享上下文模块')
    if not client or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in client):raise Blocked('工具名称无效')
    enroll(store,client)
    entry={'command':python,'args':['-I','-B','-c','import sys; sys.path.insert(0,'+repr(str(package))+'); from portable.context_server import main; sys.exit(main())','--root',str(store.root),'context','serve','--client',client]}
    folder=store.root/'工具/context'/client
    # Client is a filename component, not a path.
    if not client or any(c not in 'abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-' for c in client):raise Blocked('工具名称只支持字母、数字、下划线和短横线')
    from .core import safe
    folder=safe(store.root,'工具/context/'+client);folder.mkdir(parents=True,exist_ok=True)
    generic={'mcpServers':{'portable_context':entry}}
    atomic(folder/'mcp.json',canonical(generic))
    atomic(folder/'codex.toml','[mcp_servers.portable_context]\ncommand = '+json.dumps(python)+'\nargs = '+json.dumps(entry['args'],ensure_ascii=False)+'\n')
    # Entries reference the one protocol file; never copy contract contents.
    protocol_path=store.root/'工具/协议.json' if package.is_file() else package/'portable/protocol.json'
    pointer='共享上下文协议：'+str(protocol_path)+'\n上下文入口：'+str(folder/'mcp.json')+'\n普通笔记不派工；工程验证走 review-verification。\n'
    atomic(folder/'AGENTS.md',pointer);atomic(folder/'CLAUDE.md',pointer)
    return {'folder':str(folder),'entry':entry,'global_config_modified':False,'loaded':False}

def isolated_environment(home):
    return {'HOME':str(home),'CODEX_HOME':str(home/'codex'),'CLAUDE_CONFIG_DIR':str(home/'claude'),'XDG_CONFIG_HOME':str(home/'config'),'PATH':os.environ.get('PATH','/usr/bin:/bin'),'TMPDIR':str(home/'tmp'),'DISABLE_AUTOUPDATER':'1','DISABLE_TELEMETRY':'1','DISABLE_ERROR_REPORTING':'1','CLAUDE_CODE_SIMPLE':'1','PYTHONDONTWRITEBYTECODE':'1'}

def check_clients(store,client,*,run_clients=False):
    config=configure_context(store,client)
    entry=config['entry'];requests=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'isolated-check','version':'1'}}},{'jsonrpc':'2.0','method':'notifications/initialized'},{'jsonrpc':'2.0','id':2,'method':'tools/list'},{'jsonrpc':'2.0','id':3,'method':'tools/call','params':{'name':'projects','arguments':{}}}]
    with tempfile.TemporaryDirectory(prefix='portable-client-') as temp:
        home=Path(temp).resolve()
        for name in ('codex','claude','config','tmp'):(home/name).mkdir()
        env=isolated_environment(home)
        result=subprocess.run([entry['command'],*entry['args']],input=''.join(canonical(r)+'\n' for r in requests),capture_output=True,text=True,env=env,cwd=home,timeout=20)
        try:responses=[json.loads(line) for line in result.stdout.splitlines()]
        except ValueError:responses=[]
        passed=result.returncode==0 and len(responses)==3 and all('result' in r for r in responses) and responses[1]['result'].get('tools') and not responses[2]['result'].get('isError')
        checks={'stdio':bool(passed),'client_checks':{},'new_chat_created':False,'global_config_modified':False}
        if not run_clients:return checks
        atomic(home/'codex/config.toml',Path(config['folder'],'codex.toml').read_text())
        atomic(home/'mcp.json',canonical({'mcpServers':{'portable_context':entry}}))
        commands={'codex':['mcp','list','--json'],'claude':['--bare','mcp','list']}
        for tool,args in commands.items():
            binary=shutil.which(tool)
            if tool=='codex':
                # PATH may start with a package-manager shim that needs the real
                # user's HOME. Prefer an actual installed executable in PATH.
                for part in sorted(env['PATH'].split(os.pathsep),key=lambda x:('.app/' not in x,x)):
                    candidate=Path(part)/'codex'
                    if candidate.is_file() and os.access(candidate,os.X_OK) and candidate.resolve().name not in ('vp','vite-plus'):
                        binary=str(candidate);break
            if not binary:checks['client_checks'][tool]={'status':'not_installed','loaded':False};continue
            try:
                if tool=='claude':
                    added=subprocess.run([binary,'--bare','mcp','add','--scope','user','portable_context','--',entry['command'],*entry['args']],cwd=home,env=env,capture_output=True,text=True,timeout=20)
                    if added.returncode:raise ValueError('isolated configuration not accepted')
                run_env=env
                if tool=='codex' and Path(binary).resolve().name in ('vp','vite-plus'):
                    # A vite-plus shim locates its packages through the real HOME; the
                    # config under test still comes only from the isolated CODEX_HOME.
                    run_env={**env,'HOME':os.environ.get('HOME',env['HOME'])}
                r=subprocess.run([binary,*args],cwd=home,env=run_env,capture_output=True,text=True,timeout=40)
                if tool=='codex':
                    rows=json.loads(r.stdout) if r.returncode==0 else []
                    loaded=any(row.get('name')=='portable_context' and row.get('transport',{}).get('command')==entry['command'] and row.get('transport',{}).get('args')==entry['args'] for row in rows)
                else:loaded=r.returncode==0 and 'portable_context' in r.stdout and ('Connected' in r.stdout or '✓' in r.stdout)
                checks['client_checks'][tool]={'status':'loaded' if loaded else 'load_failed','loaded':loaded,'exit_code':r.returncode}
            except (OSError,ValueError,subprocess.SubprocessError):checks['client_checks'][tool]={'status':'load_failed','loaded':False}
        return checks

def check_sdk(store,client,sdk_python):
    """Reuse a caller-selected existing package interpreter; never pip install."""
    config=configure_context(store,client)
    code="""
import asyncio,json,sys
from mcp import ClientSession,StdioServerParameters
from mcp.client.stdio import stdio_client
async def run():
    c=json.load(sys.stdin)
    async with stdio_client(StdioServerParameters(command=c['command'],args=c['args'])) as (read,write):
        async with ClientSession(read,write) as s:
            await s.initialize()
            tools=await s.list_tools()
            result=await s.call_tool('projects',{})
            assert any(t.name=='checkpoint' for t in tools.tools)
            assert not getattr(result,'isError',False)
            print(json.dumps({'sdk_stdio':True,'tools':len(tools.tools),'projects_call':True}))
asyncio.run(run())
"""
    with tempfile.TemporaryDirectory(prefix='portable-sdk-') as temp:
        home=Path(temp).resolve()
        for name in ('codex','claude','config','tmp'):(home/name).mkdir()
        r=subprocess.run([str(Path(sdk_python).absolute()),'-I','-B','-c',code],input=canonical(config['entry']),env=isolated_environment(home),cwd=home,capture_output=True,text=True,timeout=30)
        if r.returncode:return {'sdk_stdio':False,'exit_code':r.returncode,'reason':'现有SDK不可用或协议检查失败；未安装全局依赖'}
        return json.loads(r.stdout)

def install(source,target):
    from .distribution import build
    target=Path(target)
    if target.exists():raise Blocked('安装目标必须为新目录；已有安装请使用现有升级入口')
    build(Path(source),target)
    return {'installed':str(target),'next':'python3 -m portable.onboarding --root '+str(target)+' onboarding menu','execution_enabled':False}


DAILY=['看所有项目进度','给某项目提需求','看某个需求进展','立刻备份','打开某项目目录']
ADVANCED=['继续设置','新建需求','查看项目','素材与方法','跨工具续接','检查安装','修改或撤回偏好','登记项目','机器人私密设置','后台与备份','导入素材','敏感携带选择','后台启用或暂停','连接机器人','新建自管项目','工具与服务携带','完整旧资产保全']

def _pick_project(store):
    from .tools import project_choices
    choices=project_choices(store)['projects']
    if not choices:raise Blocked('尚无登记项目')
    for i,row in enumerate(choices,1):print(str(i)+'. '+row['name'])
    raw=input('项目编号（回车返回）：').strip()
    if not raw:return None
    if not raw.isdigit() or not 1<=int(raw)<=len(choices):raise Blocked('项目编号无效')
    return choices[int(raw)-1]

def _requirement_progress(store):
    from .workflows import status
    rows=store.db.execute("SELECT id,body FROM records WHERE kind='requirement' ORDER BY rowid DESC LIMIT 20").fetchall()
    if not rows:return {'requirements':[],'message':'还没有提过需求'}
    names={r['id']:r['name'] for r in store.db.execute('SELECT id,name FROM projects')}
    for i,row in enumerate(rows,1):
        body=json.loads(row['body']);print(str(i)+'. ['+names.get(body.get('project'),'未归类')+'] '+body.get('idea','')[:40])
    raw=input('需求编号（回车返回）：').strip()
    if not raw:return {'requirements':len(rows)}
    if not raw.isdigit() or not 1<=int(raw)<=len(rows):raise Blocked('需求编号无效')
    state=status(store,rows[int(raw)-1]['id'])
    labels={'awaiting_project':'还没选项目','interview':'还差几句说明：'+(state['question'] or {}).get('text',''),'awaiting_implementation_intent':'说明已齐，等你说「实施」','needs_engineer_plan':'已交给工程排期/战队','awaiting_project_reception':'等项目接收','needs_task_adjustment':'范围变了，等重新排期'}
    print('进展：'+labels.get(state['state'],state['state']))
    return {'id':state['id'],'state':state['state'],'planning':state['planning']}

def _backup_now(store):
    cfg=store.get('runner:config',{}) or {}
    if not cfg.get('backup_dir'):raise Blocked('还没设置备份目录：高级 → 后台与备份')
    from .distribution import backup
    result=backup(store,cfg['backup_dir'],'daily')
    print('已备份到：'+result['backup']+('；各项目仓库：'+result['external']['backup'] if result.get('external') else ''))
    return result

def menu(store):
    if not sys.stdin.isatty():return {'choices':DAILY+['高级'],'advanced':ADVANCED,'interview':interview(store),'execution_enabled':False}
    print('随身开发：1 看所有项目进度 · 2 给某项目提需求 · 3 看某个需求进展 · 4 立刻备份 · 5 打开某项目目录 · 6 高级')
    top=input('选择（回车退出）：').strip()
    if top=='1':
        from .board import overview
        result=overview(store);print(result['page']);return {'projects':len(result['projects'])}
    if top=='3':return _requirement_progress(store)
    if top=='4':return _backup_now(store)
    if top=='5':
        selected=_pick_project(store)
        if not selected:return {'saved':True}
        from .console import open_project
        return open_project(store,selected['id'])
    if top=='2':choice='2'
    elif top=='6':
        print('高级：'+' · '.join(str(i)+' '+n for i,n in enumerate(ADVANCED,1)))
        choice=input('选择（回车返回）：').strip()
    else:return {'saved':True,'execution_enabled':False}
    if choice=='1':
        state=interview(store)
        while state['question']:
            q=state['question'];answer=input(q['text']+'（回车保存进度退出）：').strip()
            if not answer:return state
            preference(store,q['field'],answer);state=interview(store)
        return state
    if choice=='2':
        from .workflows import start,status,answer
        from .tools import project_choices
        choices=project_choices(store)['projects']
        if not choices:
            print('尚无项目；先访谈需求，明确实施后可直接新建自管项目。')
        for i,row in enumerate(choices,1):print(str(i)+'. '+row['name'])
        project=None
        if choices:
            raw=input('项目编号（0新项目，回车保存退出）：').strip()
            if not raw:return {'saved':True}
            if raw!='0':
                if not raw.isdigit() or not 1<=int(raw)<=len(choices):raise Blocked('项目编号无效')
                project=choices[int(raw)-1]['id'];project_choices(store,project)
        rid=start(store,project,input('描述想法：').strip())
        state=status(store,rid)
        while state['question']:
            q=state['question'];v=input(q['text']+'（回车保存退出）：').strip()
            if not v:return state
            state=answer(store,rid,q['field'],v,state['revision'])
        intent=input('现在明确要求实施首版吗？输入“实施”确认，回车暂存：').strip()=='实施'
        state=answer(store,rid,'implement',intent,state['revision'])
        if intent and project is None:
            name=input('明确新建自管项目的名称（回车仅保存需求）：').strip()
            if name:
                import uuid
                from .candidates import create_managed
                from .workflows import link_project
                project='project-'+uuid.uuid4().hex[:10];create_managed(store,name,project)
                state=link_project(store,rid,project,state['revision'])
                state=answer(store,rid,'implement',True,state['revision'])
        return state
    if choice=='3':
        from .tools import project_choices
        choices=project_choices(store)['projects']
        for i,row in enumerate(choices,1):print(str(i)+'. '+row['name'])
        raw=input('选择项目编号（回车返回）：').strip()
        if not raw:return {'projects':choices}
        if not raw.isdigit() or not 1<=int(raw)<=len(choices):raise Blocked('项目编号无效')
        selected=project_choices(store,choices[int(raw)-1]['id'])
        if input('输入“打开”在文件管理器打开项目，回车返回：').strip()=='打开':
            from .console import open_project
            return open_project(store,selected['selected']['id'])
        return selected
    if choice=='4':
        from .knowledge import search
        return search(store,input('搜索问题或方法：').strip())
    if choice=='5':
        from .session_adapters import resume
        return resume(store)
    if choice=='6':return dependency_check(store)
    if choice=='17':
        from .legacy_cli import wizard
        return wizard(store)
    if choice=='16':
        from .component_cli import wizard
        return wizard(store)
    if choice=='15':
        from .candidates import create_managed
        import uuid
        return create_managed(store,input('新项目名称：').strip(),'project-'+uuid.uuid4().hex[:10])
    if choice=='13':
        from .automation import pause
        from .supervisor import service
        value=input('输入“启用”启动本产品后台，“暂停”停写，回车返回：').strip()
        if value=='暂停':return pause(store,by='onboarding',reason='用户在菜单选了暂停')
        if value=='启用':
            pause(store,False)
            return service(store.root,Path.home()/'Library/LaunchAgents','install',execute=True)
        return {'changed':False}
    if choice=='14':
        from .bots import private_config
        from .core import atomic
        path=Path(store.get('bots_private_path',str(store.root/'数据/私密/bots.json')));value=private_config(path)
        if input('仅连接已绑定本人账号并允许收发，输入“启用”确认：').strip()!='启用':return {'enabled':False}
        if value.get('root')!=str(store.root):raise Blocked('机器人配置属于旧路径，请重新设置')
        from .bot_dependencies import ready,rebuild
        try:modules=ready(store)
        except Blocked:modules=rebuild(store)
        value['moduleRoot']=modules
        value['enabled']=True;atomic(path,canonical(value));path.chmod(0o600)
        cfg=store.get('runner:config',{});cfg['bots_private']=str(path)
        from .supervisor import configure_local
        configure_local(store,cfg)
        return {'enabled':True,'next':'后台启用或暂停','secret_output':False}
    if choice=='12':
        from .transfer import wizard
        return wizard(store)
    if choice=='11':
        from .knowledge import ingest
        source=input('素材文件路径、公开链接或来源名称：').strip()
        if source.startswith('https://'):return ingest(store,source,network=True)
        if Path(source).is_file():return ingest(store,source)
        return ingest(store,source,text=input('粘贴素材文字：').strip())
    if choice=='8':
        name=input('项目名称：').strip();path=input('项目目录：').strip()
        return {'project':store.register(name,path),'original_modified':False}
    if choice=='9':
        from .bots import wizard
        return wizard(store)
    if choice=='10':
        from .supervisor import configure_local
        path=input('包外备份目录（回车返回）：').strip()
        if not path:return {'saved':False}
        return configure_local(store,{'backup_dir':path})
    if choice=='7':
        keys=list(PROMPTS)+['important_rules']
        for i,key in enumerate(keys,1):print(str(i)+'. '+PROMPTS.get(key,'已确认的重要规则'))
        raw=input('选择编号（回车返回）：').strip()
        if not raw:return interview(store)
        if not raw.isdigit() or not 1<=int(raw)<=len(keys):raise Blocked('偏好编号无效')
        key=keys[int(raw)-1];value=input('新偏好（输入“撤回”移除，回车返回）：').strip()
        if not value:return interview(store)
        if value=='撤回':return preference(store,key,None,confirmed=key=='important_rules')
        confirmed=key!='important_rules' or input('重要规则将长期保存，输入“确认”同意：').strip()=='确认'
        return preference(store,key,value,confirmed=confirmed)
    return {'saved':True,'execution_enabled':False}

def register(subparser):
    p=subparser.add_parser('onboarding');s=p.add_subparsers(dest='onboarding_action',required=True)
    for name in ('menu','status','doctor'):s.add_parser(name)
    a=s.add_parser('preference');a.add_argument('key');a.add_argument('value',help='JSON value; null revokes');a.add_argument('--temporary',action='store_true');a.add_argument('--confirm',action='store_true')
    a=s.add_parser('configure');a.add_argument('client')
    a=s.add_parser('check-clients');a.add_argument('client');a.add_argument('--run-clients',action='store_true');a.add_argument('--sdk-python')
    a=s.add_parser('install');a.add_argument('source');a.add_argument('target')
    return p

def run(store,args):
    action=args.onboarding_action
    if action=='menu':return menu(store)
    if action=='status':return interview(store)
    if action=='doctor':return dependency_check(store)
    if action=='preference':return preference(store,args.key,json.loads(args.value),persistent=not args.temporary,confirmed=args.confirm)
    if action=='configure':return configure_context(store,args.client)
    if action=='check-clients':
        result=check_clients(store,args.client,run_clients=args.run_clients)
        if args.sdk_python:result['sdk']=check_sdk(store,args.client,args.sdk_python)
        return result
    return install(args.source,args.target)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True));args=p.parse_args(argv)
    if args.onboarding_action=='install':
        try:print(canonical(install(args.source,args.target)));return 0
        except (Blocked,ValueError,OSError) as e:print(canonical({'status':'BLOCKED','reason':str(e)}));return 2
    s=Store(args.root)
    try:print(canonical(run(s,args)));return 0
    except (Blocked,ValueError,OSError,subprocess.SubprocessError) as e:print(canonical({'status':'BLOCKED','reason':str(e)}));return 2
    finally:s.db.close()

if __name__=='__main__':sys.exit(main())
