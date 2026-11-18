"""Actual T3Adapter tool-call wiring on synthetic shared input; no real tools."""
import json
from pathlib import Path
import tempfile

from portable.taskline_handoff import HandoffStore, sha256, utf16_units
from taskline_shared_fixture import load_fixture, FixtureQueries, SimulatedT3, FIXTURE_SHA256


def main():
    fixture = load_fixture()
    request = fixture['request']
    manifest = request['manifest']
    queries = FixtureQueries(fixture)
    service = SimulatedT3(fixture)
    with tempfile.TemporaryDirectory(prefix='portable-taskline-demo-') as directory:
        root = Path(directory).resolve() / 'state'
        store = HandoffStore(root)
        prepared = store.prepare_manifest(**request)
        mid = manifest['manifest_id']
        absent = store.advance(mid)
        adapter = queries.adapter(service)  # production T3Adapter, injected transport
        hids = list(queries.bindings)
        queries.ready_member(hids[0])
        incomplete = store.advance(mid, adapter)
        queries.ready_member(hids[1])
        service.archive_mode = 'after_timeout'
        unknown = store.advance(mid, adapter)
        start = len(service.calls)
        recovered = HandoffStore(root).advance(mid, queries.adapter(service))
        title = service.threads[service.source]['title']
        print(json.dumps({
            'validation': 'SIMULATED_ONLY_NO_REAL_T3',
            'adapter': 'portable.taskline_handoff.T3Adapter',
            'fixture_sha256': FIXTURE_SHA256,
            'manifest_digest': sha256(manifest),
            'prepared': prepared['phase'], 'without_adapter': absent['blocker'],
            'two_tasks_only_one_ready': incomplete['blocker'],
            'unknown_archive_result': unknown['archive_outcome'],
            'restart_readback': recovered['phase'],
            'tool_sequence': [name for name, _ in service.calls],
            'recovery_sequence': [name for name, _ in service.calls[start:]],
            'readback_title': title, 'title_utf16_units': utf16_units(title),
            'original_task_runs': [{'task_id': b['task_id'], 'run_id': b['run_id']}
                                   for b in manifest['bindings']],
            'worker_launches': 0,
            'rename_calls': sum(name == 't3_thread_update' for name, _ in service.calls),
            'archive_calls': sum(name == 't3_thread_organize' for name, _ in service.calls),
            'repeated_archive_calls': sum(name == 't3_thread_organize' for name, _ in service.calls[start:])
        }, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
