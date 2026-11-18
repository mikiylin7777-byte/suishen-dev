"""SD-28 搬出/卸载：项目原样留在原地，AI 工具的规则文件恢复原状，随身开发整个移进归档（不删）。

默认只出方案不动手；要加 --confirm 卸载 才执行。
- 项目：从来没被搬动过，原地不动；项目里的 STATUS.md 等是项目自己的记录，留着。
- 规则文件：只删向导加的那两行；文件本来不存在、删完是空的，就把它移进归档。
- 后台：停写，卸下后台服务，服务定义移进归档。
- 随身开发目录：整个移到 ~/归档/<年-月>/，里面有怎么恢复的说明。
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from .core import Blocked, atomic
from . import wizard

def _plan(store,home):
    from .board import load
    from .supervisor import identity
    root=Path(store.root).absolute()
    archive=home/'归档'/time.strftime('%Y-%m')/('随身开发-卸载-'+time.strftime('%Y%m%d-%H%M%S'))
    lines=wizard.all_hook_lines(store);rules=[]
    known={h['file']:h for h in store.get(wizard.HOOKS,[])}
    for rel in wizard.TOOLS.values():
        f=home/rel
        if f.is_file() and not f.is_symlink():
            hit=[l for l in f.read_text(encoding='utf-8').splitlines() if l in lines]
            if hit:rules.append({'文件':str(f),'删掉这几行':hit,'向导新建的文件':bool(known.get(str(f),{}).get('created'))})
    plist=home/'Library/LaunchAgents'/(identity(root)+'.plist')
    projects=[{'名字':e['name'],'位置':e['path']} for e in load(store)]
    return {'项目（原地不动）':projects,'规则文件':rules,'后台服务':str(plist) if plist.exists() else None,'随身开发目录':str(root),'移到':str(archive)},archive,plist

def leave(store,confirm=False,home=None,launchctl=None):
    home=Path(home or wizard._home());plan,archive,plist=_plan(store,home)
    if not confirm:return {**plan,'已执行':False,'下一步':'用户确认后加 --confirm 卸载 执行；什么都不会被永久删除'}
    root=Path(store.root).absolute()
    if archive.exists():raise Blocked('归档位置已存在，换个时间再试：'+str(archive))
    archive.mkdir(parents=True)
    # 1 停后台
    from .automation import pause
    pause(store,by='leave',reason='搬出卸载')
    if plan['后台服务']:
        if launchctl is None and sys.platform=='darwin':
            subprocess.run(['/bin/launchctl','bootout','gui/'+str(os.getuid()),str(plist)],capture_output=True,timeout=15)
        elif launchctl:launchctl('bootout',plist)
        shutil.move(str(plist),str(archive/plist.name))
    # 2 规则文件恢复原状：只删我们加的行
    restored=[]
    for r in plan['规则文件']:
        f=Path(r['文件']);(archive/'规则文件原样').mkdir(exist_ok=True)
        shutil.copy2(f,archive/'规则文件原样'/(f.parent.name.lstrip('.')+'-'+f.name))
        rest=[l for l in f.read_text(encoding='utf-8').splitlines() if l not in r['删掉这几行']]
        if r['向导新建的文件'] and not ''.join(rest).strip():
            shutil.move(str(f),str(archive/'规则文件原样'/('空-'+f.parent.name.lstrip('.')+'-'+f.name)));restored.append(str(f)+'（向导新建，已移进归档）')
        else:
            atomic(f,'\n'.join(rest)+('\n' if rest else ''));restored.append(str(f))
    # 3 说明
    atomic(archive/'恢复方法.md','# 怎么恢复随身开发\n\n'
           f'1. 把本文件夹里的「{root.name}」移回 {root.parent}\n'
           '2. 对 AI 说「帮我重新接上随身开发」，AI 会跑 wizard 把规则行加回去；\n'
           '3. 后台服务的定义也在本文件夹里，AI 会帮你装回去。\n\n'
           '## 当时登记的项目（都在原地，没动过）\n\n'+''.join(f'- {p["名字"]}：{p["位置"]}\n' for p in plan['项目（原地不动）']))
    store.db.close()
    # 4 最后才搬走自己（之后不再读包里的任何文件）
    shutil.move(str(root),str(archive/root.name))
    return {'已执行':True,'项目':'全部原地不动，共 '+str(len(plan['项目（原地不动）']))+' 个','恢复了的规则文件':restored,
            '后台':'已停并卸下' if plan['后台服务'] else '没装过','随身开发移到':str(archive/root.name),'恢复方法':str(archive/'恢复方法.md'),'永久删除':False}

def run(args):
    from .core import Store
    s=Store(args.root)
    confirm=args.confirm=='卸载'
    if args.confirm and not confirm:raise Blocked('确认词是「卸载」')
    try:return leave(s,confirm)
    finally:
        try:s.db.close()
        except Exception:pass

def register(subparser):
    p=subparser.add_parser('leave',help='搬出/卸载：先看方案；用户同意后 --confirm 卸载 执行（只移不删）')
    p.add_argument('--confirm');p.set_defaults(leave_run=run);return p
