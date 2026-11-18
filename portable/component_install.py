"""Explicit local persistent service installation; never restore old approval."""
import json
import os
from pathlib import Path
import platform
import plistlib
import subprocess
import sys
import uuid
from . import components as c
from .core import Blocked,canonical,digest,atomic


def install(store,generation,component,stage,scope,*,launchdir=None,confirm=None,launchctl=None):
    if confirm is None and not sys.stdin.isatty():raise Blocked('持久服务安装须本人本机终端明确批准')
    if scope not in ('service','external_connection'):raise Blocked('必须准确指定启用范围')
    hook=c.activation_hook_for_store(store,task='component-'+uuid.uuid4().hex,owner='local-uid-'+str(os.getuid()))
    proposal=c.prepare_service_activation(generation,component,stage,device=store.device,scopes=[scope],hook=hook)
    row,definition,receipt=c._validate_service_stage(Path(generation),component,Path(stage).resolve(),store.device)
    if scope=='external_connection' and not receipt.get('ssh_identity'):raise Blocked('SSH必须重新绑定本机身份与known_hosts')
    directory=Path(launchdir or Path.home()/'Library/LaunchAgents').absolute()
    for parent in (directory,*directory.parents):
        if parent.is_symlink():raise Blocked('持久安装目录包含链接')
    target=directory/(definition['Label']+'.plist')
    active=dict(definition,Disabled=False,RunAtLoad=True)
    blob=plistlib.dumps(active,sort_keys=True)
    binding={'task':'install-'+uuid.uuid4().hex,'action':'install_component_service','target':str(target),'scope':'persistent_install,'+scope,'version':digest(canonical({'proposal':proposal,'definition':digest(blob),'host':platform.node(),'uid':os.getuid()}).encode()),'owner':'local-uid-'+str(os.getuid())}
    display={'device':store.device,'component':component,'component_hash':row['hash'],'label':definition['Label'],'target':str(target),'scope':binding['scope'],'definition_sha256':digest(blob),'effect':'当前登录启用，并在以后登录加载；健康及重启仍需实测'}
    if confirm is None:
        print(canonical(display));approved=input('批准此新机目标的持久安装和准确范围，输入“批准持久安装”：').strip()=='批准持久安装'
    else:approved=confirm(display) is True # trusted local test/UI callback; never imported from JSON
    if not approved:return {'installed':False}
    token=store.approval(binding);store.consume(token,binding,binding['owner'])
    # Revalidate exact bytes and new identity immediately before exclusive write.
    current=c._validate_service_stage(Path(generation),component,Path(stage).resolve(),store.device)
    if current!=(row,definition,receipt):raise Blocked('批准后服务定义或绑定变化')
    directory.mkdir(parents=True,exist_ok=True)
    fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'wb') as stream:stream.write(blob);stream.flush();os.fsync(stream.fileno())
    if target.is_symlink() or target.read_bytes()!=blob:raise Blocked('持久定义读回失败，保留现场')
    evidence={'device':store.device,'root':str(store.root),'generation':str(Path(generation).resolve()),'stage':str(Path(stage).resolve()),'component':component,'component_hash':row['hash'],'target':str(target),'definition_sha256':digest(blob),'label':definition['Label'],'scope':scope,'state':'persistent_definition_installed','health':'not_verified','system_boot_verified':False}
    key='components:installation:'+component;store.put(key,evidence)
    if launchctl is None:
        r=subprocess.run(['/bin/launchctl','bootstrap','gui/'+str(os.getuid()),str(target)],capture_output=True,timeout=30,env={'PATH':'/usr/bin:/bin'})
        if r.returncode:raise Blocked('持久定义已保留但bootstrap未确认；请检查状态后重新批准，不自动覆盖或重试')
    else:launchctl('gui/'+str(os.getuid()),target)
    evidence['state']='persistent_bootstrap_requested';store.put(key,evidence)
    return evidence


def required_status(store):
    from .component_cli import local
    value=local(store);required=value.get('required')
    if required is None:return {'complete':False,'missing':['尚未明确本次迁移所需组件；components require登记，空列表须明确选择'],'components':[]}
    rows=[]
    for need in required:
        generation=need['generation'];cid=need['id']
        try:
            status=next(r for r in c.status(generation,rebuilt_prefixes=value['prefixes'].get(generation,{})) if r['id']==cid)
            if status['hash']!=need['hash']:raise Blocked('组件hash变化')
            ready=status['state']=='code_ready' and status.get('login')!='needs_login'
            if status.get('login')=='needs_login':
                prefix=value['prefixes'].get(generation,{}).get(cid)
                if prefix:
                    try:login=check_login(store,generation,cid,prefix)
                    except (OSError,ValueError,Blocked,subprocess.SubprocessError):login={}
                else:login={}
                ready=status['state']=='code_ready' and login.get('authenticated') is True
                status={**status,'login':'local_cli_authenticated' if ready else 'needs_login','state':status['state'] if ready else 'needs_login'}
            if status['state']=='pending_activation':
                receipt=store.get('components:installation:'+cid,{})
                path=Path(receipt.get('target','/nonexistent'))
                ready=receipt.get('device')==store.device and receipt.get('root')==str(store.root) and receipt.get('component_hash')==need['hash'] and receipt.get('state')=='persistent_bootstrap_requested' and path.is_file() and not path.is_symlink() and digest(path.read_bytes())==receipt.get('definition_sha256')
                status={**status,'persistent_installation':ready,'health':receipt.get('health','not_verified')}
                # An installed plist alone is not a healthy restored service.
                ready=bool(ready and receipt.get('health')=='verified')
            rows.append({**status,'ready':ready})
        except (OSError,ValueError,Blocked,StopIteration):rows.append({'id':cid,'state':'needs_rebuild','ready':False})
    return {'complete':all(r['ready'] for r in rows),'components':rows,'missing':[r['id']+':'+r['state'] for r in rows if not r['ready']]}


def probe(store,component,*,read_status=None,http_health=None,connect=None):
    """Fixed read-only probes for an explicitly approved, unchanged new service."""
    import re,socket,urllib.request
    key='components:installation:'+component;receipt=store.get(key,{})
    if receipt.get('device')!=store.device or receipt.get('root')!=str(store.root) or receipt.get('state')!='persistent_bootstrap_requested':raise Blocked('没有当前设备已批准的服务安装')
    target=Path(receipt['target'])
    if target.is_symlink() or digest(target.read_bytes())!=receipt['definition_sha256']:raise Blocked('持久定义变化，旧批准失效')
    row,definition,staged=c._validate_service_stage(Path(receipt['generation']),component,Path(receipt['stage']),store.device)
    if row['hash']!=receipt['component_hash']:raise Blocked('组件代码或绑定变化')
    label='gui/'+str(os.getuid())+'/'+receipt['label']
    if read_status is None:
        r=subprocess.run(['/bin/launchctl','print',label],capture_output=True,text=True,timeout=10,env={'PATH':'/usr/bin:/bin'})
        if r.returncode:raise Blocked('当前登录服务尚未加载')
        text=r.stdout
    else:text=read_status(label)
    argv=definition['ProgramArguments'];proof=None
    if '--version' in argv:
        if re.search(r'last exit code\s*=\s*0\b',text):proof='owned_native_version_job_exit0'
    elif re.search(r'state\s*=\s*running\b',text):
        if 'serve' in argv and '--hostname' in argv and argv[argv.index('--hostname')+1]=='127.0.0.1':
            port=int(argv[argv.index('--port')+1]);url='http://127.0.0.1:'+str(port)+'/global/health'
            if http_health is None:
                opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(url,timeout=3) as response:value=json.loads(response.read(4096))
            else:value=http_health(url)
            if value.get('healthy') is True and isinstance(value.get('version'),str):proof='opencode_official_global_health'
        elif argv[0]=='/usr/bin/ssh' and '-L' in argv:
            forward=argv[argv.index('-L')+1];parts=forward.split(':');port=int(parts[1] if parts[0]=='127.0.0.1' else parts[0])
            if connect is None:
                with socket.create_connection(('127.0.0.1',port),timeout=3):pass
            else:connect(('127.0.0.1',port))
            proof='owned_ssh_local_forward_listening_remote_application_not_tested'
    if not proof:raise Blocked('此服务没有通过对应固定健康探针；不以bootstrap代替健康')
    receipt.update(health='verified',health_proof=proof);store.put(key,receipt);return receipt


def check_login(store,generation,component,prefix,*,runner=None):
    """Explicit metadata-only local CLI authentication probe; never copy credentials."""
    import re
    row=next((r for r in c._catalog(Path(generation)) if r['id']==component),None)
    if not row or row['adapter'] not in c.NATIVE_ADAPTERS:raise Blocked('此应用无可核实的本机登录探针；不能推测登录成功')
    entry=c._validate_native_prefix(Path(generation),row,Path(prefix))
    commands={'codex-native':['login','status'],'claude-native':['auth','status','--json'],'opencode-native':['auth','list']}
    if row['adapter'] not in commands:raise Blocked('此工具登录状态接口未适配')
    args=[str(entry),*commands[row['adapter']]]
    if runner is None:
        env={**os.environ,'DISABLE_AUTOUPDATER':'1','DISABLE_TELEMETRY':'1','DISABLE_ERROR_REPORTING':'1'}
        result=subprocess.run(args,capture_output=True,text=True,stdin=subprocess.DEVNULL,timeout=15,env=env)
        code=result.returncode;text=result.stdout+'\n'+result.stderr
    else:code,text=runner(args) # trusted fixture integration only
    authenticated=False
    if code==0:
        if row['adapter']=='claude-native':
            try:authenticated=json.loads(text).get('loggedIn') is True
            except ValueError:pass
        elif row['adapter']=='codex-native':authenticated=bool(re.search(r'(?i)logged in',text)) and not bool(re.search(r'(?i)not logged in',text))
        else:
            clean=re.sub(r'\x1b\[[0-9;]*[A-Za-z]','',text);count=re.search(r'\b([0-9]+) credentials?\b',clean,re.I);authenticated=bool(count and int(count[1])>0)
    value={'device':store.device,'root':str(store.root),'component':component,'component_hash':row['hash'],'prefix':str(Path(prefix).resolve()),'authenticated':authenticated,'source':'local_cli_auth_status','credentials_copied':False,'model_called':False}
    store.put('components:login:'+component,value);return value
