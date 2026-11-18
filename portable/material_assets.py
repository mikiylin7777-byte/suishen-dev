"""Portable originals outside the vault; private quarantine and reversible retention."""
import os
import time
from .core import Blocked, atomic, canonical, cooperative, digest, safe
from .privacy import redact_text

DAY=86400

def load(store,asset):
    value=store.get('material_asset:'+asset)
    if not value:raise Blocked('素材原件不存在')
    return value

@cooperative
def preserve(store,raw,source,projects=(),favorite=False,*,private=False,now=None,kind='file'):
    if len(raw)>4*1024*1024:raise Blocked('原件超出4 MiB保存边界')
    asset=digest(raw);previous=store.get('material_asset:'+asset)
    value=previous or {'id':asset,'created':time.time() if now is None else now,'sources':[],'projects':[],'favorite':False,'private':private,'state':'active','kind':kind,'complete_selected_bytes':True,'encrypted':False}
    value['last_selected']=time.time() if now is None else now
    value['state']='active'
    value['private']=value['private'] or private
    value['sources']=sorted(set(value['sources']+[redact_text(source)]))
    value['projects']=sorted(set(value['projects']+list(projects)));value['favorite']=value['favorite'] or favorite
    # Strongest retention wins for content shared by several readings.
    if value['projects'] or value['favorite']:value['state']='active'
    folder='隔离' if value['private'] else '内容'
    rel='数据/素材原件/'+folder+'/'+asset
    old=safe(store.root,previous['path']) if previous else None
    path=safe(store.root,rel)
    if old and old.exists() and digest(old.read_bytes())!=asset:raise Blocked('原件摘要不符，保留现场')
    for directory in ('数据/素材原件','数据/素材原件/'+folder):
        d=safe(store.root,directory);d.mkdir(parents=True,exist_ok=True);d.chmod(0o700)
    if not path.exists():atomic(path,raw)
    if digest(path.read_bytes())!=asset:raise Blocked('内容寻址原件冲突')
    path.chmod(0o600)
    # A previous public copy must not remain public after reclassification.
    if old and old!=path and old.exists():
        old.chmod(0o600)
        value['previous_copies']=sorted(set(value.get('previous_copies',[])+[previous['path']]))
    value['path']=rel;value['retention']='long_term' if value['projects'] or value['favorite'] else 'temporary_30_plus_30'
    store.put('material_asset:'+asset,value)
    return value

@cooperative
def associate(store,asset,project=None,favorite=None):
    value=load(store,asset)
    if project:
        if not store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('项目未登记')
        value['projects']=sorted(set(value['projects']+[project]))
    if favorite is not None:value['favorite']=bool(favorite)
    path=safe(store.root,value['path']);raw=path.read_bytes()
    if digest(raw)!=asset:raise Blocked('原件摘要不符')
    return preserve(store,raw,value['sources'][0],value['projects'],value['favorite'],private=value['private'],now=value['created'],kind=value['kind'])

def refresh_links(store,old,new):
    import os
    folder=store.root/'知识库/素材'
    old_link='[携带原件]('+os.path.relpath(store.root/old,folder)+')'
    new_link='[携带原件]('+os.path.relpath(store.root/new,folder)+')'
    for p in folder.glob('*.md'):
        safe(store.root,p.relative_to(store.root));text=p.read_text()
        if old_link in text:atomic(p,text.replace(old_link,new_link))

@cooperative
def retention(store,now=None):
    now=time.time() if now is None else now;rows=[]
    for row in store.db.execute("SELECT key,value FROM meta WHERE key LIKE 'material_asset:%'").fetchall():
        import json
        value=json.loads(row['value'])
        if value['retention']=='long_term':continue
        if value['state']=='active' and now-value.get('last_selected',value['created'])>=30*DAY:
            old=safe(store.root,value['path']);target='数据/素材原件/恢复区/'+value['id']
            path=safe(store.root,target);path.parent.mkdir(parents=True,exist_ok=True);path.parent.chmod(0o700)
            if not old.is_file() or digest(old.read_bytes())!=value['id']:raise Blocked('恢复区移动前原件校验失败')
            if path.exists() and digest(path.read_bytes())!=value['id']:raise Blocked('恢复区存在冲突')
            os.replace(old,path);path.chmod(0o600);refresh_links(store,value['path'],target);value.update(path=target,state='recovery',recovery_at=now)
        value['owner_delete_eligible']=value['state']=='recovery' and now-value.get('recovery_at',now)>=30*DAY
        store.put(row['key'],value);rows.append(value)
    return {'assets':rows,'permanently_deleted':False}

@cooperative
def restore(store,asset,now=None):
    value=load(store,asset);path=safe(store.root,value['path'])
    if not path.is_file() or digest(path.read_bytes())!=asset:raise Blocked('恢复原件校验失败')
    folder='隔离' if value['private'] else '内容';rel='数据/素材原件/'+folder+'/'+asset;target=safe(store.root,rel)
    target.parent.mkdir(parents=True,exist_ok=True);target.parent.chmod(0o700)
    if target.exists() and digest(target.read_bytes())!=asset:raise Blocked('恢复目标冲突')
    if path!=target:
        os.replace(path,target);refresh_links(store,value['path'],rel)
    value.update(path=rel,state='active',created=time.time() if now is None else now,last_selected=time.time() if now is None else now,owner_delete_eligible=False)
    store.put('material_asset:'+asset,value)
    return value


def original_path(store,asset,*,local_private=False):
    value=load(store,asset)
    if value['private'] and not local_private:raise Blocked('隔离原件不得向模型输出；等待本人本机加密/私密处理')
    path=safe(store.root,value['path'])
    if not path.is_file() or digest(path.read_bytes())!=asset:raise Blocked('原件缺失或损坏')
    return path
