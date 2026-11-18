"""Synthetic fixtures only; never open installed project/state sources."""
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch
from portable import legacy_assets as la
from portable import git_portable as gp
from portable.core import Blocked


class LegacyAssetsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.repo = self.root/'repo'
        self.repo.mkdir()
        gp.git(self.repo, 'init')
        gp.git(self.repo, 'config', 'user.name', 'Fixture')
        gp.git(self.repo, 'config', 'user.email', 'fixture@invalid')
        (self.repo/'file').write_text('base\n')
        gp.git(self.repo, 'add', '.')
        gp.git(self.repo, 'commit', '-m', 'fixture')
        self.w1, self.w2 = self.root/'wt1', self.root/'wt2'
        for i, w in enumerate((self.w1, self.w2)):
            gp.git(self.repo, 'worktree', 'add', '-b', 'branch'+str(i), str(w))
        gp.git(self.repo, 'worktree', 'lock', str(self.w2))
        (self.w1/'file').write_text('staged\n')
        gp.git(self.w1, 'add', 'file')
        (self.w1/'file').write_text('unstaged\n')
        (self.w1/'new').write_text('untracked\n')
        missing = self.root/'gone'
        gp.git(self.repo, 'worktree', 'add', '-b', 'missing', str(missing))
        (missing/'file').write_text('missing staged\n')
        gp.git(missing, 'add', 'file')
        shutil.rmtree(missing)
        self.context = self.root/'rules.md'
        self.context.write_text('Historical business decision; requires current review.\n')
        self.state = self.root/'state'
        for caller in ('one', 'two'):
            p = self.state/'tasks'/caller
            p.mkdir(parents=True)
            (p/'spec.json').write_text(json.dumps({'caller':caller,'status':'done'}))
            (p/'result.md').write_text('Evidence only\n')
        (self.state/'auth').mkdir()
        (self.state/'auth'/'private').write_text('excluded fixture account material')
        self.projects = [dict(id='project',path=str(self.repo),context_roots=[str(self.context)],project_home=str(self.root))]
        self.histories = [dict(id='old',state_root=str(self.state),selections=['tasks/one/spec.json','tasks/one/result.md','tasks/two/spec.json'])]

    def capture(self, **kwargs):
        out = self.root/'bundle'
        la.capture(self.projects,out,histories=self.histories,history_plaintext=True,**kwargs)
        return out

    def test_destroy_sources_restore_exact_git_and_inert_history(self):
        expected = [(gp.git(w,'rev-parse','HEAD').stdout,gp.git(w,'diff','--binary').stdout,gp.git(w,'diff','--cached','--binary').stdout,gp.git(w,'status','--porcelain').stdout) for w in (self.repo,self.w1,self.w2)]
        index = la.history_index(self.state)
        self.assertEqual(len(index),2)
        report = la.preflight(self.projects+[dict(id='alias',path=str(self.w1))])
        self.assertEqual(len(report['projects']),1)
        self.assertEqual(report['projects'][0]['aliases'],['alias'])
        self.assertTrue(any(w['locked'] for w in report['projects'][0]['worktrees']))
        self.assertIn('missing',la.format_preflight(report))
        controls = self.repo/'.git/worktrees/gone'
        missing_index = (controls/'index').read_bytes()
        package = self.capture()
        for p in (self.repo,self.w1,self.w2,self.state):
            shutil.rmtree(p)
        self.context.unlink()
        target = self.root/'new-machine'
        manifest = la.restore(package,target)
        self.assertFalse(manifest['complete'])
        self.assertTrue(manifest['selection_captured'])
        self.assertEqual(len(manifest['unresolved']),1)
        actual = []
        for w in manifest['projects'][0]['worktrees']:
            if w['exists']:
                r = target/w['restored_path']
                actual.append((gp.git(r,'rev-parse','HEAD').stdout,gp.git(r,'diff','--binary').stdout,gp.git(r,'diff','--cached','--binary').stdout,gp.git(r,'status','--porcelain').stdout))
                self.assertTrue((r/'.git').is_dir())
                self.assertFalse((r/'.git/worktrees').exists())
        self.assertEqual(actual,expected)
        self.assertEqual((target/'projects/project/context/0').read_text(),'Historical business decision; requires current review.\n')
        historical = target/'projects/project/historical-control'
        self.assertTrue(list(historical.rglob('index')))
        self.assertEqual((historical/'gone/index').read_bytes(), missing_index)
        self.assertTrue(any(c['source_gitdir'].endswith('/gone/.git') for c in manifest['projects'][0]['historical_controls']))
        self.assertFalse(list(historical.rglob('gitdir')))
        self.assertFalse(list(target.rglob('auth')))
        draft = la.continuation_draft(manifest,'one',new_caller='fresh')
        self.assertEqual(draft['done'],'unknown')
        self.assertIn('DRAFT',draft['state'])
        self.assertEqual((target/'history/old/tasks/one/spec.json').read_text(),'{"caller": "one", "status": "done"}')
        with self.assertRaises(Exception):
            la.restore(package,target)

    def test_flat_task_metadata_survives_source_loss(self):
        task_id = 'task-flat_123'
        run_id = 'run-flat_456'
        metadata = {'task_id': task_id, 'caller': 'historical-owner',
                    'status': 'done', 'runs': [run_id],
                    'goal': 'synthetic historical goal'}
        flat = self.state/'tasks'/f'{task_id}.json'
        flat.write_text(json.dumps(metadata))
        run = self.state/'dispatches'/run_id
        run.mkdir(parents=True)
        (run/'meta.json').write_text(json.dumps({'task_id': task_id, 'caller': 'historical-owner'}))
        (run/'result.md').write_text('historical evidence; no business success asserted')
        # Listing must not expose or parse caller/spec/log content.
        with patch.object(Path, 'read_text', side_effect=AssertionError('content read')), patch.object(Path, 'read_bytes', side_effect=AssertionError('content read')):
            index = la.history_index(self.state)
        flat_entry = next(e for e in index if e['id'] == task_id)
        self.assertEqual(flat_entry['layout'], 'flat-json')
        self.assertNotIn('historical-owner', json.dumps(index))
        self.assertTrue(any(e['id'] == run_id for e in index))
        histories = [dict(id='old', state_root=str(self.state), selections=[
            f'tasks/{task_id}.json', 'tasks/one/spec.json',
            f'dispatches/{run_id}/meta.json', f'dispatches/{run_id}/result.md'])]
        before = gp.fingerprint(self.state)
        package = self.root/'flat-package'
        la.capture([], package, histories=histories, history_plaintext=True)
        self.assertEqual(gp.fingerprint(self.state), before)
        shutil.rmtree(self.state)
        target = self.root/'flat-restored'
        manifest = la.restore(package, target)
        restored = json.loads((target/f'history/old/tasks/{task_id}.json').read_text())
        self.assertEqual(restored, metadata)
        run_metadata = json.loads((target/f'history/old/dispatches/{restored["runs"][0]}/meta.json').read_text())
        self.assertEqual(run_metadata['task_id'], task_id)
        self.assertFalse((target/'tasks').exists())
        self.assertFalse((target/'dispatches').exists())
        self.assertFalse(list(target.rglob('auth')))
        draft = la.continuation_draft(manifest, task_id, new_caller='fresh-executor')
        self.assertEqual(draft['source_caller'], 'unknown')
        self.assertEqual(draft['done'], 'unknown')
        self.assertEqual(draft['new_caller'], 'fresh-executor')
        self.assertEqual(len(draft['evidence']), 1)
        self.assertTrue(draft['evidence'][0]['restored_path'].endswith(f'tasks/{task_id}.json'))
        with self.assertRaises(Blocked):
            la.continuation_draft(manifest, task_id, new_caller='historical-owner',
                                  checkpoint={'source_caller': 'historical-owner'})

    def test_flat_task_selection_rejects_paths_secrets_and_links(self):
        flat = self.state/'tasks/task-safe.json'
        flat.write_text(json.dumps({'task_id': 'task-safe', 'password': 'syntheticfixtureonly'}))
        for name in ('tasks/task-safe.json', 'tasks/../auth/private',
                     'tasks/../../outside.json', 'tasks/.json',
                     'tasks/bad.id.json', 'dispatches/run.json', 'auth/task-safe.json',
                     '/tasks/task-safe.json', 'tasks//task-safe.json',
                     'tasks/task-safe.json/', 'tasks/task-safe.json/../other',
                     'tasks/task-safe.json\\outside'):
            with self.subTest(selection=name):
                target = self.root/'rejected'
                with self.assertRaises(Blocked):
                    la.capture([], target, histories=[dict(id='old', state_root=str(self.state), selections=[name])], history_plaintext=True)
                self.assertFalse(target.exists())
        flat.write_text('{"caller":')
        with self.assertRaises(Blocked):
            la.capture([], self.root/'partial-rejected', histories=[dict(id='old', state_root=str(self.state), selections=['tasks/task-safe.json'])], history_plaintext=True)
        self.assertFalse((self.root/'partial-rejected').exists())
        flat.unlink()
        flat.symlink_to(self.state/'tasks/one/spec.json')
        with self.assertRaises(Blocked):
            la.capture([], self.root/'link-rejected', histories=[dict(id='old', state_root=str(self.state), selections=['tasks/task-safe.json'])], history_plaintext=True)
        self.assertFalse(any(e['id'] == 'task-safe' for e in la.history_index(self.state)))
        flat.unlink()
        flat.mkdir()
        with self.assertRaises(Blocked):
            la.capture([], self.root/'directory-rejected', histories=[dict(id='old', state_root=str(self.state), selections=['tasks/task-safe.json'])], history_plaintext=True)

    def test_change_after_pack_before_publish(self):
        pack = la.sealed.pack
        def changed(*args, **kwargs):
            result = pack(*args, **kwargs)
            (self.w2/'late-change').write_text('changed after pack')
            return result
        with patch.object(la.sealed, 'pack', side_effect=changed):
            with self.assertRaises(Blocked):
                self.capture()
        self.assertFalse((self.root/'bundle').exists())

    def test_source_mutation_never_publishes(self):
        def normalize(src,dst):
            gp.export(src,dst)
            (self.w2/'late').write_text('changed')
        with self.assertRaises(Blocked):
            self.capture(normalize=normalize)
        self.assertFalse((self.root/'bundle').exists())

    def test_failures(self):
        with self.assertRaises(Blocked):
            self.capture(stopped=lambda:None)
        with patch.object(la.shutil,'disk_usage',return_value=type('Space',(),{'free':0})()):
            with self.assertRaises(Blocked):
                self.capture()
        with self.assertRaises(Blocked):
            self.capture(archives=[dict(id='overlap',path=str(self.w1))])
        (self.root/'external').write_text('outside')
        (self.w1/'link').symlink_to(self.root/'external')
        with self.assertRaises(Blocked):
            self.capture()
        (self.w1/'link').unlink()
        with self.assertRaises(Blocked):
            la.capture(self.projects,self.root/'bundle',histories=[dict(id='bad',state_root=str(self.state),selections=['auth/private'])])
        with patch.object(gp,'export',side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.capture()
        self.assertFalse((self.root/'bundle').exists())

    def test_tampering_secret_and_partial_metadata(self):
        (self.w1/'private.txt').write_text('password='+'syntheticfixturevalue')
        with self.assertRaises(Blocked):
            self.capture()
        (self.w1/'private.txt').unlink()
        with patch.object(gp,'git',return_value=type('Bad',(),{'stdout':b'HEAD partial\0\0'})()):
            with self.assertRaises(Blocked):
                la._records(self.repo)
        package = self.capture()
        raw = bytearray(package.read_bytes()); raw[-10] ^= 1; package.write_bytes(raw)
        with self.assertRaises(Exception):
            la.restore(package,self.root/'corrupt')
        self.assertFalse((self.root/'corrupt').exists())

    @unittest.skipUnless(shutil.which('age') and shutil.which('age-keygen'), 'age tools unavailable')
    def test_real_age_with_synthetic_identity(self):
        import os
        import subprocess
        key = self.root/'identity'
        with open(key, 'wb') as out:
            subprocess.run(['age-keygen'], stdout=out, stderr=subprocess.DEVNULL, check=True)
        os.chmod(key, 0o600)
        recipient = subprocess.run(['age-keygen', '-y', str(key)], capture_output=True, check=True).stdout.decode().strip()
        selected = self.state/'tasks/task-private.json'
        selected.write_text(json.dumps({'password': 'syntheticfixtureonly'}))
        histories = [dict(id='old', state_root=str(self.state), selections=['tasks/task-private.json'])]
        package = self.root/'encrypted'
        la.capture(self.projects, package, histories=histories, sensitive=True, recipient=recipient)
        self.assertTrue(la.sealed.inspect(package)['encrypted'])
        la.restore(package, self.root/'decrypted', identity_file=key)
        self.assertTrue((self.root/'decrypted/history/old/tasks/task-private.json').exists())
        self.assertNotIn(b'syntheticfixtureonly', package.read_bytes())

    def test_stop_during_packaging_and_private_history_gate(self):
        with self.assertRaises(Blocked):
            la.capture(self.projects,self.root/'bundle',histories=self.histories)
        calls = iter((True, True, False))
        with self.assertRaises(Blocked):
            self.capture(stopped=lambda:next(calls))
        self.assertFalse((self.root/'bundle').exists())

    def test_archives_and_exclusions(self):
        archive = self.root/'cards'; archive.mkdir()
        (archive/'card.md').write_text('historical card')
        p = self.capture(archives=[dict(id='cards',path=str(archive))],exclusions=[{'source':'unknown historical folder','reason':'not selected'}])
        m = la.restore(p,self.root/'restored')
        self.assertFalse(m['complete'])
        self.assertEqual((self.root/'restored/archives/cards/original/card.md').read_text(),'historical card')


if __name__ == '__main__':
    unittest.main()
