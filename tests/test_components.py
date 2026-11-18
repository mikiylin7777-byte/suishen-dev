"""Synthetic fixtures plus explicitly authorized installed CODE assets only.
No real accounts, configurations, Keychain, models or service activation.
"""
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from portable import components as c
from portable import sealed_bundle as b


class ComponentsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.source = self.root/'old'
        self.source.mkdir()
        self.app = self.source/'T3.app'
        (self.app/'Contents/Frameworks/Thing.framework/Versions/A').mkdir(parents=True)
        (self.app/'Contents/MacOS').mkdir()
        (self.app/'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier':'test.modified.t3', 'CFBundleVersion':'42',
            'CFBundleShortVersionString':'1.2'}))
        exe = self.app/'Contents/MacOS/T3'
        exe.write_bytes(b'custom locally modified app code')
        exe.chmod(0o755)
        (self.app/'Contents/Frameworks/Thing.framework/Versions/A/resource').write_bytes(b'modified framework')
        (self.app/'Contents/Frameworks/Thing.framework/Versions/Current').symlink_to('A')
        self.cli = self.source/'cli'
        self.cli.mkdir()
        (self.cli/'version.json').write_text('{"version":"1.2"}')
        (self.cli/'resource.dat').write_bytes(b'complete resource closure')
        self.config = self.source/'config.json'
        self.config.write_text(json.dumps({'workdir':str(self.source/'work'),'theme':'private-fixture'}))
        self.service = self.source/'service.json'
        self.service.write_text(json.dumps({'program_component':'cli','schedule':60,'external_connection':True}))
        self.tunnel = self.source/'tunnel.json'
        self.tunnel.write_text(json.dumps({'program_component':'cli','endpoint':'fixture.invalid:443','external_connection':True}))
        self.items = [
            c.register('app',self.app,kind='app',adapter='app-code',version='1.2',selected=True,architectures=['arm64']),
            c.register('cli',self.cli,kind='cli',adapter='version-resource',version='1.2',selected=True,login_required=True),
            c.register('config',self.config,kind='config',adapter='json-config',selected=True),
            c.register('service',self.service,kind='launchd',adapter='launchd-recipe',selected=True),
            c.register('tunnel',self.tunnel,kind='tunnel',adapter='tunnel-recipe',selected=True),
            c.register('unknown',self.source/'missing',kind='cli',selected=True)]

    def keys(self):
        if not shutil.which('age') or not shutil.which('age-keygen'):
            self.skipTest('age and age-keygen required for actual sealed roundtrip')
        key = self.root/'key'
        with open(key,'wb') as f:
            subprocess.run(['age-keygen'],stdout=f,stderr=subprocess.DEVNULL,check=True)
        key.chmod(0o600)
        recipient = subprocess.check_output(['age-keygen','-y',str(key)],stderr=subprocess.DEVNULL).decode().strip()
        return key,recipient

    def plain(self, selections=None):
        p = self.root/'plain.bundle'
        c.save(selections or self.items[:2],p)
        return p

    def test_sealed_offline_roundtrip_and_real_rebuild(self):
        key,recipient = self.keys()
        p = self.root/'sealed.bundle'
        c.save(self.items,p,recipient=recipient)
        self.assertTrue(b.inspect(p)['encrypted'])
        self.assertNotIn(b'private-fixture',p.read_bytes())
        shutil.rmtree(self.source)  # actual old source loss, no getstate mocks
        restored = self.root/'new'
        states = c.restore(p,restored,identity_file=key)
        self.assertEqual({x['id'] for x in states}, {s['id'] for s in self.items})
        self.assertEqual(next(x for x in states if x['id']=='unknown')['state'],'cannot_migrate')
        self.assertEqual(next(x for x in states if x['id']=='cli')['login'],'needs_login')
        self.assertEqual((restored/'app/Contents/MacOS/T3').stat().st_mode & 0o777,0o755)
        self.assertEqual(os.readlink(restored/'app/Contents/Frameworks/Thing.framework/Versions/Current'),'A')
        rebuilt = self.root/'prefix'
        self.assertEqual(c.rebuild_version_resource(restored,'cli',rebuilt)['probe'],'version_verified')
        self.assertEqual((rebuilt/'resources/resource.dat').read_bytes(),b'complete resource closure')
        query = c.status(restored,rebuilt_prefixes={'cli':rebuilt})
        self.assertEqual(next(x for x in query if x['id']=='cli')['state'],'restored')
        self.assertEqual(next(x for x in query if x['id']=='cli')['login'],'needs_login')
        output = subprocess.check_output([os.sys.executable,'-I',str(rebuilt/'version.py'),'--version'])
        self.assertEqual(output,b'1.2\n')
        with self.assertRaises(c.ComponentError):
            c.stage_config(restored,'config',self.root/'bad-config')
        c.stage_config(restored,'config',self.root/'staged-config',path_fields={'workdir':'cli'},bindings={'cli':str(rebuilt)})
        self.assertNotIn(str(self.source),(self.root/'staged-config').read_text())
        self.assertTrue(json.loads((self.root/'staged-config').read_text())['disabled'])
        self.assertFalse((self.root/'counter').exists())
        proposal = c.prepare_gate(restored,'service',device='new-device',target=rebuilt,scopes=['tunnel'])
        d = c.decision(proposal,approved=True)
        args = dict(device='new-device',target=rebuilt,component_hash=proposal['hash'],scopes=['tunnel'])
        self.assertTrue(c.gate_valid(proposal,d,**args))
        for keyname,value in [('device','other'),('target',self.root/'other'),('component_hash','changed'),('scopes',['notify'])]:
            self.assertFalse(c.gate_valid(proposal,d,**{**args,keyname:value}))
        fresh = c.prepare_gate(restored,'service',device='new-device',target=rebuilt,scopes=['tunnel'])
        self.assertFalse(c.gate_valid(fresh,d,**args))
        self.assertFalse(c.gate_valid(proposal,c.decision(proposal),**args))

    def test_launchd_plist_binding_and_unknown_preservation(self):
        key,recipient = self.keys()
        plist = self.source/'fixture.plist'
        program = str(self.source/'old-venv/bin/python')
        plist.write_bytes(plistlib.dumps({'Label':'old.service','ProgramArguments':[program],
            'StartInterval':60,'RunAtLoad':True,'KeepAlive':True}))
        item = c.register('plist',plist,kind='launchd',adapter='launchd-plist',selected=True,
                          program_bindings={program:'cli'})
        p = self.root/'plist.bundle'; c.save([item],p,recipient=recipient)
        shutil.rmtree(self.source)
        target = self.root/'new'; states = c.restore(p,target,identity_file=key)
        self.assertEqual(states[0]['state'],'pending_activation')
        asset = json.loads((target/'plist/asset').read_bytes())
        self.assertEqual(asset['program_component'],'cli')
        self.assertNotIn(program,json.dumps(asset))
        self.assertNotIn('RunAtLoad',asset)
        # Unsupported real service fields remain encrypted and block Gate.
        unknown = self.root/'unknown.plist'
        unknown.write_bytes(plistlib.dumps({'Label':'unknown','ProgramArguments':['/bin/sh','-c','touch counter'],
                                          'EnvironmentVariables':{'FIXTURE_SECRET':'private'}}))
        item = c.register('legacy',unknown,kind='launchd',adapter='launchd-plist',selected=True)
        p2 = self.root/'unknown.bundle'; c.save([item],p2,recipient=recipient)
        unknown.unlink()
        target2 = self.root/'new2'; states = c.restore(p2,target2,identity_file=key)
        self.assertEqual(states[0]['state'],'cannot_migrate')
        self.assertIn(b'FIXTURE_SECRET',(target2/'legacy/original/asset').read_bytes())
        with self.assertRaises(c.ComponentError):
            c.prepare_gate(target2,'legacy',device='new',target=target2,scopes=['service'])

    def test_native_offline_closure_real_version_probe(self):
        if os.sys.platform!='darwin' or not Path('/usr/bin/clang').exists():
            self.skipTest('Mach-O compile/probe requires macOS developer tools')
        source = self.source/'native'; source.mkdir()
        code = self.source/'fixture.c'
        code.write_text('#include <stdio.h>\nint main(int c,char **v){if(c==2){puts("fixture-1.2");return 0;}return 2;}\n')
        result = subprocess.run(['/usr/bin/clang',str(code),'-o',str(source/'opencode')],
                                stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        self.assertEqual(result.returncode,0)
        item = c.register('native',source,kind='cli',adapter='native-closure',version='fixture-1.2',selected=True)
        # A different native closure deliberately depends on an old absolute dylib.
        bad = self.source/'badnative'; bad.mkdir()
        libsrc = self.source/'lib.c'; libsrc.write_text('int fixture(void){return 0;}\n')
        lib = self.source/'libfixture.dylib'
        subprocess.run(['/usr/bin/clang','-dynamiclib',str(libsrc),'-o',str(lib),
                        '-Wl,-install_name,'+str(lib)],check=True,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        code.write_text('#include <stdio.h>\nextern int fixture(void); int main(){puts("fixture-1.2");return fixture();}\n')
        subprocess.run(['/usr/bin/clang',str(code),str(lib),'-o',str(bad/'codex')],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        baditem = c.register('badnative',bad,kind='cli',adapter='native-closure',version='fixture-1.2',selected=True)
        p = self.plain([item,baditem]); shutil.rmtree(self.source)
        target = self.root/'native-new'; c.restore(p,target)
        row = c._catalog(target)[0]
        result = c.probe_native(target,'native','opencode',trusted_component_hash=row['hash'])
        self.assertEqual(result['probe'],'version_verified')
        badrow = next(r for r in c._catalog(target) if r['id']=='badnative')
        with self.assertRaises(c.ComponentError):
            c.probe_native(target,'badnative','codex',trusted_component_hash=badrow['hash'])
        with patch.object(c.platform,'machine',return_value='incompatible'):
            with self.assertRaises(c.ComponentError):
                c.probe_native(target,'native','opencode',trusted_component_hash=row['hash'])
        with self.assertRaises(c.ComponentError):
            c.probe_native(target,'native','opencode',trusted_component_hash='old')

    def test_doctor_never_reads_contents(self):
        with patch.object(Path,'read_bytes',side_effect=AssertionError('content read')):
            rows = c.doctor(self.items)
        self.assertNotIn(str(self.source),json.dumps(rows))
        self.assertEqual(rows[-1]['state'],'cannot_migrate')

    def test_private_plaintext_refused(self):
        with self.assertRaises(c.ComponentError): c.save(self.items,self.root/'no')
        self.assertFalse((self.root/'no').exists())

    def test_conflict_preserves_new_note_and_cas(self):
        p = self.plain()
        target = self.root/'new'
        c.restore(p,target)
        (target/'new-note').write_text('keep')
        with self.assertRaises(c.ComponentError): c.restore(p,target)
        self.assertEqual((target/'new-note').read_text(),'keep')
        with self.assertRaises(c.ComponentError): c.plan(target)
        (target/'new-note').unlink()
        (target/'cli/version.json').write_text('{"version":"changed"}')
        with self.assertRaises(c.ComponentError): c.rebuild_version_resource(target,'cli',self.root/'prefix')

    def test_container_tamper_and_publication_race(self):
        p = self.plain()
        raw = bytearray(p.read_bytes()); raw[-1] ^= 1; p.write_bytes(raw)
        with self.assertRaises(b.BundleError): c.restore(p,self.root/'new')
        self.assertFalse((self.root/'new').exists())
        q = self.root/'newbundle'; c.save(self.items[:2],q)
        original = b._publish
        target = self.root/'race'
        def racing(source,dest):
            if Path(dest)==target:
                target.mkdir(); (target/'note').write_text('new')
            return original(source,dest)
        with patch.object(b,'_publish',side_effect=racing):
            with self.assertRaises(b.BundleError): c.restore(q,target)
        self.assertEqual((target/'note').read_text(),'new')

    def test_malicious_service_and_paths(self):
        key,recipient = self.keys()
        self.service.write_text('{"shell":"touch /tmp/never", "ProgramArguments":["sh"]}')
        with self.assertRaises(c.ComponentError): c.save([self.items[3]],self.root/'bad',recipient=recipient)
        self.assertFalse((self.root/'bad').exists())
        with self.assertRaises(c.ComponentError): c.register('../escape',self.cli,kind='cli')
        (self.cli/'escape').symlink_to('/tmp')
        with self.assertRaises(b.BundleError): c.save([self.items[1]],self.root/'bad-link')

    def test_config_old_account_or_approval_never_staged(self):
        key,recipient = self.keys()
        for field in ('session','approved','api_key','PID','environment'):
            self.config.write_text(json.dumps({field:'synthetic-private-fixture'}))
            p = self.root/(field+'.bundle')
            c.save([self.items[2]],p,recipient=recipient)
            target = self.root/(field+'-new')
            c.restore(p,target,identity_file=key)
            with self.assertRaises(c.ComponentError):
                c.stage_config(target,'config',self.root/(field+'-active'))
            self.assertFalse((self.root/(field+'-active')).exists())

    def test_unknown_architecture_and_native_dependencies_not_ready(self):
        native = self.source/'native'; native.mkdir()
        (native/'codex').symlink_to('/opt/homebrew/bin/codex')
        item = c.register('native',native,kind='cli',adapter='native-closure',selected=True)
        with self.assertRaises(b.BundleError): c.save([item],self.root/'bad-native')
        (native/'codex').unlink(); (native/'codex').write_text('#!/bin/sh\nexit 0\n'); (native/'codex').chmod(0o755)
        p = self.plain([item,self.items[0]])
        target = self.root/'new'; c.restore(p,target)
        self.assertTrue(all(x['state']=='needs_rebuild' for x in c.plan(target,machine='incompatible')))
        row = c._catalog(target)[0]
        with self.assertRaises(c.ComponentError): c.probe_native(target,'native','codex',trusted_component_hash=row['hash'])

    def test_package_scripts_never_used_and_restore_interrupted(self):
        counter = self.root/'counter'
        (self.cli/'install.sh').write_text('touch '+str(counter))
        (self.cli/'version.py').write_text('raise Exception("untrusted package script")')
        p = self.plain(); target = self.root/'new'
        with patch.object(c,'plan',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): c.restore(p,target)
        self.assertFalse(target.exists())
        c.restore(p,target)
        c.rebuild_version_resource(target,'cli',self.root/'prefix')
        self.assertFalse(counter.exists())
        # Verify CPU metadata derives from bytes, not a caller claim.
        binary = self.app/'Contents/MacOS/T3'
        binary.write_bytes(bytes.fromhex('cffaedfe0c000001')+b'\x00'*24)
        self.assertEqual(c.app_code_metadata(self.app)['architectures'],['arm64'])

    def test_interrupted_build_and_repeated_prefix(self):
        p = self.plain(); target = self.root/'new'; c.restore(p,target)
        prefix = self.root/'prefix'
        with patch.object(subprocess,'run',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): c.rebuild_version_resource(target,'cli',prefix)
        self.assertFalse(prefix.exists())
        c.rebuild_version_resource(target,'cli',prefix)
        (prefix/'note').write_text('keep')
        with self.assertRaises(c.ComponentError): c.rebuild_version_resource(target,'cli',prefix)
        self.assertEqual((prefix/'note').read_text(),'keep')
        query = c.status(target,rebuilt_prefixes={'cli':prefix})
        self.assertEqual(next(x for x in query if x['id']=='cli')['state'],'needs_rebuild')





class ActualAdapterTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name).resolve()
        if os.sys.platform!='darwin' or not Path('/usr/bin/clang').exists():
            self.skipTest('Real native adapter checks require macOS developer tools')

    def compile(self,path):
        path.parent.mkdir(parents=True,exist_ok=True)
        source=self.root/('fixture-'+str(len(list(self.root.glob('*.c'))))+'.c')
        source.write_text('#include <stdio.h>\n#include <string.h>\nint main(int n,char **v){'
                          'if(n==2 && !strcmp(v[1],"--version")){puts("1.2.3");return 0;}'
                          'for(int i=1;i+1<n;i++){if(!strcmp(v[i],"--directory")){'
                          'char p[4096];snprintf(p,sizeof(p),"%s/counter",v[i+1]);'
                          'FILE *f=fopen(p,"a");if(!f)return 3;fputs("1",f);fclose(f);return 0;}}return 2;}\n')
        subprocess.run(['/usr/bin/clang',str(source),'-o',str(path)],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        return path

    def key(self):
        if not shutil.which('age-keygen') or not shutil.which('age'):
            self.skipTest('Real age toolchain required')
        key=self.root/'identity'
        with open(key,'wb') as f: subprocess.run(['age-keygen'],stdout=f,stderr=subprocess.DEVNULL,check=True)
        key.chmod(0o600)
        recipient=subprocess.check_output(['age-keygen','-y',str(key)],stderr=subprocess.DEVNULL).decode().strip()
        return key,recipient

    def test_all_known_adapters_offline_deleted_source_and_relocated_entry(self):
        for name in ('codex','claude','opencode'):
            with self.subTest(name=name):
                source=self.root/('old-'+name)
                if name=='codex':
                    target=c._TARGETS[c.platform.machine()]
                    binary=self.compile(source/'vendor'/target/'bin/codex')
                    self.compile(source/'vendor'/target/'codex-path/rg')
                    (source/'vendor'/target/'resources').mkdir()
                    (source/'vendor'/target/'resources/modified').write_text('retained')
                else: binary=self.compile(source/'versioned-file')
                selection=c.select_installed([name],candidates={name:binary})[0]
                self.assertEqual(selection['version'],'1.2.3')
                package=self.root/(name+'.bundle'); c.save([selection],package)
                shutil.rmtree(source)
                restored=self.root/('restore-'+name); c.restore(package,restored)
                row=c._catalog(restored)[0]
                prefix=self.root/('prefix-'+name)
                result=c.materialize_native(restored,name,prefix,trusted_component_hash=row['hash'])
                self.assertEqual(result['version'],'1.2.3')
                self.assertEqual(Path(result['entrypoint']).name,name)
                self.assertEqual(result['login'],'needs_login')
                moved=self.root/('relocated-'+name); prefix.rename(moved)
                self.assertEqual(c._probe_version(moved/'bin'/name),'1.2.3')
                self.assertEqual(c.status(restored,rebuilt_prefixes={name:moved})[0]['state'],'code_ready')
                with patch.object(c.platform,'machine',return_value='incompatible'):
                    self.assertEqual(c.status(restored,rebuilt_prefixes={name:moved})[0]['state'],'needs_rebuild')
                (moved/'new-note').write_text('keep')
                with self.assertRaises(c.ComponentError):
                    c.materialize_native(restored,name,moved,trusted_component_hash=row['hash'])
                self.assertEqual((moved/'new-note').read_text(),'keep')
                if name=='codex':
                    self.assertEqual((moved/'code/vendor'/target/'resources/modified').read_text(),'retained')

    def test_native_metadata_shim_and_version_failure_closed(self):
        self.assertTrue(c._system_library_available('/usr/lib/libSystem.B.dylib'))
        self.assertFalse(c._system_library_available('/usr/lib/not-a-real-component-asset.dylib'))
        shim=self.root/'vp'; shim.write_text('#!/bin/sh\necho 1.2.3\n'); shim.chmod(0o755)
        with self.assertRaises(c.ComponentError): c.select_installed(['codex'],candidates={'codex':shim})
        native=self.compile(self.root/'claude')
        selection=c.register('cli',native,kind='cli',adapter='claude-native',version='9.9.9',selected=True)
        package=self.root/'code.bundle';c.save([selection],package)
        restored=self.root/'restore';c.restore(package,restored)
        row=c._catalog(restored)[0]
        with self.assertRaises(c.ComponentError):
            c.materialize_native(restored,'cli',self.root/'prefix',trusted_component_hash=row['hash'])
        self.assertFalse((self.root/'prefix').exists())
        original_copy=c._copy
        def mutate_before_copy(source,target):
            if source==restored/'cli': (source/'code/claude').write_bytes(b'changed after catalog validation')
            return original_copy(source,target)
        with patch.object(c,'_copy',side_effect=mutate_before_copy), patch.object(c,'_probe_version',side_effect=AssertionError('untrusted code executed')):
            with self.assertRaises(c.ComponentError):
                c.materialize_native(restored,'cli',self.root/'raced-prefix',trusted_component_hash=row['hash'])
        self.assertFalse((self.root/'raced-prefix').exists())
        doc=json.loads((restored/'components.json').read_bytes())
        doc['components'][0]['native']['binary']='../../escape'
        item=doc['components'][0];item['hash']=c._hash({k:v for k,v in item.items() if k!='hash'})
        (restored/'components.json').write_text(json.dumps(doc))
        with self.assertRaises(c.ComponentError): c.plan(restored)

    def test_strict_signed_app_restored_ready_and_modified_signature_rejected(self):
        app=self.root/'SelfModified (Local).app'
        self.compile(app/'Contents/MacOS/Fixture (Local)')
        (app/'Contents/Resources').mkdir()
        (app/'Contents/Resources/actual').write_text('my own modifications')
        (app/'Contents/Resources/link').symlink_to('actual')
        (app/'Contents/Info.plist').write_bytes(plistlib.dumps({
            'CFBundleIdentifier':'test.portable.signed','CFBundleExecutable':'Fixture (Local)','CFBundleVersion':'1'}))
        subprocess.run(['/usr/bin/codesign','--force','--sign','-',str(app)],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        selection=c.register('app',app,kind='app',adapter='app-code',version='1',selected=True,login_required=True)
        package=self.root/'app.bundle';c.save([selection],package)
        shutil.rmtree(app)
        restored=self.root/'restored';states=c.restore(package,restored)
        self.assertEqual(states[0]['state'],'code_ready')
        proof=c.verify_app(restored,'app')
        self.assertEqual(proof['signature'],'strict_valid')
        self.assertEqual(proof['login'],'needs_login')
        self.assertEqual(proof['system_authorization'],'first_launch_not_performed')
        with patch.object(c.platform,'machine',return_value='incompatible'):
            self.assertEqual(c.verify_app(restored,'app')['state'],'needs_rebuild')
        # Valid tree hashes alone must not make a modified, invalid signature ready.
        (restored/'app/Contents/Resources/actual').write_text('unsigned change')
        selection=c.register('app2',restored/'app',kind='app',adapter='app-code',selected=True)
        p2=self.root/'modified.bundle';c.save([selection],p2)
        states=c.restore(p2,self.root/'modified')
        self.assertEqual(states[0]['state'],'needs_signature_review')
        self.assertEqual(c.inspect_app(restored/'app')['signature'],'not_verified')

    def service_fixture(self):
        key,recipient=self.key()
        source=self.root/'old'
        native=self.compile(source/'old-cli')
        work=source/'old-work';work.mkdir()
        service=source/'service.plist'
        service.write_bytes(plistlib.dumps({'Label':'old.runtime.identity',
            'ProgramArguments':[str(native),'--port','8099','--directory',str(work)],
            'WorkingDirectory':str(work),'StartCalendarInterval':[{'Hour':9,'Minute':30},{'Weekday':1,'Hour':10}],
            'RunAtLoad':True,'KeepAlive':True}))
        selections=[c.register('worker',native,kind='cli',adapter='claude-native',version='1.2.3',selected=True),
                    c.register('service',service,kind='launchd',adapter='launchd-plist',selected=True,
                        program_bindings={str(native):{'component':'worker','profile':'dev-server',
                                                      'path_bindings':{str(work):'workspace'}},str(work):'workspace'})]
        package=self.root/'services.bundle';c.save(selections,package,recipient=recipient)
        shutil.rmtree(source)
        restored=self.root/'restored';c.restore(package,restored,identity_file=key)
        row=next(r for r in c._catalog(restored) if r['id']=='worker')
        prefix=self.root/'new-cli';c.materialize_native(restored,'worker',prefix,trusted_component_hash=row['hash'])
        workspace=self.root/'new-work';workspace.mkdir()
        staged=self.root/'staged'
        c.stage_service(restored,'service',staged,device='new-device',bindings={'worker':prefix,'workspace':workspace})
        return restored,prefix,workspace,staged

    def test_disabled_actual_plist_gate_consume_and_fixed_counter(self):
        from portable.core import Store, Blocked
        root,prefix,workspace,staged=self.service_fixture()
        definition=plistlib.loads((staged/'disabled.plist').read_bytes())
        self.assertTrue(definition['Disabled']);self.assertFalse(definition['RunAtLoad'])
        self.assertEqual(len(definition['StartCalendarInterval']),2)
        self.assertNotIn('old.runtime.identity',json.dumps(definition))
        self.assertNotIn(str(self.root/'old'),json.dumps(definition))
        self.assertFalse((workspace/'counter').exists())
        (staged/'new-note').write_text('keep')
        with self.assertRaises(c.ComponentError):
            c.stage_service(root,'service',staged,device='new-device',bindings={'worker':prefix,'workspace':workspace})
        self.assertEqual((staged/'new-note').read_text(),'keep')
        calls=[]
        def fake_launchctl(domain,plist):
            calls.append(domain)
            value=plistlib.loads(plist.read_bytes())
            self.assertFalse(value['Disabled'])
            # A fixed synthetic native counter only; no real launchctl/service/network.
            subprocess.run(value['ProgramArguments'],env=value['EnvironmentVariables'],
                           cwd=value['WorkingDirectory'],check=True)
        store=Store(self.root/'private-gate-store')
        hook=c.activation_hook_for_store(store,task='test-task',owner='new-owner',launchctl=fake_launchctl)
        p=c.prepare_service_activation(root,'service',staged,device='new-device',scopes=['service'],hook=hook)
        token=store.approval(c.service_approval_binding(p,task='test-task',owner='new-owner'))
        for key,val in [('device','other'),('scopes',['external_connection']),('hash','changed'),('host','old-host')]:
            changed={**p,key:val}
            with self.assertRaises(c.ComponentError):
                c.activate_service(root,'service',staged,changed,token,device='new-device',scopes=['service'],hook=hook)
        newer=c.prepare_service_activation(root,'service',staged,device='new-device',scopes=['service'],hook=hook)
        with self.assertRaises(c.ComponentError):
            c.activate_service(root,'service',staged,p,token,device='new-device',scopes=['service'],hook=hook)
        # A serialized boolean decision is not an existing Gate token.
        with self.assertRaises((Blocked,c.ComponentError,TypeError)):
            c.activate_service(root,'service',staged,newer,c.decision(newer,approved=True),device='new-device',scopes=['service'],hook=hook)
        self.assertFalse(calls);self.assertFalse((workspace/'counter').exists())
        p=c.prepare_service_activation(root,'service',staged,device='new-device',scopes=['service'],hook=hook)
        token=store.approval(c.service_approval_binding(p,task='test-task',owner='new-owner'))
        result=c.activate_service(root,'service',staged,p,token,device='new-device',scopes=['service'],hook=hook)
        self.assertEqual(result['state'],'bootstrap_requested')
        self.assertEqual((workspace/'counter').read_text(),'1')
        self.assertEqual(len(calls),1)
        self.assertTrue(plistlib.loads((staged/'disabled.plist').read_bytes())['Disabled'])
        with self.assertRaises(c.ComponentError):
            c.activate_service(root,'service',staged,p,token,device='new-device',scopes=['service'],hook=hook)
        self.assertEqual((workspace/'counter').read_text(),'1')

    def test_service_binding_and_plist_tamper_prevent_activation(self):
        root,prefix,workspace,staged=self.service_fixture()
        hook=c.TrustedActivationHook(lambda token,p:True,launchctl=lambda *args:self.fail('must not launch'))
        proposal=c.prepare_service_activation(root,'service',staged,device='new-device',scopes=['service'],hook=hook)
        (workspace/'new-note').write_text('new content')
        with self.assertRaises(c.ComponentError):
            c.activate_service(root,'service',staged,proposal,'token',device='new-device',scopes=['service'],hook=hook)
        self.assertEqual((workspace/'new-note').read_text(),'new content')
        (workspace/'new-note').unlink()
        value=plistlib.loads((staged/'disabled.plist').read_bytes());value['Disabled']=False
        (staged/'disabled.plist').write_bytes(plistlib.dumps(value))
        with self.assertRaises(c.ComponentError):
            c.prepare_service_activation(root,'service',staged,device='new-device',scopes=['service'],hook=hook)

    def test_ssh_forward_disabled_recipe_and_unsafe_argv_preservation(self):
        key,recipient=self.key()
        source=self.root/'ssh.plist'
        source.write_bytes(plistlib.dumps({'Label':'old.ssh','ProgramArguments':[
            '/usr/bin/ssh','-N','-T','-L','127.0.0.1:8123:db.internal:5432','-p','2222','worker@fixture.invalid'],
            'StartCalendarInterval':{'Minute':0},'RunAtLoad':True}))
        item=c.register('tunnel',source,kind='launchd',adapter='launchd-plist',selected=True,
                        program_bindings={'/usr/bin/ssh':{'component':'system-ssh','profile':'ssh-forward'}})
        package=self.root/'ssh.bundle';c.save([item],package,recipient=recipient);source.unlink()
        restored=self.root/'ssh-restored';c.restore(package,restored,identity_file=key)
        staged=self.root/'new-ssh';c.stage_service(restored,'tunnel',staged,device='new-device',bindings={})
        value=plistlib.loads((staged/'disabled.plist').read_bytes())
        self.assertTrue(value['Disabled'])
        self.assertIn('BatchMode=yes',value['ProgramArguments'])
        self.assertIn('StrictHostKeyChecking=yes',value['ProgramArguments'])
        self.assertIn('IdentityFile=none',value['ProgramArguments'])
        self.assertEqual(value['ProgramArguments'][0],'/usr/bin/ssh')
        self.assertFalse((staged/'home/known_hosts').exists())
        hook=c.TrustedActivationHook(lambda *a:self.fail('Gate must not be called'))
        with self.assertRaises(c.ComponentError):
            c.prepare_service_activation(restored,'tunnel',staged,device='new-device',scopes=['service'],hook=hook)
        proposal=c.prepare_service_activation(restored,'tunnel',staged,device='new-device',scopes=['external_connection'],hook=hook)
        self.assertTrue(proposal['disabled'])
        identity=self.root/'new identity';identity.write_text('synthetic-key-never-used');identity.chmod(0o600)
        hosts=self.root/'new known hosts';hosts.write_text('synthetic-host-never-connected');hosts.chmod(0o600)
        bound=self.root/'bound-ssh'
        files={'identity_file':str(identity),'known_hosts':str(hosts)}
        # Only metadata is touched, no account/key contents are read by preparation.
        original_read=Path.read_bytes
        def no_identity_read(path):
            if path in {identity,hosts}: self.fail('must not read new credential contents')
            return original_read(path)
        with patch.object(Path,'read_bytes',no_identity_read):
            c.stage_service(restored,'tunnel',bound,device='new-device',bindings={},ssh_identity=files)
        bound_def=plistlib.loads((bound/'disabled.plist').read_bytes())
        self.assertIn('IdentityFile='+c._ssh_path_option(str(identity)),bound_def['ProgramArguments'])
        with self.assertRaises(c.ComponentError): c._ssh_path_option('/private/%d/old_identity')
        self.assertTrue(bound_def['Disabled'])
        c.prepare_service_activation(restored,'tunnel',bound,device='new-device',scopes=['external_connection'],hook=hook)
        identity.write_text('changed-new-identity')
        with self.assertRaises(c.ComponentError):
            c.prepare_service_activation(restored,'tunnel',bound,device='new-device',scopes=['external_connection'],hook=hook)
        # Unknown ssh options and shell commands are kept for review, never normalized.
        source.write_bytes(plistlib.dumps({'ProgramArguments':['/usr/bin/ssh','-o','ProxyCommand=touch counter','fixture.invalid']}))
        inspected=c.inspect_launchd(source,program_bindings={'/usr/bin/ssh':{'component':'system-ssh','profile':'ssh-forward'}})
        self.assertEqual(inspected['state'],'cannot_migrate')
        self.assertNotIn('ProxyCommand',json.dumps(inspected))

    def test_authorized_real_installed_three_cli_code_only_offline(self):
        metadata=Path(os.environ.get('PORTABLE_REAL_CLI_METADATA','/nonexistent'))
        if not metadata.is_file(): self.skipTest('Authorized real code inventory absent; synthetic adapters tested separately')
        rows=json.loads(metadata.read_bytes())
        self.assertEqual({r['name'] for r in rows},{'codex','claude','opencode'})
        for row in rows:
            with self.subTest(tool=row['name']):
                # Per-tool lifetime bounds disk use; all source installs remain untouched.
                with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                    folder=Path(tmp)
                    name=row['name']
                    selected=c.select_installed([name],candidates={name:row['native']})[0]
                    source,_,_=c._native_spec(selected['adapter'],selected['source'])
                    mirror=folder/('vendor' if name=='codex' else 'original-versioned-binary')
                    if source.is_dir(): shutil.copytree(source,mirror,symlinks=True)
                    else: shutil.copy2(source,mirror)
                    selected['source']=str(mirror)
                    package=folder/'code.bundle';c.save([selected],package)
                    if mirror.is_dir(): shutil.rmtree(mirror)
                    else: mirror.unlink()
                    restored=folder/'restored';c.restore(package,restored)
                    component=c._catalog(restored)[0]
                    prefix=folder/'new-prefix'
                    result=c.materialize_native(restored,name,prefix,trusted_component_hash=component['hash'])
                    self.assertEqual(result['version'],selected['version'])
                    self.assertEqual(result['probe'],'version_verified')
                    self.assertEqual(result['probe_environment'],'new_home_and_system_plus_local_path')
                    self.assertEqual(Path(result['entrypoint']).name,name)
                    self.assertEqual(c.status(restored,rebuilt_prefixes={name:prefix})[0]['state'],'code_ready')
                    if name=='codex':
                        target=c._TARGETS[c.platform.machine()]
                        self.assertTrue((prefix/'code/vendor'/target/'codex-path/rg').is_file())
                        self.assertTrue((prefix/'code/vendor'/target/'bin/codex-code-mode-host').is_file())

    def test_selected_real_t3_readonly_strict_inspection(self):
        app=Path.home()/'Applications/T3 Code (Beta).app'
        if not app.is_dir(): self.skipTest('Selected T3 code bundle absent')
        result=c.inspect_app(app)
        self.assertIn(result['state'],{'code_ready','needs_rebuild','needs_signature_review'})
        self.assertEqual(result['system_authorization'],'first_launch_not_performed')
        if result['state']=='code_ready':
            self.assertEqual(result['signature'],'strict_valid')
            self.assertEqual(result['code']['dependencies'],'closure_or_system_verified')


if __name__=='__main__': unittest.main()
