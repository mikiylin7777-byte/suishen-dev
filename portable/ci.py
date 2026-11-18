"""统一的「查 CI、等 CI、再合并」：每一项检查都是 success、有执行记录才算通过；跳过、没跑完、没有结果都不算。
不看耗时：0 秒的步骤是正常的。从第一次查到没有任何检查起过了很久，报「CI 没触发」，不空等。
只看提交状态（commit status / check runs）；平台上手动触发的运行不认，分不清是不是同一轮、是不是全部任务。
--approval 只是记下用户点头的出处供审计，程序没法验证它真假。

只用这台电脑已有的在线仓库登录（Gitea 用 GITEA_TOKEN 或 git 凭据助手，GitHub 用 gh），不新增、不输出凭据。
合并按协议【按风险分级】：低风险 CI 通过、高风险用户点头后，由 AI 带 --confirm 执行；合并前当场复查，并把头提交号交给平台，头提交变了就不合。
"""
import json
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request
from .core import Blocked
from . import remote_publish as rp

PASS='通过'
VERDICT_TEXT={
    '通过':'每一项检查都通过了，可以按规矩合并（低风险的直接合，高风险的要用户点头，见【按风险分级】）。',
    '没跑完':'还有检查在排队或在跑，不能合并；用 ci wait 等它跑完。',
    '没有检查':'这个 PR 的头提交还没有任何检查结果：CI 可能马上就要启动；用 ci wait 等一会儿。',
    '没触发':'等了这么久这个头提交还没有任何检查记录，多半是 CI 没触发。常见原因是分支从旧的 main 切出来，带着旧的 CI 配置：把目标分支合进这个分支再推一次。也可能这个仓库没配 CI，没配的按本地全量测试为准。',
    '没有执行记录':'有检查显示通过，但没有对应的执行记录（运行链接），不能确认真的跑过；去平台上手动触发一次 CI。',
    '被跳过':'有检查被跳过，不算通过。常见原因是这个仓库的 CI 只对合进 main 的 PR 跑：要让 CI 也对这个目标分支的 PR 自动跑（改 CI 配置，按高风险等用户点头）。在那之前 ci merge 不放行；手动跑的结果 ci check 不认，因为分不清是不是同一轮、是不是全部任务。',
    '没通过':'有检查失败了，不能合并；先看失败那一项的日志。',
    '有冲突':'检查之外，这个 PR 和目标分支有冲突，平台不让合；先解决冲突。',
    '已合并':'这个 PR 已经合并了，不用再合。',
    '已关闭':'这个 PR 已经关闭（没合并），不能合并。',
}
LOG='ci:merges'


def _project(store,name):
    """项目可以写注册表里的 id、名字，或项目目录。"""
    from .board import load
    for e in load(store):
        if name in (e.get('id'),e.get('name')):return Path(e['path'])
    p=Path(name).expanduser()
    if (p/'.git').exists():return p.resolve()
    raise Blocked('找不到项目「'+str(name)+'」：写注册表里的项目 id、名字，或项目目录')


def _identity(path):
    url=rp.remote(path)
    if not url:raise Blocked('这个项目没连在线仓库，没有在线 CI；按本地全量测试为准')
    return rp.repository(url)


def _checks_gitea(api,idn,sha):
    status=api(idn,'GET',f"repos/{idn['repo']}/commits/{sha}/status")
    out=[]
    for s in status.get('statuses') or []:
        state=(s.get('status') or s.get('state') or '').lower()
        out.append({'名称':s.get('context') or '','结果':state,'说明':s.get('description') or '','链接':s.get('target_url') or ''})
    return out


def _checks_github(api,idn,sha):
    out=[]
    runs=api(idn,'GET',f"repos/{idn['repo']}/commits/{sha}/check-runs?per_page=100")
    for r in runs.get('check_runs') or []:
        state=(r.get('conclusion') or '').lower() if r.get('status')=='completed' else 'pending'
        out.append({'名称':r.get('name') or '','结果':state,'说明':r.get('status') or '','链接':r.get('html_url') or ''})
    status=api(idn,'GET',f"repos/{idn['repo']}/commits/{sha}/status")
    for s in status.get('statuses') or []:
        out.append({'名称':s.get('context') or '','结果':(s.get('state') or '').lower(),'说明':s.get('description') or '','链接':s.get('target_url') or ''})
    return out


def judge(pr,checks):
    """只认每项都 success。汇总状态不看：Gitea 会把「全部跳过」汇总成 success。"""
    if pr.get('merged'):return '已合并'
    if pr.get('state')!='open':return '已关闭'
    states=[c['结果'] for c in checks]
    if any(s in ('failure','error','cancelled','timed_out','action_required','startup_failure') for s in states):return '没通过'
    if any(s in ('pending','running','queued','in_progress','waiting','blocked','') for s in states):return '没跑完'
    if not states:return '没有检查'
    if any(s!='success' for s in states):return '被跳过' if any(s in ('skipped','neutral','stale') for s in states) else '没通过'
    if any(not c.get('链接') for c in checks):return '没有执行记录'
    if pr.get('mergeable') is False:return '有冲突'
    return PASS


SEEN='ci:no-check-seen'


def _quiet_minutes(store,idn,number,sha,now):
    """这个头提交从第一次被看到「没有任何检查」起过了几分钟。不用提交时间或 PR 更新时间：前者可能很旧或在未来，后者随便一改就重置。"""
    seen=dict(store.get(SEEN,{}) or {});key=f"{idn.get('origin') or idn['platform']}/{idn['repo']}#{int(number)}@{sha}"
    if key not in seen:
        seen[key]=now();store.put(SEEN,dict(list(seen.items())[-200:]))
    return int(max(0,now()-seen[key])//60)


def _get_pr(api,idn,number):
    """读 PR；平台回 404 时直接说「没有这个 PR」并给出最近几个 PR 号（2026-11-16 冷启动验收：以前报「既有 Gitea API 查询不可用」，看着像故障）。"""
    try:return api(idn,'GET',f"repos/{idn['repo']}/pulls/{int(number)}")
    except rp.DeliveryError as error:
        if error.code!=404:raise
    recent=''
    try:
        query='pulls?state=all&per_page=5' if idn['platform']=='github' else 'pulls?state=all&limit=5&sort=newest'
        nums=[int(x['number']) for x in api(idn,'GET',f"repos/{idn['repo']}/{query}") or [] if isinstance(x,dict) and x.get('number')]
        if nums:recent='；这个仓库最近的 PR 号：'+'、'.join(str(n) for n in sorted(nums,reverse=True)[:5])
    except Exception:pass
    raise Blocked(f"没有这个 PR：{idn['repo']} 里找不到 #{int(number)}{recent}")


def check(store,project,number,api=None,no_run_minutes=10,now=time.time):
    api=api or rp.api
    path=_project(store,project);idn=_identity(path)
    pr=_get_pr(api,idn,number)
    sha=(pr.get('head') or {}).get('sha') or ''
    if not sha:raise Blocked('读不到这个 PR 的头提交，没法判断检查结果')
    checks=(_checks_github if idn['platform']=='github' else _checks_gitea)(api,idn,sha)
    verdict=judge(pr,checks)
    quiet=None
    if verdict=='没有检查':
        quiet=_quiet_minutes(store,idn,number,sha,now)
        if quiet>=no_run_minutes:verdict='没触发'
    note=VERDICT_TEXT[verdict]
    if quiet is not None:note='从第一次查到现在 '+str(quiet)+' 分钟，这个头提交还没有任何检查记录。'+note
    if verdict=='已合并' and (not checks or any(c['结果']!='success' for c in checks)):
        note+='注意：它合并时检查并没有全部通过（'+('、'.join(c['名称']+'='+(c['结果'] or '无') for c in checks) or '没有检查结果')+'），要补跑或复核。'
    return {'结论':verdict,'能合并':verdict==PASS,'说明':note,'仓库':idn['repo'],'PR':int(number),
            '目标分支':(pr.get('base') or {}).get('ref') or '','头提交':sha,'链接':pr.get('html_url') or '',
            '检查':[{k:c[k] for k in ('名称','结果','说明','链接')} for c in checks]}


def wait(store,project,number,timeout=3600,interval=20,api=None,sleep=time.sleep,clock=time.monotonic,no_run_minutes=10,now=time.time):
    """等到检查跑完（不是等合并）。刚推上去还没有检查的先等着；超过 no_run_minutes 分钟仍没有，报「没触发」，不空等。"""
    end=clock()+max(0,timeout)
    while True:
        result=check(store,project,number,api,no_run_minutes,now)
        if result['结论'] not in ('没跑完','没有检查'):return result
        if clock()>=end:
            result['超时']=True
            result['说明']='等了 '+str(int(timeout//60))+' 分钟检查还没跑完：'+result['说明']
            return result
        sleep(interval)


def _merge_gitea(idn,number,sha):
    body=json.dumps({'Do':'merge','head_commit_id':sha,'delete_branch_after_merge':False}).encode()
    req=Request(idn['origin']+f"/api/v1/repos/{idn['repo']}/pulls/{int(number)}/merge",data=body,method='POST',headers=rp.gitea_headers(idn))
    try:
        with rp.urlopen(req,timeout=60) as response:response.read()
    except HTTPError as error:
        code=error.code;error.close()
        if code in (401,403):raise Blocked('这台电脑现有的在线仓库登录没有合并权限，本次没有合并；不借别的窗口的账号，请总管或用户安排有权限的窗口合并') from None
        if code==409:raise Blocked('合并前头提交变了（有人又推了提交），本次没有合并；重新 ci check') from None
        if code==405:raise Blocked('平台不让合（可能有冲突、分支保护或缺少批准），本次没有合并') from None
        raise rp.DeliveryError('sent_unknown','合并请求结果未知：先 ci check 看是不是已经合并，不要直接重发') from None
    except Blocked:raise
    except Exception:raise rp.DeliveryError('sent_unknown','合并请求结果未知：先 ci check 看是不是已经合并，不要直接重发') from None


def _merge_github(api,idn,number,sha):
    api(idn,'PUT',f"repos/{idn['repo']}/pulls/{int(number)}/merge",{'sha':sha,'merge_method':'merge'})


HIGH_RISK_PATHS=(('.gitea/workflows/','CI 配置'),('tools/ci/','CI 判定脚本'),('.github/workflows/','CI 配置'),('.gitlab-ci.yml','CI 配置'),('.gitleaks.toml','密钥扫描配置'),
                 ('tools/gate/','合并把关'),('portable/protocol.json','规矩'),('portable/settings.py','本机规矩模板'),('portable/ci.py','合并把关'))


MAX_FILE_PAGES=50


def _changed_files(api,idn,number):
    """PR 改了哪些文件，返回（新路径, 状态, 改名前路径）。翻到最后一页（不满一页）才算读完；翻到上限还没读完就拒绝，不拿半份清单判低风险。"""
    out=[];size=100 if idn['platform']=='github' else 50
    for page in range(1,MAX_FILE_PAGES+1):
        q=f"per_page={size}&page={page}" if idn['platform']=='github' else f"limit={size}&page={page}"
        got=api(idn,'GET',f"repos/{idn['repo']}/pulls/{int(number)}/files?{q}")
        if not isinstance(got,list):raise Blocked('读不到这个 PR 改了哪些文件，没法确认是低风险；按高风险处理')
        out+=[(f.get('filename') or '',(f.get('status') or '').lower(),f.get('previous_filename') or '') for f in got]
        if len(got)<size:return out
    raise Blocked(f'这个 PR 改的文件超过 {MAX_FILE_PAGES} 页，没法确认全部读完；不按低风险合，按高风险处理')


def high_risk_reasons(files):
    """程序能认出的高风险改动：动 CI、密钥扫描、合并把关、规矩，或删掉测试文件。认不出的（比如碰真实数据）仍靠窗口按【按风险分级】自己判。"""
    found=[]
    for item in files:
        name,status,old=(tuple(item)+('',))[:3]
        for path in sorted({name,old}-{''}):
            for prefix,what in HIGH_RISK_PATHS:
                if path==prefix or path.startswith(prefix):found.append(what+'：'+path+('（改名 '+old+' → '+name+'）' if old else ''))
        if status in ('removed','deleted') and 'test' in name.lower():found.append('删测试文件：'+name)
        if old and old!=name and 'test' in old.lower():found.append('测试文件改名：'+old+' → '+name+'（改名后可能不再被测试发现，按高风险）')
    return found


def _has_branch(api,idn,name):
    """只有平台明确回 404 才算没有这个分支；认证、权限、限流和其他失败一律当成「查不到」，不按低风险放行。"""
    try:api(idn,'GET',f"repos/{idn['repo']}/branches/{name}");return True
    except rp.DeliveryError as error:
        if getattr(error,'code',None)==404:return False
        raise Blocked('查不到这个仓库有没有 '+name+' 分支，没法确认是低风险；按高风险处理') from None


MODE_MARK='随身开发CI模式='


def _read_job_log(idn,link):
    """读 CI 某个 job 的日志原文（Gitea）。读不到返回 None。"""
    import re
    m=re.search(r'/jobs/(\d+)',link or '')
    if not m or idn.get('platform')=='github':return None
    req=Request(idn['origin']+f"/api/v1/repos/{idn['repo']}/actions/jobs/{m.group(1)}/logs",headers=rp.gitea_headers(idn))
    try:
        with rp.urlopen(req,timeout=60) as response:return response.read().decode('utf-8','replace')
    except Exception:return None


def _repo_has_docs_gate(api,idn,base):
    """基线分支上有没有 tools/ci/docs_only.py（这个仓库接没接轻量 CI）。查不清按「接了」处理（宁可拦）。"""
    try:api(idn,'GET',f"repos/{idn['repo']}/contents/tools/ci/docs_only.py?ref={base}");return True
    except rp.DeliveryError as error:return getattr(error,'code',None)!=404


def docs_mode_check(api,idn,result,files,log_reader=None):
    """合并前再判一次「只改文档」，并和 CI 日志里的模式对账（第 77 题方案甲§3）。返回 (类别, CI 模式)；对不上就 Blocked。"""
    from .ci_docs_only import classify
    mine,_=classify(files)
    kind='文档类' if mine=='docs' else '代码类'
    if not _repo_has_docs_gate(api,idn,result.get('目标分支') or 'main'):return kind,'未接轻量CI'
    reader=log_reader or _read_job_log
    modes=set()
    import re
    pattern=re.compile(re.escape(MODE_MARK)+r'(docs|full)\b')
    for c in result.get('检查',[]):
        text=reader(idn,c.get('链接')) or ''
        # 只认「=」后面紧跟的 docs/full；日志里回显的命令行（带引号、带 ${{ }}）不算（11-16 验证 PR108/109 发现读成了 full"）
        modes.update(m.group(1) for m in pattern.finditer(text))
    if not modes:raise Blocked('这个仓库已接轻量 CI，但读不到这次 CI 的模式记录（日志里没有「'+MODE_MARK+'」），本次没有合并：CI 可能被改过或日志丢了，要人来看')
    if mine=='full' and modes!={'full'}:raise Blocked('这个 PR 改了文档以外的文件，但 CI 按「只改文档」跑的（'+'、'.join(sorted(modes))+'），全量测试没跑，本次没有合并：CI 判错了或被改过，要人来看')
    return kind,'、'.join(sorted(modes))


def merge(store,project,number,confirm=False,wait_first=False,timeout=3600,api=None,merger=None,sleep=time.sleep,clock=time.monotonic,no_run_minutes=10,now=time.time,risk=None,approval='',log_reader=None):
    if not confirm:raise Blocked('合并要加 --confirm：低风险改动 CI 通过就可以加；高风险的等用户明确说合以后再加（见【按风险分级】）')
    if risk not in ('低','高'):raise Blocked('合并要写 --risk 低 或 --risk 高（见【按风险分级】）；拿不准就按高')
    if risk=='高' and not (approval or '').strip():raise Blocked('高风险合并要写 --approval "<用户点头的原话和出处>"，没有用户点头就不合')
    api=api or rp.api
    result=wait(store,project,number,timeout,api=api,sleep=sleep,clock=clock,no_run_minutes=no_run_minutes,now=now) if wait_first else check(store,project,number,api,no_run_minutes,now)
    if result['结论']!=PASS:
        result['已合并']=result['结论']=='已合并';result['本次合并']=False
        return result
    idn=_identity(_project(store,project));sha=result['头提交']
    if risk=='低':
        base=result.get('目标分支') or ''
        if base in ('main','master') and _has_branch(api,idn,'development'):
            raise Blocked('这个仓库有 development 分支，合进 '+base+' 按高风险（见【按风险分级】），本次没有合并：用户点头后用 --risk 高 --approval "<原话和出处>" 再合')
        files=_changed_files(api,idn,number)
        reasons=high_risk_reasons(files)
        if reasons:raise Blocked('这个 PR 不是低风险（'+'；'.join(reasons[:5])+'），本次没有合并：给用户看过、用户点头后用 --risk 高 --approval "<原话和出处>" 再合')
    if risk!='低':files=_changed_files(api,idn,number)
    kind,ci_mode=docs_mode_check(api,idn,result,files,log_reader)
    if merger:merger(idn,number,sha)
    elif idn['platform']=='github':_merge_github(api,idn,number,sha)
    else:_merge_gitea(idn,number,sha)
    after=api(idn,'GET',f"repos/{idn['repo']}/pulls/{int(number)}")
    if not after.get('merged'):raise Blocked('合并请求已发出，但读回 PR 还没显示已合并；先 ci check 再看，不要直接重发')
    record={'时间':time.strftime('%Y-%m-%d %H:%M:%S'),'仓库':idn['repo'],'PR':int(number),'头提交':sha,
            '合并提交':after.get('merge_commit_sha') or after.get('merged_commit_id') or '','检查':[c['名称']+'='+c['结果'] for c in result['检查']],
            '风险':risk,'用户点头':(approval or '').strip(),'类别':kind,'CI模式':ci_mode}
    store.put(LOG,(store.get(LOG,[])+[record])[-200:])
    out=dict(result,**{'本次合并':True,'已合并':True,'合并提交':record['合并提交'],'说明':'检查全部通过，已合并并读回确认。','类别':kind,'CI模式':ci_mode})
    if kind=='代码类':out['提醒']='记得另开文档 PR 更新 STATUS.md、功能清单.md（见【收工必写】）'
    return out


def run(args):
    from .core import Store
    s=Store(args.root)
    try:
        n=args.no_run_minutes
        if args.action=='check':return check(s,args.project,args.pr,no_run_minutes=n)
        if args.action=='wait':return wait(s,args.project,args.pr,args.timeout,no_run_minutes=n)
        if args.action=='merge':return merge(s,args.project,args.pr,args.confirm,args.wait,args.timeout,no_run_minutes=n,risk=args.risk,approval=args.approval or '')
        return {'合并记录':s.get(LOG,[])[-20:]}
    finally:s.db.close()


def register(subparser):
    p=subparser.add_parser('ci',help='查 CI：check <项目> <PR号>；等 CI 跑完：wait；合并：merge --confirm --risk 低|高 [--approval 原话出处] [--wait]；看合并记录：log')
    p.add_argument('action',choices=['check','wait','merge','log'])
    p.add_argument('project',nargs='?');p.add_argument('pr',nargs='?',type=int)
    p.add_argument('--timeout',type=int,default=3600,help='最多等几秒，默认 3600')
    p.add_argument('--wait',action='store_true',help='merge 前先等检查跑完')
    p.add_argument('--confirm',action='store_true',help='可以合：低风险 CI 通过，或高风险用户已点头')
    p.add_argument('--risk',choices=['低','高'],help='merge 必填：按【按风险分级】判的档；低风险时程序会再查改动文件，碰 CI、规矩、合并把关或删测试的不让按低风险合')
    p.add_argument('--approval',help='高风险必填：用户点头的原话和出处')
    p.add_argument('--no-run-minutes',type=int,default=10,help='从第一次查到没有任何检查起，超过几分钟就报「CI 没触发」，默认 10')
    p.set_defaults(ci_run=_checked(run));return p


def _checked(fn):
    def inner(args):
        if args.action!='log' and (not args.project or args.pr is None):raise Blocked('要写项目和 PR 号：ci '+args.action+' <项目> <PR号>')
        return fn(args)
    return inner
