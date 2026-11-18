"""Fresh package-local official SDK generations; no global npm/user credential reads."""
import json,os,shutil,subprocess,tempfile,uuid
from pathlib import Path
from .core import Blocked,canonical,atomic,digest,host_identity
from .dependencies import generated_manifest,seal_generated


def ready(store):
    record=store.get('bot-runtime',{})
    if record.get('device')!=store.device or record.get('host')!=host_identity() or record.get('root')!=str(store.root):raise Blocked('机器人依赖需要在本机重建')
    folder=Path(record['folder'])
    if any(p.is_symlink() for p in (folder,*folder.parents)):raise Blocked('机器人依赖路径不能是链接')
    if not folder.is_relative_to(store.root/'工具/bot-runtimes'):raise Blocked('机器人依赖路径不属于本产品')
    receipt=json.loads((folder/'install.json').read_text());raw=(folder/'environment-files.json').read_bytes()
    if receipt.get('state')!='ready' or digest(raw)!=receipt.get('environment_sha256') or json.loads(raw)!=generated_manifest(folder,['node_modules']):raise Blocked('机器人依赖已变化，拒绝加载')
    if digest((store.root/'工具/adapters/bots/package-lock.json').read_bytes())!=record['lock']:raise Blocked('机器人依赖锁变化，先重建')
    return str(folder)


def rebuild(store):
    npm=shutil.which('npm')
    if not npm or not shutil.which('node'):raise Blocked('先安装本人选择的Node/npm；不做系统全局安装')
    source=store.root/'工具/adapters/bots';folder=store.root/'工具/bot-runtimes'/uuid.uuid4().hex;folder.mkdir(parents=True)
    for name in ('package.json','package-lock.json'):shutil.copy2(source/name,folder/name)
    atomic(folder/'install.json',canonical({'schema':'portable.generated/v1','state':'building','generated_dirs':['node_modules']}))
    with tempfile.TemporaryDirectory(prefix='portable-npm-') as tmp:
        env={'HOME':tmp,'PATH':os.environ.get('PATH','/usr/bin:/bin'),'NPM_CONFIG_USERCONFIG':os.devnull,'NPM_CONFIG_GLOBALCONFIG':os.devnull,'NPM_CONFIG_CACHE':tmp+'/cache','NPM_CONFIG_IGNORE_SCRIPTS':'true'}
        try:r=subprocess.run([npm,'ci','--ignore-scripts','--no-audit','--no-fund','--registry=https://registry.npmjs.org'],cwd=folder,env=env,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
        except (OSError,subprocess.SubprocessError):seal_generated(folder,['node_modules'],'failed');raise Blocked('机器人依赖重建未完成，旧环境保留') from None
    seal_generated(folder,['node_modules'],'ready' if r.returncode==0 else 'failed')
    if r.returncode:raise Blocked('机器人依赖安装失败；候选保留可诊断，不回退旧机路径')
    record={'folder':str(folder),'root':str(store.root),'device':store.device,'host':host_identity(),'lock':digest((source/'package-lock.json').read_bytes())}
    store.put('bot-runtime',record)
    return ready(store)
