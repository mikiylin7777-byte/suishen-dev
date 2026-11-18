"""SD-29 花费提醒：按量计费时先估算，超过本机设置的上限就先问用户。

不内置任何模型价格（价格会变，写死就是编造）；估算用用户或服务商给的单价。
账本只记金额和用途，不记账号、密钥。
"""
import time
from .core import Blocked
from . import settings

LEDGER='cost:ledger'

def estimate(tokens_in,tokens_out,price_in,price_out):
    """按「每百万 token 多少元」估算一次调用的花费（元）。"""
    for v in (tokens_in,tokens_out,price_in,price_out):
        if isinstance(v,bool) or not isinstance(v,(int,float)) or v<0:raise Blocked('估算参数要是不小于 0 的数字')
    return round((tokens_in*price_in+tokens_out*price_out)/1_000_000,4)

def _month():return time.strftime('%Y-%m')

def spent(store,month=None):
    month=month or _month()
    return round(sum(r['yuan'] for r in store.get(LEDGER,[]) if r['month']==month),4)

def check(store,yuan,what=''):
    """会花钱之前问一次：要不要先问用户。"""
    if isinstance(yuan,bool) or not isinstance(yuan,(int,float)) or yuan<0:raise Blocked('预估金额要是不小于 0 的数字（元）')
    cfg=settings.load(store);used=spent(store)
    if cfg['billing']!='metered':
        return {'先问用户':False,'原因':'本机设置为包月/不另花钱；新开收费服务仍要先问','预估':yuan}
    reasons=[]
    if yuan>cfg['cost_limit_yuan']:reasons.append(f'单次预估 {yuan} 元，超过上限 {cfg["cost_limit_yuan"]} 元')
    if cfg['monthly_budget_yuan'] and used+yuan>cfg['monthly_budget_yuan']:reasons.append(f'本月已花 {used} 元，加上这次会超过月预算 {cfg["monthly_budget_yuan"]} 元')
    return {'先问用户':bool(reasons),'原因':'；'.join(reasons) or '在上限以内，可以直接做','预估':yuan,'本月已花':used,'用途':what}

def record(store,yuan,what):
    if isinstance(yuan,bool) or not isinstance(yuan,(int,float)) or yuan<0:raise Blocked('金额要是不小于 0 的数字（元）')
    if not what or not what.strip():raise Blocked('要写清这笔钱花在什么上')
    rows=store.get(LEDGER,[]);rows.append({'month':_month(),'yuan':yuan,'what':what.strip()[:200],'at':time.time()})
    store.put(LEDGER,rows[-2000:])
    return {'记下':yuan,'本月已花':spent(store)}

def show(store):
    cfg=settings.load(store)
    return {'计费方式':'按量计费' if cfg['billing']=='metered' else '包月/不另花钱','单次上限':cfg['cost_limit_yuan'],'月预算':cfg['monthly_budget_yuan'] or '不设','本月已花':spent(store),
            '最近':[{k:r[k] for k in ('month','yuan','what')} for r in store.get(LEDGER,[])[-10:]]}

def run(args):
    from .core import Store
    s=Store(args.root)
    try:
        if args.action=='show':return show(s)
        if args.action=='estimate':
            return {'预估元':estimate(args.tokens_in,args.tokens_out,args.price_in,args.price_out)}
        if args.yuan is None:raise Blocked('要给出金额：--yuan 数字')
        if args.action=='check':return check(s,args.yuan,args.what or '')
        return record(s,args.yuan,args.what or '')
    finally:s.db.close()

def register(subparser):
    p=subparser.add_parser('cost',help='花费提醒：check --yuan 预估 / record --yuan 金额 --what 用途 / show / estimate')
    p.add_argument('action',choices=['check','record','show','estimate'])
    p.add_argument('--yuan',type=float);p.add_argument('--what')
    for k in ('tokens-in','tokens-out','price-in','price-out'):p.add_argument('--'+k,type=float,default=0)
    p.set_defaults(cost_run=run);return p
