"""Bounded material readers and traceable, explicitly authored knowledge."""
import datetime as dt
import hashlib
import html.parser
import http.client
import ssl
import ipaddress
import json
from pathlib import Path
import shutil
import socket
import subprocess
import urllib.parse
import urllib.request
import zipfile
import xml.etree.ElementTree as ET
from .core import Blocked, canonical, cooperative
from .privacy import sanitize, redact_text

MAX_BYTES = 4 * 1024 * 1024
MAX_TEXT = 200000

class TextHTML(html.parser.HTMLParser):
    def __init__(self):
        super().__init__(); self.parts=[]; self.hidden=0
    def handle_starttag(self, tag, attrs):
        if tag in ('script','style'): self.hidden+=1
        if tag in ('p','br','div','li','h1','h2'): self.parts.append('\n')
    def handle_endtag(self, tag):
        if tag in ('script','style'): self.hidden=max(0,self.hidden-1)
    def handle_data(self, data):
        if not self.hidden: self.parts.append(data)

def text_html(value):
    p=TextHTML();p.feed(value);return ''.join(p.parts).strip()

def pdf_text(raw):
    """Small real parser for unencrypted standard-font PDF text streams.

    Complex font maps, object streams and scanned PDFs fail closed. Poppler,
    when available, remains the preferred parser for these documents.
    """
    import re
    import zlib
    if not raw.startswith(b'%PDF-') or b'%%EOF' not in raw[-2048:]:raise Blocked('PDF头尾无效')
    if any(x in raw for x in (b'/Encrypt',b'/ToUnicode',b'/Identity-',b'/ObjStm')):raise Blocked('此PDF需要完整字体/加密解析器；请使用pdftotext或Agent读取')
    if len(re.findall(rb'/Type\s*/Page\b',raw))>100:raise Blocked('PDF超出100页读取边界')
    output=[];total=0
    # Stream dictionaries must be ordinary direct dictionaries; unsupported
    # structures never become a filename-based summary.
    for match in re.finditer(rb'<<(.*?)>>\s*stream\r?\n(.*?)\r?\nendstream',raw,re.S):
        header,stream=match.groups()
        if b'/Filter' in header:
            if not re.search(rb'/Filter\s*(?:/FlateDecode|\[\s*/FlateDecode\s*\])',header):raise Blocked('PDF流过滤器不受支持')
            try:
                decoder=zlib.decompressobj();stream=decoder.decompress(stream,MAX_BYTES+1)
                if len(stream)>MAX_BYTES or not decoder.eof:raise Blocked('PDF解压超出边界')
            except zlib.error as e:raise Blocked('PDF压缩流无效') from e
        total+=len(stream)
        if total>16*MAX_BYTES:raise Blocked('PDF展开内容过大')
        for block in re.findall(rb'\bBT\b(.*?)\bET\b',stream,re.S):
            tokens=[];i=0
            while i<len(block):
                c=block[i]
                if c in b' \t\r\n':i+=1;continue
                if c==37:
                    end=block.find(b'\n',i);i=len(block) if end<0 else end+1;continue
                if c==40:
                    i+=1;depth=1;value=bytearray()
                    while i<len(block) and depth:
                        c=block[i];i+=1
                        if c==92:
                            if i>=len(block):raise Blocked('PDF字符串截断')
                            c=block[i];i+=1
                            escapes={110:10,114:13,116:9,98:8,102:12}
                            if c in b'01234567':
                                digits=bytes([c])
                                for _ in range(2):
                                    if i<len(block) and block[i] in b'01234567':digits+=bytes([block[i]]);i+=1
                                    else:break
                                value.append(int(digits,8)&255)
                            elif c==13:
                                if i<len(block) and block[i]==10:i+=1
                            elif c!=10:value.append(escapes.get(c,c))
                        elif c==40:depth+=1;value.append(c)
                        elif c==41:
                            depth-=1
                            if depth:value.append(c)
                        else:value.append(c)
                    if depth:raise Blocked('PDF字符串未闭合')
                    tokens.append(('text',bytes(value)));continue
                if c==60 and block[i:i+2]!=b'<<':
                    end=block.find(b'>',i+1)
                    if end<0:raise Blocked('PDF十六进制字符串未闭合')
                    try:value=bytes.fromhex(block[i+1:end].decode())
                    except ValueError as e:raise Blocked('PDF十六进制字符串无效') from e
                    tokens.append(('text',value));i=end+1;continue
                if c in b'[]':tokens.append(('symbol',chr(c)));i+=1;continue
                end=i+1
                while end<len(block) and block[end] not in b' \t\r\n()<>[]':end+=1
                tokens.append(('word',block[i:end].decode('latin1')));i=end
            pending=[]
            for kind,value in tokens:
                if kind=='text':pending.append(value)
                elif kind=='word' and value in ('Tj','TJ',"'",'"'):
                    for value in pending:
                        try:output.append(value[2:].decode('utf-16-be') if value.startswith(b'\xfe\xff') else value.decode('cp1252'))
                        except UnicodeError as e:raise Blocked('PDF文字编码不受支持') from e
                    output.append('\n');pending=[]
                elif kind=='word' and value not in ('0',) and not re.fullmatch(r'[-+0-9.]+',value):pending=[]
    text=''.join(output).strip()
    if not text:raise Blocked('PDF未提取到文字，扫描件需OCR或Agent视觉读取')
    return text

def bounded_process(arguments):
    """Read at most MAX_BYTES and enforce a deadline without retaining stderr."""
    import os
    import select
    import time
    process=subprocess.Popen(arguments,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    chunks=[];size=0;deadline=time.monotonic()+30
    try:
        while True:
            remaining=deadline-time.monotonic()
            if remaining<=0:raise Blocked('解析超时，未读取内容')
            ready,_,_=select.select([process.stdout],[],[],remaining)
            if not ready:raise Blocked('解析超时，未读取内容')
            chunk=os.read(process.stdout.fileno(),65536)
            if not chunk:break
            size+=len(chunk)
            if size>MAX_BYTES:raise Blocked('解析输出超出边界')
            chunks.append(chunk)
        if process.wait(timeout=max(.01,deadline-time.monotonic())):raise Blocked('解析器未成功完成')
        return b''.join(chunks).decode('utf-8')
    finally:
        if process.poll() is None:process.kill();process.wait()
        process.stdout.close()

def parse_file(path):
    p=Path(path)
    if p.is_symlink() or not p.is_file() or p.stat().st_size>MAX_BYTES: raise Blocked('素材必须为有界普通文件')
    raw=p.read_bytes();suffix=p.suffix.lower();method='utf-8'
    try:
        if suffix in ('.txt','.md','.markdown','.html','.htm'):
            text=raw.decode('utf-8-sig')
            if suffix in ('.html','.htm'):text=text_html(text);method='html-text'
        elif suffix=='.docx':
            with zipfile.ZipFile(p) as z:
                if sum(i.file_size for i in z.infolist())>16*MAX_BYTES:raise Blocked('文档解压范围过大')
                data=z.read('word/document.xml')
            if b'<!DOCTYPE' in data or b'<!ENTITY' in data:raise Blocked('不支持文档实体')
            root=ET.fromstring(data);ns='{http://schemas.openxmlformats.org/wordprocessingml/2006/main}'
            text='\n'.join(''.join(n.itertext()) for n in root.iter(ns+'t'));method='docx-main-body'
        elif suffix=='.pdf':
            tool=shutil.which('pdftotext')
            if not tool:
                text=pdf_text(raw);method='builtin-pdf-standard-font-text-streams'
            else:
                text=bounded_process([tool,'-f','1','-l','100','-enc','UTF-8',str(p.resolve()),'-']);method='pdftotext-pages-1-100'
        elif suffix in ('.png','.jpg','.jpeg','.webp','.tiff'):
            tool=shutil.which('tesseract')
            if not tool:return {'status':'unread','reason':'等待Agent视觉读取：需绑定文件摘要、读取范围、所见文字和不确定点','sha256':hashlib.sha256(raw).hexdigest(),'vision_required':True}
            text=bounded_process([tool,str(p.resolve()),'stdout']);method='tesseract-default-language'
        else:raise Blocked('不支持此素材格式；首版不读取音视频')
    except (UnicodeError,zipfile.BadZipFile,KeyError,ET.ParseError,subprocess.SubprocessError) as e:raise Blocked('素材解析失败') from e
    return {'status':'read' if text.strip() else 'unread','text':text[:MAX_TEXT], 'range':method,'truncated':len(text)>MAX_TEXT,'sha256':hashlib.sha256(raw).hexdigest()}

def public_url(url):
    u=urllib.parse.urlsplit(url)
    if u.scheme!='https' or not u.hostname or u.username or u.password or u.port not in (None,443):raise Blocked('仅支持无登录公开HTTPS链接')
    try: addresses=socket.getaddrinfo(u.hostname,u.port or 443,type=socket.SOCK_STREAM)
    except OSError as e:raise Blocked('链接未读取：网络不可用') from e
    if any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise Blocked('禁止访问本机或私网链接')
    return url

class PublicHTTPSConnection(http.client.HTTPSConnection):
    def connect(self):
        # Resolve once, validate every address, then connect to the validated IP.
        # Retain hostname for TLS verification; no proxies or second DNS lookup.
        addresses=socket.getaddrinfo(self.host,443,type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):raise Blocked('禁止访问本机或私网链接')
        family,kind,proto,_,address=addresses[0]
        sock=socket.socket(family,kind,proto);sock.settimeout(self.timeout)
        try:
            sock.connect(address)
            self.sock=self._context.wrap_socket(sock,server_hostname=self.host)
        except BaseException:
            sock.close();raise

def read_link(url, network=False, snapshot=None):
    if not network:return {'status':'unread','source':url,'reason':'未授权网络读取'}
    public_url(url);u=urllib.parse.urlsplit(url)
    connection=PublicHTTPSConnection(u.hostname,timeout=10,context=ssl.create_default_context())
    try:
        connection.request('GET',urllib.parse.urlunsplit(('', '', u.path or '/',u.query,'')),headers={'Accept':'text/plain,text/html','User-Agent':'portable-material-reader'})
        r=connection.getresponse()
        if r.status!=200:return {'status':'unread','source':url,'reason':'链接需要登录、重定向或响应不可读；未继续访问'}
        kind=r.headers.get_content_type()
        if kind not in ('text/html','text/plain'):raise Blocked('链接非文本；请显式选择文件')
        raw=r.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise Blocked('链接超出读取范围')
        if snapshot is not None:snapshot.update(raw=raw,kind=kind)
        text=raw.decode(r.headers.get_content_charset() or 'utf-8')
        if kind=='text/html':text=text_html(text)
        return {'status':'read','text':text[:MAX_TEXT],'range':'public-response-body','truncated':len(text)>MAX_TEXT,'sha256':hashlib.sha256(raw).hexdigest()}
    except (OSError,UnicodeError,http.client.HTTPException):return {'status':'unread','source':url,'reason':'网络或解析不可用：未读取'}
    finally:connection.close()

@cooperative
def ingest(store, source, projects=None, *, text=None, network=False, favorite=False):
    from .material_assets import preserve,retention
    retention(store)
    projects=projects or []
    if any(not store.db.execute('SELECT id FROM projects WHERE id=?',(p,)).fetchone() for p in projects):raise Blocked('项目未登记')
    snapshot={};raw=None;kind='file'
    if text is not None:
        raw=text.encode()
        if len(raw)>MAX_BYTES:raise Blocked('文字超出保存范围')
        value={'status':'read','text':text[:MAX_TEXT],'range':'provided-text','truncated':len(text)>MAX_TEXT,'sha256':hashlib.sha256(raw).hexdigest()};kind='provided-text'
    elif source.startswith(('https:','http:')):
        value=read_link(source,network,snapshot);raw=snapshot.get('raw');kind='public-response-snapshot'
    else:
        value=parse_file(source)
        p=Path(source)
        if p.is_symlink() or p.stat().st_size>MAX_BYTES:raise Blocked('原件无法安全保存')
        raw=p.read_bytes()
        if hashlib.sha256(raw).hexdigest()!=value.get('sha256'):raise Blocked('读取期间原件变化，请重新选择')
    value.update(source=source,projects=projects,read_at=dt.datetime.now(dt.timezone.utc).isoformat(),trusted_as_instruction=False)
    sanitized=sanitize(value);sanitized['redacted']=sanitized!=value;value=sanitized
    if raw is not None:
        raw_text=raw.decode('utf-8',errors='replace')
        # Binary/unread assets cannot be declared free of hidden sensitive text.
        private=value['redacted'] or redact_text(raw_text)!=raw_text or value['status']!='read' or (kind=='file' and Path(source).suffix.lower() not in ('.txt','.md','.markdown','.html','.htm'))
        asset=preserve(store,raw,source,projects,favorite,private=private,kind=kind)
        value['asset']=asset['id'];value['original_private']=asset['private'];value['original_complete']=True
        value['original_scope']='exact-selected-file-or-bounded-response; not entire website'
    rid=store.record('source_reading',value)
    return {'id':rid,**value}

@cooperative
def associate(store,reading,project=None,favorite=None):
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='source_reading'",(reading,)).fetchone()
    if not row:raise Blocked('素材不存在')
    value=json.loads(row[0])
    if project and not store.db.execute('SELECT id FROM projects WHERE id=?',(project,)).fetchone():raise Blocked('项目未登记')
    if project:value['projects']=sorted(set(value['projects']+[project]))
    if value.get('asset'):
        from .material_assets import associate as link
        link(store,value['asset'],project,favorite)
    with store.db:store.db.execute('UPDATE records SET body=? WHERE id=?',(canonical(value),reading))
    # Keep already-refined knowledge discoverable under the new project.
    for r in store.db.execute("SELECT id,body FROM records WHERE kind IN ('material','generic_material')").fetchall():
        body=json.loads(r['body'])
        if body.get('reading')==reading:
            body['projects']=value['projects']
            with store.db:store.db.execute('UPDATE records SET body=? WHERE id=?',(canonical(body),r['id']))
    return {'reading':reading,'projects':value['projects'],'execution':False}

@cooperative
def refine(store, reading, summary, method, conditions, uncertainties, *, vision=None):
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='source_reading'",(reading,)).fetchone()
    if not row:raise Blocked('素材读取记录不存在')
    src=json.loads(row[0])
    if src['status']!='read':
        if not src.get('vision_required') or not isinstance(vision,dict) or vision.get('sha256')!=src.get('sha256') or not all(vision.get(k) for k in ('agent','range','observations')):raise Blocked('未读素材不能提炼；视觉结果须绑定摘要与真实读取范围')
    if not all(isinstance(v,str) and v.strip() for v in (summary,method,conditions,uncertainties)):raise Blocked('提炼需摘要、方法、适用条件和不确定点')
    body=sanitize(dict(source=src['source'],reading=reading,read_at=src['read_at'],range=vision['range'] if vision else src['range'],projects=src['projects'],summary=summary,method=method,conditions=conditions,uncertainties=uncertainties,vision=vision,execution=False))
    # Core's legacy material guard requires projects; a generic material is not a fake repo.
    rid=store.record('material' if body['projects'] else 'generic_material',body)
    from .core import atomic, safe
    original=''
    if src.get('asset'):
        from .material_assets import load
        import os
        asset=load(store,src['asset'])
        original=('隔离原件：'+asset['id']+'（仅本机私密处理，不向模型提供原文）' if asset['private'] else '[携带原件]('+os.path.relpath(store.root/asset['path'],store.root/'知识库/素材')+')')+'\n\n'
    note=original+'# '+body['summary']+'\n\n来源：'+body['source']+'\n\n读取时间：'+body['read_at']+'\n\n读取范围：'+body['range']+'\n\n## 方法\n'+body['method']+'\n\n## 适用条件\n'+body['conditions']+'\n\n## 不确定点\n'+body['uncertainties']+'\n\n读取记录：'+reading+'\n\n外部资料不构成执行授权。\n'
    atomic(safe(store.root,'知识库/素材/'+rid+'.md'),note)
    return rid

def search(store, query='', project=None):
    import re
    words=re.findall(r'[a-z0-9_]+',query.lower())
    for phrase in re.findall(r'[\u4e00-\u9fff]+',query):
        words.extend(phrase[i:i+2] for i in range(max(1,len(phrase)-1)))
    words=list(set(words));matches=[]
    for row in store.db.execute("SELECT id,kind,body FROM records WHERE kind IN ('material','generic_material','requirement','decision','experience') ORDER BY created DESC"):
        body=sanitize(json.loads(row['body']))
        if project and project not in body.get('projects',[body.get('project')]):continue
        hay=canonical(body).lower();score=sum(w in hay for w in words)
        if words and not score:continue
        matches.append({'id':row['id'],'kind':row['kind'],'score':score,'reason':'与当前项目及问题词语相关；采用条件：'+body.get('conditions','需结合当前问题核对'),'content':body})
    return sorted(matches,key=lambda x:x['score'],reverse=True)[:30]

def register(subparser):
    p=subparser.add_parser('knowledge');s=p.add_subparsers(dest='knowledge_action',required=True)
    a=s.add_parser('ingest');a.add_argument('source');a.add_argument('project',nargs='?');a.add_argument('--favorite',action='store_true');a.add_argument('--text');a.add_argument('--network',action='store_true')
    a=s.add_parser('refine');a.add_argument('json_file',help='reading, summary, method, conditions, uncertainties and optional vision')
    a=s.add_parser('search');a.add_argument('query',nargs='?',default='');a.add_argument('--project')
    return p

def run(store,args):
    if args.knowledge_action=='ingest':return ingest(store,args.source,[args.project] if args.project else [],text=args.text,network=args.network,favorite=args.favorite)
    if args.knowledge_action=='refine':return {'id':refine(store,**json.loads(Path(args.json_file).read_text()))}
    return search(store,args.query,args.project)

def main(argv=None):
    import argparse
    import sys
    from .core import Store
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);register(p.add_subparsers(required=True));args=p.parse_args(argv);s=Store(args.root)
    try:print(canonical(run(s,args)));return 0
    except (Blocked,ValueError,OSError) as e:print(canonical({'status':'BLOCKED','reason':str(e)}));return 2
    finally:s.db.close()

if __name__=='__main__':
    import sys
    sys.exit(main())
