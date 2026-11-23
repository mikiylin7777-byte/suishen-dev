"""子会话按档位选模型与额度协调（方案 docs/plans/方案-子会话选模型与额度协调-20261123.md 第 3、4 节的程序部分）。

派活窗口在派活入口前调这里，全部输出 JSON：

  quota show                          # 台账、被挡的池、简报一行
  quota pick --tier 重 --writer-vendor openai [--task T] [--available a,b]
  quota record --vendor X --model Y --pool Z --error "原文" [--task T]
  quota occupy --task T [--limit N] [--statuses '{"task":"ended"}']
  quota release --task T
  quota clear --pool P                # 小探测单确认恢复后放行一个池
  quota takeover-check --error "原文" # 识别主会话撞 5 小时额度
  quota takeover --window W [--start S] [--end E] [--model M] [--vendor V]
  quota show

要点（型号一律放设置里的 quota 档位表，不写死在代码里）：
- 额度台账 数据/额度台账.json 全机共用；只记家/模型/池/时间/类型/报错讲额度那一句/任务号；
  入账前先过密钥扫描；讲额度的那一句本身是环境故障（如 host executable was not found）才不入账；
  恢复时间只认报错原文，没写记「未知」，余额类记「待充值」；到点只允许下一次新派活重新选，不续跑旧单。
- Codex 剩余额度读 ~/.codex/sessions 里带 rate_limits 的那份记录（过期看这份文件的修改时间，
  或读数行上的时间戳；不看目录里别的更新的文件）。剩余低于门槛挡掉配置里 codex_pools 的池；
  读数超过 stale_hours 小时，或 resets_at 已到，按未知、不挡池。
- 选家：档位首选→备选，跳过被挡的池，包月优先。公开默认不带任何按量授权。
  包月全被挡且配置了按量出口时，普通/重活先走该出口（记账并返回要告诉用户的提示），
  它也不可用时普通活排队、重活报用户；没配则轻活和普通活排队、重活报用户。
  写代码档不选 Anthropic 家；审查档与写代码的厂商不同，先后只看配置里的首选顺序，
  只在没有包月可用时才用按量，用了同样记账并提示。程序不写死任何一条线路；
  配置里删掉某几行后，用剩下还能用的家。按量型号只允许 deepseek/*。claude-api-active 不进子会话池。
- 全机名额：查上限与占名额在同一把文件锁里原子完成；占位带任务号和时间；
  失败或结束 release；派活窗口崩溃的占位按任务号查状态，已结束回收，查不到且超 24 小时也回收。
- 主会话临时接管只做两件：识别「5 小时额度」报错、记接管台账；改哪个窗口由总管在 T3 里操作。
"""
import calendar
import json
import re
import time
from pathlib import Path

from .core import Blocked, atomic, canonical, lock, safe, user_home
from .privacy import redact_text

TIERS = ('重', '普通', '轻', '审查')
CODE_TIERS = ('重', '普通', '轻')
EXCLUDED_SUBSESSION = {'claude-api-active'}
LEDGER = '数据/额度台账.json'
STALE_DAYS_MS = 24 * 3600
ENDED_STATES = ('ended', 'complete', 'completed', 'failed', 'cancelled', 'canceled', 'archived', 'done')
RUNNING_STATES = ('running', 'active', 'in_progress')

# 只认明确的额度说法。泛化的 exceeded / insufficient 不算。
QUOTA_WORD = re.compile(r'额度|限流|余额|充值|quota|rate.?limit|usage limit|'
                        r'not included in your weekly usage|weekly usage|'
                        r'insufficient\s+(?:balance|quota|credits?)|'
                        r'over limit|limit reached|\b429\b|\b402\b', re.I)
ENV_WORD = re.compile(r'host executable was not found|executable was not found|command not found|'
                      r'no such file or directory|enoent', re.I)
BALANCE_WORD = re.compile(r'余额|充值|insufficient\s+balance|balance\s+insufficient|\b402\b|billing', re.I)
FIVE_HOUR = re.compile(r'5[\s-]?hours?|5 小时|五小时|five[\s-]?hours?', re.I)
LIMIT_WORD = re.compile(r'usage limit|限额|额度|limit', re.I)
TIME_RX = re.compile(r'(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?[T ](\d{1,2}):(\d{2})(?::(\d{2}))?')

METERED_NOTE = '这一单走了按量直连 DeepSeek，要花钱；已记台账，请告诉用户一句。'

DEFAULT_QUOTA = {
    'gpt_floor_percent': 40,   # GPT 剩余低于这个百分比就挡池（门槛定 40）
    'stale_hours': 6,          # Codex 读数超过几小时没更新算未知
    'machine_max': 20,         # 全机同时派单上限
    'codex_sessions_dir': '',  # 留空按 ~/.codex/sessions
    'codex_pools': [],         # 共用 GPT 周额度的池，读数吃紧时一起挡
    'priority_project': '',    # 排队时优先放行的项目；留空表示不优先。本机要保哪个项目由设置写，不写死
    'metered_deepseek': [],  # 公开默认不带任何按量授权，也不写本机型号
    'tiers': {
        '重': {'preferred': [{'model': 'default', 'vendor': 'unknown', 'pool': 'default', 'billing': 'subscription'}], 'backup': []},
        '普通': {'preferred': [{'model': 'default', 'vendor': 'unknown', 'pool': 'default', 'billing': 'subscription'}], 'backup': []},
        '轻': {'preferred': [{'model': 'default', 'vendor': 'unknown', 'pool': 'default', 'billing': 'subscription'}], 'backup': []},
        '审查': {'preferred': [{'model': 'default', 'vendor': 'unknown', 'pool': 'default', 'billing': 'subscription'}], 'backup': []},
    },
}


# ---------- 配置校验（settings 的 quota 项用同一套） ----------

def check_config(value):
    if not isinstance(value, dict):
        raise Blocked('quota 要是对象（JSON 对象）')
    for key in value:
        if key not in DEFAULT_QUOTA:
            raise Blocked('quota 里有未知字段：' + str(key))
    floor = value.get('gpt_floor_percent')
    if type(floor) is not int or not 0 <= floor <= 100:
        raise Blocked('quota.gpt_floor_percent 要是 0 到 100 的整数（GPT 剩余低于它就挡池）')
    stale = value.get('stale_hours')
    if isinstance(stale, bool) or not isinstance(stale, (int, float)) or stale <= 0:
        raise Blocked('quota.stale_hours 要是大于 0 的数字（读数几小时没更新算未知）')
    machine = value.get('machine_max')
    if type(machine) is not int or machine < 1:
        raise Blocked('quota.machine_max 要是不小于 1 的整数（全机同时派单上限）')
    if not isinstance(value.get('codex_sessions_dir'), str):
        raise Blocked('quota.codex_sessions_dir 要是文字路径，可以留空')
    pools = value.get('codex_pools')
    if not isinstance(pools, list) or any(not isinstance(p, str) or not p.strip() for p in pools):
        raise Blocked('quota.codex_pools 要是一组非空文字（共用 GPT 周额度的池名）')
    if not isinstance(value.get('priority_project'), str) or '\n' in value.get('priority_project', ''):
        raise Blocked('quota.priority_project 要是一行文字，可以留空（排队时优先放行的项目）')
    meters = value.get('metered_deepseek')
    if not isinstance(meters, list) or any(not _check_model(m) for m in meters):
        raise Blocked('quota.metered_deepseek 里的型号要写全 model、vendor、pool、billing')
    for m in meters:
        _reject_excluded(m)
        if m['billing'] != 'metered' or not str(m['model']).startswith('deepseek/'):
            raise Blocked('quota.metered_deepseek 里只能放直连 DeepSeek 的按量型号（型号要以 deepseek/ 开头）')
    tiers = value.get('tiers')
    if not isinstance(tiers, dict) or set(tiers) != set(TIERS):
        raise Blocked('quota.tiers 要正好是 重、普通、轻、审查 四档')
    for tier in TIERS:
        entry = tiers[tier]
        if not isinstance(entry, dict) or set(entry) != {'preferred', 'backup'}:
            raise Blocked('quota.tiers.' + tier + ' 要写 preferred 和 backup 两个列表')
        for slot in ('preferred', 'backup'):
            entries = entry[slot]
            if not isinstance(entries, list):
                raise Blocked('quota.tiers.' + tier + '.' + slot + ' 要是列表')
            for m in entries:
                if not _check_model(m):
                    raise Blocked('quota.tiers.' + tier + '.' + slot + ' 里的型号要写全 model、vendor、pool、billing')
                _reject_excluded(m)
                if m['billing'] == 'metered' and not str(m['model']).startswith('deepseek/'):
                    raise Blocked('按量型号只允许直连 deepseek/*：' + str(m['model']))


def _check_model(entry):
    return (isinstance(entry, dict) and isinstance(entry.get('model'), str) and entry['model'].strip()
            and isinstance(entry.get('vendor'), str)
            and isinstance(entry.get('pool'), str) and entry['pool'].strip()
            and entry.get('billing') in ('subscription', 'metered'))


def _excluded_model(entry):
    return str((entry or {}).get('model') or '').strip().lower() in EXCLUDED_SUBSESSION


def _reject_excluded(entry):
    if _excluded_model(entry):
        raise Blocked('claude-api-active 不进子会话池，只用于主会话临时接管')


# ---------- 台账文件 ----------

def ledger_path(root):
    return safe(Path(root).absolute(), LEDGER)


def _ledger_lock(root):
    path = ledger_path(root)
    return path.parent / (path.name + '.lock')


def _empty_ledger():
    return {'schema': 1, 'records': [], '占用': [], '接管': []}


def load_ledger(root):
    path = ledger_path(root)
    if not path.is_file():
        return _empty_ledger()
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        raise Blocked('额度台账不是有效 JSON：' + str(path))
    if not isinstance(value, dict) or not all(isinstance(value.get(k), list) for k in ('records', '占用', '接管')):
        raise Blocked('额度台账格式不对：' + str(path))
    return value


def _save_ledger(root, ledger):
    atomic(ledger_path(root), canonical(ledger))


# ---------- 报错入账 ----------

def classify_error(text):
    """限流/余额类报错入账。讲额度的那一句本身是环境故障则不入账；其余返回 None。"""
    sentence = quota_sentence(text)
    if not sentence:
        return None
    return '余额' if BALANCE_WORD.search(sentence) else '限流'


def quota_sentence(text):
    """报错原文里讲额度、且这一句本身不是环境故障的那一句。"""
    text = str(text or '')
    parts = [p.strip() for p in re.split(r'(?<=[。！？!?;\n])', text) if p.strip()]
    for sentence in parts:
        if QUOTA_WORD.search(sentence) and not ENV_WORD.search(sentence):
            return sentence
    return ''


def _parse_time(text):
    """只认原文里写的时间；返回 epoch 或 None。"""
    match = TIME_RX.search(str(text or ''))
    if not match:
        return None
    y, mo, d, h, mi = (int(match.group(i)) for i in range(1, 6))
    sec = int(match.group(6) or 0)
    is_utc = match.end() < len(str(text)) and str(text)[match.end()] == 'Z'
    try:
        if is_utc:
            return calendar.timegm((y, mo, d, h, mi, sec, 0, 0, 0))
        return time.mktime((y, mo, d, h, mi, sec, 0, 0, -1))
    except (ValueError, OverflowError):
        return None


def _fmt(epoch):
    return time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(epoch))


def _reset_label(value):
    """resets_at 是原文时间就原样留下；是 unix 秒（或毫秒）就换成本地时间。"""
    if isinstance(value, bool) or value in (None, ''):
        return ''
    number = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):
        number = float(value.strip())
    if number is None:
        return str(value).strip()
    if number >= 10 ** 12:
        number /= 1000.0
    try:
        return _fmt(number)
    except (OverflowError, OSError, ValueError):
        return str(value).strip()


def record_error(root, vendor, model, pool, error_text, task='', now=None):
    """子会话报错里出现额度/限流/余额字样时记一笔；环境故障不入账；入账前过密钥扫描。"""
    now = time.time() if now is None else now
    kind = classify_error(error_text)
    if kind is None:
        return {'入账': False, '类型': None, '原因': '不是额度/限流/余额报错，或属于环境故障'}
    sentence = quota_sentence(error_text)
    note = redact_text(sentence)
    if kind == '余额':
        recovery = '待充值'
    else:
        parsed = _parse_time(error_text)
        recovery = _fmt(parsed) if parsed is not None else '未知'
    entry = {'家': str(vendor or ''), '模型': str(model or ''), '池': str(pool or ''), '时间': now,
             '类型': kind, '说明': note, '恢复': recovery, '任务': str(task or '')}
    with lock(_ledger_lock(root)):
        ledger = load_ledger(root)
        ledger['records'].append(entry)
        _save_ledger(root, ledger)
    return {'入账': True, '类型': kind, '恢复': recovery, '记录': entry}


def clear_pool(root, pool):
    """小探测单确认恢复后放行一个池：只清它的限流/余额记录，派单与按量记录保留。"""
    with lock(_ledger_lock(root)):
        ledger = load_ledger(root)
        before = len(ledger['records'])
        ledger['records'] = [r for r in ledger['records']
                             if not (r.get('类型') in ('限流', '余额') and r.get('池') == pool)]
        _save_ledger(root, ledger)
    return {'池': pool, '清掉': before - len(ledger['records'])}


# ---------- Codex 剩余额度读数 ----------

def _primary_reading(obj, depth=0):
    """rate_limits 可能在一行 JSON 的顶层，也可能嵌在 payload 里。"""
    if not isinstance(obj, dict) or depth > 6:
        return None
    rate = obj.get('rate_limits')
    primary = rate.get('primary') if isinstance(rate, dict) else None
    if isinstance(primary, dict):
        used = primary.get('used_percent')
        if isinstance(used, (int, float)) and not isinstance(used, bool):
            return primary
    for value in obj.values():
        if isinstance(value, dict):
            found = _primary_reading(value, depth + 1)
            if found is not None:
                return found
    return None


def _scan_jsonl(path):
    try:
        with open(path, 'rb') as handle:
            handle.seek(0, 2)
            size = handle.tell()
            handle.seek(max(0, size - 262144))
            data = handle.read().decode('utf-8', 'replace')
    except OSError:
        return None
    found = None
    for line in data.splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        primary = _primary_reading(obj)
        if primary is None:
            continue
        found = {'已用百分比': float(primary['used_percent']),
                 '恢复时刻': _reset_label(primary.get('resets_at')),
                 '恢复epoch': _reset_epoch(primary.get('resets_at')),
                 '周期分钟': primary.get('window_minutes'),
                 '行时间': _line_time(obj)}
    return found


def _line_time(obj):
    """读数行上的时间戳，换成秒。没有或不是数字就返回 None。"""
    if not isinstance(obj, dict):
        return None
    stamp = obj.get('timestamp')
    if isinstance(stamp, bool) or not isinstance(stamp, (int, float)):
        return None
    stamp = float(stamp)
    if stamp >= 10 ** 12:
        stamp /= 1000.0
    return stamp


def _reset_epoch(value):
    """resets_at 换成 epoch。认不出来返回 None。"""
    if isinstance(value, bool) or value in (None, ''):
        return None
    number = None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str) and re.fullmatch(r'\d+(?:\.\d+)?', value.strip()):
        number = float(value.strip())
    if number is not None:
        if number >= 10 ** 12:
            number /= 1000.0
        return number
    return _parse_time(str(value))


def _none_reading(reason):
    return {'状态': 'none', '说明': reason}


def read_codex_rate(sessions_dir='', stale_hours=None, now=None):
    """读带 rate_limits 的那份会话记录。过期看这份文件的修改时间，或读数行上的时间戳。"""
    now = time.time() if now is None else now
    stale_hours = 6 if stale_hours is None else stale_hours
    base = Path(sessions_dir).expanduser() if sessions_dir else Path(user_home()) / '.codex' / 'sessions'
    try:
        is_dir = base.is_dir()
    except OSError:
        is_dir = False
    if not is_dir:
        return _none_reading('没有会话记录目录：' + str(base))
    try:
        candidates = list(base.rglob('*.jsonl'))
    except OSError:
        return _none_reading('读会话记录失败')
    dated = []
    for path in candidates:
        try:
            dated.append((path.stat().st_mtime, path))
        except OSError:
            continue
    if not dated:
        return _none_reading('会话记录目录里没有可读的 .jsonl')
    reading = None
    reading_mtime = None
    for mtime, path in sorted(dated, key=lambda item: item[0], reverse=True):
        found = _scan_jsonl(path)
        if not found:
            continue
        found['文件'] = str(path)
        reading = found
        reading_mtime = mtime
        break
    if not reading:
        return _none_reading('会话记录里没有 rate_limits')
    line_stamp = reading.pop('行时间', None)
    reset_at = reading.pop('恢复epoch', None)
    result = {'状态': 'ok'}
    result.update(reading)
    stamp = reading_mtime if line_stamp is None else line_stamp
    if now - float(stamp) > float(stale_hours) * 3600:
        result['状态'] = 'stale'
        result['说明'] = '读数超过 ' + str(stale_hours) + ' 小时没更新，按未知处理'
    elif isinstance(reset_at, (int, float)) and not isinstance(reset_at, bool) and reset_at <= now:
        result['状态'] = 'unknown'
        result['说明'] = '恢复时间已到或已过，按未知处理'
    return result


# ---------- 被挡的池 ----------

def blocked_pools(root, config, now=None, sessions_dir=None):
    """返回 (被挡的池 {池: {来源,恢复,时刻}}, Codex 读数)。台账按池挡，不是按单个型号。"""
    now = time.time() if now is None else now
    blocks = {}
    for record in load_ledger(root)['records']:
        if record.get('类型') not in ('限流', '余额'):
            continue
        pool = record.get('池')
        if not pool:
            continue
        if record.get('类型') == '余额':
            label, until = '待充值', None
        else:
            label = record.get('恢复') or '未知'
            until = _parse_time(label) if label != '未知' else None
            if until is not None and until <= now:
                continue  # 到点：只放行新派活，不续跑旧单
        current = blocks.get(pool)
        if _stricter_block(current, until):
            blocks[pool] = {'来源': '台账', '恢复': label, '时刻': until,
                            '家': record.get('家', ''), '模型': record.get('模型', '')}
    reading = read_codex_rate(config.get('codex_sessions_dir') or sessions_dir or '',
                              config.get('stale_hours'), now)
    if reading.get('状态') == 'ok' and config.get('codex_pools'):
        used = reading.get('已用百分比')
        if isinstance(used, (int, float)) and (100 - used) < config.get('gpt_floor_percent', 40):
            label = reading.get('恢复时刻') or '未知'
            until = _parse_time(label) if label != '未知' else None
            for pool in config.get('codex_pools'):
                if pool not in blocks:
                    blocks[pool] = {'来源': 'codex读数', '恢复': label, '时刻': until}
    return blocks, reading


# ---------- 选家 ----------

def _tier_entries(tier_config):
    seen = set()
    entries = []
    for item in [*(tier_config.get('preferred') or []), *(tier_config.get('backup') or [])]:
        model = item.get('model')
        if model in seen:
            continue
        seen.add(model)
        entries.append(item)
    return entries


def _billing_rank(entry):
    return 0 if entry.get('billing') == 'subscription' else 1


def _stricter_block(current, until):
    """同一池多条记录时，留挡得更久的：待充值/未知压过具体时间，具体时间留更晚的。"""
    if current is None:
        return True
    old = current.get('时刻')
    if old is None:
        return False
    if until is None:
        return True
    return until > old


def _record_dispatch(root, tier, entry, model, task, now, metered):
    with lock(_ledger_lock(root)):
        ledger = load_ledger(root)
        ledger['records'].append({'家': entry.get('vendor') or '', '模型': model, '池': entry.get('pool') or '',
                                  '时间': now, '类型': '按量' if metered else '派单',
                                  '说明': '档位：' + tier, '恢复': '', '任务': task or ''})
        _save_ledger(root, ledger)


def pick(root, config, tier, writer_vendor='', task='', available=None, now=None,
         sessions_dir=None, record=True, project=''):
    """按档位选模型：首选→备选，跳过被挡的池，包月优先；返回 JSON 供派活窗口调用。

    审查档换哪一家只按这份配置的首选顺序，不在程序里偏向任何一条线路。
    """
    if tier not in TIERS:
        raise Blocked('档位只能是：' + '、'.join(TIERS))
    now = time.time() if now is None else now
    available = [str(a).strip() for a in (available or []) if str(a).strip()]
    blocks, reading = blocked_pools(root, config, now=now, sessions_dir=sessions_dir)
    tier_config = config.get('tiers', {}).get(tier) or {'preferred': [], 'backup': []}
    writer = (writer_vendor or '').strip().lower()

    def resolve(entry):
        model = str(entry.get('model') or '')
        if model == 'default':
            return 'default'
        if model.endswith('*'):
            if not available:
                return model
            for name in available:
                if name.startswith(model[:-1]):
                    return name
            return None
        if not available:
            return model
        for name in available:
            if name == model or name.endswith('/' + model):
                return name
        return None

    def usable(entry):
        if _excluded_model(entry):
            return False
        return (entry.get('pool') or '') not in blocks and resolve(entry) is not None

    def finish(result):
        name = str(config.get('priority_project') or '').strip()
        if result.get('状态') == '排队' and name and str(project or '').strip() == name:
            result['优先'] = True
            result['原因'] = result.get('原因', '') + '；这是优先项目，额度恢复后先放行这一单。'
        return result

    def result_ok(entry, model, metered=False):
        result = {'状态': 'ok', '档位': tier, '模型': model, '家': entry.get('vendor') or '',
                  '池': entry.get('pool') or '', '计费': 'metered' if metered else 'subscription',
                  '任务': task or ''}
        if metered:
            result['提示'] = METERED_NOTE
        if config.get('codex_pools') and reading.get('状态') != 'ok':
            result['codex读数'] = reading
        if record:
            _record_dispatch(root, tier, entry, model, task, now, metered)
        return result

    if tier == '审查':
        def rank(entry):
            vendor = (entry.get('vendor') or '').strip().lower()
            if not vendor or vendor == 'unknown':
                return 1
            return 2 if (writer and vendor == writer) else 0

        ordered = sorted(_tier_entries(tier_config), key=lambda e: (rank(e), _billing_rank(e)))
        usable_entries = [e for e in ordered if usable(e)]
        # 还有包月就不取按量。选中按量时按 billing 记账并提示。
        subscription = [e for e in usable_entries if e.get('billing') != 'metered']
        chosen = subscription[0] if subscription else next(
            (e for e in usable_entries if e.get('billing') == 'metered'), None)
        if chosen is None:
            return finish({'状态': '报用户', '档位': tier, '原因': '审查档没有可用的模型（全被挡或没配）', '任务': task or ''})
        result = result_ok(chosen, resolve(chosen), metered=chosen.get('billing') == 'metered')
        result['审查同家'] = False
        note = None
        if rank(chosen) == 2:
            result['审查同家'] = True
            note = '没有不同家的审查模型，用了和写代码同一家的，请多看一眼。'
        elif rank(chosen) == 1:
            note = '审查模型厂商未知（或只有一家），请多看一眼。'
        if note:
            result['提示'] = (result.get('提示') or '') + note
        return finish(result)

    entries = _tier_entries(tier_config)
    subscription = [e for e in entries
                    if usable(e) and (e.get('vendor') or '').strip().lower() != 'anthropic'
                    and e.get('billing') == 'subscription']
    if subscription:
        chosen = subscription[0]
        return finish(result_ok(chosen, resolve(chosen)))
    if tier == '轻':
        return finish({'状态': '排队', '档位': tier, '任务': task or '',
                       '原因': '包月全被挡，轻活排队等额度恢复（记进项目 STATUS.md）'})
    metered = [e for e in entries if usable(e) and (e.get('vendor') or '').strip().lower() != 'anthropic'
               and e.get('billing') == 'metered']
    metered.extend(e for e in (config.get('metered_deepseek') or []) if usable(e))
    if metered:
        chosen = metered[0]
        return finish(result_ok(chosen, resolve(chosen), metered=True))
    configured = bool(config.get('metered_deepseek')) or any(e.get('billing') == 'metered' for e in entries)
    if tier == '重':
        reason = ('重活没有可用模型：包月和按量出口都被挡，由用户决定要不要用按量的。' if configured
                  else '重活没有可用模型：包月全被挡，也没配按量出口，由用户决定。')
        return finish({'状态': '报用户', '档位': tier, '任务': task or '', '原因': reason})
    reason = ('包月全被挡，按量出口也不可用，排队等额度恢复（记进项目 STATUS.md）' if configured
              else '包月全被挡，排队等额度恢复（记进项目 STATUS.md）')
    return finish({'状态': '排队', '档位': tier, '任务': task or '', '原因': reason})


# ---------- 全机名额 ----------

def _reclaim(occupied, now, statuses):
    statuses = statuses or {}
    kept = []
    for entry in occupied:
        status = statuses.get(entry.get('任务'))
        lowered = str(status).strip().lower() if status is not None else None
        if lowered in ENDED_STATES:
            continue
        if lowered in RUNNING_STATES:
            kept.append(entry)
            continue
        age = now - (entry.get('时间') or 0)
        if status is None and age > STALE_DAYS_MS:
            continue
        kept.append(entry)
    return kept


def occupy(root, task, limit, now=None, statuses=None):
    """查上限与占名额在同一把文件锁里一次做完；派发失败/子会话结束用 release 释放。"""
    if not str(task or '').strip():
        raise Blocked('占名额要带任务号')
    now = time.time() if now is None else now
    with lock(_ledger_lock(root)):
        ledger = load_ledger(root)
        ledger['占用'] = _reclaim(ledger['占用'], now, statuses)
        running = [e.get('任务') for e in ledger['占用']]
        if len(ledger['占用']) >= limit:
            _save_ledger(root, ledger)
            return {'占位': False, '在跑': len(ledger['占用']), '在跑任务': running,
                    '原因': '全机同时派单已达上限 ' + str(limit)}
        ledger['占用'].append({'任务': str(task).strip(), '时间': now})
        _save_ledger(root, ledger)
        return {'占位': True, '在跑': len(ledger['占用']), '在跑任务': running + [str(task).strip()]}


def release(root, task):
    with lock(_ledger_lock(root)):
        ledger = load_ledger(root)
        before = len(ledger['占用'])
        ledger['占用'] = [e for e in ledger['占用'] if e.get('任务') != task]
        _save_ledger(root, ledger)
    return {'释放': before - len(ledger['占用']), '任务': task}


# ---------- 主会话临时接管（3.5） ----------

def takeover_error(text):
    """识别「主会话撞 5 小时额度」报错；只通知总管，程序不自动改任何窗口。"""
    text = str(text or '')
    if not FIVE_HOUR.search(text) or not LIMIT_WORD.search(text):
        return {'命中': False}
    sentence = quota_sentence(text) or text
    return {'命中': True, '原文': redact_text(sentence)[:400]}


def record_takeover(root, window, model='', vendor='', start='', end='', now=None):
    """记一次接管台账：哪个窗口、起止时间、型号；改窗口由总管在 T3 里操作。"""
    now = time.time() if now is None else now
    entry = {'窗口': str(window or ''), '开始': str(start) or _fmt(now), '结束': str(end or ''),
             '型号': str(model or ''), '家': str(vendor or ''), '时间': now}
    with lock(_ledger_lock(root)):
        ledger = load_ledger(root)
        ledger['接管'].append(entry)
        _save_ledger(root, ledger)
    return {'接管': entry}


# ---------- 看账与简报 ----------

def _yesterday_window(now):
    local = time.localtime(now)
    today_start = time.mktime((local.tm_year, local.tm_mon, local.tm_mday, 0, 0, 0, 0, 0, -1))
    return today_start - 86400, today_start


def _takeover_start(entry):
    parsed = _parse_time(entry.get('开始') or '')
    return parsed if parsed is not None else 0


def briefing_line(yesterday, blocks, takeovers):
    """「各家昨日单数、谁被挡着、何时恢复」一行读数。"""
    if yesterday:
        parts = ['昨日派单：' + '、'.join(f'{name} {count} 单' for name, count in yesterday.items())]
    else:
        parts = ['昨日派单：无']
    if blocks:
        items = []
        for pool, info in blocks.items():
            label = info.get('恢复') or '未知'
            items.append(f'{pool}（{label}）' if label in ('待充值', '未知') else f'{pool}（{label} 恢复）')
        parts.append('被挡：' + '、'.join(items))
    else:
        parts.append('被挡：无')
    if takeovers:
        parts.append(f'今日接管 {takeovers} 次')
    return '；'.join(parts) + '。'


def show(root, config, now=None, sessions_dir=None, count=10):
    now = time.time() if now is None else now
    ledger = load_ledger(root)
    blocks, _ = blocked_pools(root, config, now=now, sessions_dir=sessions_dir)
    start, end = _yesterday_window(now)
    yesterday = {}
    for record in ledger['records']:
        if record.get('类型') not in ('派单', '按量'):
            continue
        stamp = record.get('时间')
        if not isinstance(stamp, (int, float)) or not start <= stamp < end:
            continue
        vendor = record.get('家') or '未知'
        yesterday[vendor] = yesterday.get(vendor, 0) + 1
    yesterday = dict(sorted(yesterday.items(), key=lambda item: -item[1]))
    takeovers = sum(1 for entry in ledger['接管'] if _takeover_start(entry) >= end)
    return {'配置': {'gpt剩余门槛': config.get('gpt_floor_percent'),
                     '读数过期小时': config.get('stale_hours'),
                     '全机名额上限': config.get('machine_max'),
                     '优先项目': config.get('priority_project') or ''},
            '档位': config.get('tiers'), '被挡的池': blocks, '昨日单数': yesterday,
            '今日接管': takeovers, '占用名额': ledger['占用'],
            '最近台账': ledger['records'][-count:],
            '简报一行': briefing_line(yesterday, blocks, takeovers),
            '台账文件': str(ledger_path(root))}


# ---------- CLI ----------

def _model_field(config, model, field):
    for tier in TIERS:
        for entry in _tier_entries(config['tiers'][tier]):
            if entry.get('model') == model and entry.get(field):
                return entry[field]
    for entry in config.get('metered_deepseek') or []:
        if entry.get('model') == model and entry.get(field):
            return entry[field]
    return ''


def run(args):
    from types import SimpleNamespace
    from . import settings as settings_module
    root = Path(args.root).absolute()

    def config():
        return settings_module.load(SimpleNamespace(root=root))['quota']

    action = args.quota_action
    if action == 'show':
        return show(root, config())
    if action == 'pick':
        available = [x.strip() for x in (args.available or '').split(',') if x.strip()]
        return pick(root, config(), args.tier, args.writer_vendor, args.task, available,
                    record=not args.no_record, project=args.project)
    if action == 'record':
        current = config()
        vendor = args.vendor or _model_field(current, args.model, 'vendor')
        pool = args.pool or _model_field(current, args.model, 'pool')
        if not vendor or not pool:
            raise Blocked('不知道这个型号的厂商或额度池，请给 --vendor / --pool')
        return record_error(root, vendor, args.model, pool, args.error, args.task)
    if action == 'occupy':
        current = config()
        limit = args.limit if args.limit is not None else current['machine_max']
        statuses = json.loads(args.statuses) if args.statuses else None
        return occupy(root, args.task, limit, statuses=statuses)
    if action == 'release':
        return release(root, args.task)
    if action == 'clear':
        return clear_pool(root, args.pool)
    if action == 'takeover-check':
        return takeover_error(args.error)
    if action == 'takeover':
        return record_takeover(root, args.window, args.model, args.vendor, args.start, args.end)
    raise Blocked('未实现的 quota 子命令')


def register(subparser):
    p = subparser.add_parser('quota', help='子会话选模型与额度协调：show / pick / record / occupy / release / clear / takeover-check / takeover')
    actions = p.add_subparsers(dest='quota_action', required=True)
    actions.add_parser('show', help='台账、被挡的池、昨日单数与简报一行')
    pick_p = actions.add_parser('pick', help='按档位选模型（派活前调用）')
    pick_p.add_argument('--tier', required=True, help='重 / 普通 / 轻 / 审查')
    pick_p.add_argument('--writer-vendor', default='', help='审查档：写代码那单的厂商')
    pick_p.add_argument('--task', default='')
    pick_p.add_argument('--project', default='', help='项目名；和设置里的优先项目相同的排队单先放行')
    pick_p.add_argument('--available', default='', help='orchestrator_capabilities 当场能列出的型号，逗号分隔')
    pick_p.add_argument('--no-record', action='store_true', help='只选不记派单')
    record_p = actions.add_parser('record', help='子会话报错入账（额度/限流/余额字样自动识别）')
    record_p.add_argument('--vendor', default='')
    record_p.add_argument('--model', required=True)
    record_p.add_argument('--pool', default='')
    record_p.add_argument('--error', required=True)
    record_p.add_argument('--task', default='')
    occupy_p = actions.add_parser('occupy', help='占全机名额（与查上限同一文件锁）')
    occupy_p.add_argument('--task', required=True)
    occupy_p.add_argument('--limit', type=int, default=None)
    occupy_p.add_argument('--statuses', default='', help='已知任务状态 JSON：{"任务号":"ended"}')
    release_p = actions.add_parser('release', help='释放名额')
    release_p.add_argument('--task', required=True)
    clear_p = actions.add_parser('clear', help='小探测单确认恢复后放行一个池')
    clear_p.add_argument('--pool', required=True)
    takeover_p = actions.add_parser('takeover-check', help='识别主会话撞 5 小时额度的报错')
    takeover_p.add_argument('--error', required=True)
    record_takeover_p = actions.add_parser('takeover', help='记一次主会话接管台账')
    record_takeover_p.add_argument('--window', required=True)
    record_takeover_p.add_argument('--model', default='')
    record_takeover_p.add_argument('--vendor', default='')
    record_takeover_p.add_argument('--start', default='')
    record_takeover_p.add_argument('--end', default='')
    p.set_defaults(quota_run=run)
    return p
