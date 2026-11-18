"""Invalidate copied execution authority while retaining history and disabled recipes."""
import json

def invalidate_database(db):
    rows=db.execute("SELECT key,value FROM meta").fetchall()
    recipes={k:json.loads(v) if v.startswith(('{','[')) else v for k,v in rows if k in ('runner:config','readiness','readiness_projects','team_config','context_clients')}
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('restore:recipes',json.dumps(recipes,ensure_ascii=False)))
    for key,value in rows:
        if key.startswith(('runner:','context_instruction:','bot-approval:','bot-connection:','bot-target:','approval-decision:','planner:','bot-requirement:','request-control:','request-amendment:','original-intent:','supervision-reauthorization:')) or key in ('readiness','readiness_projects','context_clients','context_source_files'):
            db.execute('DELETE FROM meta WHERE key=?',(key,))
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('automation:v1',json.dumps({'schema':1,'links':{},'events':{},'paused':True})))
    db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',('restore:needs_takeover','yes'))
    for row in db.execute("SELECT id,body FROM records WHERE kind='requirement'").fetchall():
        value=json.loads(row[1]);value['intent']=False;value['historical_intent_invalidated']=True
        db.execute('UPDATE records SET body=? WHERE id=?',(json.dumps(value,ensure_ascii=False),row[0]))
    db.execute('UPDATE approvals SET expires=0 WHERE consumed IS NULL')
    db.execute("UPDATE outbox SET state='historical-readonly' WHERE state NOT IN ('terminal','rejected')")

def require_local_runtime(store):
    from .core import Blocked
    if store.get('restore:needs_takeover') or store.get('execution') in ('copied-readonly','restored-readonly','handoff-blocked','recovery-paused'):
        raise Blocked('恢复记录只读；请重新映射本机路径并完成接管，旧运行配方未启用')

def reset_restored_tree(root,logical_root=None):
    from pathlib import Path
    import sqlite3,uuid
    from .core import host_identity,digest
    root=Path(root);db=root/'数据/state.sqlite'
    if not db.is_file():return False
    if db.is_symlink():raise ValueError('restored state cannot be a link')
    connection=sqlite3.connect(db)
    try:
        with connection:
            old=connection.execute("SELECT value FROM meta WHERE key='device'").fetchone()
            invalidate_database(connection)
            for key,value in {'history_device':old[0] if old else 'unknown','device':str(uuid.uuid4()),'execution':'restored-readonly','team_config':'{}','host_binding':digest((host_identity()+':'+str(Path(logical_root or root).absolute())).encode())}.items():
                connection.execute('INSERT OR REPLACE INTO meta VALUES (?,?)',(key,value))
    finally:connection.close()
    guard=root/'数据/runner.guard'
    if guard.is_file() and not guard.is_symlink():guard.unlink() # inert copied lock in this newly restored tree
    return True
