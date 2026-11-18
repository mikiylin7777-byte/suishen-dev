"""Versioned, opt-in component assets. No installers or service activation.

Only local Python adapters below can execute. Container data is never code.
"""
import ctypes
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import subprocess
import struct
import sys
import tempfile
import uuid
from . import sealed_bundle as bundle

VERSION = 1
VERSION_LAUNCHER = 'import json,pathlib,sys\nif sys.argv[1:] != ["--version"]: sys.exit(2)\nprint(json.loads((pathlib.Path(__file__).parent/"resources/version.json").read_text())["version"])\n'
KINDS = {'app', 'cli', 'config', 'launchd', 'tunnel', 'hook'}
NATIVE_ADAPTERS = {'codex-native', 'claude-native', 'opencode-native'}
ADAPTERS = NATIVE_ADAPTERS | {'app-code', 'native-closure', 'version-resource', 'json-config',
            'launchd-recipe', 'launchd-plist', 'tunnel-recipe', 'manifest-hook', 'unsupported'}


class ComponentError(ValueError):
    """Safe message; never includes config, subprocess output or source paths."""


def _require(ok, message='Component validation failed'):
    if not ok:
        raise ComponentError(message)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def _hash(value):
    return hashlib.sha256(_json(value)).hexdigest()


def _id(value):
    _require(isinstance(value, str) and re.fullmatch(r'[a-zA-Z0-9_-]{1,80}', value))
    return value


def _read(path):
    _require(path.is_file() and not path.is_symlink() and path.stat().st_size <= 4*1024*1024)
    try:
        return json.loads(path.read_bytes())
    except (ValueError, OSError):
        raise ComponentError('Invalid component document') from None


def _write(path, value):
    with open(path, 'xb') as f:
        os.chmod(path, 0o600)
        f.write(_json(value))


def register(component_id, source, *, kind, adapter='unsupported', version='unknown',
             selected=False, architectures=(), login_required=False, program_bindings=None):
    """Explicit single source only. Registration does not read source contents.

    Returned private selection contains a path; do not log/publish it. doctor()
    and all package metadata omit source paths. No discovery of HOME occurs.
    """
    _id(component_id)
    _require(kind in KINDS and adapter in ADAPTERS)
    _require(isinstance(version, str) and re.fullmatch(r'[A-Za-z0-9_.+ -]{1,100}', version))
    _require(set(architectures) <= {'arm64', 'x86_64', 'any'})
    pairs = {**{a:'cli' for a in NATIVE_ADAPTERS}, 'app-code':'app', 'native-closure':'cli', 'version-resource':'cli',
             'json-config':'config', 'launchd-recipe':'launchd', 'launchd-plist':'launchd',
             'tunnel-recipe':'tunnel', 'manifest-hook':'hook'}
    _require(adapter == 'unsupported' or pairs[adapter] == kind)
    return dict(schema=VERSION, id=component_id, source=str(Path(source).absolute()),
                kind=kind, adapter=adapter, version=version, selected=bool(selected),
                architectures=list(architectures), login_required=bool(login_required),
                program_bindings=program_bindings or {})


def doctor(selections):
    """Read only lstat of explicit sources; never reads accounts/configuration."""
    return [dict(id=s['id'], kind=s['kind'], adapter=s['adapter'],
                 selected=s['selected'], private=s['kind'] in {'config','launchd','tunnel'},
                 source_available=os.path.lexists(s['source']),
                 state='cannot_migrate' if s['adapter']=='unsupported' else 'not_saved')
            for s in selections]


def _copy(source, target):
    _require(not source.is_symlink(), 'Root symlink is not a portable closure')
    if source.is_dir():
        before = bundle._tree(source)
        shutil.copytree(source, target, symlinks=True)
        _require(before == bundle._tree(source) == bundle._tree(target), 'Source changed')
    else:
        _require(source.is_file())
        target.mkdir(mode=0o700)
        before = bundle._digest(source)
        shutil.copy2(source, target/'asset')
        _require(before == bundle._digest(source) == bundle._digest(target/'asset'))


def _recipe(path, adapter):
    value = _read(path)
    _validate_recipe(value, adapter)
    return value


def save(selections, target, *, recipient=None):
    """Explicitly selected private assets require age; no plaintext fallback."""
    chosen = [s for s in selections if s['selected']]
    _require(chosen and len({s['id'] for s in chosen}) == len(chosen))
    for s in chosen:
        if s['adapter'] in {'launchd-recipe','tunnel-recipe'}:
            _recipe(Path(s['source']), s['adapter'])
    private = any(s['kind'] in {'config','launchd','tunnel'} for s in chosen)
    _require(not private or recipient, 'Private selections require an age recipient')
    def populate(root):
        rows = []
        for s in chosen:
            # Validate registration again, including imported selections.
            register(s['id'], s['source'], kind=s['kind'], adapter=s['adapter'],
                     version=s['version'], architectures=s['architectures'], program_bindings=s.get('program_bindings'))
            row = {k:s[k] for k in ('id','kind','adapter','version','architectures','login_required')}
            row['state'] = 'saved' if s['adapter'] != 'unsupported' else 'cannot_migrate'
            if s['adapter'] != 'unsupported':
                src = Path(s['source'])
                dest = root/s['id']
                if s['adapter'] in NATIVE_ADAPTERS:
                    row['native'] = _save_native(s, dest)
                elif s['adapter'] == 'launchd-plist':
                    inspected = inspect_launchd(src, program_bindings=s.get('program_bindings'))
                    dest.mkdir(mode=0o700)
                    if inspected['state']=='cannot_migrate':
                        _copy(src, dest/'original')
                        row['state'] = 'cannot_migrate'
                        row['inspection'] = inspected
                    else:
                        _write(dest/'asset', inspected['recipe'])
                elif s['adapter'] in {'launchd-recipe','tunnel-recipe'}:
                    value = _recipe(src, s['adapter'])
                    dest.mkdir(mode=0o700)
                    _write(dest/'asset', value)
                else:
                    _copy(src, dest)
                if s['adapter'] == 'app-code':
                    info = dest/'Contents/Info.plist'
                    try:
                        data = plistlib.loads(info.read_bytes())
                        row['app'] = {k:data.get(k) for k in ('CFBundleIdentifier','CFBundleShortVersionString','CFBundleVersion')}
                    except (OSError, ValueError, plistlib.InvalidFileException):
                        raise ComponentError('App Info.plist is required') from None
                    # Signature bytes/resources stay intact; no claim of trust verification.
                    row['signature'] = 'preserved_unverified'
                    row['code_metadata'] = app_code_metadata(dest)
                row['tree'] = bundle._tree(dest)
            row['hash'] = _hash(row)
            rows.append(row)
        _write(root/'components.json', {'schema':VERSION,'components':rows})
    return bundle.pack(None, target, sensitive=private or bool(recipient),
                       recipient=recipient, populate=populate)


def _catalog(root):
    doc = _read(root/'components.json')
    _require(set(doc)=={'schema','components'} and doc['schema']==VERSION)
    _require(isinstance(doc['components'], list))
    ids = set()
    for row in doc['components']:
        _id(row['id'])
        _require(row['id'] not in ids)
        ids.add(row['id'])
        _require(row['adapter'] in ADAPTERS and row['kind'] in KINDS)
        register(row['id'], '.', kind=row['kind'], adapter=row['adapter'],
                 version=row['version'], architectures=row['architectures'])
        if row['adapter'] in NATIVE_ADAPTERS:
            _validate_native_metadata(row['adapter'],row['native'])
        _require(row['hash']==_hash({k:v for k,v in row.items() if k!='hash'}))
        if row['adapter'] != 'unsupported':
            _require(bundle._tree(root/row['id'])==row['tree'], 'Component changed')
    _require({p.name for p in root.iterdir()} == ids-{
        r['id'] for r in doc['components'] if r['adapter']=='unsupported'} | {'components.json'})
    return doc['components']


def plan(root, *, machine=None):
    """Validate all saved bytes. Does not execute any saved executable."""
    root = Path(root)
    rows = _catalog(root)
    result = []
    for row in rows:
        a = row['adapter']
        state, reason = 'restored', 'asset_hash_verified'
        if a=='unsupported':
            state, reason = 'cannot_migrate', 'adapter_unavailable_source_not_saved'
        elif a in NATIVE_ADAPTERS:
            state, reason = 'pending_verification', 'materialize_native_then_version_probe'
        elif a in {'native-closure','app-code'}:
            state, reason = 'needs_rebuild', 'architecture_dependency_or_signature_not_verified'
        elif a=='version-resource':
            state, reason = 'needs_rebuild', 'local_launcher_required'
        elif a in {'launchd-recipe','launchd-plist','tunnel-recipe','manifest-hook'}:
            state, reason = 'pending_activation', 'staged_disabled_no_login_or_approval_inherited'
        if a=='tunnel-recipe' and _read(root/row['id']/'asset').get('profile')!='ssh-forward':
            state, reason = 'cannot_migrate', 'structured_ssh_forward_parameters_required'
        if row.get('inspection', {}).get('state')=='cannot_migrate':
            state, reason = 'cannot_migrate', 'unsupported_launchd_fields_preserved_for_local_review'
        if a=='app-code' and machine is None:
            evidence = verify_app(root, row['id'])
            state, reason = evidence['state'], evidence['reason']
        actual_arch = row.get('code_metadata', {}).get('architectures', [])
        if actual_arch and (machine or platform.machine()) not in actual_arch:
            state, reason = 'needs_rebuild', 'actual_macho_architecture_mismatch_no_app_build_adapter'
        if row['architectures'] and (machine or platform.machine()) not in row['architectures'] and 'any' not in row['architectures']:
            state, reason = 'needs_rebuild', 'architecture_mismatch'
        result.append(dict(id=row['id'], hash=row['hash'], state=state, reason=reason,
                           login='needs_login' if row['login_required'] else 'not_required'))
    return result


def restore(package, target, *, identity_file=None):
    """Validate in private staging then atomically publish a fresh generation."""
    target = bundle._parents(target)
    _require(not os.path.lexists(target), 'Target exists; use a fresh generation')
    with tempfile.TemporaryDirectory(prefix='.components-', dir=target.parent) as tmp:
        os.chmod(tmp, 0o700)
        stage = Path(tmp)/'tree'
        bundle.restore(package, stage, identity_file=identity_file)
        result = plan(stage)
        bundle._publish(stage, target)
    return result


def rebuild_version_resource(root, component_id, prefix):
    """Concrete offline synthetic adapter: rebuild a launcher from version.json.

    No executable or shell from the package is used. Useful for integration
    fixtures and simple resource-only tools, NOT a Codex/Claude/OpenCode installer.
    """
    root = Path(root)
    row = next((r for r in _catalog(root) if r['id']==component_id), None)
    _require(row and row['adapter']=='version-resource')
    resource = _read(root/component_id/'version.json')
    _require(set(resource)=={'version'} and resource['version']==row['version'])
    prefix = bundle._parents(prefix)
    _require(not os.path.lexists(prefix), 'Prefix exists')
    with tempfile.TemporaryDirectory(prefix='.rebuild-', dir=prefix.parent) as tmp:
        os.chmod(tmp, 0o700)
        staged = Path(tmp)/'prefix'
        staged.mkdir(mode=0o700)
        shutil.copytree(root/component_id, staged/'resources', symlinks=True)
        # Fixed adapter code; resource data cannot become Python source.
        script = VERSION_LAUNCHER
        (staged/'version.py').write_text(script)
        probe = subprocess.run([sys.executable,'-I',str(staged/'version.py'),'--version'],
                               stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=10,
                               env={'PATH':os.defpath, 'HOME':str(staged)})
        _require(probe.returncode==0 and probe.stdout.decode().strip()==row['version'])
        bundle._publish(staged, prefix)
    return dict(id=component_id, state='restored', probe='version_verified',
                runtime='current_python', tree=bundle._tree(prefix))


def stage_config(root, component_id, target, *, path_fields=None, bindings=None):
    """Only explicitly named top-level JSON path fields can be rebound.

    Unmapped absolute path values anywhere in the document fail closed. No
    runtime/session/environment fields are accepted. Output stays private.
    """
    root = Path(root)
    row = next((r for r in _catalog(root) if r['id']==component_id), None)
    _require(row and row['adapter']=='json-config')
    value = _read(root/component_id/'asset')
    _require(isinstance(value, dict))
    for field, binding in (path_fields or {}).items():
        _require(field in value and binding in (bindings or {}))
        value[field] = {'component_binding':_id(binding)}
    def check(v):
        if isinstance(v, dict):
            forbidden = {'pid','token','session','cookies','cookie','environment','env',
                         'command','shell','argv','lock','password','secret','api_key',
                         'apikey','credentials','credential','login','auth','authorization',
                         'approved','approval','permissions','caller','device','hostname'}
            _require(all(isinstance(k,str) and k.lower() not in forbidden and
                         not re.search(r'(?:token|password|secret|session|credential|cookie)', k, re.I)
                         for k in v), 'Runtime or account state requires local regeneration')
            for item in v.values(): check(item)
        elif isinstance(v, list):
            for item in v: check(item)
        elif isinstance(v, str):
            _require(not v.startswith(('/', '~')) and not re.search(r'[A-Za-z]:[\\/]', v), 'Unmapped old path')
    check(value)
    for name, path in (bindings or {}).items():
        _id(name)
        local = bundle._parents(path)
        _require(local.is_dir() and not local.is_symlink(), 'Binding requires an existing new local directory')
    target = bundle._parents(target)
    _write(target, {'disabled':True,'configuration':value,'bindings':bindings or {}})
    return {'state':'restored','disabled':True}


def prepare_gate(root, component_id, *, device, target, scopes):
    """Return a fresh proposal for existing production Gate, never activate."""
    rows = _catalog(Path(root))
    row = next((r for r in rows if r['id']==component_id), None)
    _require(row and row['adapter'] in {'launchd-recipe','launchd-plist','tunnel-recipe','manifest-hook'} and
             row['state'] != 'cannot_migrate')
    _id(device)
    _require(scopes and all(isinstance(s,str) and re.fullmatch(r'[a-z_-]{1,60}', s) for s in scopes))
    return dict(schema=VERSION, component=component_id, hash=row['hash'], device=device,
                target=str(Path(target).absolute()), scopes=sorted(set(scopes)),
                nonce=uuid.uuid4().hex, disabled=True)


def decision(proposal, *, approved=False):
    """Caller must obtain an explicit NEW local decision. Not an activator."""
    return {'binding':_hash(proposal), 'approved':bool(approved)}


def gate_valid(proposal, local_decision, *, device, target, component_hash, scopes):
    return bool(local_decision.get('approved') is True and
                local_decision.get('binding')==_hash(proposal) and
                proposal.get('device')==device and
                proposal.get('target')==str(Path(target).absolute()) and
                proposal.get('hash')==component_hash and
                proposal.get('scopes')==sorted(set(scopes)))


def probe_native(root, component_id, executable, *, trusted_component_hash):
    """Opt-in native CLI --version, after local trust AND Mach-O closure checks.

    Only recognized tool names, native Mach-O, and system dylibs supported.
    Node/Homebrew symlinks, scripts, @rpath and non-system dylibs need a separate
    runtime adapter; never guessed portable. No accounts/home are inherited.
    """
    root = Path(root)
    row = next((r for r in _catalog(root) if r['id']==component_id), None)
    _require(row and row['adapter']=='native-closure' and
             row['hash']==trusted_component_hash, 'Local code trust required')
    rel = bundle._name(executable)
    binary = root/component_id/str(rel)
    _require(binary.name in {'codex','claude','opencode'})
    _require(not binary.is_symlink() and binary.is_file() and os.access(binary, os.X_OK))
    with open(binary, 'rb') as f:
        magic = f.read(4)
    _require(magic in {b'\xcf\xfa\xed\xfe',b'\xfe\xed\xfa\xcf',b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf'},
             'Native Mach-O required; external runtime unsupported')
    _require(platform.system()=='Darwin' and Path('/usr/bin/otool').exists(), 'Mach-O inspector unavailable')
    arch = subprocess.run(['/usr/bin/lipo','-archs',str(binary)], capture_output=True, timeout=10)
    _require(arch.returncode==0 and platform.machine() in arch.stdout.decode().split(), 'Architecture incompatible')
    deps = subprocess.run(['/usr/bin/otool','-L',str(binary)], capture_output=True, timeout=10)
    _require(deps.returncode==0, 'Dependency inspection failed')
    for line in deps.stdout.decode().splitlines()[1:]:
        # Fat binary section headings are not dependencies.
        if not line.startswith((' ', '\t')):
            continue
        name = line.strip().split(' (',1)[0]
        _require(name.startswith(('/usr/lib/','/System/Library/')), 'Non-system dependency needs rebuild adapter')
    with tempfile.TemporaryDirectory(prefix='.component-probe-') as home:
        os.chmod(home, 0o700)
        env = {'PATH':'/usr/bin:/bin', 'HOME':home, 'TMPDIR':home,
               'XDG_CONFIG_HOME':home, 'XDG_CACHE_HOME':home,
               'DISABLE_AUTOUPDATER':'1', 'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1'}
        result = subprocess.run([str(binary.resolve()),'--version'], env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=15)
    _require(result.returncode==0 and row['version'].encode() in result.stdout[:4096], 'Version probe failed')
    return {'id':component_id,'state':'restored','probe':'version_verified',
            'login':'needs_login' if row['login_required'] else 'not_required'}


def inspect_launchd(source, *, program_bindings=None):
    """Selected plist -> whitelisted typed recipe; unknown original is retained."""
    source = Path(source)
    _require(source.is_file() and not source.is_symlink() and source.stat().st_size<4*1024*1024)
    try:
        data = plistlib.loads(source.read_bytes())
    except (ValueError, plistlib.InvalidFileException):
        raise ComponentError('Invalid launchd plist') from None
    _require(isinstance(data,dict))
    allowed = {'Label','Program','ProgramArguments','WorkingDirectory','StartInterval',
               'StartCalendarInterval','RunAtLoad','KeepAlive','Disabled'}
    reasons = []
    unknown = set(data)-allowed
    if unknown: reasons.append('unknown_fields')
    bindings = program_bindings or {}
    args = data.get('ProgramArguments', [])
    program = data.get('Program', args[0] if isinstance(args,list) and args else None)
    recipe = None
    try:
        _require(isinstance(program,str) and program in bindings)
        descriptor = bindings[program]
        if isinstance(descriptor,str): descriptor = {'component':descriptor,'profile':'component-direct'}
        _require(isinstance(descriptor,dict) and set(descriptor)<={'component','profile','path_bindings'})
        profile = descriptor.get('profile','component-direct')
        _require(isinstance(args,list) and (not args or args[0]==program))
        parameters = _parse_service_argv(profile,args[1:] if args else [],descriptor.get('path_bindings',{}))
        recipe = {'program_component':_id(descriptor['component']), 'profile':profile,
                  'parameters':parameters,'external_connection':True}
        if 'WorkingDirectory' in data:
            wd = data['WorkingDirectory']
            _require(isinstance(wd,str) and wd in bindings)
            bound = bindings[wd]
            recipe['working_component'] = _id(bound if isinstance(bound,str) else bound['component'])
        if 'StartInterval' in data: recipe['schedule']=data['StartInterval']
        if 'StartCalendarInterval' in data: recipe['calendar']=data['StartCalendarInterval']
        _validate_recipe(recipe,'launchd-recipe')
    except (ValueError,KeyError,TypeError):
        reasons.append('ProgramArguments_paths_or_schedule_requires_trusted_adapter')
    if reasons:
        return {'state':'cannot_migrate','fields':sorted(allowed & set(data)),
                'unknown_field_count':len(unknown),'reasons':reasons}
    return {'state':'pending_activation','recipe':recipe,
            'discarded_runtime_policy':['Label','RunAtLoad','KeepAlive','Disabled'],
            'reason':'new_identity_disabled_no_source_runtime_state'}


def app_code_metadata(root):
    """Read Mach-O headers, signature resource presence; never run the app.

    Claimed plist/platform architecture is not used as hardware evidence. Fat
    and thin Mach-O CPUs are read directly. Unknown formats stay unknown.
    Cryptographic code signature trust is intentionally not asserted.
    """
    root = Path(root)
    architectures = set()
    native_files = 0
    mapping = {0x0100000c:'arm64',0x01000007:'x86_64'}
    for entry in bundle._tree(root):
        if entry['kind']!='file' or not _is_macho(root/entry['path']): continue
        with open(root/entry['path'],'rb') as f:
            header = f.read(4096)
        if len(header)<8: continue
        magic = header[:4]
        cpus = []
        if magic in {b'\xcf\xfa\xed\xfe',b'\xce\xfa\xed\xfe'}:
            cpus = [struct.unpack('<I',header[4:8])[0]]
        elif magic in {b'\xfe\xed\xfa\xcf',b'\xfe\xed\xfa\xce'}:
            cpus = [struct.unpack('>I',header[4:8])[0]]
        elif magic in {b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf'}:
            count = struct.unpack('>I',header[4:8])[0]
            stride = 32 if magic[-1]==0xbf else 20
            _require(0<count<=64 and 8+count*stride<=len(header),'Invalid fat Mach-O')
            cpus = [struct.unpack('>I',header[8+i*stride:12+i*stride])[0] for i in range(count)]
        if cpus:
            native_files += 1
            architectures.update(mapping.get(cpu,'unknown') for cpu in cpus)
    return {'architectures':sorted(architectures), 'native_file_count':native_files,
            'signature_resources_present':(root/'Contents/_CodeSignature/CodeResources').is_file(),
            'signature_trust':'not_verified', 'dependencies':'requires_local_inspection'}


def status(root, *, rebuilt_prefixes=None):
    """Read back actual rebuilt assets; login/activation never inferred ready.

    Prefix mappings are local integration state, not imported container fields.
    Native trust/version probes are explicitly requested via probe_native().
    """
    root = Path(root)
    result = plan(root)
    rows = {r['id']:r for r in _catalog(root)}
    for item in result:
        if item['id'] not in (rebuilt_prefixes or {}): continue
        prefix = Path(rebuilt_prefixes[item['id']])
        row = rows[item['id']]
        if row['adapter'] in NATIVE_ADAPTERS:
            try:
                _validate_native_prefix(root, row, prefix)
                _macho_closure(prefix/'code',prefix/row['native']['binary'])
                item.update(state='code_ready', reason='native_closure_and_local_entry_verified')
            except (ValueError, OSError):
                item.update(state='needs_rebuild',reason='local_native_changed_or_incomplete')
            continue
        if row['adapter'] != 'version-resource': continue
        try:
            _require(not prefix.is_symlink() and prefix.is_dir())
            _require({p.name for p in prefix.iterdir()}=={'version.py','resources'})
            _require(not (prefix/'version.py').is_symlink())
            _require((prefix/'version.py').read_text()==VERSION_LAUNCHER)
            _require(bundle._tree(prefix/'resources')==row['tree'])
            item.update(state='restored',reason='local_rebuild_code_and_resource_hashes_verified')
        except (ValueError, OSError):
            item.update(state='needs_rebuild',reason='local_rebuild_changed_or_incomplete')
    return result

# Known native adapters: only these local builders define executable layout.
_MACH_MAGICS = {b'\xcf\xfa\xed\xfe', b'\xfe\xed\xfa\xcf', b'\xca\xfe\xba\xbe', b'\xca\xfe\xba\xbf'}
_TARGETS = {'arm64':'aarch64-apple-darwin', 'x86_64':'x86_64-apple-darwin'}


def _is_macho(path):
    if not path.is_file() or path.is_symlink(): return False
    with open(path,'rb') as f: header=f.read(4096)
    if header[:4] not in _MACH_MAGICS or len(header)<8: return False
    if header[:4] in {b'\xca\xfe\xba\xbe',b'\xca\xfe\xba\xbf'}:
        count=struct.unpack('>I',header[4:8])[0]
        wide=header[3]==0xbf
        stride=32 if wide else 20
        if not 0<count<=64 or len(header)<8+count*stride: return False
        for i in range(count):
            start=8+i*stride+8
            offset,size=struct.unpack('>QQ' if wide else '>II',header[start:start+(16 if wide else 8)])
            if offset<8+count*stride or size==0 or offset+size>path.stat().st_size: return False
    return True


def _tool(args, *, pass_fds=()):
    try:
        r = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                           env={'PATH':'/usr/bin:/bin','LC_ALL':'C'}, timeout=30, pass_fds=pass_fds)
        _require(r.returncode==0, 'Native inspection failed')
        return r.stdout.decode('utf-8',errors='strict')
    except (OSError, UnicodeError, subprocess.SubprocessError):
        raise ComponentError('Native inspection unavailable') from None


def _macho_arch(binary):
    with open(binary,'rb') as descriptor:
        fd=descriptor.fileno()
        return _tool(['/usr/bin/lipo','-archs','/dev/fd/'+str(fd)],pass_fds=(fd,)).split()


def _macho_info(binary):
    _require(platform.system()=='Darwin' and _is_macho(binary), 'Mach-O inspector required')
    arch = _macho_arch(binary)
    _require(platform.machine() in arch,'Architecture incompatible')
    # otool interprets a filename ending in '(...)' as an archive member.
    # A read-only inherited fd avoids that parser and never renames source code.
    with open(binary,'rb') as descriptor:
        fd=descriptor.fileno()
        text = _tool(['/usr/bin/otool','-arch',platform.machine(),'-l','/dev/fd/'+str(fd)],pass_fds=(fd,))
        headers = _tool(['/usr/bin/otool','-arch',platform.machine(),'-hv','/dev/fd/'+str(fd)],pass_fds=(fd,))
    dependencies, rpaths = [], []
    command = None
    minimum = None
    for line in text.splitlines():
        fields = line.strip().split()
        if fields[:1]==['cmd'] and len(fields)==2:
            command = fields[1]
            _require(command!='LC_DYLD_ENVIRONMENT','Embedded loader environment unsupported')
        if fields[:1]==['name'] and command in {'LC_LOAD_DYLIB','LC_LOAD_WEAK_DYLIB','LC_REEXPORT_DYLIB','LC_LOAD_UPWARD_DYLIB','LC_LAZY_LOAD_DYLIB'}:
            dependencies.append(line.strip()[5:].split(' (offset ',1)[0])
        if fields[:1]==['path'] and command=='LC_RPATH':
            rpaths.append(line.strip()[5:].split(' (offset ',1)[0])
        if (fields[:1]==['minos'] and command=='LC_BUILD_VERSION') or (fields[:1]==['version'] and command=='LC_VERSION_MIN_MACOSX'):
            minimum = fields[1]
    return dict(architectures=arch, dependencies=dependencies, rpaths=rpaths, minimum=minimum,
                is_executable=bool(re.search(r'\bEXECUTE\b',headers)))


def _system_library(name):
    return name==os.path.normpath(name) and name.startswith(('/usr/lib/','/System/Library/'))


def _system_library_available(name):
    if not _system_library(name): return False
    if Path(name).is_file(): return True
    # macOS system dylibs may live exclusively in the dyld shared cache.
    # SDK mach-o/dyld.h declares this read-only bool predicate (macOS 11+).
    try:
        lookup=ctypes.CDLL(None)._dyld_shared_cache_contains_path
        lookup.argtypes=[ctypes.c_char_p]
        lookup.restype=ctypes.c_bool
        return bool(lookup(os.fsencode(name)))
    except (AttributeError,OSError):
        return False


def _macho_closure(root, main):
    """Resolve load commands against code-only closure; never inherit host rpath.

    Absolute third party dependencies are rejected even if installed on this
    machine. Missing/unknown dependency is never converted into ready.
    """
    root, main = Path(root).resolve(), Path(main).resolve()
    _require(main.is_relative_to(root) and _is_macho(main), 'Native main missing')
    paths = [root/e['path'] for e in bundle._tree(root) if e['kind']=='file' and _is_macho(root/e['path'])]
    infos = {}
    inactive = 0
    for p in paths:
        rel=p.relative_to(root).as_posix()
        known_variant = ('/prebuilds/darwin-x64/' in '/'+rel or '/prebuilds/darwin-arm64/' in '/'+rel) and p.suffix=='.node'
        if known_variant and platform.machine() not in _macho_arch(p):
            expected='x86_64' if '/prebuilds/darwin-x64/' in '/'+rel else 'arm64'
            _require(_macho_arch(p)==[expected],'Unknown bundled platform variant')
            inactive+=1
            continue
        infos[p]=_macho_info(p)
    for info in infos.values():
        _require(platform.machine() in info['architectures'], 'Architecture incompatible')
        if info['minimum']:
            def numbers(v): return tuple(int(x) for x in v.split('.')[:2])
            _require(numbers(info['minimum'])<=numbers(platform.mac_ver()[0]), 'macOS version incompatible')
    def expand(value, image, executable):
        if value=='@loader_path' or value.startswith('@loader_path/'):
            path = image.parent/value[len('@loader_path'):].lstrip('/')
        elif value=='@executable_path' or value.startswith('@executable_path/'):
            path = executable.parent/value[len('@executable_path'):].lstrip('/')
        else:
            return None
        resolved = path.resolve()
        _require(resolved.is_relative_to(root), 'Dependency escapes code closure')
        return resolved
    for image, info in infos.items():
        # Helper .app executables establish their own executable_path.
        executable = image if info['is_executable'] else main
        rpaths = [(rp,image) for rp in info['rpaths']]
        if not info['is_executable']:
            rpaths += [(rp,main) for rp in infos[main]['rpaths']]
        for rp,owner in rpaths:
            _require(rp.startswith(('@loader_path','@executable_path')) or
                     _system_library(rp.rstrip('/')+'/__probe__'),'External runtime search path unsupported')
        for name in info['dependencies']:
            if _system_library(name):
                _require(_system_library_available(name),'System dependency unavailable on this Mac')
                continue
            possible = []
            if name.startswith('@rpath/'):
                for rp,owner in rpaths:
                    base = expand(rp,owner,executable)
                    if base is not None: possible.append((base/name[len('@rpath/'):]).resolve())
            else:
                resolved = expand(name,image,executable)
                if resolved is not None: possible.append(resolved)
            _require(possible and all(p.is_relative_to(root) for p in possible), 'External dependency unsupported')
            _require(any(p.is_file() and p in infos for p in possible), 'Unresolved code dependency')
    return {'native_files':len(infos), 'inactive_architecture_assets':inactive, 'architecture':platform.machine(),
            'dependencies':'closure_or_system_verified', 'macos':platform.mac_ver()[0]}


def _native_spec(adapter, source):
    _require(adapter in NATIVE_ADAPTERS)
    source = Path(source)
    _require(not source.is_symlink(), 'Choose resolved native code, not an installation shim')
    name = adapter.split('-')[0]
    if name=='codex':
        if source.is_file():
            _require(source.name=='codex' and source.parent.name=='bin' and source.parents[2].name=='vendor', 'Codex vendor closure required')
            source = source.parents[2]
        _require(source.is_dir() and source.name=='vendor', 'Codex vendor closure required')
        target = _TARGETS.get(platform.machine())
        _require(target is not None)
        binary = source/target/'bin/codex'
        _require(_is_macho(binary) and (source/target/'codex-path/rg').is_file(), 'Codex helper closure missing')
        return source, 'code/vendor/'+target+'/bin/codex', ['code/vendor/'+target+'/codex-path']
    _require(source.is_file() and _is_macho(source), 'Standalone native binary required')
    return source, 'code/'+name, []


def _save_native(selection, destination):
    source, binary, paths = _native_spec(selection['adapter'], selection['source'])
    destination.mkdir(mode=0o700)
    code = destination/'code'
    code.mkdir(mode=0o700)
    if source.is_dir():
        _copy(source, code/'vendor')
    else:
        before = bundle._digest(source)
        name = selection['adapter'].split('-')[0]
        shutil.copy2(source,code/name)
        _require(before==bundle._digest(source)==bundle._digest(code/name), 'Native source changed')
    evidence = _macho_closure(code, destination/binary)
    return {'name':selection['adapter'].split('-')[0], 'binary':binary, 'path':paths, 'inspection':evidence}


def _native_launcher(native):
    # All fragments are fixed local adapter constants, never JSON shell text.
    if native['name']!='codex': return None
    target = _TARGETS.get(platform.machine())
    _require(native['binary']=='code/vendor/'+str(target)+'/bin/codex')
    return ('#!/bin/sh\nset -eu\nroot=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd -P)\n'
            'PATH="$root/code/vendor/'+target+'/codex-path:/usr/bin:/bin"\nexport PATH\n'
            'exec "$root/code/vendor/'+target+'/bin/codex" "$@"\n')


def _probe_version(binary, *, path_dirs=(), expected=None):
    with tempfile.TemporaryDirectory(prefix='.native-probe-') as tmp:
        home = Path(tmp).resolve()
        os.chmod(home,0o700)
        env = {'PATH':':'.join([*(str(p) for p in path_dirs),'/usr/bin','/bin']),
               'HOME':str(home),'TMPDIR':str(home),'LC_ALL':'C',
               'CODEX_HOME':str(home/'codex'),'CLAUDE_CONFIG_DIR':str(home/'claude'),
               'XDG_CONFIG_HOME':str(home/'config'),'XDG_DATA_HOME':str(home/'data'),
               'XDG_STATE_HOME':str(home/'state'),'XDG_CACHE_HOME':str(home/'cache'),
               'DISABLE_AUTOUPDATER':'1','CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1',
               'NO_COLOR':'1','DO_NOT_TRACK':'1'}
        r = subprocess.run([str(Path(binary).absolute()),'--version'],cwd=home,env=env,
                           stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=30)
        _require(r.returncode==0, 'Native version probe failed')
        text = r.stdout[:4096].decode('utf-8',errors='replace')
        found = re.search(r'(?<![\w.])\d+\.\d+(?:\.[0-9A-Za-z.+_-]+)?',text)
        _require(found is not None, 'No safe version metadata')
        version = found.group(0)
        _require(expected in (None,'unknown',version), 'Native version mismatch')
        return version


def _validate_native_prefix(root, row, prefix):
    _require(prefix.is_dir() and not prefix.is_symlink())
    doc = _read(prefix/'native.json')
    _require(doc=={'schema':1,'component':row['id'],'hash':row['hash'],'native':row['native']})
    _require({p.name for p in prefix.iterdir()}=={'code','bin','native.json'})
    expected=[{**e,'path':e['path'][5:]} for e in row['tree'] if e['path'].startswith('code/')]
    _require(bundle._tree(prefix/'code')==expected, 'Native code changed')
    _require({p.name for p in (prefix/'bin').iterdir()}=={row['native']['name']})
    entry = prefix/'bin'/row['native']['name']
    launcher = _native_launcher(row['native'])
    if launcher is not None:
        _require(not entry.is_symlink() and entry.read_text()==launcher and os.access(entry,os.X_OK))
    else:
        _require(entry.is_symlink() and os.readlink(entry)=='../'+row['native']['binary'])
    return entry


def materialize_native(root, component_id, prefix, *, trusted_component_hash):
    """Build a new local entry + complete native closure and actually --version.

    Trust must be decided locally for this code hash. No package shell is run.
    No source paths or account/environment state are consulted by the probe.
    """
    root = Path(root)
    row = next((r for r in _catalog(root) if r['id']==component_id),None)
    _require(row and row['adapter'] in NATIVE_ADAPTERS and row['hash']==trusted_component_hash,'Local native trust required')
    prefix = bundle._parents(prefix)
    _require(not os.path.lexists(prefix),'Prefix exists')
    with tempfile.TemporaryDirectory(prefix='.native-',dir=prefix.parent) as tmp:
        os.chmod(tmp,0o700)
        staged = Path(tmp)/'prefix'
        _copy(root/component_id,staged)
        _require(bundle._tree(staged)==row['tree'],'Native source changed since trust')
        _macho_closure(staged/'code',staged/row['native']['binary'])
        (staged/'bin').mkdir(mode=0o700)
        entry = staged/'bin'/row['native']['name']
        launcher = _native_launcher(row['native'])
        if launcher is None: entry.symlink_to('../'+row['native']['binary'])
        else:
            entry.write_text(launcher); entry.chmod(0o700)
        _write(staged/'native.json',{'schema':1,'component':component_id,'hash':row['hash'],'native':row['native']})
        version = _probe_version(entry,path_dirs=[staged/p for p in row['native']['path']],expected=row['version'])
        _validate_native_prefix(root,row,staged)
        bundle._publish(staged,prefix)
    # Probe the final path too, proving staging location was not embedded.
    entry = _validate_native_prefix(root,row,prefix)
    _probe_version(entry,path_dirs=[prefix/p for p in row['native']['path']],expected=version)
    return {'id':component_id,'hash':row['hash'],'state':'code_ready','probe':'version_verified',
            'version':version,'entrypoint':str(prefix/'bin'/row['native']['name']),
            'login':'needs_login','probe_environment':'new_home_and_system_plus_local_path'}


def select_installed(names, *, candidates=None):
    """Opt-in bounded code discovery; never glob accounts/configs/session HOME.

    Only explicitly named tools are considered. candidates optionally supplies
    resolved code paths from an installation inventory. Generic vp/node shims
    are refused, never copied or executed as if they were Codex/Node.
    """
    _require(set(names)<={'codex','claude','opencode'} and len(set(names))==len(names))
    result = []
    for name in names:
        options = []
        if candidates and name in candidates:
            options = [Path(candidates[name])]
        else:
            located = shutil.which(name)
            if located: options.append(Path(located).resolve())
            home = Path.home()
            if name=='claude': options.append((home/'.local/bin/claude').resolve())
            if name=='opencode': options.extend([Path('/opt/homebrew/bin/opencode').resolve(),Path('/usr/local/bin/opencode').resolve()])
            if name=='codex':
                for base in (Path('/opt/homebrew/lib/node_modules/@openai/codex'),Path('/usr/local/lib/node_modules/@openai/codex')):
                    if base.is_dir(): options.extend(base.glob('node_modules/@openai/codex-darwin-*/vendor/*/bin/codex'))
                code_base = home/'.local/share/vite-plus/packages/@openai/codex'
                if code_base.is_dir():
                    options.extend(code_base.glob('*/lib/node_modules/@openai/codex/node_modules/@openai/codex-darwin-*/vendor/*/bin/codex'))
        valid = []
        for path in options:
            try:
                path = path.resolve(strict=True)
                _native_spec(name+'-native',path)
                if name!='codex': _require(path.name!= 'vp' and _is_macho(path))
                valid.append(path)
            except (ValueError,OSError): continue
        valid = list(dict.fromkeys(valid))
        _require(len(valid)==1,'No unique supported native installation; choose explicit code candidate')
        selection = register(name,valid[0],kind='cli',adapter=name+'-native',selected=True,login_required=True)
        with tempfile.TemporaryDirectory(prefix='.selected-native-') as tmp:
            staged = Path(tmp).resolve()/'tool'
            native = _save_native(selection,staged)
            selection['version'] = _probe_version(staged/native['binary'],path_dirs=[staged/p for p in native['path']])
        result.append(selection)
    return result


def verify_app(root, component_id):
    """Read-only strict signature + architecture + code dependency verification.

    Never launch, re-sign, remove quarantine or read Application Support.
    Code readiness is independent of first system approval and account login.
    """
    root = Path(root)
    row = next((r for r in _catalog(root) if r['id']==component_id),None)
    _require(row and row['adapter']=='app-code')
    evidence=inspect_app(root/component_id)
    _require(bundle._tree(root/component_id)==row['tree'],'App changed during verification')
    return {'id':component_id,'hash':row['hash'],**evidence,
            'login':'needs_login' if row['login_required'] else 'not_required'}


def inspect_app(source):
    """Explicit .app code only; safe result omits source path and command output."""
    app=Path(source)
    before=bundle._tree(app)
    result={'state':'needs_rebuild','reason':'unknown_app_code',
            'signature':'not_verified','system_authorization':'first_launch_not_performed'}
    try:
        info=plistlib.loads((app/'Contents/Info.plist').read_bytes())
        executable=info.get('CFBundleExecutable')
        _require(isinstance(executable,str) and '/' not in executable and executable not in {'.','..'},'Unknown app executable')
        result['code']=_macho_closure(app,app/'Contents/MacOS'/executable)
        result.update(state='needs_signature_review',reason='strict_signature_failed')
        _tool(['/usr/bin/codesign','--verify','--deep','--strict',str(app)])
        result.update(state='code_ready',reason='strict_signature_architecture_dependencies_verified',signature='strict_valid')
    except (ValueError,OSError,KeyError,plistlib.InvalidFileException) as error:
        safe_reasons={'Architecture incompatible':'architecture_mismatch',
                      'macOS version incompatible':'macos_version_incompatible',
                      'External dependency unsupported':'external_dependency',
                      'Unresolved code dependency':'unresolved_dependency',
                      'System dependency unavailable on this Mac':'system_dependency_unavailable',
                      'Unknown app executable':'unknown_app_executable',
                      'Dependency escapes code closure':'dependency_outside_bundle'}
        if result['state']!='needs_signature_review':
            result['reason']=safe_reasons.get(str(error),'strict_signature_or_code_inspection_failed')
    _require(before==bundle._tree(app),'App changed during inspection')
    result['code_hash']=_hash(before)
    return result


def _validate_native_metadata(adapter, native):
    _require(isinstance(native,dict) and set(native)=={'name','binary','path','inspection'})
    name=adapter.split('-')[0]
    _require(native['name']==name)
    if name=='codex':
        _require(any(native['binary']=='code/vendor/'+target+'/bin/codex' and
                     native['path']==['code/vendor/'+target+'/codex-path']
                     for target in _TARGETS.values()),'Unsupported Codex target layout')
    else:
        _require(native['binary']=='code/'+name and native['path']==[])


def _calendar(value):
    rows = value if isinstance(value,list) else [value]
    limits = {'Minute':(0,59),'Hour':(0,23),'Day':(1,31),'Weekday':(0,7),'Month':(1,12)}
    _require(0<len(rows)<=32)
    for row in rows:
        _require(isinstance(row,dict) and row and set(row)<=set(limits))
        for k,v in row.items(): _require(type(v)==int and limits[k][0]<=v<=limits[k][1])
    return value


def _port(value):
    _require(type(value)==int and 1<=value<=65535,'Invalid service port')
    return value


def _host(value):
    _require(isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.-]{0,252}',value),'Invalid host')
    return value


def _reference(value):
    _require(isinstance(value,dict) and set(value)=={'component','relative'})
    _id(value['component'])
    if value['relative']!='.': bundle._name(value['relative'])
    return value


def _parameters(profile, value):
    _require(isinstance(value,dict))
    if profile=='component-direct': _require(not value)
    elif profile=='native-version': _require(not value)
    elif profile=='opencode-serve':
        _require(set(value)=={'port','hostname'} and value['hostname']=='127.0.0.1')
        _port(value['port'])
    elif profile=='dev-server':
        _require(set(value)<={'port','host','directory'} and value)
        if 'port' in value: _port(value['port'])
        if 'host' in value: _require(value['host']=='127.0.0.1')
        if 'directory' in value: _reference(value['directory'])
    elif profile=='ssh-forward':
        _require(set(value)=={'host','user','port','local_port','remote_host','remote_port'})
        _host(value['host']); _host(value['remote_host'])
        _require(isinstance(value['user'],str) and re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_-]{0,63}',value['user']))
        for key in ('port','local_port','remote_port'): _port(value[key])
    else:
        raise ComponentError('Service profile requires a trusted local adapter')
    return value


def _validate_recipe(value, adapter):
    _require(isinstance(value,dict))
    allowed = {'program_component','working_component','schedule','calendar','external_connection',
               'profile','parameters','endpoint'}
    _require(set(value)<=allowed,'Unknown service fields require local inspection')
    if 'program_component' in value: _id(value['program_component'])
    if 'working_component' in value: _id(value['working_component'])
    if 'schedule' in value: _require(type(value['schedule'])==int and 60<=value['schedule']<=86400)
    if 'calendar' in value: _calendar(value['calendar'])
    if 'endpoint' in value:
        _require(isinstance(value['endpoint'],str) and re.fullmatch(r'[A-Za-z0-9.-]+:[0-9]{1,5}',value['endpoint']))
    _require(value.get('external_connection',True) is True)
    if 'profile' in value: _parameters(value['profile'],value.get('parameters',{}))
    else: _require('parameters' not in value)
    return value


def _parse_service_argv(profile, argv, path_bindings):
    _require(all(isinstance(a,str) for a in argv))
    if profile=='component-direct': _require(not argv); return {}
    if profile=='native-version': _require(argv==['--version']); return {}
    if profile in {'dev-server','opencode-serve'}:
        if profile=='opencode-serve':
            _require(argv[:1]==['serve']); argv=argv[1:]
        _require(len(argv)%2==0)
        result = {}
        for flag,val in zip(argv[::2],argv[1::2]):
            allowed = {'--port':'port','--hostname':'hostname'} if profile=='opencode-serve' else {'--port':'port','--host':'host','--directory':'directory'}
            _require(flag in allowed and allowed[flag] not in result)
            key = allowed[flag]
            if key=='port': _require(val.isdecimal()); val=int(val)
            if key=='directory':
                _require(val in path_bindings)
                val={'component':_id(path_bindings[val]),'relative':'.'}
            result[key]=val
        if profile=='opencode-serve':
            result.setdefault('port',4096); result.setdefault('hostname','127.0.0.1')
        return _parameters(profile,result)
    if profile=='ssh-forward':
        flags, destination = {}, None
        i=0
        while i<len(argv):
            a=argv[i]
            if a in {'-N','-T'}: i+=1; continue
            if a in {'-L','-p'}:
                _require(i+1<len(argv) and a not in flags); flags[a]=argv[i+1]; i+=2; continue
            _require(not a.startswith('-') and destination is None); destination=a; i+=1
        _require(destination is not None and destination.count('@')==1 and '-L' in flags)
        parts=flags['-L'].split(':')
        if len(parts)==4: _require(parts.pop(0)=='127.0.0.1')
        _require(len(parts)==3)
        user,host=destination.split('@')
        _require(parts[0].isdecimal() and parts[2].isdecimal() and flags.get('-p','22').isdecimal())
        return _parameters(profile,dict(host=host,user=user,port=int(flags.get('-p','22')),
                                       local_port=int(parts[0]),remote_host=parts[1],remote_port=int(parts[2])))
    raise ComponentError('Service profile requires a trusted local adapter')


def _service_row(root, component_id):
    row=next((r for r in _catalog(root) if r['id']==component_id),None)
    _require(row and row['adapter'] in {'launchd-plist','launchd-recipe','tunnel-recipe'} and row['state']!='cannot_migrate')
    recipe=_read(root/component_id/'asset')
    _validate_recipe(recipe,row['adapter'])
    return row,recipe


def _local_bindings(root, bindings):
    rows={r['id']:r for r in _catalog(root)}
    result={}
    for name,value in bindings.items():
        _id(name)
        path=bundle._parents(value)
        _require(path.is_dir() and not path.is_symlink(),'New local binding directory required')
        entry=None
        if name in rows:
            _require(rows[name]['adapter'] in NATIVE_ADAPTERS,'Service executable needs supported native adapter')
            entry=_validate_native_prefix(root,rows[name],path)
            _macho_closure(path/'code',path/rows[name]['native']['binary'])
        result[name]={'root':str(path),'tree':_hash(bundle._tree(path)),
                      'entrypoint':str(entry) if entry else None,
                      'component_hash':rows[name]['hash'] if name in rows else None}
    return result


def _service_definition(row, recipe, device, target, bindings, ssh_identity=None):
    profile=recipe.get('profile','component-direct')
    params=_parameters(profile,recipe.get('parameters',{}))
    def reference(ref):
        ref=_reference(ref)
        _require(ref['component'] in bindings)
        base=Path(bindings[ref['component']]['root'])
        path=(base/ref['relative']).resolve()
        _require(path.is_relative_to(base) and path.exists(),'New service path binding unavailable')
        return str(path)
    if profile=='ssh-forward':
        program='/usr/bin/ssh'
        _require(Path(program).is_file(),'System SSH unavailable')
        identity_path=ssh_identity['identity_file']['path'] if ssh_identity else 'none'
        known_hosts_path=ssh_identity['known_hosts']['path'] if ssh_identity else str(target/'home/known_hosts')
        args=['-F','/dev/null','-N','-T','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
              '-o','ExitOnForwardFailure=yes','-o','IdentityAgent=none','-o','IdentitiesOnly=yes',
              '-o','IdentityFile='+_ssh_path_option(identity_path),'-o','UserKnownHostsFile='+_ssh_path_option(known_hosts_path),
              '-L',f"127.0.0.1:{params['local_port']}:{params['remote_host']}:{params['remote_port']}",
              '-p',str(params['port']),params['user']+'@'+params['host']]
    else:
        component=recipe.get('program_component')
        _require(component in bindings and bindings[component]['entrypoint'],'Trusted native program binding required')
        program=bindings[component]['entrypoint']
        if profile=='opencode-serve':
            _require(row['id'] and Path(program).name=='opencode','OpenCode native binding required')
            args=['serve','--hostname','127.0.0.1','--port',str(params['port'])]
        elif profile=='native-version': args=['--version']
        elif profile=='dev-server':
            args=[]
            if 'port' in params: args+=['--port',str(params['port'])]
            if 'host' in params: args+=['--host','127.0.0.1']
            if 'directory' in params: args+=['--directory',reference(params['directory'])]
        else: args=[]
    label='portable.'+device+'.'+row['id']+'.'+row['hash'][:12]
    env={'HOME':str(target/'home'),'PATH':'/usr/bin:/bin',
         'XDG_CONFIG_HOME':str(target/'home/config'),'XDG_DATA_HOME':str(target/'home/data'),
         'XDG_CACHE_HOME':str(target/'home/cache'),'XDG_STATE_HOME':str(target/'home/state'),
         'CODEX_HOME':str(target/'home/codex'),'CLAUDE_CONFIG_DIR':str(target/'home/claude'),
         'CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC':'1','DISABLE_AUTOUPDATER':'1'}
    definition={'Label':label,'ProgramArguments':[program,*args],'Disabled':True,'RunAtLoad':False,
                'KeepAlive':False,'EnvironmentVariables':env,
                'StandardOutPath':str(target/'logs/stdout'),'StandardErrorPath':str(target/'logs/stderr')}
    if 'working_component' in recipe:
        name=recipe['working_component']; _require(name in bindings)
        definition['WorkingDirectory']=bindings[name]['root']
    else: definition['WorkingDirectory']=str(target/'home')
    if 'schedule' in recipe: definition['StartInterval']=recipe['schedule']
    if 'calendar' in recipe: definition['StartCalendarInterval']=_calendar(recipe['calendar'])
    return definition


def stage_service(root, component_id, target, *, device, bindings, ssh_identity=None):
    """Produce actual disabled launchd plist, never install or bootstrap it."""
    root=Path(root); _id(device)
    row,recipe=_service_row(root,component_id)
    target=bundle._parents(target)
    _require(not os.path.lexists(target),'Service target exists')
    local=_local_bindings(root,bindings)
    identity=_ssh_identity(ssh_identity) if ssh_identity is not None else None
    _require(identity is None or recipe.get('profile')=='ssh-forward')
    definition=_service_definition(row,recipe,device,target,local,identity)
    receipt={'schema':1,'component':component_id,'hash':row['hash'],'device':device,
             'target':str(target),'bindings':local,'definition':_hash(definition),'ssh_identity':identity}
    with tempfile.TemporaryDirectory(prefix='.service-',dir=target.parent) as tmp:
        os.chmod(tmp,0o700)
        staged=Path(tmp)/'service'; staged.mkdir(mode=0o700)
        for name in ('home','logs'): (staged/name).mkdir(mode=0o700)
        _write(staged/'service.json',receipt)
        with open(staged/'disabled.plist','xb') as f:
            os.chmod(f.fileno(),0o600); plistlib.dump(definition,f,sort_keys=True)
        _require(plistlib.loads((staged/'disabled.plist').read_bytes())==definition)
        bundle._publish(staged,target)
    return {'state':'pending_activation','disabled':True,'plist':str(target/'disabled.plist'),
            'component_hash':row['hash'],'definition_hash':receipt['definition'],
            'identity':'new_local_identity_bound' if identity else ('needs_login' if recipe.get('profile')=='ssh-forward' else 'not_inherited')}


def _validate_service_stage(root, component_id, target, device):
    row,recipe=_service_row(root,component_id)
    receipt=_read(target/'service.json')
    _require(receipt['hash']==row['hash'] and receipt['component']==component_id and
             receipt['device']==device and receipt['target']==str(target),'Service binding changed')
    bindings=_local_bindings(root,{k:v['root'] for k,v in receipt['bindings'].items()})
    _require(bindings==receipt['bindings'],'Bound code or work directory changed')
    identity=receipt.get('ssh_identity')
    if identity is not None:
        current=_ssh_identity({k:v['path'] for k,v in identity.items()})
        _require(current==identity,'New SSH identity changed')
    definition=_service_definition(row,recipe,device,target,bindings,identity)
    _require(_hash(definition)==receipt['definition'])
    _require(not (target/'disabled.plist').is_symlink())
    _require(plistlib.loads((target/'disabled.plist').read_bytes())==definition,'Disabled definition changed')
    return row,definition,receipt


class TrustedActivationHook:
    """LOCAL capability: integrate real OWNER Gate consume(token, binding).

    Never construct from JSON/imported decisions. Pending proposals are process
    local, latest-only and one-shot. Default launchctl is fixed trusted code;
    tests inject a local no-network function. No invocation happens on creation.
    """
    def __init__(self, authorize, *, launchctl=None):
        _require(callable(authorize) and (launchctl is None or callable(launchctl)))
        self._authorize=authorize
        self._launchctl=launchctl or self._system_launchctl
        self._pending={}

    @staticmethod
    def _system_launchctl(domain, plist):
        r=subprocess.run(['/bin/launchctl','bootstrap',domain,str(plist)],
                         stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30,
                         env={'PATH':'/usr/bin:/bin'})
        _require(r.returncode==0,'launchctl bootstrap failed; inspect local service')


def prepare_service_activation(root, component_id, target, *, device, scopes, hook):
    _require(isinstance(hook,TrustedActivationHook))
    target=bundle._parents(target)
    row,definition,receipt=_validate_service_stage(Path(root),component_id,target,device)
    required='external_connection' if definition['ProgramArguments'][0]=='/usr/bin/ssh' else 'service'
    _require(set(scopes)=={required},'Exact activation scope required')
    proposal=prepare_gate(root,component_id,device=device,target=target,scopes=scopes)
    proposal['definition_hash']=_hash(definition)
    proposal['receipt_hash']=_hash(receipt)
    proposal['host']=platform.node()
    proposal['uid']=os.getuid()
    hook._pending[str(target)]=_hash(proposal)
    return proposal


def activate_service(root, component_id, target, proposal, token, *, device, scopes, hook):
    """Only after real local Gate consumption: new active generation + bootstrap.

    This API performs a formal action WHEN CALLED; this project never calls it
    on real services in tests. A dict with approved=True cannot authorize it.
    """
    _require(isinstance(hook,TrustedActivationHook))
    _require(isinstance(token,str) and 0<len(token)<=200,'Existing OWNER Gate token required')
    target=bundle._parents(target)
    row,definition,receipt=_validate_service_stage(Path(root),component_id,target,device)
    if definition['ProgramArguments'][0]=='/usr/bin/ssh':
        _require(receipt.get('ssh_identity') is not None,'New local SSH identity and known_hosts must be explicitly bound')
    _require(proposal.get('device')==device and proposal.get('target')==str(target) and
             proposal.get('component')==component_id and proposal.get('hash')==row['hash'] and
             proposal.get('definition_hash')==_hash(definition) and proposal.get('receipt_hash')==_hash(receipt) and
             proposal.get('scopes')==sorted(set(scopes)) and proposal.get('host')==platform.node() and
             proposal.get('uid')==os.getuid(),'Activation proposal invalidated')
    _require(hook._pending.get(str(target))==_hash(proposal),'Activation proposal expired or consumed')
    generation=target/('activation-'+_id(proposal['nonce']))
    _require(not os.path.lexists(generation),'Activation generation exists')
    # Reserve before consuming the Gate. Any failure needs a fresh proposal;
    # no automatic retries or rollback of a possibly bootstrapped real service.
    generation.mkdir(mode=0o700)
    del hook._pending[str(target)]
    _require(hook._authorize(token,dict(proposal)) is True,'Existing OWNER Gate denied activation')
    _validate_service_stage(Path(root),component_id,target,device)
    active=dict(definition,Disabled=False,RunAtLoad=True)
    plist=generation/'LaunchAgent.plist'
    with open(plist,'xb') as f:
        os.chmod(f.fileno(),0o600); plistlib.dump(active,f,sort_keys=True)
    _require(plistlib.loads(plist.read_bytes())==active)
    hook._launchctl('gui/'+str(os.getuid()),plist)
    _write(generation/'activation.json',{'schema':1,'binding':_hash(proposal),
                                       'state':'bootstrap_requested','label':active['Label']})
    return {'state':'bootstrap_requested','label':active['Label'],'plist':str(plist),
            'health':'requires_service_specific_probe_no_model_calls'}


def service_approval_binding(proposal, *, task, owner):
    """Map the full new-device proposal to existing Store approval contract."""
    _id(task)
    _require(isinstance(owner,str) and 0<len(owner)<=200 and all(ord(c)>=32 for c in owner))
    return {'task':task,'action':'activate_component','target':proposal['target'],
            'scope':','.join(proposal['scopes']),'version':_hash(proposal),'owner':owner}


def activation_hook_for_store(store, *, task, owner, launchctl=None):
    """Consumes Store's expiring, one-use approval; does not mint approval."""
    def consume(token, proposal):
        return store.consume(token,service_approval_binding(proposal,task=task,owner=owner),owner)
    return TrustedActivationHook(consume,launchctl=launchctl)


def _ssh_identity(value):
    """Only metadata of two NEW explicitly selected local identity files.

    No key bytes are read, copied, logged or written. This is not an exported
    login state. The gated SSH process alone may later read these local files.
    """
    _require(isinstance(value,dict) and set(value)=={'identity_file','known_hosts'})
    result={}
    for name,path in value.items():
        p=bundle._parents(path)
        _ssh_path_option(str(p))
        _require(p.is_file() and not p.is_symlink(),'Explicit new SSH file required')
        s=p.stat()
        _require(s.st_uid==os.getuid() and not s.st_mode & 0o022,'Unsafe SSH file ownership or mode')
        if name=='identity_file': _require(s.st_mode & 0o077==0,'New identity must be private')
        result[name]={'path':str(p),'device':s.st_dev,'inode':s.st_ino,'size':s.st_size,
                      'mtime':s.st_mtime_ns,'ctime':s.st_ctime_ns,'mode':s.st_mode}
    return result


def _ssh_path_option(path):
    # -o values follow ssh_config quoting/token expansion, not shell quoting.
    # Do not allow percent expansion to select a different identity than gated.
    _require(isinstance(path,str) and not any(c in path for c in '\r\n\x00%"\\'),
             'SSH identity path contains unsupported expansion or control characters')
    return '"'+path+'"' if any(c.isspace() for c in path) else path
