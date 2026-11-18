"""生成依赖目录（机器人 node_modules 等）的封存与备份排除。不在机器之间复制 venv。

自带战队后台已移除（见 DECISIONS 2026-11-18）。这里留下的：
- 机器人依赖用的 generated_manifest / seal_generated；
- 备份、携带、搬家时把已封存的生成依赖排除掉（旧版留下的 工具/runtimes、工具/hosts 没被 team_retire 挪走时也照样排除，不让备份失败）；
- validate_adapter_folder：team.py 校验旧版接入配置时还在用。
"""
import json
import os
from pathlib import Path
from .core import Blocked,atomic,canonical,digest,safe

def validate_adapter_folder(folder):
    folder=Path(folder)
    index=folder/'BUNDLE.json'
    if not index.is_file():raise Blocked('内置适配器缺少完整性清单')
    expected=json.loads(index.read_text())
    actual={p.relative_to(folder).as_posix() for p in folder.rglob('*') if p.is_file() or p.is_symlink()}
    if actual!=set(expected)|{'BUNDLE.json'}:raise Blocked('内置适配器存在未知文件或缓存，不能隐式加载')
    for rel,sha in expected.items():
        item=safe(folder,rel)
        if not item.is_file() or digest(item.read_bytes())!=sha:raise Blocked('内置适配器已变化，停止依赖重建/配置')

def environment_files(folder):
    result={}
    for name in ('venv','private-home'):
        base=folder/name
        if not base.exists():continue
        for parent,dirs,files in os.walk(base,followlinks=False):
            for child in dirs+files:
                p=Path(parent)/child;rel=p.relative_to(folder).as_posix()
                if p.is_symlink():result[rel]={'link':os.readlink(p)}
                elif p.is_file():result[rel]={'sha256':digest(p.read_bytes())}
                elif p.is_dir():result[rel]={'directory':True}
                else:raise Blocked('生成环境出现未知特殊文件')
    return result

def seal_environment(folder):
    values=environment_files(folder)
    raw=canonical(values);atomic(folder/'environment-files.json',raw)
    return digest(raw.encode())

def generated_manifest(folder,names):
    values={};folder=Path(folder)
    for name in names:
        base=folder/name
        if not base.exists():continue
        if base.is_symlink():raise Blocked('生成依赖目录不能是链接')
        for parent,dirs,files in os.walk(base,followlinks=False):
            for child in dirs+files:
                p=Path(parent)/child;rel=p.relative_to(folder).as_posix()
                if p.is_symlink():values[rel]={'link':os.readlink(p)}
                elif p.is_file():values[rel]={'sha256':digest(p.read_bytes())}
                elif p.is_dir():values[rel]={'directory':True}
                else:raise Blocked('生成依赖出现未知特殊文件')
    return values

def seal_generated(folder,names,state='ready'):
    folder=Path(folder);raw=json.dumps(generated_manifest(folder,names),sort_keys=True).encode()
    (folder/'environment-files.json').write_bytes(raw)
    value={'schema':'portable.generated/v1','state':state,'generated_dirs':names,'environment_sha256':digest(raw)}
    (folder/'install.json').write_text(json.dumps(value,sort_keys=True))
    return value

def generated_product_runtimes(root):
    root=Path(root);excluded={}
    folders=list((root/'工具/bot-runtimes').glob('*'))+list((root/'工具/hosts').glob('*/runtimes/*'))
    for folder in folders:
        if not folder.is_dir() or folder.is_symlink():raise Blocked('生成依赖目录存在未知类型')
        record=folder/'install.json';index=folder/'environment-files.json'
        if not record.is_file() or not index.is_file():raise Blocked('后端/机器人依赖尚未封存，禁止静默丢弃')
        value=json.loads(record.read_text());raw=index.read_bytes();names=value.get('generated_dirs')
        if value.get('schema')!='portable.generated/v1' or value.get('state') not in ('ready','failed') or names not in (['venv'],['node_modules']):raise Blocked('生成依赖配方未知或正在安装')
        allowed=set(names)|{'install.json','environment-files.json','package.json','package-lock.json'}
        if any(p.name not in allowed for p in folder.iterdir()):raise Blocked('生成目录有未知文件，保持原件')
        present=any((folder/name).exists() for name in names)
        if digest(raw)!=value.get('environment_sha256') or (present and json.loads(raw)!=generated_manifest(folder,names)):raise Blocked('生成依赖包含未知改动，未排除')
        for name in names:
            if (folder/name).exists():excluded[(folder/name).relative_to(root).as_posix()]='按包内锁与generation记录在新机重建；不携带旧SDK/缓存'
    return excluded


def generated_environment_dirs(root):
    """Omit only sealed, unchanged generated environments; preserve rebuild recipes and records."""
    runtimes=Path(root)/'工具/runtimes';excluded=generated_product_runtimes(root)
    if not runtimes.is_dir():return excluded
    marker=runtimes/'install.lock'
    if marker.is_file():
        import fcntl
        try:
            with marker.open('rb') as handle:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise Blocked('依赖仍在安装，不能备份或携带半环境')
    for folder in runtimes.iterdir():
        if not folder.is_dir() or folder.is_symlink():continue
        record=folder/'install.json';manifest=folder/'environment-files.json'
        if not record.is_file() or not manifest.is_file():raise Blocked('依赖安装中断或来源未知；先恢复安装，不能静默丢弃环境')
        value=json.loads(record.read_text());raw=manifest.read_bytes()
        # A backup intentionally omits both generated trees but retains their seal/recipe.
        present=any((folder/name).exists() for name in ('venv','private-home'))
        if digest(raw)!=value.get('environment_sha256') or (present and json.loads(raw)!=environment_files(folder)):
            raise Blocked('生成环境包含未知改动，未静默排除')
        if any(p.name not in ('venv','private-home','install.json','environment-files.json') for p in folder.iterdir()):raise Blocked('运行环境目录存在未知文件，未丢弃')
        for name in ('venv','private-home'):
            if (folder/name).exists():excluded[(folder/name).relative_to(root).as_posix()]='设备专用生成依赖，按包内锁文件重建；不复制venv/缓存'
    return excluded
