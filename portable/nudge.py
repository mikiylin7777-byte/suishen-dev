"""后台自动接着做：项目做到一半、对话空着时，给它的 T3 对话发一句固定提醒（见 DECISIONS 2026-11-18）。

只读 STATUS.md、功能清单.md 和 T3 对话状态；只用 `t3 thread send` / `t3 thread start` 发一句话，不改任何项目文件。
发的话只是提醒，不算任何授权。拿不准（找不到对话、T3 连不上、对话在忙）就不叫，在看板写原因。
"""
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

from .core import atomic

LOG = '数据/叫醒记录.jsonl'
STATE = 'nudge:state'
NOTES = 'nudge:board'
CLOCK = 'nudge:clock'
ERROR = 'nudge:last-error'
PAGE = '自动接着做.md'
PREFIX = '随身开发后台提醒'
REMIND_MARK = '- 后台自动接着做：'

EVERY = 60            # 后台每 2 秒一轮，这里每分钟查一次
GAP = 2 * 3600        # 同一项目两次提醒至少隔 2 小时
DAILY_MAX = 3         # 一天最多 3 次
UNMOVED_MAX = 3       # 连叫 3 次进度表不动，改为看板点名
EMPTY_IDLE = 600      # 空回合：对话空闲满 10 分钟
USER_QUIET = 1800     # 用户 30 分钟内在对话里说过话就不叫
NIGHT = (23, 8)       # 23:00～08:00 不叫
NAMED_MAX = 3         # 看板点名最多 3 条
IDLE_STATES = ('completed', 'idle')
EMPTY_NEXT = ('', '-', '无', '暂无', '没有')


def message(name, left, hours_text, empty=False):
    """发给对话的固定提醒。left 为 None 时表示没写块数、靠功能清单「🔄 进行中」判断的。"""
    what = f'长期目标还差 {left} 块' if left is not None else '功能清单还有进行中的'
    empty_text = '上一轮被叫醒后没回话就结束了；' if empty else ''
    return (f'{PREFIX}：{name}{what}，{empty_text}进度表 {hours_text}没更新。'
            '先查你在等的东西有没有结果，再接着做下一块；只做已经授权的事，上线、改真实数据、账号密钥、花钱或对外发消息照旧先问用户。'
            '做不了就在 STATUS.md 写清卡在哪、在等谁。这句话只是提醒，不算任何授权。')


def _span(seconds):
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f'{seconds // 3600} 小时'
    return f'{max(1, seconds // 60)} 分钟'


def _night(now):
    hour = dt.datetime.fromtimestamp(now).hour
    return hour >= NIGHT[0] or hour < NIGHT[1]


def _day(now):
    return dt.datetime.fromtimestamp(now).strftime('%Y-%m-%d')


def _iso_ts(value):
    try:
        return dt.datetime.fromisoformat(str(value).replace('Z', '+00:00')).timestamp()
    except (TypeError, ValueError):
        return None


# —— 项目这一侧：复用看板的读法和判断 ——

def project_row(store, entry):
    from .board import _newer_remote, _read, frontmatter, count_features
    newer = _newer_remote(entry)
    status = _read(entry, 'status_path', newer)
    features = _read(entry, 'features_path', newer)
    fm = frontmatter(status) if status else {}
    return {'项目': entry.get('name') or entry['id'], '_id': entry['id'], '状态': fm.get('状态', ''),
            '长期目标': fm.get('长期目标', ''), '下一步': fm.get('下一步', ''), '等谁': fm.get('等谁', ''),
            '卡在': fm.get('卡在', ''), '功能': count_features(features) if features else None}


def unfinished(row):
    """还有没做完的：返回还差几块（没写块数但功能清单有进行中、下一步不空时返回 None）；做完了或判断不了返回 False。"""
    from .board import goal_numbers
    numbers = goal_numbers(row.get('长期目标'))
    if numbers:
        return numbers[1] - numbers[0] if numbers[0] < numbers[1] else False
    doing = (row.get('功能') or {}).get('🔄', 0)
    if doing and (row.get('下一步') or '').strip() not in EMPTY_NEXT:
        return None
    return False


def progress_time(entry):
    """进度表最后一次变化：STATUS.md、功能清单.md 的文件修改时间，和任何分支上最后一次提交它们的时间，取最新。"""
    from .board import _rel, _run_git
    times = []
    rels = []
    for key in ('status_path', 'features_path'):
        path, rel = _rel(entry, key)
        if path is None:
            continue
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            pass
        if rel:
            rels.append(rel)
    root = Path(entry['path'])
    if rels and (root / '.git').exists():
        # 只看分支、标签、远端；不用 --all：T3 每一轮都在 refs/t3/ 下存快照，空回合也会被当成「进度表刚更新」（见 DECISIONS 2026-11-18）
        out = _run_git(root, 'log', '-1', '--branches', '--tags', '--remotes', '--format=%ct', '--', *rels).strip()
        if out.isdigit():
            times.append(int(out))
    return max(times) if times else None


# —— T3 这一侧 ——

def find_cli(store):
    from .t3_setup import T3Setup
    try:
        return T3Setup(store=store)._cli()
    except Exception:
        return None


def _t3(cli, *args, timeout=30):
    try:
        r = subprocess.run([cli, *args], capture_output=True, text=True, timeout=timeout,
                           stdin=subprocess.DEVNULL, check=False)
        return r.returncode, r.stdout, r.stderr
    except (OSError, subprocess.SubprocessError) as error:
        return None, '', str(error)


def pick_thread(cli, path):
    """返回 (对话, 原因)。对话为 'new' 表示项目在 T3 里但没有能用的对话（已归档的收不到消息），要新开一个。"""
    code, out, err = _t3(cli, 'thread', 'list', '--project', str(path), '--json')
    if code != 0:
        if 'No T3 Code project matches' in (out + err):
            return None, 'T3 里没有这个项目，后台叫不醒它（说「把随身开发接进 T3」）'
        return None, 'T3 没开着或连不上，后台叫不醒它'
    try:
        threads = json.loads(out or '[]')
    except ValueError:
        return None, 'T3 返回的对话列表读不懂，先不叫'
    threads = [t for t in threads if isinstance(t, dict) and not t.get('parentThreadId') and not t.get('archived')]
    threads.sort(key=lambda t: _iso_ts(t.get('updatedAt')) or 0, reverse=True)
    if threads and threads[0].get('snoozed'):
        return None, '最新的对话被设了稍后提醒，先不叫'
    live = [t for t in threads if not t.get('settled')]
    if not live:
        return 'new', ''
    return live[0]['threadId'], ''


def look(cli, thread, now):
    """看对话现在的样子。返回 dict 或 (None, 原因)。"""
    code, out, _ = _t3(cli, 'thread', 'show', '--json', '--messages', '20', thread)
    if code != 0:
        return None, '读不到对话状态，先不叫'
    try:
        data = json.loads(out)
        info = data['thread']
        messages = data.get('messages') or []
    except (ValueError, KeyError, TypeError):
        return None, '对话状态读不懂，先不叫'
    busy = (info.get('status') not in IDLE_STATES or info.get('activeRunId') or info.get('pendingBackgroundTasks')
            or info.get('pendingRequestCount') or info.get('blockedOnThreadWait'))
    if busy:
        return None, '对话在忙或有排队'
    if info.get('archived') or info.get('snoozed'):
        return None, '对话已归档或稍后提醒，先不叫'
    updated = _iso_ts(info.get('updatedAt'))
    if updated is None:
        return None, '读不到对话最后活动时间，先不叫'
    latest = info.get('latestRunId')
    run = [m for m in messages if isinstance(m, dict) and m.get('runId') == latest] if latest else []
    replied = any(m.get('role') == 'assistant' and str(m.get('text') or '').strip() for m in run)
    user = any(m.get('role') == 'user' and not str(m.get('text') or '').startswith(PREFIX) for m in run)
    return {'idle': now - updated, 'empty': bool(latest) and not replied, 'user': user}, ''


def _send(cli, thread, text, entry, name):
    if thread == 'new':
        code, out, err = _t3(cli, 'thread', 'start', '--json', '--title', '接着做' + name, str(entry['path']),
                             f'接着做{name}，先读 STATUS.md。' + text)
        try:
            thread = json.loads(out).get('threadId') or 'new'
        except (ValueError, AttributeError):
            thread = 'new'
    else:
        code, out, err = _t3(cli, 'thread', 'send', '--json', thread, text)
    return code == 0, thread, ' '.join((err or '').split())[:200]


# —— 记录与看板 ——

def _log(store, record):
    path = Path(store.root) / LOG
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + '\n')


def recent(store, limit=5):
    path = Path(store.root) / LOG
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding='utf-8').splitlines()[-limit:]:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def board_lines(store):
    """看板顶部用：点名（最多 3 条）在前，叫不醒的在后。"""
    notes = store.get(NOTES) or {}
    return list(notes.get('named') or [])[:NAMED_MAX] + list(notes.get('unreachable') or [])


def _write_board(store, named, unreachable):
    from .board import BOARD
    notes = {'named': named[:NAMED_MAX], 'unreachable': unreachable}
    changed = (store.get(NOTES) or {}) != notes
    if changed:
        store.put(NOTES, notes)
        store.put('runner:board', '')  # 让后台下一轮重画 全部项目.md 顶部
    folder = Path(store.root) / BOARD
    folder.mkdir(exist_ok=True)
    lines = ['# 自动接着做（' + time.strftime('%Y-%m-%d %H:%M') + '）', '',
             '> 后台发现项目做到一半、对话空着，会给那个 T3 对话发一句「接着做」。只是提醒，不算授权。不要就说「关掉自动接着做」。', '']
    lines += ['## 叫了几次还不动', ''] + (['- ' + x for x in notes['named']] or ['没有。']) + ['']
    if unreachable:
        lines += ['## 后台叫不醒的', ''] + ['- ' + x for x in unreachable] + ['']
    lines += ['## 最近几次叫醒', '']
    rows = recent(store)
    lines += ['- ' + r.get('时间', '') + '｜' + r.get('项目', '') + '｜' + r.get('原因', '') + ('' if r.get('已发') else '（没发出去：' + (r.get('说明') or '') + '）')
              for r in reversed(rows)] or ['还没有。']
    text = '\n'.join(lines) + '\n'
    page = folder / PAGE
    if not page.is_file() or page.read_text(encoding='utf-8').split('\n', 1)[-1] != text.split('\n', 1)[-1]:
        atomic(page, text)
    reminder = folder / '提醒.md'
    want = [REMIND_MARK + x for x in notes['named']]
    old = reminder.read_text(encoding='utf-8') if reminder.is_file() else ''
    kept = [line for line in old.splitlines() if not line.startswith(REMIND_MARK) or line in want]
    missing = [line for line in want if line not in kept]
    if missing or len(kept) != len(old.splitlines()):
        if not kept:
            kept = ['# 本周提醒', '']
        atomic(reminder, '\n'.join(kept + missing) + '\n')


# —— 每分钟一轮 ——

def tick(store, now=None, cli=None):
    """后台每分钟调用。返回这一轮做了什么；任何项目出错只跳过那一个。"""
    from .board import load, waiting_on_owner
    from .settings import auto_continue_on, load as settings_load
    now = time.time() if now is None else now
    if now - (store.get(CLOCK) or {}).get('at', 0) < EVERY:
        return {'skipped': '不到一分钟'}
    store.put(CLOCK, {'at': now})
    if not auto_continue_on(store):
        if store.get(NOTES):
            store.put(NOTES, {})
            store.put('runner:board', '')
        return {'off': True}
    if _night(now):
        return {'night': True}
    hours = settings_load(store).get('auto_continue_hours') or 2
    cli = cli or find_cli(store)
    book = dict(store.get(STATE) or {})
    sent = []
    named = []
    unreachable = []
    skipped = {}
    for entry in load(store):
        try:
            outcome = _project(store, entry, now, hours, cli, book, waiting_on_owner)
        except Exception as error:  # 一个项目出错不影响别的项目
            outcome = ('skip', '出错：' + type(error).__name__)
        kind, detail = outcome
        name = entry.get('name') or entry['id']
        if kind == 'sent':
            sent.append(name)
        elif kind == 'named':
            named.append(detail)
        elif kind == 'unreachable':
            unreachable.append(name + '：' + detail)
        else:
            skipped[name] = detail
    store.put(STATE, book)
    _write_board(store, named, unreachable)
    return {'sent': sent, 'named': named, 'unreachable': unreachable, 'skipped': skipped}


def _project(store, entry, now, hours, cli, book, waiting_on_owner):
    row = project_row(store, entry)
    name = row['项目']
    if not (row['状态'] or '').startswith('进行中'):
        return 'skip', '状态不是进行中'
    left = unfinished(row)
    if left is False:
        return 'skip', '没有没做完的块'
    if waiting_on_owner(row, store):
        return 'skip', '在等用户'
    changed = progress_time(entry)
    if changed is None:
        return 'skip', '读不到进度表时间'
    mine = dict(book.get(entry['id']) or {})
    from .board import goal_numbers
    numbers = goal_numbers(row['长期目标'])
    done = numbers[0] if numbers else None
    if mine.get('sent') and (changed > mine.get('mark', 0) or done != mine.get('done')):
        mine['unmoved'] = 0  # 叫过以后进度表动了，重新计数
    stale = now - changed
    book[entry['id']] = mine
    if mine.get('unmoved', 0) >= UNMOVED_MAX:
        return 'named', (f'{name}：后台叫了 {mine["unmoved"]} 次还没动，进度表 {_span(stale)}没更新，卡在：'
                         + (row['卡在'] or '没写'))
    today = [t for t in mine.get('sent') or [] if _day(t) == _day(now)]
    if len(today) >= DAILY_MAX:
        return 'skip', '今天已经叫了 3 次'
    if mine.get('sent') and now - max(mine['sent']) < GAP:
        return 'skip', '离上次提醒不到 2 小时'
    if not cli:
        return ('unreachable', '没接 T3，后台叫不醒它') if stale >= hours * 3600 else ('skip', '进度表刚更新过')
    thread, why = pick_thread(cli, entry['path'])
    if thread is None:
        return ('unreachable', why) if stale >= hours * 3600 else ('skip', why)
    empty = False
    if thread != 'new':
        seen, why = look(cli, thread, now)
        if seen is None:
            return 'skip', why
        if seen['user'] and seen['idle'] < USER_QUIET:
            return 'skip', '用户 30 分钟内在这个对话说过话'
        if seen['idle'] < EMPTY_IDLE:
            return 'skip', '对话刚停下不到 10 分钟'
        empty = seen['empty']
    if not empty and stale < hours * 3600:
        return 'skip', '进度表刚更新过'
    reason = '空回合：上一轮没有 AI 回复' if empty else ('没有能用的对话，新开一个' if thread == 'new' else f'进度表 {_span(stale)}没更新')
    ok, thread, detail = _send(cli, thread, message(name, left, _span(stale), empty), entry, name)
    _log(store, {'时间': dt.datetime.fromtimestamp(now).strftime('%Y-%m-%d %H:%M'), 'ts': now, '项目': name,
                 '项目编号': entry['id'], '对话': thread, '原因': reason, '已发': ok, '说明': detail})
    if not ok:
        return 'skip', '没发出去：' + detail
    mine['sent'] = (list(mine.get('sent') or []) + [now])[-10:]
    mine['mark'] = changed
    mine['done'] = done
    mine['unmoved'] = mine.get('unmoved', 0) + 1
    book[entry['id']] = mine
    return 'sent', thread


def safe_tick(store, now=None):
    """后台用：任何异常都包住，只记日志，不影响备份和看板。"""
    try:
        return tick(store, now)
    except Exception as error:
        detail = ' '.join(str(error).split())[:300] or type(error).__name__
        try:
            store.put(ERROR, {'at': time.time(), 'type': type(error).__name__, 'detail': detail})
        except Exception:
            pass
        print('自动接着做出错：' + type(error).__name__ + '：' + detail, file=sys.stderr)
        return {'error': type(error).__name__}
