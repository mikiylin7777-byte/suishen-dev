"""停用随身开发自带的战队后台（见 DECISIONS 2026-11-17）。

真战队（用户自己的派工入口）不受影响；这里只把安装目录里自带的那一层移进 ~/归档，只移不删。
升级换完程序后调用；中途出错原样留着，升级照常完成，结果里写一句。
"""
import json
import os
import shutil
import time
import uuid
from pathlib import Path
from .core import Blocked

PATHS=('工具/team-host-release','工具/adapters/team-server','工具/hosts','工具/runtimes','数据/team-mcp','数据/team-mcp-config.json')
CONFIGS=('工具/mcp.json','工具/codex.toml','工具/claude.mcp.json')
CLOSED=('terminal','rejected','superseded','historical-readonly')

def _own_team(root,cfg):
    """用户接的是自己的战队（服务程序在安装目录外面）：连接和接口配置都是他的，不动（见 DECISIONS 2026-11-18）。"""
    server=cfg.get('server')
    return bool(server) and not Path(server).absolute().is_relative_to(Path(root).absolute())

def _leftovers(root,own=False):
    found=[rel for rel in PATHS if (root/rel).exists() or (root/rel).is_symlink()]
    for rel in ([] if own else CONFIGS):
        p=root/rel
        # 只挪自带战队生成的接口配置；里面还有别的（比如共享上下文）就不动
        if p.is_file() and 'ai_team' in (text:=p.read_text(encoding='utf-8',errors='replace')) and 'portable_context' not in text:found.append(rel)
    return found

def _open_rows(store):
    rows=store.db.execute('SELECT id,state,reference FROM outbox').fetchall()
    return [r['id'] for r in rows if r['state'] not in CLOSED and not (r['state']=='withdrawn' and not r['reference'])]

def _move(src,dest):
    dest.parent.mkdir(parents=True,exist_ok=True)
    try:
        os.rename(src,dest);return
    except OSError as e:
        if e.errno!=18:raise  # 18=跨盘
    if src.is_dir() and not src.is_symlink():
        shutil.copytree(src,dest,symlinks=True)
        a=sorted(p.relative_to(src).as_posix() for p in src.rglob('*'));b=sorted(p.relative_to(dest).as_posix() for p in dest.rglob('*'))
        if a!=b:raise Blocked('复制到归档后文件对不上：'+str(src))
        shutil.rmtree(src)
    else:
        shutil.copy2(src,dest,follow_symlinks=False)
        if not src.is_symlink() and src.read_bytes()!=dest.read_bytes():raise Blocked('复制到归档后内容对不上：'+str(src))
        src.unlink()

def _shipped(root):
    """这一版发行清单里还带着自带后台（比如退回到 0.2.51）：它要用，不动。"""
    try:files=json.loads((root/'发行.json').read_text(encoding='utf-8')).get('files',{})
    except (OSError,ValueError):return False
    return any(f.startswith(('工具/team-host-release/','工具/adapters/team-server/')) for f in files)

def retire(store,archive_home=None):
    root=Path(store.root);cfg=store.get('team_config',{}) or {}
    if _shipped(root):return {'状态':'这一版自带，不动','移走':[]}
    own=_own_team(root,cfg)
    left=_leftovers(root,own)
    if not left and (own or not cfg):return {'状态':'没有自带战队后台','移走':[],**({'保留':'你自己的战队连接'} if own else {})}
    blocked=[] if own else _open_rows(store)
    if blocked:return {'状态':'没停用','原因':'自带后台还有 '+str(len(blocked))+' 条派工记录没结束，等用户定','移走':[]}
    home=Path(archive_home) if archive_home else Path(os.environ.get('PORTABLE_HOME') or Path.home())
    month=time.strftime('%Y-%m');stamp=time.strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    target=home/'归档'/month/('随身开发-自带战队后台-'+stamp)
    moved=[];failed=[]
    for rel in left:
        try:_move(root/rel,target/rel);moved.append(rel)
        except Exception as e:failed.append(rel+'：'+str(e))
    if failed:return {'状态':'只移走了一部分','归档':str(target),'移走':moved,'没移走':failed}
    if not own:
        with store.db:
            store.db.execute("UPDATE outbox SET state='historical-readonly' WHERE state NOT IN ('historical-readonly')")
        if cfg:store.put('team_config:retired',{'at':time.time(),'archive':str(target),'config':cfg})
        store.put('team_config',{})
    (target).mkdir(parents=True,exist_ok=True)
    (target/'说明.txt').write_text('随身开发自带的战队后台，2026-11-17 按用户决定停用，从安装目录移到这里，只移不删。\n原位置：'+str(root)+'\n派工记录在 数据/team-mcp/ 下。\n',encoding='utf-8')
    return {'状态':'已停用','归档':str(target),'移走':moved,**({'保留':'你自己的战队连接'} if own else {})}
