"""SD-30 升级通道：收到新版一句话升级、可退回，不依赖任何人的固定路径。

用法（用新版里的程序跑，--root 指向正在用的随身开发）：
  python3 <新版>/工具/portable.pyz --root <随身开发> upgrade apply <新版目录>
  python3 <随身开发>/工具/portable.pyz --root <随身开发> upgrade rollback
一条命令做完：停后台 → 确认没有在跑的任务 → 包外备份 → 换新版 → 恢复战队/项目登记/本机规则 → 开后台 → 自检读回。
用户的数据、设置、项目登记都不动；退回只换程序文件，不丢升级后新增的数据。
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from .core import Blocked, Store

LAST='upgrade:last'
REPO_KEY='upgrade:repo'
REPO_RX=re.compile(r'^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$')
GH_URL_RX=re.compile(r'^https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+?)(?:\.git|/releases.*|/.*)?$')
README_GH_RX=re.compile(r'https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)')
SHA256_RX=re.compile(r'\b([0-9a-fA-F]{64})\b')

def _ver(v):return tuple(int(x) for x in v.split('.'))

def _launch(root,action,launchctl=None):
    from .supervisor import identity
    label=identity(root);plist=Path.home()/'Library/LaunchAgents'/(label+'.plist')
    if not plist.exists():return False
    if launchctl:
        if launchctl(action,plist) is False:raise Blocked('后台'+action+'未确认')
        return True
    if sys.platform!='darwin':return False
    dom='gui/'+str(os.getuid())
    if action=='stop':
        subprocess.run(['/bin/launchctl','bootout',dom+'/'+label],capture_output=True,timeout=15)
        # bootout 返回时进程可能还在收尾（比如正在备份），等它真正退出再判断，最多 30 秒。
        for _ in range(30):
            if subprocess.run(['/bin/launchctl','print',dom+'/'+label],capture_output=True,timeout=15).returncode!=0:break
            time.sleep(1)
        else:raise Blocked('后台停止未确认：等了 30 秒后台还没退出，本次没有升级，原版本照常运行')
    else:
        subprocess.run(['/bin/launchctl','bootstrap',dom,str(plist)],capture_output=True,timeout=15)
        result=subprocess.run(['/bin/launchctl','kickstart',dom+'/'+label],capture_output=True,timeout=15)
        if result.returncode or subprocess.run(['/bin/launchctl','print',dom+'/'+label],capture_output=True,timeout=15).returncode:raise Blocked('后台启动未确认')
    return True

def _backup_dir(store):
    from . import settings
    chosen=settings.load(store).get('backup_dir') or (store.get('runner:config') or {}).get('backup_dir')
    root=Path(store.root).absolute()
    return Path(os.path.expanduser(chosen)) if chosen else root.parent/(root.name+'备份')

def _resume(root,launchctl=None,paused=False):
    try:
        s=Store(root)
        try:
            from .automation import pause
            pause(s,paused)
        finally:s.db.close()
    finally:
        started=_launch(root,'start',launchctl) if not paused else False
    return started

def _restore(root,team,bound,python):
    """换完程序后的收尾：停用自带战队后台、项目登记、本机规则。"""
    s=Store(root);done=[];failed=[]
    def step(name,action,describe=None):
        try:
            result=action();label=describe(result) if describe else name
            if label:done.append(label)
        except Exception as e:failed.append(name+'：'+str(e))
    try:
        # 自带战队后台已停用（见 DECISIONS 2026-11-17）：不再重建、不再接回，旧文件移进归档；用户自己的战队入口不受影响
        from .team_retire import retire
        def describe(r):
            if r['状态'] in ('没有自带战队后台','这一版自带，不动'):return None
            if r['状态']=='已停用':return '自带战队后台已停用，旧文件移进 '+r['归档']
            raise Blocked('自带战队后台'+r['状态']+'：'+(r.get('原因') or '；'.join(r.get('没移走',[]))))
        step('自带战队后台停用',lambda:retire(s),describe)
        from . import settings
        from .board import apply
        step('新登记的项目写进本机设置',lambda:settings.keep_local_registrations(s),
             lambda names:'新登记的项目已写进本机设置：'+'、'.join(names) if names else None)
        step('项目登记已对齐本机设置',lambda:apply(s,None))
        if settings.path(s).is_file():step('本机规则已重新生成',lambda:settings.render_rules(s))
    finally:s.db.close()
    return done,failed

def _initialize_local(root,python=None,launchctl=None):
    """Configured legacy installs need the same local facilities as new installs."""
    from . import settings,supervisor,board
    s=Store(root)
    try:
        if not settings.path(s).is_file():return None
        if sys.platform!='darwin' and launchctl is None:raise Blocked('当前系统不是 Mac，后台、每日自动备份与看板未初始化')
        cfg=s.get('runner:config',{}) or {}
        destination=Path(cfg.get('backup_dir') or _backup_dir(s)).expanduser().absolute()
        if destination.is_relative_to(s.root) or s.root.is_relative_to(destination):raise Blocked('备份必须在包外')
        if any(p.is_symlink() for p in (destination,*destination.parents)):raise Blocked('备份目录不能是链接')
        destination.mkdir(exist_ok=True)
        if not destination.stat().st_mode & 0o222:raise Blocked('备份目录不可写')
        with tempfile.TemporaryFile(dir=destination):pass
        if not cfg.get('backup_dir'):s.put('runner:config',{**cfg,'backup_dir':str(destination)})
        if board.load(s):board.overview(s,sync=True)
        plist=Path.home()/'Library/LaunchAgents'/(supervisor.identity(root)+'.plist')
        existed=plist.exists()
        # Generate while paused. Starting is a separate, checked step after resume.
        status=supervisor.service(root,plist.parent,'install',execute=False,python=python,preserve_existing=True)
        s.put('runner:service',{**status,'running':False})
        return not existed
    finally:s.db.close()

def preload():
    """把运行包里的每个模块先载入内存；个别模块缺可选依赖载不进来就跳过，返回载入失败的名字。"""
    import importlib
    from .core import PROTOCOL
    missed=[]
    for name in PROTOCOL['distribution']['runtime_files']:
        if not name.endswith('.py') or name in ('__init__.py','__main__.py'):continue
        try:importlib.import_module(__package__+'.'+name[:-3])
        except Exception:missed.append(name)
    return missed

def upgrade(root,candidate,backup_dir=None,*,allow_older=False,launchctl=None,python=None,_retire_service=None):
    from .distribution import release_verify,prepare_update,atomic_upgrade
    # 换程序文件前先把运行包里的模块全部载入：退回时正在跑的程序包会被换掉，之后再按需载入会读到新包报
    # 「bad local file header」（见 DECISIONS 2026-11-18）
    preload()
    from . import dependencies,team,team_retire,board,settings,automation,supervisor  # noqa: F401  后面直接用这几个名字
    root=Path(root).absolute();candidate=Path(candidate).absolute()
    new=release_verify(candidate);cur=release_verify(root)
    if _ver(new['version'])==_ver(cur['version']):raise Blocked('已经是 '+cur['version']+'，不用升级')
    if _ver(new['version'])<_ver(cur['version']) and not allow_older:raise Blocked(f'新版 {new["version"]} 比正在用的 {cur["version"]} 还旧；要退回请用 upgrade rollback')
    s=Store(root)
    try:
        backup_dir=Path(backup_dir) if backup_dir else _backup_dir(s);backup_dir.mkdir(parents=True,exist_ok=True)
        team=s.get('team_config',{}) or {}
        from .automation import pause,state
        was_paused=state(s)['paused']
        pause(s,by='upgrade',reason='升级换程序前停写')
    finally:s.db.close()
    try:
        _launch(root,'stop',launchctl)
        s=Store(root)
        try:
            prepare_update(s)
            bound=sorted((s.get('readiness_disabled:upgrade') or {}).get('projects',{}))
        finally:s.db.close()
        result=atomic_upgrade(root,candidate,backup_dir)
    except BaseException:
        # 没换成：原版本继续用，后台照常
        try:_resume(root,launchctl,was_paused)
        except Exception:pass
        raise
    done=[];failed=[];restored=False
    created=False;retirement_failed=False;initialized=None
    try:
        if _retire_service is not None:
            try:
                import uuid
                disabled=_retire_service.with_suffix('.plist.'+uuid.uuid4().hex+'.upgrade-disabled')
                if _retire_service.exists():_retire_service.rename(disabled)
            except Exception as e:
                retirement_failed=True
                failed.append('回退后台配方停用失败，已保持暂停：'+str(e))
        try:
            restored_done,restored_failed=_restore(root,team,bound,python or sys.executable)
            done.extend(restored_done);failed.extend(restored_failed)
        except Exception as e:failed.append('收尾初始化：'+str(e))
        if not allow_older and new.get('edition')=='friend':
            try:
                initialized=_initialize_local(root,python or sys.executable,launchctl)
                created=initialized is True
                if initialized is not None:done.append('每日备份配置与看板已初始化')
                else:done.append('尚未配置，后台、每日备份与看板未初始化，保留首次配置入口')
            except Exception as e:failed.append('本机后台初始化：'+str(e))
        s=Store(root)
        try:
            with s.db:s.db.execute("DELETE FROM meta WHERE key='upgrade:created-service'")
            if new.get('repo') or cur.get('repo'):s.put(REPO_KEY,new.get('repo') or cur.get('repo'))
            try:s.put(LAST,{'from':result['from'],'to':result['to'],'previous_package':result['previous_package'],'backup':result['backup'],'receipt':result['receipt'],'created_service':str(Path.home()/'Library/LaunchAgents'/(supervisor.identity(root)+'.plist')) if created else None,'paused':was_paused})
            except Exception as e:failed.append('升级记录：'+str(e))
            try:health=s.doctor()
            except Exception as e:failed.append('自检：'+str(e))
        finally:s.db.close()
    except Exception as e:failed.append('升级记录或自检：'+str(e))
    finally:
        try:
            restored=bool(_resume(root,launchctl,was_paused or retirement_failed))
            if created and not was_paused and not restored:raise Blocked('后台启动未确认')
            if created and not was_paused and sys.platform!='darwin' and not launchctl:raise Blocked('本机没有自动备份：当前系统不支持后台')
            if initialized is not None or _retire_service is not None:
                s=Store(root)
                try:s.put('runner:service',{'running':restored,'paused':was_paused or retirement_failed,'real_launchd':launchctl is None and sys.platform=='darwin'})
                finally:s.db.close()
        except Exception as e:failed.append('后台恢复：'+str(e))
    if failed:
        raise Blocked('程序已换成新版 '+result['to']+'；收尾没做完的：'+'；'.join(failed)+'；'+('后台已恢复' if restored else ('后台保持暂停' if was_paused or retirement_failed else '后台未确认恢复')))
    out={'升级':result['from']+' → '+result['to'],'备份':result['backup'],'收尾':done,'自检版本':release_verify(root)['version'],'项目数':health.get('projects'),'后台':'保持暂停' if was_paused else ('已确认恢复' if restored else '未安装或未确认启动'),
            '退回方法':'对 AI 说「退回上一版」（upgrade rollback）'}
    if new.get('edition')=='friend':
        # 朋友版升级默认打开自动接着做，并告诉用户怎么关（见 DECISIONS 2026-11-18）
        s=Store(root)
        try:
            if settings.auto_continue_on(s):out['自动接着做']='已打开自动接着做，不要就说『关掉自动接着做』'
        except Exception:pass
        finally:s.db.close()
    return out

def rollback(root,launchctl=None,python=None):
    root=Path(root).absolute();s=Store(root)
    try:last=s.get(LAST)
    finally:s.db.close()
    if not last:raise Blocked('没有可退回的升级记录')
    prev=Path(last['previous_package'])
    if not (prev/'发行.json').is_file():raise Blocked('上一版的程序文件不在了：'+str(prev))
    if last.get('created_service'):
        from .supervisor import identity
        path=Path.home()/'Library/LaunchAgents'/(identity(root)+'.plist')
        if str(path)!=last['created_service']:raise Blocked('新增后台定义身份不符')
    else:path=None
    out=upgrade(root,prev,allow_older=True,launchctl=launchctl,python=python,_retire_service=path)
    return {**out,'退回':last['to']+' → '+last['from'],'后台兼容性':'升级新增的后台定义已停用归档；旧版不会自动运行新版配方' if last.get('created_service') else '保留原后台定义'}

def _normalize_repo(value):
    text=str(value or '').strip()
    if not text:return None
    m=GH_URL_RX.match(text)
    if m:text=m.group(1)
    if text.endswith('.git'):text=text[:-4]
    return text if REPO_RX.match(text) else None

def _resolve_repo(root,repo=None):
    root=Path(root).absolute()
    candidates=[repo,os.environ.get('PORTABLE_GITHUB_REPO')]
    if (root/'发行.json').is_file():
        try:candidates.append(json.loads((root/'发行.json').read_text(encoding='utf-8')).get('repo'))
        except Exception:pass
    if (root/'数据/state.sqlite').is_file():
        s=Store(root)
        try:candidates.append(s.get(REPO_KEY))
        finally:s.db.close()
    for md in (root/'README.md',root.parent/'README.md'):
        if md.is_file():
            try:
                m=README_GH_RX.search(md.read_text(encoding='utf-8'))
                if m:candidates.append(m.group(1))
            except OSError:pass
    for item in candidates:
        slug=_normalize_repo(item)
        if slug:return slug
    raise Blocked('还没有设置 GitHub 升级仓库：请用 upgrade apply 账号/仓库名，或给出新版 .zip / 目录路径')

def _fetch_release_json(slug):
    url=f'https://api.github.com/repos/{slug}/releases/latest'
    req=urllib.request.Request(url,headers={'Accept':'application/vnd.github+json','User-Agent':'suishen-dev-updater'})
    try:
        with urllib.request.urlopen(req,timeout=30) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        if e.code==404:raise Blocked(f'GitHub 仓库 {slug} 没有找到已发布的 Release')
        raise Blocked(f'查询 GitHub Release 失败（HTTP {e.code}）：{slug}')
    except Exception as e:
        raise Blocked(f'连不上 GitHub（{slug}）：{e}')

def check_remote(root,repo=None,*,fetch_json=None):
    from .distribution import release_verify
    root=Path(root).absolute()
    cur=release_verify(root)['version']
    slug=_resolve_repo(root,repo)
    data=(fetch_json or _fetch_release_json)(slug)
    if not isinstance(data,dict):raise Blocked('GitHub Release 返回格式读不懂')
    tag=str(data.get('tag_name') or '').strip().lstrip('vV')
    if not re.fullmatch(r'\d+\.\d+\.\d+',tag):
        raise Blocked('GitHub Release 版本号格式不对：'+str(data.get('tag_name') or ''))
    assets=[a for a in (data.get('assets') or []) if isinstance(a,dict) and str(a.get('name') or '').lower().endswith('.zip')]
    if not assets:raise Blocked(f'GitHub Release v{tag} 里没有 .zip 安装包')
    preferred=[a for a in assets if str(a.get('name') or '').startswith('suishen-dev-')]
    asset=(preferred or assets)[0]
    url=str(asset.get('browser_download_url') or '').strip()
    if not url.startswith('https://'):raise Blocked('GitHub Release 下载地址不是 https://')
    digest=str(asset.get('digest') or '').strip()
    sha=digest.split(':',1)[1].lower() if digest.lower().startswith('sha256:') and SHA256_RX.fullmatch(digest.split(':',1)[1]) else None
    body=str(data.get('body') or '').strip()
    if not sha:
        m=SHA256_RX.search(body)
        if m:sha=m.group(1).lower()
    s=Store(root)
    try:s.put(REPO_KEY,slug)
    finally:s.db.close()
    return {'仓库':slug,'当前版本':cur,'最新版本':tag,'有新版':_ver(tag)>_ver(cur),'安装包':asset['name'],'下载地址':url,'sha256':sha,'更新说明':body}

def _extract_zip(zip_path,dest_dir,expected_sha256=None):
    zip_path=Path(zip_path).expanduser().resolve()
    dest_dir=Path(dest_dir).resolve()
    if not zip_path.is_file():raise Blocked('找不到安装包文件：'+str(zip_path))
    actual=hashlib.sha256(zip_path.read_bytes()).hexdigest().lower()
    if expected_sha256:
        want=str(expected_sha256).strip().lower()
        if actual!=want:raise Blocked(f'安装包 SHA256 校验失败：期望 {want}，实际 {actual}')
    try:zf=zipfile.ZipFile(zip_path)
    except zipfile.BadZipFile:raise Blocked('不是有效的 .zip 压缩包：'+str(zip_path))
    with zf:
        for info in zf.infolist():
            name=info.filename
            if name.startswith(('/','\\')) or '..' in Path(name).parts:
                raise Blocked('压缩包含不安全路径：'+name)
            mode=(info.external_attr>>16)&0o170000
            if mode==0o120000:raise Blocked('压缩包含符号链接，拒绝解压：'+name)
            target=(dest_dir/name).resolve()
            if not (target==dest_dir or target.is_relative_to(dest_dir)):
                raise Blocked('压缩包路径越界：'+name)
            if info.is_dir():target.mkdir(parents=True,exist_ok=True)
            else:
                target.parent.mkdir(parents=True,exist_ok=True)
                target.write_bytes(zf.read(info))
                perm=(info.external_attr>>16)&0o777
                if perm&0o111:os.chmod(target,perm)
    if (dest_dir/'发行.json').is_file():return dest_dir,actual
    if (dest_dir/'随身开发'/'发行.json').is_file():return dest_dir/'随身开发',actual
    found=[p for p in dest_dir.iterdir() if p.is_dir() and (p/'发行.json').is_file()]
    if len(found)==1:return found[0],actual
    raise Blocked('压缩包里找不到随身开发（缺少 发行.json）')

def _download_zip(url,dest_file,urlopen=None):
    url=str(url or '').strip()
    if not url.startswith('https://'):raise Blocked('下载地址必须是 https:// 开头')
    req=urllib.request.Request(url,headers={'User-Agent':'suishen-dev-updater'})
    try:
        with (urlopen or urllib.request.urlopen)(req,timeout=120) as resp:
            Path(dest_file).write_bytes(resp.read())
    except Exception as e:
        raise Blocked(f'下载安装包失败（{url}）：{e}')

def _is_local_or_zip_url(candidate):
    if not candidate:return False
    text=str(candidate).strip()
    if text.startswith(('https://','http://')):
        return text.lower().endswith('.zip') or '/releases/download/' in text
    if text.startswith(('/','./','../','~')) or text.lower().endswith('.zip'):return True
    if Path(text).expanduser().exists():return True
    return _normalize_repo(text) is None

def apply_target(root,candidate=None,backup_dir=None,*,repo=None,sha256=None,launchctl=None,python=None,fetch_json=None,urlopen=None):
    if not repo and _is_local_or_zip_url(candidate):
        text=str(candidate).strip()
        if text.startswith(('https://','http://')):
            with tempfile.TemporaryDirectory(prefix='portable-up-') as tmp:
                pkg=Path(tmp)/'pkg.zip'
                _download_zip(text,pkg,urlopen=urlopen)
                extracted,actual=_extract_zip(pkg,Path(tmp)/'unpacked',sha256)
                out=upgrade(root,extracted,backup_dir,launchctl=launchctl,python=python)
                return {**out,'来源':text,'sha256':actual}
        p=Path(text).expanduser()
        if p.is_file() and p.suffix.lower()=='.zip':
            with tempfile.TemporaryDirectory(prefix='portable-up-') as tmp:
                extracted,actual=_extract_zip(p,Path(tmp)/'unpacked',sha256)
                out=upgrade(root,extracted,backup_dir,launchctl=launchctl,python=python)
                return {**out,'来源':str(p.absolute()),'sha256':actual}
        return upgrade(root,p,backup_dir,launchctl=launchctl,python=python)
    info=check_remote(root,repo or candidate,fetch_json=fetch_json)
    if _ver(info['最新版本'])==_ver(info['当前版本']):
        raise Blocked('已经是 '+info['当前版本']+'，不用升级')
    if _ver(info['最新版本'])<_ver(info['当前版本']):
        raise Blocked(f'新版 {info["最新版本"]} 比正在用的 {info["当前版本"]} 还旧；要退回请用 upgrade rollback')
    with tempfile.TemporaryDirectory(prefix='portable-up-') as tmp:
        pkg=Path(tmp)/info['安装包']
        _download_zip(info['下载地址'],pkg,urlopen=urlopen)
        extracted,actual=_extract_zip(pkg,Path(tmp)/'unpacked',sha256 or info.get('sha256'))
        out=upgrade(root,extracted,backup_dir,launchctl=launchctl,python=python)
    s=Store(root)
    try:s.put(REPO_KEY,info['仓库'])
    finally:s.db.close()
    return {**out,'来源':info['仓库'],'安装包':info['安装包'],'sha256':actual}

def run(args):
    if args.action=='check':return check_remote(args.root,args.repo or args.candidate)
    if args.action=='apply':return apply_target(args.root,args.candidate,args.backup,repo=args.repo,sha256=args.sha256)
    return rollback(args.root)

def register(subparser):
    p=subparser.add_parser('upgrade',help='一句话升级：check [仓库] 查新版 / apply [目录|.zip|仓库] 升级 / rollback 退回上一版')
    p.add_argument('action',choices=['apply','check','rollback']);p.add_argument('candidate',nargs='?')
    p.add_argument('--backup');p.add_argument('--repo',default='');p.add_argument('--sha256',default='')
    p.set_defaults(upgrade_run=run);return p
