"""本机设置：每个用户自己的名字、战队、项目目录、项目名单和附加规则。

发行包里不写死任何人的路径或名字；这些都在 数据/本机设置.json（本人 600），由首次配置或 settings 命令写入。
本机附加规则另生成 数据/本机规则.md，公共协议里写明「存在时一并遵守」。
"""
import json
import os
from pathlib import Path
from .core import Blocked, atomic

FILE='数据/本机设置.json'
RULES='数据/本机规则.md'
DISPATCH_MODES=('host','t3','team','solo')
DEFAULTS={'owner_name':'用户','team_name':'AI 战队','workspace':'','projects':None,'extra_rules':[],'code_hosting':'local','hosting_url':'','team_enabled':False,'billing':'subscription','cost_limit_yuan':20,'monthly_budget_yuan':0,'backup_dir':'','team_entry':'','talk_style':'','auto_continue':None,'auto_continue_hours':2,
          'dispatch_mode':None,'plan_only':None,'max_parallel':4,'t3_sub_provider':''}
KEYS=tuple(DEFAULTS)

def path(store):return Path(store.root)/FILE

def load(store):
    p=path(store)
    if not p.is_file():return dict(DEFAULTS)
    if p.is_symlink():raise Blocked('本机设置不能是链接')
    value=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(value,dict):raise Blocked('本机设置格式不对')
    return {**DEFAULTS,**{k:v for k,v in value.items() if k in KEYS}}

def _check(key,value):
    if key not in KEYS:raise Blocked('未知设置项：'+key+'；可设：'+'、'.join(KEYS))
    if key in ('owner_name','team_name','workspace') and not isinstance(value,str):raise Blocked(key+' 要是文字')
    if key=='code_hosting' and value not in ('local','remote'):raise Blocked('code_hosting 只能是 local（只存本机）或 remote（推到在线代码仓库）')
    if key=='hosting_url' and not isinstance(value,str):raise Blocked('hosting_url 要是文字')
    if key=='team_enabled' and not isinstance(value,bool):raise Blocked('team_enabled 要是 true 或 false')
    if key=='dispatch_mode' and value is not None and value not in DISPATCH_MODES:raise Blocked('dispatch_mode 只能是 host、t3、team、solo 或 null（未选）')
    if key=='plan_only' and value is not None and not isinstance(value,bool):raise Blocked('plan_only 要是 true、false 或 null（按派工方式默认）')
    if key=='max_parallel' and (type(value) is not int or value<1):raise Blocked('max_parallel 要是不小于 1 的整数')
    if key=='t3_sub_provider' and (not isinstance(value,str) or '\n' in value or '\r' in value):raise Blocked('t3_sub_provider 要是一行文字，可以留空')
    if key=='billing' and value not in ('subscription','metered'):raise Blocked('billing 只能是 subscription（包月/不另花钱）或 metered（按量计费）')
    if key in ('cost_limit_yuan','monthly_budget_yuan') and (isinstance(value,bool) or not isinstance(value,(int,float)) or value<0):raise Blocked(key+' 要是不小于 0 的数字（元）')
    if key=='backup_dir' and not isinstance(value,str):raise Blocked('backup_dir 要是文字')
    if key=='team_entry' and (not isinstance(value,str) or '\n' in value):raise Blocked('team_entry 要是一行文字：战队的派工命令')
    if key=='talk_style' and value not in ('','带编号','大白话'):raise Blocked('talk_style 只能是 带编号（回复里带提交号、PR 这些编号）或 大白话（只说结论，编号写进记录）；留空=带编号')
    if key=='auto_continue' and value is not None and not isinstance(value,bool):raise Blocked('auto_continue 要是 true（打开自动接着做）、false（关掉）或 null（按版本默认：朋友版开、完整版关）')
    if key=='auto_continue_hours' and (isinstance(value,bool) or not isinstance(value,(int,float)) or value<=0):raise Blocked('auto_continue_hours 要是大于 0 的数字（进度表几小时没更新才叫）')
    if key=='extra_rules' and (not isinstance(value,list) or any(not isinstance(r,str) or not r.strip() for r in value)):raise Blocked('extra_rules 要是一组非空文字')
    if key=='projects' and value is not None:
        if not isinstance(value,list):raise Blocked('projects 要是项目列表')
        for e in value:
            if not isinstance(e,dict) or any(not e.get(k) for k in ('id','name','path')):raise Blocked('每个项目要有 id、name、path')
        if len({e['id'] for e in value})!=len(value):raise Blocked('项目编号重复')

def auto_continue_on(store):
    """自动接着做开没开：本机设置写了就按设置；没写按版本——朋友版（发行.json 里 edition 是 friend）默认开，完整版默认关（见 DECISIONS 2026-11-18）。"""
    value=load(store).get('auto_continue')
    if isinstance(value,bool):return value
    try:return json.loads((Path(store.root)/'发行.json').read_text(encoding='utf-8')).get('edition')=='friend'
    except (OSError,ValueError,AttributeError):return False

def save(store,value):
    for k,v in value.items():_check(k,v)
    mode=value.get('dispatch_mode')
    if mode is not None:
        value={**value,'team_enabled':mode=='team'}
    p=path(store);p.parent.mkdir(parents=True,exist_ok=True)
    atomic(p,json.dumps({k:value[k] for k in KEYS if k in value},ensure_ascii=False,indent=2)+'\n')
    os.chmod(p,0o600)
    render_rules(store,value)
    return load(store)

def apply_changes(current,changes):
    """合并显式修改，兼容旧入口写入的战队开关。"""
    for key,value in changes.items():_check(key,value)
    updated={**current,**changes}
    if 'team_enabled' in changes and 'dispatch_mode' not in changes:
        if changes['team_enabled']:
            updated['dispatch_mode']='team'
            updated['plan_only']=False
        elif current.get('dispatch_mode')=='team':
            updated['dispatch_mode']='solo'
    return updated

def set_value(store,key,value):
    return save(store,apply_changes(load(store),{key:value}))

def effective_dispatch(value):
    """未选派工方式时沿用旧行为，不启用只规划审核。"""
    mode=value.get('dispatch_mode')
    if mode is None:
        return ('team' if value.get('team_enabled') else 'solo'),False
    return mode,mode!='solo' and value.get('plan_only') is not False

TEAM_RULES=["【战队续接】有未完战队任务时，使用新会话真实身份，先从正式入口只读核对原任务号、归属、当前运行和接任条件。归属相同且可续做时沿原入口继续，不额外套用仅转交适用的条件；需换归属时，复用已有明确接任授权，按项目流程登记并正式转交、读回，再继续同一任务。用户明确要求工程续做且条件已满足时，当回合执行续做，不能停在预检报告。不冒用旧 caller、不手改任务状态、不另派重复任务；原任务仍在运行时不并发续做，不为提高战队利用率恢复暂停项目。已有接任授权不重复问；确实缺授权、存在冲突写入或入口不支持时，给出确切缺项并继续不依赖它的工作。运行中的工人不重启。", "【交接包·战队】交接时另写：未完战队任务的原任务号、最后核对的归属/运行状态、候选路径、续接或验收入口、是否已正式转交及读回证据；发出去还没回的消息写对象 + 时间 + clientRequestId。"]

def _dispatch_rule(value,mode,plan_only):
    if mode=='solo':
        if value['dispatch_mode'] is None:
            return '没有接入 AI 战队：由当前对话里的 AI 自己动手做，不派工、不提战队。'
        return '由当前对话里的 AI 自己动手做，大活按块顺序做，不派工。'

    verify='帮手自报通过不算数，窗口亲自重跑测试、看改动后才合。'
    if mode=='host':
        rule='用 AI 工具自带的子 Agent 派工。派活单写清开发分支和验收标准。'+verify+'宿主不支持后台运行时，按块等结果。'
    elif mode=='t3':
        provider=value['t3_sub_provider'].strip()
        model=(f'子会话默认用「{provider}」，与主会话用不同家的模型。' if provider
               else '同一家模型，验收时多看一眼。')
        rule=('用 T3 的 delegate_task 派给子会话。'+model
              +'派活单第一句写明先切到最新开发分支，子会话不提交主分支。'
              +f'同时在跑的子会话不超过 {value["max_parallel"]} 个。'
              +'一家派不出去改派另一家并告诉用户一句；都不行就报用户。'+verify)
    else:
        work='把活' if plan_only else '一个窗口能做完的自己写；大活'
        rule=f'已接入「{value["team_name"]}」：{work}拆成互不冲突的块，同时并行派给它（见公共协议【派工】），派工走战队入口。'
        if value['team_entry']:
            rule+=f'派工入口命令：{value["team_entry"]}（第一次用先看它的帮助，再按帮助派工；放后台运行，见【等不无限】）。'
        if plan_only:
            rule+=verify
    if plan_only:
        rule+='窗口只规划审核：读现状、定方案、拆块、写规格和验收标准、派工、审查验收、把关合并、记台账和汇报，其余全部派出去；派不出去报用户，不改回自己写。'
        if mode in ('host','t3'):
            rule+='告诉用户可以说一句换派工方式或关掉只规划审核（settings set dispatch_mode … / settings set plan_only false）。'
    return rule

def render_rules(store,value=None):
    value=load(store) if value is None else {**DEFAULTS,**value}
    p=Path(store.root)/RULES
    lines=['# 本机附加规则','','> 由 settings 命令生成，只对本机生效；改规则请用 settings，不手改本文件。','']
    lines+=[f'- 用户称呼：{value["owner_name"]}；战队名称：{value["team_name"]}'+(f'；项目默认放在 {value["workspace"]}' if value['workspace'] else '')]
    default='推到在线仓库'+(f'（{value["hosting_url"]}）' if value['hosting_url'] else '') if value['code_hosting']=='remote' else '只存本机'
    lines+=['- 【本机流程】每个项目按它自己的情况走：项目已经连着在线仓库（git remote 里有在线仓库地址，如 GitHub、Gitea），就推分支开 PR → CI 全绿 → 按公共协议【按风险分级】合并（低风险自己审、CI 通过就合；高风险给用户看成品、点头后合），不改它原来连的仓库；没连在线仓库的，代码只存本机：开分支 → 改代码 → 本地测试 → 同样按【按风险分级】合进本地 main（高风险等用户说「可以」）。新建项目默认'+default+'。',
            '- 【在线仓库账号】GitHub 等在线仓库的登录由用户本人完成，AI 不碰密码；私有仓库第一次开自动检查（如 GitHub Actions）可能超出免费额度，先问用户。']
    mode,plan_only=effective_dispatch(value)
    lines+=['- 【本机派工】'+_dispatch_rule(value,mode,plan_only)]
    if value['billing']=='metered':
        budget=f'，或本月累计会超过 {value["monthly_budget_yuan"]} 元' if value['monthly_budget_yuan'] else ''
        lines+=[f'- 【本机花费】用的 AI 或服务按量计费：会花钱的操作先用 cost check 估算；单次超过 {value["cost_limit_yuan"]} 元{budget}，先问用户，点头后再做，做完用 cost record 记一笔。']
    else:
        lines+=['- 【本机花费】用的是包月或不另花钱的工具，日常开发不用估算；要新开收费服务、买东西时仍要先问用户。']
    if value.get('talk_style'):lines+=[f'- 【说话方式】{value["talk_style"]}']
    if mode=='team':lines+=['- '+r for r in TEAM_RULES]
    lines+=['- '+r for r in value['extra_rules']]
    atomic(p,'\n'.join(lines)+'\n')
    return str(p)

def current_registrations(store):
    """当前全部登记：名单里的项目（带进度文件位置）加上只在本机 register 过的项目。"""
    from .board import load as registry
    entries=list(registry(store));known={e['id'] for e in entries}
    for row in store.db.execute('SELECT p.id,p.name,(SELECT path FROM paths WHERE project=p.id ORDER BY device=? DESC LIMIT 1) AS path FROM projects p',(store.device,)).fetchall():
        if row['id'] not in known and row['path']:
            r=row['path'];entries.append({'id':row['id'],'name':row['name'],'path':r,'status_path':r+'/STATUS.md','features_path':r+'/功能清单.md','prd_path':r+'/docs/PRD.md'})
    return entries

def init_from_registry(store):
    """首次切到本机设置：把当前已登记的项目原样记进设置，不增不减。已有名单时不动。"""
    current=load(store)
    if current['projects'] is None:
        current['projects']=current_registrations(store)
        return save(store,current)
    return current

def keep_local_registrations(store):
    """已启用本机名单时补入新登记项目，保留名单里已有的条目。"""
    if not path(store).is_file():return []
    current=load(store);entries=current['projects']
    if not isinstance(entries,list):return []
    known={e['id'] for e in entries}
    added=[e for e in current_registrations(store) if e['id'] not in known]
    if added:
        current['projects']=entries+added
        save(store,current)
    return [e['name'] for e in added]

def projects(store):
    """registry-apply 的默认名单：本机设置里的项目；还没设置时沿用当前全部登记（等于不变）。"""
    value=load(store)['projects']
    return current_registrations(store) if value is None else value

def hosting_of(path):
    """项目连着哪个在线仓库：优先 origin，否则读第一个远端，去掉地址里的账号密码；没连就是 local。"""
    import re,subprocess
    remotes=subprocess.run(['git','-C',str(path),'remote'],capture_output=True,text=True)
    names=remotes.stdout.splitlines()
    if remotes.returncode or not names:return 'local'
    remote='origin' if 'origin' in names else names[0]
    r=subprocess.run(['git','-C',str(path),'remote','get-url',remote],capture_output=True,text=True)
    url=r.stdout.strip()
    if r.returncode or not url:return 'local'
    return re.sub(r'(://)[^/@]+@',r'\1',url)
