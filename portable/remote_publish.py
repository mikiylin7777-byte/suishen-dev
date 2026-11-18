"""Bound remote PR operations. Transports are injectable; errors never echo credentials."""
import base64
import json
import os
import re
import shutil
import subprocess
from urllib.error import HTTPError
from urllib.parse import urlsplit, urlencode
from urllib.request import Request, build_opener, HTTPRedirectHandler
from .core import Blocked
from .git_portable import git


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,*args,**kwargs):
        raise Blocked('PR 接口重定向被拒绝；未转发凭据')

urlopen=build_opener(NoRedirect).open


def repository(url):
    if not isinstance(url,str):raise Blocked('远端身份未知')
    if url.startswith('git@github.com:'):
        host='github.com';path=url.split(':',1)[1];origin='https://github.com'
    else:
        try:p=urlsplit(url);port=p.port
        except ValueError:raise Blocked('远端身份无法解析') from None
        if p.scheme not in ('http','https','ssh') or (p.username and not (p.scheme=='ssh' and p.username=='git')) or p.password or p.query or p.fragment:
            raise Blocked('远端身份不支持或含凭据；保留候选')
        host=p.hostname;path=p.path.lstrip('/');origin=p.scheme+'://'+p.netloc
    path=path.removesuffix('.git')
    if not re.fullmatch(r'[\w.-]+/[\w.-]+',path):raise Blocked('远端仓库身份未知')
    if host=='github.com':
        if not url.startswith(('git@github.com:','ssh://','https://')) or (not url.startswith('git@') and port not in (None,22,443)):raise Blocked('GitHub 远端必须使用 HTTPS 或 SSH')
        platform='github';origin='https://github.com'
    elif (host in ('localhost','127.0.0.1') and p.scheme in ('http','https')) or (host=='gitea.com' and p.scheme=='https'):platform='gitea'
    else:raise Blocked('未知托管平台；保留候选')
    return {'platform':platform,'repo':path,'origin':origin}


def remote(repo):
    names=git(repo,'remote').stdout.decode().split()
    if not names:return None
    if 'origin' not in names and len(names)!=1:raise Blocked('多个远端且无 origin，目标不明确')
    return git(repo,'remote','get-url','origin' if 'origin' in names else names[0]).stdout.decode().strip()


def local_base(repo):
    names=git(repo,'remote').stdout.decode().split()
    name='origin' if 'origin' in names else names[0]
    ref=git(repo,'symbolic-ref','refs/remotes/'+name+'/HEAD',check=False).stdout.decode().strip()
    if ref:return ref.removeprefix('refs/remotes/'+name+'/')
    # No cached remote default: defer selection to live remote HEAD at publish.
    return None


def base_branch(repo,url=None,explicit=None):
    if explicit and (explicit.startswith('-') or git(repo,'check-ref-format','--branch',explicit,check=False).returncode):raise Blocked('无效 base')
    if not url:
        current=git(repo,'symbolic-ref','--short','HEAD',check=False).stdout.decode().strip()
        if not (explicit or current):raise Blocked('本地默认分支未知')
        selected=explicit or current
        git(repo,'rev-parse','--verify','refs/heads/'+selected)
        return selected
    result=network(repo,'ls-remote','--symref',url,'HEAD')
    match=re.search(r'^ref: refs/heads/(.+)\tHEAD$',result.stdout.decode(),re.M)
    if not match:raise Blocked('远端默认分支未知')
    default=match[1]
    names=git(repo,'remote').stdout.decode().split()
    name='origin' if 'origin' in names else names[0]
    local=git(repo,'symbolic-ref','refs/remotes/'+name+'/HEAD',check=False).stdout.decode().strip()
    if local and local!='refs/remotes/'+name+'/'+default:raise Blocked('本地与远端默认分支冲突')
    selected=explicit or default
    if not network(repo,'ls-remote','--heads',url,'refs/heads/'+selected).stdout.strip():raise Blocked('目标 base 不存在')
    return selected


def network(root,*args,check=True):
    try:
        p=subprocess.run(['git','-c','core.hooksPath='+os.devnull,'-C',str(root),*args],capture_output=True,env={**os.environ,'GIT_TERMINAL_PROMPT':'0'},timeout=300)
    except OSError:raise DeliveryError('not_sent','Git 未启动；请求未发送') from None
    except subprocess.TimeoutExpired:raise DeliveryError('sent_unknown','远端 Git 响应未知；请重试查询原操作') from None
    if check and p.returncode:raise Blocked('远端 Git 操作失败；保留候选与原分支')
    return p


class DeliveryError(Blocked):
    def __init__(self,state,message,code=None):
        super().__init__(message);self.state=state;self.code=code  # code：平台回的 HTTP 状态码，拿不到时为 None


def capability(identity):
    if identity['platform']=='github' and not shutil.which('gh'):
        raise DeliveryError('not_sent','缺少已登录的 gh；请求未发送，可恢复重试')


def gitea_headers(identity):
    headers={'Content-Type':'application/json'}
    token=os.environ.get('GITEA_TOKEN')
    if token:headers['Authorization']='token '+token;return headers
    # Reuse the owner's existing Git HTTP credential helper, without approving,
    # rejecting, logging or writing credentials. Public repositories may query
    # anonymously. A private deployment without API read capability stops safely.
    parsed=urlsplit(identity['origin'])
    request='protocol='+parsed.scheme+'\nhost='+parsed.netloc+'\npath='+identity['repo']+'.git\n\n'
    try:
        result=subprocess.run(['git','credential','fill'],input=request.encode(),capture_output=True,
            env={**os.environ,'GIT_TERMINAL_PROMPT':'0','GCM_INTERACTIVE':'Never'},timeout=30)
        fields=dict(line.split('=',1) for line in result.stdout.decode().splitlines() if '=' in line) if not result.returncode else {}
    except (OSError,subprocess.TimeoutExpired,UnicodeError):fields={}
    if fields.get('username') is not None and fields.get('password') is not None:
        headers['Authorization']='Basic '+base64.b64encode((fields['username']+':'+fields['password']).encode()).decode()
    return headers


def api(identity,method,path,payload=None):
    """Existing authentication only; classify proven non-delivery separately."""
    capability(identity)
    if identity['platform']=='github':
        cmd=['gh','api','--hostname','github.com','--method',method,path]
        if payload is not None:cmd+=['--input','-']
        try:p=subprocess.run(cmd,input=json.dumps(payload).encode() if payload is not None else None,capture_output=True,timeout=60)
        except OSError:raise DeliveryError('not_sent','gh 未启动；请求未发送，可恢复重试') from None
        except subprocess.TimeoutExpired:raise DeliveryError('sent_unknown','PR 响应超时；先查询原操作') from None
        if p.returncode:
            status=re.fullmatch(rb'gh: [^\r\n]* \(HTTP (\d{3})\)',(p.stderr or b'').splitlines()[0] if p.stderr else b'')
            code=int(status[1]) if status else 0
            state='rejected' if 400<=code<500 and code!=408 else 'sent_unknown'
            raise DeliveryError(state,'PR 请求被拒绝，可修复后重试' if state=='rejected' else 'PR 请求未确认；只能查询恢复',code or None)
        raw=p.stdout
    else:
        if method!='GET':raise DeliveryError('not_sent','Gitea 创建仅用现有 Git AGit，不走 token POST')
        req=Request(identity['origin']+'/api/v1/'+path,method=method,headers=gitea_headers(identity))
        try:
            with urlopen(req,timeout=60) as response:raw=response.read()
        except HTTPError as error:
            state='rejected' if 400<=error.code<500 and error.code!=408 else 'sent_unknown'
            code=error.code;error.close()
            raise DeliveryError(state,'既有 Gitea API 查询不可用；保留候选，不要求新增凭据',code) from None
        except Exception:raise DeliveryError('sent_unknown','Gitea 查询未确认；保留候选，恢复既有查询能力后重试') from None
    try:return json.loads(raw)
    except (ValueError,UnicodeError):raise DeliveryError('sent_unknown','PR 响应未知；重试时先查询') from None

api.preflight=capability


def open_pr(work,url,base,topic,title,body,transport=None,journal=None,verify_binding=None,recover_only=False):
    identity=repository(url);call=transport or api
    if git(work,'check-ref-format','refs/heads/'+topic,check=False).returncode:raise Blocked('发布分支名无效')
    getattr(call,'preflight',lambda identity:None)(identity)
    pending=journal[0].get(journal[1]) if journal else None
    if pending and (any(pending.get(k)!=v for k,v in {'identity':identity,'base':base,'topic':topic}.items()) or not re.fullmatch(r'(?:[0-9a-f]{40}|[0-9a-f]{64})',pending.get('head',''))):raise Blocked('原操作绑定或提交失效')
    if recover_only and not pending:raise Blocked('无持久原操作，禁止恢复或发送')
    author=None
    head_ref=topic
    if identity['platform']=='gitea':
        user=call(identity,'GET','user')
        if not isinstance(user,dict) or not isinstance(user.get('login'),str) or not re.fullmatch(r'[\w.-]+',user['login']) or type(user.get('id')) is not int or user['id']<=0:raise Blocked('无法核实 AGit 实际作者；未发送')
        author={'login':user['login'].lower(),'id':user['id']}
        if pending and pending.get('author')!=author:raise Blocked('原 AGit 作者缺失或认证作者变化；保留原操作，不重发')
        head_ref=topic if topic.startswith(author['login']+'/') else author['login']+'/'+topic
    prefix='repos/'+identity['repo']+'/pulls'
    query={'state':'all','base':base,'head':identity['repo'].split('/')[0]+':'+head_ref}
    rows=[]
    # Gitea deployments may ignore head/base filters. Scan all pages, then
    # require both repository identities locally before accepting a receipt.
    for page in range(1,101):
        batch=call(identity,'GET',prefix+'?'+urlencode({**query,'page':page,'limit':100,'per_page':100}))
        if not isinstance(batch,list) or any(not isinstance(r,dict) for r in batch):raise Blocked('PR 查询响应未知；禁止重复创建')
        rows.extend(batch)
        if len(batch)<100:break
    else:raise Blocked('PR 查询未穷尽；禁止创建')
    try:
        matches=[r for r in rows if r.get('base',{}).get('ref')==base and r.get('head',{}).get('ref')==head_ref and r.get('head',{}).get('repo',{}).get('full_name')==identity['repo'] and r.get('base',{}).get('repo',{}).get('full_name')==identity['repo']]
    except (AttributeError,TypeError):raise Blocked('PR 查询响应未知；禁止重复创建') from None
    if len(matches)>1:raise Blocked('PR 查询结果冲突')
    existing=network(work,'ls-remote','--heads',url,'refs/heads/'+topic).stdout.decode().split() if identity['platform']=='github' else []
    head=pending['head'] if recover_only else git(work,'rev-parse','HEAD').stdout.decode().strip()
    expected=pending['head'] if pending else head
    pending_value={'identity':identity,'base':base,'topic':topic,'head':expected}
    if author:pending_value['author']=author
    if existing and existing[0]!=expected:raise Blocked('发布分支已有不同提交；禁止覆盖')
    if matches:
        if matches[0].get('state','open')!='open':raise Blocked('原 PR 已关闭；不重复创建，候选保留')
        confirmed=receipt(identity,matches[0],base,head_ref,expected,author)
        if author:
            number=matches[0]['number']
            hidden='refs/pull/'+str(number)+'/head'
            actual=network(work,'ls-remote',url,hidden).stdout.decode().split()
            if actual!=[expected,hidden]:raise Blocked('AGit 隐藏 PR 引用缺失或 SHA 不符；禁止重发')
        elif not existing:raise Blocked('PR 分支缺失，不能确认原操作')
        if journal:journal[0].put(journal[1],{**pending_value,'state':'confirmed','pr_url':confirmed})
        return confirmed
    if pending and pending.get('state') not in ('not_sent','rejected'):
        raise Blocked('原 PR 创建结果仍未知；仅查询恢复，禁止重复创建')
    if recover_only:return None # Only proven not_sent/rejected may reach reconstruction.
    def save(state,**extra):
        if journal:journal[0].put(journal[1],{**pending_value,'state':state,**extra})
    def verify():
        if verify_binding:verify_binding()
    if head!=expected and (not existing or identity['platform']=='gitea'):raise Blocked('原操作提交不可重建；保留候选，不改写原操作')
    if not journal:raise Blocked('PR 创建需要持久操作记录，未发送')
    save('not_sent')
    if identity['platform']=='github':
        verify()
        if not existing:network(work,'push','--force-with-lease=refs/heads/'+topic+':',url,'HEAD:refs/heads/'+topic)
        verify();save('sent_unknown')
        try:result=call(identity,'POST',prefix,{'head':topic,'base':base,'title':title,'body':body})
        except DeliveryError as error:save(error.state);raise
    else:
        # Original AGit authentication and creation contract. No force-push option.
        verify();save('sent_unknown')
        try:
            pushed=network(work,'push',url,'HEAD:refs/for/'+base+'/'+topic,
                '-o','title='+title.replace('\n',' '),'-o','description='+body,check=False)
        except DeliveryError as error:save(error.state);raise
        if pushed.returncode:
            rejected=rb'^ ! \[remote rejected\] +HEAD -> '+re.escape(('refs/for/'+base+'/'+topic).encode())+rb' \([^\r\n]*\)$'
            if re.search(rejected,pushed.stderr,re.M):save('rejected')
            raise Blocked('AGit 推送未确认；保留候选，先查询恢复')
        # Never infer a PR from terminal output or a refs/for success alone.
        return open_pr(work,url,base,topic,title,body,call,journal,verify_binding)
    confirmed=receipt(identity,result,base,topic,expected)
    save('confirmed',pr_url=confirmed)
    return confirmed


def receipt(identity,result,base=None,topic=None,head=None,author=None):
    url=result.get('html_url') if isinstance(result,dict) else None
    if base is not None:
        try:
            valid=(result['base']['ref']==base and result['head']['ref']==topic and result['base']['repo']['full_name']==identity['repo'] and result['head']['repo']['full_name']==identity['repo'])
            if head is not None and result['head'].get('sha')!=head:valid=False
            if author:
                valid=valid and result['user']['login'].lower()==author['login'] and type(result['user']['id']) is int and result['user']['id']==author['id'] and type(result.get('number')) is int and result['number']>0
                valid=valid and url==identity['origin']+'/'+identity['repo']+'/pulls/'+str(result['number'])
        except (KeyError,TypeError,AttributeError):valid=False
        if not valid:raise Blocked('PR 绑定回执未知；重试时先查询')
    kind='pull' if identity['platform']=='github' else 'pulls'
    if not isinstance(url,str) or not re.fullmatch(re.escape(identity['origin']+'/'+identity['repo']+'/'+kind+'/')+r'\d+',url):raise Blocked('PR 回执未知；重试时先查询')
    return url
