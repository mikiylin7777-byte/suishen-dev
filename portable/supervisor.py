"""Foreground-first runner and strictly identified per-user launchd service."""
import argparse
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import plistlib
import signal
import sqlite3
import tempfile
import subprocess
import sys
import time
from .core import Blocked, Store, atomic, canonical, cooperative, digest, safe, identifier
from .automation import Reconciler, pause, state
from .distribution import backup
from .migration import inventory, medium, normalized_worktree
from .team import transport


def identity(root):
    return 'local.portable.runner.'+digest(str(Path(root).absolute()).encode())[:20]

def service(root, directory, action, execute=False, *, python=None, preserve_existing=False):
    """Only operates on exact generated plist; no system/global service operations."""
    root=Path(root).absolute();directory=Path(directory).absolute()
    for p in (root,directory,*directory.parents):
        if p.is_symlink():raise Blocked('服务路径不能是链接')
    label=identity(root);path=directory/(label+'.plist')
    runtime=root/'工具/portable.pyz'
    bootstrap="import runpy,sys;sys.path.insert(0,sys.argv.pop(1));runpy.run_module('portable.supervisor',run_name='__main__')"
    interpreter=Path(python or sys.executable).absolute()
    if not interpreter.is_file() or not os.access(interpreter,os.X_OK):raise Blocked('后台 Python 解释器不存在或不可执行')
    python=[str(interpreter)]
    default=[*python,'-I','-B','-c',bootstrap,str(runtime if runtime.is_file() else Path(__file__).absolute().parent.parent),'--root',str(root),'runner','run']
    command=default
    expected={'Label':label,'ProgramArguments':command,'WorkingDirectory':str(root),
        'StandardOutPath':str(root/'数据/后台日志/runner.log'),'StandardErrorPath':str(root/'数据/后台日志/runner.log'),
        'RunAtLoad':True,'KeepAlive':{'SuccessfulExit':False},'ThrottleInterval':30,
        'PortableOwner':{'schema':1,'root':str(root),'uid':os.getuid()}}
    if path.is_symlink():raise Blocked('服务标识为链接')
    old=None
    if path.exists():
        try:
            old=plistlib.loads(path.read_bytes())
            owner=old.get('PortableOwner',{})
            if not isinstance(owner,dict):raise ValueError()
        except Exception:raise Blocked('不是本产品生成的服务，拒绝管理')
        if path.stat().st_uid!=os.getuid() or old.get('Label')!=label or owner.get('root')!=str(root) or owner.get('uid')!=os.getuid():raise Blocked('不是本产品生成的服务，拒绝管理')
    elif action!='install':return {'installed':False,'label':label}
    if execute and (sys.platform!='darwin' or directory!=Path.home()/'Library/LaunchAgents'):
        raise Blocked('仅可管理当前Mac用户LaunchAgents')
    def ctl(*args):
        result=subprocess.run(['/bin/launchctl',*args],capture_output=True,timeout=15)
        return result.returncode
    target='gui/'+str(os.getuid())
    if action=='install':
        if preserve_existing and old is not None:expected={**old,'ProgramArguments':command}
        (root/'数据/后台日志').mkdir(parents=True,exist_ok=True)
        directory.mkdir(parents=True,exist_ok=True)
        atomic(path,plistlib.dumps(expected));os.chmod(path,0o600)
        if execute:
            result=ctl('kickstart',target+'/'+label) if ctl('print',target+'/'+label)==0 else ctl('bootstrap',target,str(path))
            if result!=0:raise Blocked('本产品服务加载未确认，保留plist供诊断')
    elif action=='uninstall':
        # Requires runner quiescence before unloading. Never kill another service.
        store=Store(root)
        try:
            if not state(store)['paused'] or not store.get('runner:stopped'):raise Blocked('先停写并等待运行器确认')
        finally:store.db.close()
        if execute and ctl('bootout',target,str(path))!=0:raise Blocked('卸载未确认，保留服务定义')
        disabled=path.with_suffix('.plist.disabled')
        if disabled.exists() or disabled.is_symlink():raise Blocked('旧禁用定义已存在，保留双方')
        path.rename(disabled)
    running=None
    if execute and action!='uninstall':running=ctl('print',target+'/'+label)==0
    return {'installed':path.exists(),'running':running,'label':label,'definition':str(path),'real_launchd':execute}

def project_inventory(path):
    source=Path(path).resolve(strict=True)
    if (source/'.git').exists():
        from .git_portable import fingerprint
        return {'complete_git_source':fingerprint(source)}
    return inventory(source)


@cooperative
def incremental(store, source, media_path, project):
    """Immutable content-addressed objects and full manifests; never drops unknown assets."""
    project=identifier(project)
    source=Path(source).resolve(strict=True)
    original_media=Path(media_path).absolute()
    if source.is_relative_to(original_media) or original_media.is_relative_to(source):raise Blocked('介质与源不能相互包含')
    if (source/'.git').is_file():
        with normalized_worktree(source) as candidate:return incremental(store,candidate,media_path,project)
    media,mid=medium(store,media_path)
    if source.is_relative_to(media) or media.is_relative_to(source):raise Blocked('介质与源不能相互包含')
    # Existing migration scanner rejects secrets, unknown ignored assets and special Git layouts.
    files=inventory(source)
    base=media/'portable-incremental'/store.device/project
    for p in (base,*base.parents):
        if p.is_symlink():raise Blocked('增量目录不能为链接')
    manifest={'schema':'portable.incremental/v1','device':store.device,'project':project,'medium':mid,'files':{k:dict(v) for k,v in files.items()},
              'dirs':sorted(p.relative_to(source).as_posix() for p in source.rglob('*') if p.is_dir())}
    written=0
    for rel,meta in files.items():
        if meta['sqlite']:
            with tempfile.TemporaryDirectory(prefix='portable-sqlite-') as tmp:
                dest=Path(tmp)/'consistent.sqlite'
                a=sqlite3.connect(safe(source,rel).as_uri()+'?mode=ro',uri=True);b=sqlite3.connect(dest)
                try:
                    a.backup(b)
                    if b.execute('PRAGMA integrity_check').fetchone()[0]!='ok':raise Blocked('数据库备份损坏')
                finally:b.close();a.close()
                data=dest.read_bytes()
            meta={**meta,'sha256':digest(data),'size':len(data)}
            manifest['files'][rel]=meta
        else:
            data=safe(source,rel).read_bytes()
            if digest(data)!=meta['sha256']:raise Blocked('源正在变化，未发布')
        from .backup_git import suspicious
        if suspicious(data):raise Blocked('增量对象疑似包含秘密，未发布')
        obj=safe(base,'objects/'+meta['sha256'])
        if obj.exists():
            if digest(obj.read_bytes())!=meta['sha256']:raise Blocked('介质对象损坏')
        else:atomic(obj,data);written+=1
    if inventory(source)!=files:raise Blocked('源正在变化，未发布')
    key=digest(canonical(manifest).encode())
    atomic(safe(base,'manifests/'+key+'.json'),canonical(manifest))
    atomic(safe(base,'LATEST'),key)
    return {'manifest':str(base/'manifests'/(key+'.json')),'written_objects':written}

def restore_incremental(manifest_path, target):
    manifest_path=Path(manifest_path);base=manifest_path.parent.parent
    value=json.loads(manifest_path.read_text());target=Path(target).absolute()
    if value.get('schema')!='portable.incremental/v1' or digest(canonical(value).encode())!=manifest_path.stem:raise Blocked('清单损坏')
    if target.exists() or target.is_symlink():raise Blocked('恢复目标必须不存在')
    checked={}
    for rel,meta in value['files'].items():
        if not isinstance(meta.get('sha256'),str) or len(meta['sha256'])!=64 or any(c not in '0123456789abcdef' for c in meta['sha256']):raise Blocked('对象摘要非法')
        data=safe(base,'objects/'+meta['sha256']).read_bytes()
        if digest(data)!=meta['sha256']:raise Blocked('对象损坏')
        safe(target,rel);checked[rel]=data
    for rel in value['dirs']:safe(target,rel)
    target.mkdir(parents=True)
    for rel in value['dirs']:safe(target,rel).mkdir(parents=True,exist_ok=True)
    for rel,data in checked.items():
        p=safe(target,rel);atomic(p,data);os.chmod(p,value['files'][rel]['mode'])
    from .runtime_identity import reset_restored_tree
    reset_restored_tree(target)
    return {'restored':str(target),'files':len(checked),'old_runtime_authority_restored':False}

@cooperative
def sync_project_set(store,project,source,media_path,dependencies=None):
    from .portability import create
    from .transfer import pack
    media,mid=medium(store,media_path)
    if Path(source).resolve().is_relative_to(media) or media.is_relative_to(Path(source).resolve()):raise Blocked('介质与源不能相互包含')
    with tempfile.TemporaryDirectory(prefix='portable-project-sync-') as tmp:
        package=Path(tmp).resolve()/'set'
        create([{'id':project,'source':str(source),'dependencies':dependencies or []}],package)
        destination=media/'portable-sets'/store.device/project
        destination.mkdir(parents=True,exist_ok=True)
        identifier=digest((package/'manifest.json').read_bytes())
        target=destination/(identifier+'.portable')
        if target.exists():
            from .sealed_bundle import inspect
            inspect(target);return {'container':str(target),'existing':True,'medium':mid}
        return {**pack(package,target),'medium':mid,'full_project_set':True}


def resume_legacy_shutdown_pause(store,now):
    """旧版本收到关机信号会把后台写成暂停、且不记原因；开机看到这种「没写原因的暂停」就自动恢复并记一笔。
    有人明确暂停（新版会写明谁、为什么）的不动。"""
    if not state(store)['paused'] or (store.get('runner:pause-reason') or {}).get('by'):return False
    stopped=store.get('runner:stopped') or {}
    from .automation import pause
    pause(store,False)
    store.put('runner:auto-resumed',{'at':now,'paused_since':stopped.get('at'),'reason':'开机发现旧版关机留下的暂停（没写谁停的），已自动恢复'})
    return True


UPGRADE_PAUSE_LIMIT=1800

def _upgrade_running():
    try:return subprocess.run(['pgrep','-f','portable.pyz.* upgrade (apply|rollback)'],capture_output=True,timeout=10).returncode==0
    except (OSError,subprocess.SubprocessError):return True  # 查不清就当还在升级，不擅自恢复


def resume_stale_upgrade_pause(store,now):
    """升级是程序自己发起的短暂停：暂停超过 30 分钟、又没有升级进程在跑（多半是升级中途关机），就自动恢复并记一笔。人明确停的不动。"""
    why=store.get('runner:pause-reason') or {}
    if not state(store)['paused'] or why.get('by') not in ('upgrade','update') or now-(why.get('at') or now)<UPGRADE_PAUSE_LIMIT or _upgrade_running():return False
    from .automation import pause
    pause(store,False)
    store.put('runner:auto-resumed',{'at':now,'paused_since':why.get('at'),'reason':'升级停写超过 30 分钟且没有升级在跑（多半升级中途关机），已自动恢复'})
    return True


class Runner:
    def __init__(self,store,call=None,clock=time.time):self.store=store;self.call=call;self.clock=clock

    @cooperative
    def tick(self):
        from .runtime_identity import require_local_runtime
        require_local_runtime(self.store)
        s=self.store;now=self.clock();cfg=s.get('runner:config',{})
        if state(s)['paused'] and resume_stale_upgrade_pause(s,now):pass
        if state(s)['paused']:
            if not s.get('runner:stopped'):s.put('runner:stopped',{'at':now})
            return {'paused':True}
        s.put('runner:stopped',{})
        result={}
        # All team reconciliation comes first on each wake/reconnect; no upgrade/cutover here.
        task_clock=s.get('runner:tasks',{})
        due=task_clock.get('next',0)
        pending_plans=any(__import__('json').loads(r[0]).get('intent') for r in s.db.execute("SELECT body FROM records WHERE kind='requirement'"))
        pending_controls=any(json.loads(r[0]).get('state')=='pending' for r in s.db.execute("SELECT value FROM meta WHERE key LIKE 'request-control:%'"))
        if (pending_controls or pending_plans or any(not item['stopped'] for item in state(s)['links'].values())) and (now>=due or due-now>300):
            if pending_plans:
                try:
                    from .planner import Planner
                    result['planning']=Planner(s,self.call).tick()
                except Exception:result['planning']={'unavailable':True}
            try:result['tasks']=Reconciler(s,self.call or transport(s.get('team_config',{}))).tick()
            except Exception:result['tasks']={'unavailable':True}
            failed=result['tasks'].get('unavailable') or result['tasks'].get('uncertain')
            failures=min(task_clock.get('failures',0)+1,4) if failed else 0
            s.put('runner:tasks',{'next':now+min(300,30*(2**failures)),'failures':failures})
        from .automation import notice,save
        def problem(ref):
            v=state(s);notice(v,'blocked',ref,{'reason':'备份或介质需关注，未丢弃资产'});save(s,v)
        days=dt.datetime.fromtimestamp(now)  # 按本机本地日期算「今天」（2026-11-16：按 UTC 算时 11-15 漏的一天没被记成缺口）
        previous_problems=s.get('runner:backup-problems',[])
        backup_problems=[]
        if cfg.get('backup_dir'):
            for kind,period in [('daily',days.strftime('%Y-%m-%d')),('weekly',days.strftime('%G-%V'))]:
                key='runner:backup-attempts:'+kind
                attempts=s.get(key,{})
                count=attempts.get('count',0) if attempts.get('day')==days.strftime('%Y-%m-%d') else 0
                # 当天没成功就一直试：第一次立刻试，之后每 30 分钟一次，直到成功（以前一天 3 次用完就不试了，这台电脑常常只开一两个小时）
                due=now-attempts.get('last',0)>=1800 or attempts.get('day')!=days.strftime('%Y-%m-%d')
                if s.get('runner:'+kind)!=period and due:
                    s.put(key,{'day':days.strftime('%Y-%m-%d'),'count':count+1,'last':now})
                    if kind=='daily' and s.get('runner:daily'):
                        try:
                            gap=(dt.date.fromisoformat(period)-dt.date.fromisoformat(s.get('runner:daily'))).days
                            if gap>1:s.put('runner:backup-gap',{'last_ok':s.get('runner:daily'),'catch_up':period,'missed_days':gap-1,'at':now})
                        except ValueError:pass
                    try:
                        result[kind]=backup(s,cfg['backup_dir'],kind)
                        if kind=='daily':
                            from .distribution import archive_residue
                            try:result['归档']=archive_residue(cfg['backup_dir'],now)
                            except Exception:problem('备份残留归档未完成，请帮我检查归档目录')
                        failed=result[kind].get('external',{}).get('failed',[])
                        if failed or result[kind].get('external_error'):
                            message='项目备份失败：'+('、'.join(failed) if failed else '项目仓库未完成')+'；其他项目已保留，请帮我检查后重试'
                            backup_problems.append(message);problem(message)
                        else:s.put('runner:'+kind,period)
                    except Exception as error:
                        # 写下原因原文（去掉换行、限长），不再只记一句笼统话（2026-11-16 第 3 次自动尝试失败原因丢失）
                        detail=' '.join(str(error).split())[:300] or type(error).__name__
                        message='备份没有完成（'+type(error).__name__+'：'+detail+'），请帮我检查备份目录后重试'
                        backup_problems.append(message);problem(message)
                        s.put('runner:backup-last-error',{'at':now,'kind':kind,'type':type(error).__name__,'detail':detail})
        if backup_problems:s.put('runner:backup-problems',list(dict.fromkeys(backup_problems)))
        elif result.get('daily'):s.put('runner:backup-problems',[])
        # Opened PRs: once merged, record reception so the project is no longer held (every 30 minutes).
        if now-(s.get('runner:publish') or {}).get('at',0)>=1800:
            s.put('runner:publish',{'at':now})
            from .candidates import publish
            for meta_key,raw in s.db.execute("SELECT key,value FROM meta WHERE key LIKE 'candidate:engineering:%'").fetchall():
                reception=json.loads(raw).get('reception') or {}
                if reception.get('state')=='awaiting_external_owner' and (reception.get('publication') or {}).get('state') in ('pr_opened','pushed'):
                    try:publish(s,meta_key[len('candidate:engineering:'):])
                    except Exception:problem('publish')
        # Progress board: regenerate 看板/ whenever any project's progress files change, and at least daily.
        try:
            from .board import load,overview as board_overview,board_fingerprint,weekly_check,awaiting_team_tasks,fetch_remotes,BOARD
            if load(s):
                if now-(s.get('runner:fetch') or {}).get('at',0)>=3600:
                    s.put('runner:fetch',{'at':now})
                    try:fetch_remotes(s)
                    except Exception:pass
                fp=board_fingerprint(s)
                if s.get('runner:overview')!=days.strftime('%Y-%m-%d') or s.get('runner:board')!=fp:
                    board_overview(s,True);s.put('runner:overview',days.strftime('%Y-%m-%d'));s.put('runner:board',fp)
                # Weekly check: stale progress and long waits on the user, each reminded once.
                if s.get('runner:weekly-check')!=days.strftime('%G-%V'):
                    try:waiting_tasks=awaiting_team_tasks(s)
                    except Exception:waiting_tasks=None
                    found=weekly_check(s,tasks=waiting_tasks);s.put('runner:weekly-check',days.strftime('%G-%V'))
                    if found:
                        atomic(s.root/BOARD/'提醒.md','# 本周提醒（'+days.strftime('%Y-%m-%d')+'）\n\n'+''.join('- '+m+'\n' for m in found))
                        result['reminders']=found
        except Exception:problem('overview')
        try:
            from .board import write_reply_desk
            if now-(s.get('runner:replies') or {}).get('at',0)>=60:
                found=write_reply_desk(s)
                s.put('runner:replies',{'at':now,'open':found['open']})
                if found['open']:result['等回复']=found['open']
        except Exception:problem('replies')
        # 自动接着做：项目做到一半、对话空着就给它的 T3 对话发一句提醒（见 DECISIONS 2026-11-18）；出错只记日志，不影响备份和看板
        try:
            from .nudge import safe_tick
            found=safe_tick(s,now)
            if found.get('sent'):result['自动接着做']=found['sent']
        except Exception:pass
        # Write after weekly checks so they cannot overwrite backup failures.
        backup_problems=s.get('runner:backup-problems',[])
        reminder=s.root/'看板/提醒.md'
        if backup_problems or (previous_problems and reminder.is_file()):
            text=reminder.read_text() if reminder.is_file() else '# 本周提醒\n\n'
            for old in previous_problems:
                if old not in backup_problems:text=text.replace('- '+old+'\n','')
            missing=[m for m in backup_problems if m not in text]
            atomic(reminder,text+('\n'+''.join('- '+m+'\n' for m in missing) if missing else ''))
        if cfg.get('git_backup'):
            try:
                from .backup_git import backup as git_backup
                spec=cfg['git_backup']
                fingerprint=digest(canonical({rel:digest(safe(s.root,rel).read_bytes()) for rel in spec['allowlist']}).encode())
                if s.get('runner:git')!=fingerprint:
                    result['git']=git_backup(s,spec['destination'],spec['allowlist'],spec.get('remote'))
                    s.put('runner:git',fingerprint)
            except Exception:problem('git-backup')
        for item in cfg.get('sync',[]):
            try:
                project=item['project'];path=Path(item['medium'])
                if not path.is_dir():continue # absent registered media is quiet
                medium(s,path)
                row=s.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(project,s.device)).fetchone()
                if not row:raise Blocked('当前设备无源')
                fingerprint=digest(canonical(project_inventory(row['path'])).encode())
                key='runner:sync:'+project+':'+digest(str(path).encode())[:12]
                previous=s.get(key,{})
                if previous.get('observed')!=fingerprint:
                    previous={**previous,'observed':fingerprint,'changed':now};s.put(key,previous)
                if previous.get('synced')!=fingerprint and now-previous['changed']>=30:
                    if (Path(row['path'])/'.git').exists() or item.get('dependencies'):
                        result[key]=sync_project_set(s,project,row['path'],path,item.get('dependencies',[]))
                    else:result[key]=incremental(s,row['path'],path,project)
                    s.put(key,{**previous,'synced':fingerprint})
            except Exception:problem('media:'+str(item.get('project','unknown')))
        s.put('runner:heartbeat',{'at':now,'pid':os.getpid()})
        return result

    def serve(self,interval=2):
        from .runtime_identity import require_local_runtime
        require_local_runtime(self.store)
        s=self.store;guard=safe(s.root,'数据/runner.guard');stop=[False]
        def shutdown(*_):stop[0]=True
        child=None;child_attempted=False
        previous={sig:signal.signal(sig,shutdown) for sig in (signal.SIGTERM,signal.SIGINT)}
        try:
            with guard.open('a') as handle:
                try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:raise Blocked('本产品运行器已在运行')
                resume_legacy_shutdown_pause(s,self.clock()) or resume_stale_upgrade_pause(s,self.clock())
                while not stop[0]:
                    if s.get('runner:shutdown_requested'):break
                    cfg=s.get('runner:config',{})
                    if child is not None and child.poll() is not None:
                        from .automation import notice,save
                        v=state(s);notice(v,'blocked','bots',{'reason':'机器人连接进程已停止；请运行连接诊断'});save(s,v)
                        child=None
                    if state(s)['paused'] and child is not None:
                        child.terminate()
                        try:child.wait(timeout=10)
                        except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
                        child=None;child_attempted=False
                    if not state(s)['paused'] and cfg.get('bots_private') and not child_attempted:
                        child_attempted=True
                        from .bots import private_config
                        botcfg=private_config(cfg['bots_private'])
                        entry=Path(__file__).absolute().parent.parent/'adapters/bots/index.mjs'
                        if not entry.is_file():entry=s.root/'工具/adapters/bots/index.mjs'
                        if botcfg.get('enabled') is True and Path(botcfg['root']).absolute()==s.root:
                            from .bot_dependencies import ready
                            if botcfg.get('moduleRoot')!=ready(s):raise Blocked('机器人SDK不属于本机已封存环境')
                            child=subprocess.Popen([cfg.get('node','node'),str(entry),cfg['bots_private'],'both'],stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                    try:self.tick()
                    except Exception:pass # fail closed; retries reconcile persistent original references
                    time.sleep(interval)
                if child is not None and child.poll() is None:
                    child.terminate()
                    try:child.wait(timeout=10)
                    except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
                    child=None
                # 收到关机/重启的终止信号只记一笔就退出，不再把自己写成暂停：以前每关一次机，开机后后台就一直停着（2026-11-16 已确认）
                if s.get('runner:shutdown_requested'):pause(s,by='runner',reason='收到停止请求')
                s.put('runner:stopped',{'at':self.clock(),'reason':'后台进程退出（关机、重启或停止请求）'})
        finally:
            if child is not None and child.poll() is None:
                child.terminate()
                try:child.wait(timeout=10)
                except subprocess.TimeoutExpired:child.kill();child.wait(timeout=5)
            for sig,handler in previous.items():signal.signal(sig,handler)

@cooperative
def configure_local(store,cfg):
    # Explicit new-host mapping; history and old tasks stay non-executable.
    if set(cfg)-{'backup_dir','sync','git_backup','bots_private','node'}:raise Blocked('未知运行配方字段')
    if cfg.get('backup_dir'):
        path=Path(cfg['backup_dir']).expanduser().absolute()
        if path.is_relative_to(store.root) or store.root.is_relative_to(path):raise Blocked('备份必须在包外')
        # 上一级目录在就自动建备份目录；上一级都不在（比如外接盘没插）才拒绝。
        if not path.exists() and path.parent.is_dir() and not path.parent.is_symlink():path.mkdir()
        if not path.is_dir():raise Blocked('备份目录找不到，请先建好目录或接上外接盘')
        if not path.stat().st_mode & 0o222 or not os.access(path,os.W_OK):raise Blocked('这个备份目录不能写入，请换一个有写入权限的目录')
        try:
            with tempfile.TemporaryFile(dir=path):pass
        except OSError:raise Blocked('这个备份目录不能写入，请换一个有写入权限的目录')
        cfg={**cfg,'backup_dir':str(path)}
    for item in cfg.get('sync',[]):
        medium(store,item['medium'])
        if not store.db.execute('SELECT path FROM paths WHERE project=? AND device=?',(item['project'],store.device)).fetchone():raise Blocked('先映射本机项目')
    for row in store.db.execute('SELECT project,path FROM paths WHERE device=?',(store.device,)).fetchall():
        policy=store.get('project-policy:'+row['project'],{})
        expected=store.root/'代码'/row['project']
        if policy.get('created_by_product') is True and Path(row['path'])==expected and (expected/'.git').is_dir():
            store.put('project-policy:'+row['project'],{**policy,'device':store.device,'root':str(expected)})
    store.put('runner:config',cfg)
    store.put('restore:needs_takeover','')
    if store.get('execution') in ('restored-readonly','copied-readonly'):store.put('execution','unbound')
    pause(store,False)
    return {'configured':True,'tasks_restored':False,'network_enabled':False}


def register(subparser):
    p=subparser.add_parser('runner');p.add_argument('action',choices=['tick','run','install','uninstall','status','stop','resume','pre-upgrade','restore','configure-local'])
    p.add_argument('--manifest');p.add_argument('--target');p.add_argument('--launch-agents');p.add_argument('--execute',action='store_true');p.add_argument('--config');p.set_defaults(runner_run=run);return p

def run(args):
    s=Store(args.root)
    try:
        if args.action=='configure-local':
            if not args.config:raise Blocked('需要本机新配方')
            return configure_local(s,json.loads(Path(args.config).read_text()))
        if args.config:
            from .runtime_identity import require_local_runtime
            require_local_runtime(s)
            cfg=json.loads(Path(args.config).read_text());s.put('runner:config',cfg)
        if args.action=='install':
            from .runtime_identity import require_local_runtime
            require_local_runtime(s)
        if args.action in ('install','uninstall','status'):
            return service(s.root,args.launch_agents or Path.home()/'Library/LaunchAgents',args.action,args.execute)
        if args.action=='restore':return restore_incremental(args.manifest,args.target)
        if args.action=='stop':return pause(s,by='runner stop 命令',reason='用户或 AI 手动停后台')
        if args.action=='resume':return pause(s,False)
        if args.action=='pre-upgrade':
            # Explicit foreground hook; no timer can initiate migration/update.
            from .distribution import prepare_update
            prepare_update(s)
            pause(s)
            cfg=s.get('runner:config',{})
            if not cfg.get('backup_dir'):raise Blocked('升级前需包外备份目标')
            return backup(s,cfg['backup_dir'],'upgrade')
        if args.action=='run':Runner(s).serve();return {'stopped':True}
        return Runner(s).tick()
    finally:s.db.close()

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True))
    try:print(canonical(run(p.parse_args(argv))));return 0
    except Exception:print('{"status":"BLOCKED","reason":"运行器操作未确认"}');return 2

if __name__=='__main__':raise SystemExit(main())
