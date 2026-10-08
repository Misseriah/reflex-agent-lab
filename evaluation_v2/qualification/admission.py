"""Fail-closed readiness decision; this module cannot launch a paid experiment."""
from evaluation_v2.judge import require
from reflex.types import digest
from .evidence import file_hash, read


def verify_bundle(directory):
    directory = directory.resolve()
    manifest = read(directory / 'manifest.json')
    for relative, expected in manifest['artifact_sha256'].items():
        path = (directory / relative).resolve()
        require(path.is_relative_to(directory) and path.is_file(), 'Missing or escaping qualification artifact')
        require(file_hash(path) == expected, 'Qualification artifact changed: ' + relative)
    for absolute, expected in manifest['code_sha256'].items():
        require(file_hash(absolute) == expected, 'Qualification code changed; requalification required')
    require(manifest['paid_calls'] == 0 and manifest['source_evidence_unchanged'] is True, 'Invalid qualification provenance')
    return manifest


def admission(qualification, review, plan):
    evidence = qualification['evidence_coverage']
    checks = {
        'offline_engineering_and_replay': qualification['engineering_passed'] is True,
        'original_evidence_complete': evidence['original_state_snapshots_complete'] is True,
        'independent_semantic_review': review['semantic_qualification_passed'] is True,
        'final_answer_and_safety_coverage': evidence['final_answer_claims_complete'] is True and evidence['broad_safety_contracts_complete'] is True,
        'new_population_and_splits_frozen': isinstance(plan['new_sampling_frame'], dict) and bool(plan['new_sampling_frame'])
            and isinstance(plan['new_split_manifest'], dict) and bool(plan['new_split_manifest']),
        'threshold_power_and_budget_approved': all(plan[k] is True for k in
            ('thresholds_approved', 'power_target_approved', 'budget_approved', 'decision_contracts_independently_approved')),
        'explicit_spending_authorization': plan['paid_execution_authorized'] is True,
    }
    # Even numerical/design readiness does not authorize running providers from this offline package.
    return {'status': 'READY_FOR_MANUAL_PROTOCOL_REVIEW' if all(checks.values()) else 'BLOCKED',
            'checks': checks, 'blocking_gates': [k for k, v in checks.items() if not v],
            'new_paid_experiment_allowed': False, 'provider_launch_supported': False,
            'input_digest': digest([qualification, review, plan]),
            'scope': 'Enforced for the qualification CLI only; historical runners are unchanged and are not V2-qualified entry points'}
