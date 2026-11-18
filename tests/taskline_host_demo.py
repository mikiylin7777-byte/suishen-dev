"""Isolated host product composition; no real T3, worker or model is started."""
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from taskline_cross_host_checks import CrossHostTests


def main():
    case = CrossHostTests()
    case.setUp()
    try:
        first = case.host.loop(case.mid, iterations=2)
        binding = case.fixture['request']['manifest']['bindings'][0]
        answer = case.dispatch_root / 'dispatches' / binding['run_id'] / 'final-answer.txt'
        answer.write_text('SYNTHETIC LATE RESULT')
        second = case.host.loop(case.mid, resume=True)
        count = len(case.transport.sent)
        case.host.loop(case.mid, resume=True)
        state = case.host.store.status(case.mid)
        report = {
            'validation': 'ISOLATED_PRODUCT_COMPOSITION',
            'formal_identity_provider': 'SYNTHETIC_TEST_ONLY',
            'real_t3_calls': 0, 'real_worker_or_model_starts': 0,
            'manifest_members': len(case.fixture['request']['manifest']['bindings']),
            'first_loop': first, 'late_result_loop': second,
            'verified_receipt_kinds': sorted(set(r['kind'] for r in state['receipts'])),
            'late_result_delivered': any(p['payload'].get('result', {}).get('text') == answer.read_text()
                                         for p in case.transport.sent),
            'repeat_generated_no_send': len(case.transport.sent) == count,
            'pending_events': sum(case.bridge.routes.status(b['handoff_id'])['pending_count']
                                  for b in case.fixture['request']['manifest']['bindings']),
            'archive_rpc_calls': sum(name == 't3_thread_organize'
                                     for name, _ in case.services.service.calls),
            'source_archived': case.services.service.threads[case.services.service.source]['archived'],
            'atomic_conditional_archive': False,
        }
        assert report['late_result_delivered'] and report['repeat_generated_no_send']
        assert report['pending_events'] == report['archive_rpc_calls'] == 0
        assert not report['source_archived']
        print(json.dumps(report, ensure_ascii=False, indent=2))
    finally:
        case.doCleanups()


if __name__ == '__main__':
    main()
