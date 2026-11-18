from .core import cooperative
import json
from pathlib import Path
import shutil
import subprocess
import uuid
from .core import Blocked, atomic, canonical, identifier

@cooperative
def discover(store,links,registry):
    # Read only project names and roots. Never import session payloads, secrets, or caller identities.
    mapping={}
    value=json.loads(Path(registry).read_text())
    # 登记名和开发链接名不一样时，由旧注册表自己的 aliases 写明（如 {"erp":"某某-erp"}）；发行包不写死任何人的项目名。
    aliases=value.get('aliases',{}) if isinstance(value.get('aliases',{}),dict) else {}
    for p in Path(links).iterdir():
        if p.is_symlink() and p.resolve().is_dir():
            mapping[p.name]=str(p.resolve())
    for key,entry in value.get('projects',{}).items():
        if key in aliases and aliases[key] in mapping:
            mapping[key]=mapping.pop(aliases[key])
        if key not in mapping:
            root=entry.get('root','')
            if root.startswith('workspace:'):
                # The registry alias can be stale after migration.
                # Registry path aliases are not guessed: require an actual resolved link.
                mapping[key]=None
    rows=[]
    for key,path in sorted(mapping.items()):
        if path:
            pid=store.register(key,path,identifier(key))
            rows.append({'id':pid,'path':path,'copied':False})
        else:
            rows.append({'id':key,'status':'BLOCKED','reason':'登记无可核实开发链接'})
    return rows

def probe():
    result={}
    for tool in ('git','python3','node','age','codex','claude'):
        binary=shutil.which(tool)
        result[tool]={'installed':bool(binary),'compatibility':'LIVE_UNVERIFIED'}
        if binary and tool in ('git','python3','node','age'):
            p=subprocess.run([binary,'--version'],capture_output=True,text=True,timeout=10)
            result[tool]['version']=p.stdout.strip()[:120]
    result['T3']={'default':False,'replaceable':True,'compatibility':'LIVE_UNVERIFIED'}
    return result

@cooperative
def configs(store,python,server):
    caller='portable:'+store.device
    config={'python':str(Path(python).absolute()),'server':str(Path(server).absolute()),'caller':caller}
    write_configs(store,config)
    store.put('team_config',config)
    return {'caller':caller,'configurations':['mcp.json','codex.toml','claude.mcp.json'],'compatibility':'LIVE_UNVERIFIED','execution_enabled':False,'portability':'EXTERNAL_DEPENDENCY: use bundled adapter + rebuild for carrying'}

@cooperative
def write_configs(store,config):
    args=config.get('server_args') or ['-I','-B',config['server'],'--transport','stdio','--caller',config['caller']]
    entry={'command':config['python'],'args':args}
    if config.get('environment'):entry['env']=config['environment']
    generic={'mcpServers':{'ai_team':entry}}
    out=store.root/'工具'
    atomic(out/'mcp.json',canonical(generic))
    toml='[mcp_servers.ai_team]\ncommand = '+json.dumps(config['python'])+'\nargs = '+json.dumps(args)+'\n'
    if config.get('environment'):
        toml+='[mcp_servers.ai_team.env]\n'+''.join(key+' = '+json.dumps(value)+'\n' for key,value in config['environment'].items())
    atomic(out/'codex.toml',toml)
    atomic(out/'claude.mcp.json',canonical(generic))

def encrypt(source,target,recipient=None):
    age=shutil.which('age')
    if not age:
        raise Blocked('未安装 age，不能导出敏感数据；普通包未包含它们')
    source,target=Path(source),Path(target)
    if target.exists() or source.is_dir() or (recipient is not None and not recipient.startswith('age1')):
        raise Blocked('age 需要新目标、单独敏感文件和有效 recipient 公钥')
    # Public recipient may be in argv; passphrase is never in argv or logs.
    if recipient is None:
        import sys
        if not sys.stdin.isatty():raise Blocked('密码加密须本人在TTY私密输入；不接受argv或管道密码')
        p=subprocess.run([age,'-p','-o',str(target),str(source)])
    else:
        p=subprocess.run([age,'-r',recipient,'-o',str(target),str(source)],capture_output=True)
    if p.returncode:
        raise Blocked('age 加密失败，未输出原始错误')
    return {'encrypted':str(target),'production_restore_authorized':False}

def migration_inventory(store):
    import platform
    def metadata(command):
        try:
            result=subprocess.run(command,capture_output=True,text=True,timeout=10)
            return result.stdout.splitlines() if result.returncode==0 else None
        except (OSError,subprocess.SubprocessError):
            return None
    processes=metadata(['ps','-axo','comm='])
    tunnels=sorted({Path(p.strip()).name for p in (processes or []) if Path(p.strip()).name in {'ssh','autossh','cloudflared','frpc','tailscaled','wireguard-go'}})
    labels=None
    if platform.system()=='Darwin':
        rows=metadata(['launchctl','list'])
        if rows is not None:labels=[row.split()[-1] for row in rows[1:] if row.split()]
    paths=[{'project':row['project'],'path':row['path'],'exists':Path(row['path']).exists()} for row in store.db.execute('SELECT project,path FROM paths WHERE device=?',(store.device,))]
    result={'schema':1,'device':store.device,'tools':probe(),'paths':paths,'service_labels':labels,'tunnel_process_names':tunnels,'process_inventory_available':processes is not None,'stopped_services_proven':False,'old_machine_inaccessible_test':'EXTERNAL_REQUIRED','secrets_read':False}
    store.record('migration_inventory',result)
    return result


def decrypt_sensitive(source,target):
    import sys
    age=shutil.which('age')
    if not age:raise Blocked('未安装age，无法解密')
    if not sys.stdin.isatty():raise Blocked('解密密码仅由本人在TTY输入，不接受argv或日志密码')
    target=Path(target)
    if target.exists() or target.is_symlink():raise Blocked('解密目标必须为新文件')
    p=subprocess.run([age,'-d','-o',str(target),str(source)])
    if p.returncode:raise Blocked('解密失败；未执行任何生产恢复')
    target.chmod(0o600)
    return {'decrypted':str(target),'production_restore_performed':False}

@cooperative
def project_choices(store,project=None):
    rows=[dict(row) for row in store.db.execute('SELECT p.id,p.name,x.path FROM projects p JOIN paths x ON x.project=p.id WHERE x.device=? ORDER BY p.name',(store.device,))]
    if project is not None:
        selected=next((r for r in rows if r['id']==project),None)
        if selected is None:raise Blocked('项目未登记')
        store.put('selected_project',project)
    else:selected=next((r for r in rows if r['id']==store.get('selected_project')),None)
    return {'projects':rows,'selected':selected,'next':['补齐MVP后明确要求实施','记录检查点或素材','查询战队真实任务状态'],'execution':store.get('execution'),'dispatch_ready':False}
