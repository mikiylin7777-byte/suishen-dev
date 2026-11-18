from .core import cooperative
"""Engineer-reviewed commands are separate from user acceptance prose and source materials."""
import json
from .core import Blocked,PROTOCOL,canonical,digest

def requirements(mvp):
    return {key:value for key,value in mvp.items() if key not in ('verification_review','verification_commands')}

def commands_checked(commands):
    if not isinstance(commands,list) or not 1<=len(commands)<=PROTOCOL['dispatch_verification']['max_commands']:
        raise Blocked('需要1至10条工程师受审验证命令；不能用验收描述代替')
    if any(not isinstance(c,str) or not c.strip() or c.strip() in ('true',':','exit 0') or len(c)>PROTOCOL['dispatch_verification']['max_command_length'] or any(x in c for x in ('\n','\r','\x00')) or c.lstrip().startswith(('#','echo ','printf ')) for c in commands):
        raise Blocked('验证命令为空、过长、含多行注入或只是输出文字')
    return list(commands)

@cooperative
def review(store,mvp,commands=None,*,project=None):
    from .team import require_readiness
    if project is None and mvp.get('requirement_id'):
        from .workflows import load
        project=load(store,mvp['requirement_id'])['project']
    binding=require_readiness(store,project=project)
    if any(not mvp.get(k) for k in PROTOCOL['mvp_required']):raise Blocked('先补齐MVP')
    requested=commands if commands is not None else mvp.get('verification_commands')
    harnesses=verified_harnesses(store,requested)
    values=candidate_commands_checked(requested,binding['workspace'],harnesses)
    version=None
    from pathlib import Path
    if (Path(binding['workspace'])/'.git').exists():
        from .workflows import repo_version
        version=repo_version(store,binding['project'])['sha256']
        if mvp.get('repo_version') and version!=mvp['repo_version']:raise Blocked('工程检查基线已变化，未生成审阅')
    return store.record('verification_review',{'repo_sha256':version,'harnesses':harnesses,**{'device':store.device,'workspace':binding['workspace'],'project':binding['project'],'binding_epoch':binding['epoch'],'mvp_sha256':digest(canonical(requirements(mvp)).encode()),'verification_commands':values,'reviewer':'local-engineer:'+store.device}})

def resolve(store,mvp,review_id,binding):
    if not review_id:raise Blocked('缺受审验证列表；已形成规格，尚不可提交')
    row=store.db.execute("SELECT body FROM records WHERE id=? AND kind='verification_review'",(review_id,)).fetchone()
    if not row:raise Blocked('验证审阅记录不存在；来源资料不能充当审阅')
    record=json.loads(row[0])
    expected={'device':store.device,'workspace':binding['workspace'],'project':binding['project'],'binding_epoch':binding['epoch'],'mvp_sha256':digest(canonical(requirements(mvp)).encode())}
    if any(record.get(k)!=v for k,v in expected.items()):raise Blocked('验证列表与当前MVP、目标或绑定版本不匹配，请重新审阅')
    if record.get('repo_sha256'):
        from .workflows import repo_version
        if repo_version(store,binding['project'])['sha256']!=record['repo_sha256']:raise Blocked('受审repo已变化，请重新工程审阅')
    for path,expected_hash in record.get('harnesses',{}).items():
        from pathlib import Path
        if digest(Path(path).read_bytes())!=expected_hash:raise Blocked('受审harness已变化，请重新审阅')
    values=commands_checked(record.get('verification_commands'))
    if 'verification_commands' in mvp and mvp['verification_commands']!=values:raise Blocked('MVP中的验证命令与受审列表不符')
    return values


def candidate_commands_checked(commands,source,allowed_harnesses=()):
    """Support candidate-relative checks; never silently rewrite old absolute targets."""
    import shlex
    from pathlib import Path
    values=commands_checked(commands);source=str(Path(source).resolve())
    for command in values:
        if source in command:raise Blocked('验收命令指向原项目；请使用工程候选cwd下的相对目标，不能验证旧代码')
        try:
            lexer=shlex.shlex(command,posix=True,punctuation_chars=True);lexer.whitespace_split=True;tokens=list(lexer)
        except ValueError:raise Blocked('验收命令引用格式未知，重新工程审阅') from None
        for index,token in enumerate(tokens):
            if source in token:raise Blocked('验收命令指向原项目，未派工')
            if '..' in Path(token.split('=',1)[-1]).parts:raise Blocked('验收目标不得穿越工程候选边界')
            if '{candidate}' in token and token!='{candidate}':raise Blocked('候选占位符只支持独立参数；相对测试路径以候选cwd解析')
            operand=token.split('=',1)[-1]
            if Path(operand).is_absolute():
                executable=index==0 and Path(operand).name in ('python','python3','python3.14','node','npm','bash','sh','zsh','git','make','pytest','test')
                harness=operand in allowed_harnesses and '{candidate}' in tokens
                if not executable and not harness and operand!='/dev/null':raise Blocked('绝对验收目标未绑定本候选；请改相对cwd，外部只读harness须先明确审阅登记')
    return values


def bind_candidate_commands(commands,source,candidate,harnesses=None):
    import re,shlex
    harnesses=harnesses or {}
    values=candidate_commands_checked(commands,source,harnesses)
    # Only whole explicitly registered script arguments are rebound.
    from pathlib import Path
    import shutil
    for original,expected in harnesses.items():
        file=Path(original)
        if digest(file.read_bytes())!=expected:raise Blocked('受审harness已变化')
        dest=Path(candidate)/'.portable-harnesses'/expected/file.name;dest.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(file,dest)
        argument=re.compile(r'(?<!\S)(?:'+re.escape(shlex.quote(original))+'|'+re.escape(original)+r')(?!\S)')
        values=[argument.sub(lambda _:shlex.quote(str(dest)),command) for command in values]
    pattern=re.compile(r'''(?<!\S)(?:"\{candidate\}"|'\{candidate\}'|\{candidate\})(?!\S)''')
    return [pattern.sub(lambda _:shlex.quote(str(candidate)),command) for command in values]


@cooperative
def register_harness(store,path,confirmed=False):
    from pathlib import Path
    from .migration import SECRET_NAME,SECRET_CONTENT
    path=Path(path).absolute()
    if confirmed is not True or any(p.is_symlink() for p in (path,*path.parents)) or not path.is_file() or SECRET_NAME.search(path.as_posix()):raise Blocked('须明确审阅本机普通只读harness文件')
    raw=path.read_bytes()
    if len(raw)>200000 or SECRET_CONTENT.search(raw):raise Blocked('harness过大或含疑似凭据，未登记')
    values=store.get('verification_harnesses',{});values[str(path)]=digest(raw);store.put('verification_harnesses',values)
    return {'registered':str(path),'sha256':values[str(path)],'copied_into_candidate_at_submit':True}

def verified_harnesses(store,commands=None):
    from pathlib import Path
    values=store.get('verification_harnesses',{})
    if commands is not None:values={path:value for path,value in values.items() if any(path in command for command in commands)}
    if any(not Path(path).is_file() or Path(path).is_symlink() or digest(Path(path).read_bytes())!=expected for path,expected in values.items()):raise Blocked('harness来源变化，需重新登记')
    return values
