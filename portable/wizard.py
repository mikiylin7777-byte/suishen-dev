"""SD-27 首次配置向导：大白话问几件事就装好，接着进入项目搬家。

两种用法：
- AI 对话里：AI 用大白话问完，把答案写成 JSON，跑 wizard --answers 文件；
- 双击 开始使用.command：没配置过时在终端里一问一答。
只改三处：本机设置、本机规则、AI 工具的全局规则文件里加两行指向（原文件先备份，卸载时只删这两行）。
"""
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from .core import Blocked, atomic, user_home as _home
from . import settings

HOOKS='wizard:hooks'
DONE='wizard:done'
# 各 AI 工具自动读的全局规则文件。Claude 会展开 @文件；Codex、opencode 不展开，写成一句让它先去读的话。
TOOLS={'claude':'.claude/CLAUDE.md','codex':'.codex/AGENTS.md','opencode':'.config/opencode/AGENTS.md'}

def hook_lines(store,tool='claude'):
    root=Path(store.root).absolute()
    if tool=='claude':return [f'@{root}/工具/开发协议.md',f'@{root}/数据/本机规则.md']
    return [f'【随身开发】每次开工前先用读文件工具读完并遵守这两个文件：{root}/工具/开发协议.md 和 {root}/数据/本机规则.md。']

def all_hook_lines(store):
    """卸载时要删的全部行：各工具的写法都算（含早期在 Codex 里写的 @ 行）。"""
    return {l for t in TOOLS for l in hook_lines(store,t)}|set(hook_lines(store,'claude'))

def detect_tools(home=None):
    home=home or _home()
    return [t for t,rel in TOOLS.items() if (home/rel).parent.is_dir()]

def risky_location(root,home=None):
    """随身开发装在容易被清掉的地方（下载、桌面、废纸篓）时给一句提醒；放得好就返回空。
    2026-11-16 外部用户把它解压在「下载」里就直接用了，备份也跟着放在「下载」。"""
    home=Path(home or _home()).resolve();root=Path(root).resolve()
    for name,label in (('Downloads','下载'),('Desktop','桌面'),('.Trash','废纸篓')):
        base=home/name
        if root==base or base in root.parents:
            return '随身开发装在「'+label+'」文件夹里（'+str(root)+'），这里常被当垃圾一起清掉，连备份也会丢；建议挪到个人文件夹 ~/随身开发'
    return ''

def connect_tools(store,tools,home=None):
    """在 AI 工具的全局规则文件里加两行指向随身开发；已有就不重复加。"""
    home=home or _home();done=[]
    record={h['file']:h for h in store.get(HOOKS,[])}
    for t in tools:
        if t not in TOOLS:raise Blocked('不认识的 AI 工具：'+t+'；目前支持 '+'、'.join(TOOLS))
        f=home/TOOLS[t];lines=hook_lines(store,t)
        if f.is_symlink():raise Blocked('规则文件是链接，不改：'+str(f))
        old=f.read_text(encoding='utf-8') if f.exists() else None
        have=set((old or '').splitlines())
        add=[l for l in lines if l not in have]
        entry=record.get(str(f),{'file':str(f),'created':old is None,'added':[]})
        if add:
            if old is not None:
                keep=store.root/'数据/接入备份';keep.mkdir(parents=True,exist_ok=True)
                shutil.copy2(f,keep/(t+'-'+time.strftime('%Y%m%d-%H%M%S')+'-'+f.name))
            f.parent.mkdir(parents=True,exist_ok=True)
            text=(old or '')
            if text and not text.endswith('\n'):text+='\n'
            atomic(f,text+''.join(l+'\n' for l in add))
            entry['added']=sorted(set(entry['added'])|set(add))
        record[str(f)]=entry;done.append({'工具':t,'文件':str(f),'新加':len(add)})
    store.put(HOOKS,list(record.values()))
    return done

QUESTIONS=[
    ('owner_name','怎么称呼你','用户'),
    ('workspace','你的项目一般放在哪个文件夹（回车用家目录）','~'),
    ('code_hosting','代码只存本机，还是也推到网上的代码仓库？推到网上换电脑最省事（回车=只存本机；填 remote=推到网上）','local'),
    ('team_enabled','有没有 AI 战队帮你干活？（回车=没有；填 有）','没有'),
    ('billing','你用的 AI 是包月，还是按用量花钱？（回车=包月；填 按量）','包月'),
]

def ask():
    """终端一问一答；只问业务问题，不问技术细节。"""
    out={}
    for key,q,default in QUESTIONS:
        out[key]=input(q+'：').strip() or default
    if out['code_hosting']=='remote':out['hosting_url']=input('网上仓库的地址（回车以后再填）：').strip()
    if out['billing']=='按量':
        raw=input('单次超过多少元要先问你（回车=20）：').strip()
        out['cost_limit_yuan']=float(raw) if raw else 20
    return out

def normalize(answers):
    a=dict(answers)  # 只换算用户答了的项；没答的不碰，免得把已有设置改回默认
    if 'team_enabled' in a:
        if a['team_enabled'] in ('有','是','yes','true'):a['team_enabled']=True
        elif a['team_enabled'] in ('没有','否','no','false','',None):a['team_enabled']=False
    if 'billing' in a:
        if a['billing'] in ('按量','按量计费'):a['billing']='metered'
        elif a['billing'] in ('包月','',None):a['billing']='subscription'
    if 'code_hosting' in a:
        if a['code_hosting'] in ('只存本机','',None):a['code_hosting']='local'
        elif a['code_hosting'] in ('网上','在线'):a['code_hosting']='remote'
    unknown=set(a)-set(settings.KEYS)-{'tools'}
    if unknown:raise Blocked('向导不认识这些答案：'+'、'.join(sorted(unknown)))
    return a

def _edition(store):
    try:return json.loads((Path(store.root)/'发行.json').read_text(encoding='utf-8')).get('edition','full')
    except (OSError,ValueError):return 'full'

def run_wizard(store,answers,home=None,scan=True):
    home=home or _home();a=normalize(answers)
    tools=a.pop('tools',None)
    if tools is None:tools=detect_tools(home)
    current=settings.load(store)
    if current['projects'] is None:current['projects']=settings.current_registrations(store)
    first=not settings.path(store).is_file()
    for k,v in a.items():settings._check(k,v);current[k]=v
    # 朋友版、对外版第一次配置默认大白话；升级、完整版不改，没写就是带编号
    if first and 'talk_style' not in a and _edition(store)=='friend':current['talk_style']='大白话'
    settings.save(store,current)
    connected=connect_tools(store,tools,home)
    found=[]
    if scan:
        from .move_in import scan as move_scan
        ws=Path(os.path.expanduser(current['workspace'])) if current['workspace'] else home
        try:found=[{'名字':p['名字'],'位置':p['位置'],'多久没动':p['多久没动']} for p in move_scan(store,[ws] if ws.is_dir() else None) if p['建议勾选']][:20]
        except Blocked:found=[]
    from . import supervisor,board
    cfg=store.get('runner:config',{})
    if not cfg.get('backup_dir'):
        supervisor.configure_local(store,{**cfg,'backup_dir':str(store.root.parent/'随身开发备份')})
    reminders=[]
    try:
        if sys.platform!='darwin':raise Blocked('这台电脑不是 Mac，暂不支持自动安装后台')
        status=supervisor.service(store.root,home/'Library/LaunchAgents','install',execute=True)
        store.put('runner:service',status)
        if not status.get('running'):raise Blocked('后台还没有启动成功')
    except Exception as error:
        store.put('runner:service',{'running':False})
        if sys.platform!='darwin':reason='这台电脑不是 Mac'
        elif isinstance(error,FileNotFoundError):reason='找不到系统的后台管理工具'
        elif isinstance(error,PermissionError):reason='系统没有允许写入或启动后台服务'
        elif isinstance(error,subprocess.TimeoutExpired):reason='系统启动后台等待太久，没有确认成功'
        elif isinstance(error,Blocked):reason=str(error)
        else:reason='后台服务加载失败，没有确认启动成功'
        reminders.append('本机没有自动备份，原因：'+reason+'；可以说「帮我开后台」再试')
    place=risky_location(store.root,home)
    if place:reminders.insert(0,place)
    if board.load(store):board.overview(store,sync=True)
    store.put(DONE,{'at':time.time()})
    nxt='先把下面这张清单给用户看，等用户说要搬哪几个再动手（没列出来的，问用户在哪个文件夹）；用户点名后 AI 用 move-in adopt --confirmed 原地登记' if found else '说「建一个示例项目带我走一遍」，五分钟看一遍完整流程'
    return {'提醒':reminders,'已配置':True,'称呼':current['owner_name'],'代码存放':'推到网上仓库' if current['code_hosting']=='remote' else '只存本机',
            'AI 战队':'已接入' if current['team_enabled'] else '没有（AI 自己动手）','计费':'按量（超额先问）' if current['billing']=='metered' else '包月',
            '接入的 AI 工具':connected,'建议搬进来的项目':found,'下一步':nxt,
            '你的启动语':'请先读「'+str(store.root/'AGENTS.md')+'」，按里面的规矩当我的开发助手；我不懂代码，用大白话跟你说需求。'}

def needed(store):
    return not store.get(DONE) and not settings.path(store).is_file()

def run(args):
    from .core import Store
    s=Store(args.root)
    try:
        if args.if_needed and not needed(s):return {'已配置':True,'跳过':True}
        if args.answers:
            answers=json.loads(sys.stdin.read() if args.answers=='-' else Path(args.answers).read_text(encoding='utf-8'))
            if not isinstance(answers,dict):raise Blocked('答案要是 JSON 对象')
        elif args.show_questions:
            return {'要问的事':[{'项':k,'问法':q,'默认':d} for k,q,d in QUESTIONS],'说明':'AI 用大白话问完，把答案写成 JSON 交给 wizard --answers'}
        elif sys.stdin.isatty():answers=ask()
        else:raise Blocked('没有答案：在 AI 对话里先问清楚，再用 wizard --answers 答案.json')
        return run_wizard(s,answers,scan=not args.no_scan)
    finally:s.db.close()

def register(subparser):
    p=subparser.add_parser('wizard',help='首次配置向导：--show-questions 看要问什么 / --answers 答案.json / 终端里直接一问一答')
    p.add_argument('--answers');p.add_argument('--show-questions',action='store_true');p.add_argument('--if-needed',action='store_true');p.add_argument('--no-scan',action='store_true')
    p.set_defaults(wizard_run=run);return p
