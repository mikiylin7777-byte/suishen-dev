"""Explicitly selected read-only JSONL sessions, incremental cursors, summary-only archives."""
import argparse
import json
from pathlib import Path
import sys
from .privacy import sanitize, redact_text
from .core import Store, Blocked, atomic, safe, canonical, cooperative, digest

MAX_LINE=1024*1024
MAX_BATCH=4*1024*1024
TOOLS=('codex','claude','t3')

def extract(tool,row):
    """Return stable native message id and readable content; omit tool calls and telemetry."""
    if tool=='codex':
        if row.get('type')!='response_item':return None
        item=row.get('payload',{})
        if item.get('type')!='message' or item.get('role') not in ('user','assistant'):return None
        parts=item.get('content',[]);text='\n'.join(p.get('text','') for p in parts if p.get('type') in ('input_text','output_text'))
        return {'id':item.get('id'),'role':item['role'],'text':text}
    if tool=='claude':
        if row.get('type') not in ('user','assistant'):return None
        message=row.get('message',{});content=message.get('content','')
        text=content if isinstance(content,str) else '\n'.join(p.get('text','') for p in content if p.get('type')=='text')
        return {'id':row.get('uuid') or message.get('id'),'role':row['type'],'text':text}
    if row.get('role') not in ('user','assistant'):return None
    return {'id':row.get('id'),'role':row['role'],'text':row.get('text','')}

@cooperative
def select(store,tool,path,source_id):
    p=Path(path)
    if tool not in TOOLS or not source_id or p.is_symlink() or not p.is_file() or p.suffix!='.jsonl':raise Blocked('明确选择工具、原生会话ID与只读JSONL导出；不读取数据库或凭据')
    p=p.resolve();key=digest((tool+':'+source_id).encode());existing=store.get('session_source:'+key)
    info={'tool':tool,'path':str(p),'source_id':source_id,'device':store.device}
    if not existing:
        with p.open('rb') as f:head=f.read(4096)
        store.put('session_head:'+key,{'length':len(head),'sha256':digest(head)})
    if existing and existing!=info:raise Blocked('同一原生会话已绑定其他来源；禁止静默替换')
    store.put('session_source:'+key,info)
    return {'selection':key,'format':'native JSONL' if tool!='t3' else 'explicit T3 export JSONL: id, role, text','source_modified':False}

@cooperative
def read_batch(store,selection):
    info=store.get('session_source:'+selection)
    if not info or info['device']!=store.device:raise Blocked('来源未在当前设备明确选择')
    p=Path(info['path'])
    if p.is_symlink() or not p.is_file():raise Blocked('来源已变化或不可读')
    with p.open('rb') as f:
        head=store.get('session_head:'+selection)
        if not head or digest(f.read(head['length']))!=head['sha256']:raise Blocked('会话源头已变化，停止增量读取')
    cursor=store.get('session_cursor:'+selection,{ 'offset':0,'anchor':None})
    start=cursor['offset'];stat=p.stat()
    if stat.st_size<start:raise Blocked('来源被截断；保留原归档，请重新明确选择新会话ID')
    messages=[];native_session=None
    with p.open('rb') as f:
        if start:
            f.seek(max(0,start-4096));anchor=digest(f.read(min(4096,start)))
            if anchor!=cursor['anchor']:raise Blocked('来源已改写，不能沿用增量游标')
        f.seek(start);offset=start
        while f.tell()-start<MAX_BATCH:
            pos=f.tell();line=f.readline(MAX_LINE+1)
            if not line:break
            if len(line)>MAX_LINE:raise Blocked('会话单条超出读取上限')
            if not line.endswith(b'\n'):break  # active partial line retried next time
            try:row=json.loads(line)
            except (ValueError,UnicodeError) as e:raise Blocked('会话格式无效；游标未推进') from e
            if info['tool']=='codex' and row.get('type')=='session_meta':native_session=row.get('payload',{}).get('id')
            if info['tool']=='claude' and row.get('sessionId'):native_session=row['sessionId']
            if native_session and native_session!=info['source_id']:raise Blocked('原生会话身份不符')
            message=extract(info['tool'],row)
            if message and message['text']:
                message['id']=message['id'] or str(pos)
                message['content_sha256']=digest(message['text'].encode())
                dedup=digest(canonical([message['id'],message['content_sha256']]).encode())
                if not store.get('session_seen:'+selection+':'+dedup):
                    clean=sanitize(message);clean['redacted']=clean!=message;messages.append(clean)
            offset=f.tell()
        f.seek(max(0,offset-4096));anchor=digest(f.read(min(4096,offset)))
    receipt={'selection':selection,'from':start,'to':offset,'anchor':anchor,'messages':[{'id':m['id'],'sha256':m['content_sha256']} for m in messages]}
    token=digest(canonical(receipt).encode());store.put('session_batch:'+token,receipt)
    import gzip
    staging='会话档案/待整理/'+token+'.json.gz'
    atomic(safe(store.root,staging),gzip.compress(canonical(messages).encode(),mtime=0))
    store.put('session_staging:'+token,{'path':staging,'sha256':digest(canonical(messages).encode())})
    return {'batch':token,'normalized_persisted':True,'native_original_persisted':False,'messages':messages,'bytes_read':offset-start,'more':stat.st_size>offset,'source':info,'raw_persisted':False,'next':'由当前Agent提炼摘要，再commit；未commit不会推进游标'}

@cooperative
def commit(store,batch,summary):
    receipt=store.get('session_batch:'+batch)
    if not receipt or not isinstance(summary,str) or not summary.strip():raise Blocked('需要已读批次和Agent提炼摘要')
    selection=receipt['selection'];info=store.get('session_source:'+selection)
    cursor=store.get('session_cursor:'+selection,{'offset':0})
    if cursor['offset']==receipt['to'] and store.get('session_committed:'+batch):return store.get('session_committed:'+batch)
    if cursor['offset']!=receipt['from']:raise Blocked('会话批次过期；重新增量读取')
    path=store.archive(info['tool']+':'+info['source_id'],redact_text(summary))
    packed=archive_pack(store,batch,info,receipt,summary)
    result={'compressed_archive':packed,'archive':path,'source_id':info['source_id'],'message_digests':receipt['messages'],'raw_in_vault':False}
    for message in receipt['messages']:
        dedup=digest(canonical([message['id'],message['sha256']]).encode())
        store.put('session_seen:'+selection+':'+dedup,True)
    store.put('session_cursor:'+selection,{'offset':receipt['to'],'anchor':receipt['anchor']})
    store.put('session_committed:'+batch,result)
    return result

def bounded_gzip(path):
    import gzip
    with gzip.open(path,'rb') as f:raw=f.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise Blocked('会话压缩内容超出边界')
    return raw

@cooperative
def archive_pack(store,batch,info,receipt,summary):
    import datetime as dt
    import io
    import zipfile
    staged=store.get('session_staging:'+batch)
    if not staged:raise Blocked('缺本批可恢复消息；旧批次可保留摘要，不能冒称完整会话')
    raw=bounded_gzip(safe(store.root,staged['path']))
    if digest(raw)!=staged['sha256']:raise Blocked('会话暂存内容摘要不符')
    messages=json.loads(raw);clean_summary=redact_text(summary)
    source=info['tool']+':'+info['source_id']
    payload={'source':source,'summary':clean_summary,'messages':messages,'message_digests':receipt['messages'],'byte_range':[receipt['from'],receipt['to']], 'fidelity':'normalized_selected_user_assistant_text; not complete native original','privacy':{'redacted':clean_summary!=summary or any(m.get('redacted') for m in messages),'excluded':['tool calls','hidden reasoning','native session metadata','credentials matching privacy policy']}}
    data=canonical(payload).encode();entry=digest(data)
    previous=store.get('session_archive_index:'+entry)
    if previous:return entry
    month=dt.datetime.now(dt.timezone.utc).strftime('%Y-%m');rel='会话档案/月压缩/'+month+'.zip';path=safe(store.root,rel)
    if path.exists() and path.stat().st_size>128*1024*1024:raise Blocked('本月压缩包超过128 MiB，保留暂存等待分卷')
    buffer=io.BytesIO(path.read_bytes() if path.exists() else b'')
    try:
        with zipfile.ZipFile(buffer,'a',compression=zipfile.ZIP_DEFLATED) as z:
            member=entry+'.json'
            if member in z.namelist():
                if z.read(member)!=data:raise Blocked('月档内容冲突')
            else:z.writestr(member,data)
    except zipfile.BadZipFile as e:raise Blocked('月档损坏，未覆盖') from e
    atomic(path,buffer.getvalue())
    index={'id':entry,'path':rel,'member':entry+'.json','sha256':digest(data),'source':source,'summary':clean_summary,'batch':batch,'month':month,'fidelity':payload['fidelity'],'privacy':payload['privacy']}
    store.put('session_archive_index:'+entry,index)
    return entry

def archive_search(store,query='',source=None):
    results=[]
    for row in store.db.execute("SELECT value FROM meta WHERE key LIKE 'session_archive_index:%'"):
        value=json.loads(row[0])
        if source and source!=value['source']:continue
        if query and query.lower() not in (value['summary']+' '+value['source']).lower():continue
        results.append(sanitize(value))
    return sorted(results,key=lambda x:(x['month'],x['id']),reverse=True)[:50]

def archive_get(store,archive):
    import zipfile
    index=store.get('session_archive_index:'+archive)
    if not index:raise Blocked('月档索引不存在')
    try:
        with zipfile.ZipFile(safe(store.root,index['path'])) as z:
            member=z.getinfo(index['member'])
            if member.file_size>8*1024*1024:raise Blocked('月档条目超出恢复边界')
            raw=z.read(member)
    except (OSError,KeyError,zipfile.BadZipFile) as e:raise Blocked('月档损坏或缺失，索引保留') from e
    if digest(raw)!=index['sha256']:raise Blocked('月档摘要不符')
    return sanitize(json.loads(raw))

def resume(store,project=None):
    rows=[]
    for row in store.db.execute("SELECT id,body FROM records WHERE kind='checkpoint' ORDER BY created DESC"):
        body=sanitize(json.loads(row['body']))
        if not project or body.get('project')==project:rows.append({'id':row['id'],**body})
    return {'checkpoints':rows[:10],'native_session_restored':False,'difference':'恢复目标、决定、剩余工作与授权，不恢复原工具隐藏上下文或登录状态'}

def register(subparser):
    p=subparser.add_parser('sessions');s=p.add_subparsers(dest='session_action',required=True)
    a=s.add_parser('select');a.add_argument('tool',choices=TOOLS);a.add_argument('path');a.add_argument('source_id')
    a=s.add_parser('read');a.add_argument('selection')
    a=s.add_parser('commit');a.add_argument('batch');a.add_argument('summary')
    a=s.add_parser('archive-search');a.add_argument('query',nargs='?',default='');a.add_argument('--source')
    a=s.add_parser('archive-get');a.add_argument('archive')
    a=s.add_parser('resume');a.add_argument('--project')
    return p

def run(store,args):
    if args.session_action=='archive-search':return archive_search(store,args.query,args.source)
    if args.session_action=='archive-get':return archive_get(store,args.archive)
    if args.session_action=='select':return select(store,args.tool,args.path,args.source_id)
    if args.session_action=='read':return read_batch(store,args.selection)
    if args.session_action=='commit':return commit(store,args.batch,args.summary)
    return resume(store,args.project)

def main(argv=None):
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True));args=p.parse_args(argv);s=Store(args.root)
    try:print(canonical(run(s,args)));return 0
    except (Blocked,ValueError,OSError) as e:print(canonical({'status':'BLOCKED','reason':str(e)}));return 2
    finally:s.db.close()

if __name__=='__main__':sys.exit(main())
