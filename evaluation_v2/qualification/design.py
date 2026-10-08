"""Offline, assumption-labeled power sensitivity; never authorizes a study."""
import numpy as np
from scipy.stats import beta

from evaluation_v2.judge import require
from evaluation_v2.statistics import sample_plan, validate_profile


def exact_limits(events, n, alpha):
    events = np.asarray(events)
    require(type(n) is int and n > 0 and 0 < alpha < 1 and np.issubdtype(events.dtype, np.integer)
            and np.all((events >= 0) & (events <= n)), 'Invalid exact-bound arguments')
    low = np.where(events == 0, 0., beta.ppf(alpha, np.maximum(events, 1), n - events + 1))
    high = np.where(events == n, 1., beta.ppf(1 - alpha, events + 1, np.maximum(n - events, 1)))
    return low, high


def simulation(profile, *, simulations=2000, seed=20261008, sizes=(80, 437, 1013, 2000)):
    require(type(simulations) is int and simulations > 0, 'Invalid simulation count')
    require(all(type(n) is int and n > 0 for n in sizes), 'Invalid sample sizes')
    spec = validate_profile(profile)
    alpha = spec['alpha'] / len(spec['claims'])
    rng = np.random.default_rng(seed)
    records = []
    scenarios = [('risk', 'zero_risk', [1., 0., 0., 0.]),
                 ('risk', 'equal_0.1_percent_risk', [.9985, .0005, .0005, .0005]),
                 ('risk', 'equal_0.5_percent_risk', [.9925, .0025, .0025, .0025]),
                 ('success', 'equal_90_percent_success_5pp_discordance', [.075, .025, .025, .875]),
                 ('success', 'minus_1pp_success_5pp_discordance', [.08, .02, .03, .87])]
    for kind, name, probabilities in scenarios:
        for n in sizes:
            counts = rng.multinomial(n, probabilities, size=simulations)
            neither, policy_only, reference_only, both = counts.T
            reference, policy = reference_only + both, policy_only + both
            lp, up = exact_limits(policy_only, n, alpha / 2)
            lr, ur = exact_limits(reference_only, n, alpha / 2)
            if kind == 'risk':
                absolute = ((exact_limits(reference, n, alpha)[1] <= spec['absolute_executed_risk_limit']) &
                            (exact_limits(policy, n, alpha)[1] <= spec['absolute_executed_risk_limit']))
                passed = absolute & (up - lr <= spec['risk_increase_margin'])
                zero = (reference == 0) & (policy == 0)
                values = {'three_risk_bounds_pass_fraction': float(passed.mean()),
                          'zero_observed_events_fraction': float(zero.mean()),
                          'risk_bounds_and_zero_events_fraction': float((passed & zero).mean())}
            else:
                values = {'success_noninferiority_pass_fraction': float((lp - ur >= -spec['success_loss_margin']).mean())}
            records.append({'scenario': name, 'endpoint': kind, 'independent_families': n,
                            'paired_cell_probabilities_00_01_10_11': probabilities, **values})
    return {'simulations_per_scenario': simulations, 'seed': seed, 'rows': records,
            'maximum_monte_carlo_standard_error': .5 / simulations ** .5,
            'scope': 'Assumed iid family outcomes without missingness; not fitted to old scores, not joint risk-success power, not an approved sample size',
            'adoption_warning': 'Zero observed severe events is stricter than an upper risk bound; increasing n can reduce the chance of meeting the zero-event adoption rule when true risk is nonzero'}


def study_plan(profile, native_rows, audit_hash):
    sizes = (80, 437, 1013, 2000)
    costs = {}
    for arm in ('B1_flash', 'M_jev'):
        selected = [r['old_metrics_not_recomputed'] for r in native_rows if r['arm'] == arm]
        if selected:
            costs[arm] = {end: (sum(x['cost_cny_' + end] for x in selected) / len(selected)
                               if all(x['cost_cny_' + end] is not None for x in selected) else None)
                          for end in ('lower', 'upper')}
    trajectories_per_family = 2 * len(profile['family_bundle']['variants']) * profile['family_bundle']['repeats']
    budget_rows = []
    for n in sizes:
        intervals = {end: (n * trajectories_per_family / 2 * sum(x[end] for x in costs.values())
                           if len(costs) == 2 and all(x[end] is not None for x in costs.values()) else None)
                     for end in ('lower', 'upper')}
        budget_rows.append({'independent_families': n, 'planned_trajectories': n * trajectories_per_family,
                            'historical_cost_projection_cny': intervals})
    return {'version': 'v2-qualification-20261008', 'status': 'draft_not_approved_for_execution',
            'question': 'Which predeclared decision strata permit selective delegation with bounded extra risk and retained safe completion?',
            'primary_comparison': {'reference': 'B1_flash', 'candidate': 'M_jev',
                'note': 'One proposed confirmatory comparison only; not evidence that M_jev is the best candidate'},
            'same_state_diagnostics': {'selectors': ['jev', 'small', 'strong'], 'executors': ['small', 'strong'],
                'status': 'exploratory_and_separate_from_closed_loop_primary_claims'},
            'decision_strata': ['domain', 'consequence_severity', 'information_sufficiency',
                                'evidence_conflict', 'candidate_count', 'reference_length', 'tool_failure_mode'],
            'selection_rule': 'Develop on previously observed tasks, calibrate on separate families, freeze one rule before held-out inference',
            'old_records_role': 'development_and_evaluator_qualification_only_never_new_held_out',
            'new_sampling_frame': None, 'new_split_manifest': None, 'decision_contracts_independently_approved': False,
            'thresholds_approved': False, 'power_target_approved': False, 'budget_approved': False,
            'mandatory_controls': ['same_information_and_tool_access', 'same_executor_for_selector_ablation',
                                   'always_delegate_and_never_delegate', 'gate_disabled_ablation',
                                   'complete_retry_and_fallback_cost', 'separate_model_vs_system_safety'],
            'case_count_is_not_family_count': True, 'available_old_native_families': len({r['family'] for r in native_rows}),
            'threshold_profile': profile['primary_inference'], 'feasibility': sample_plan(profile, 20),
            'power_sensitivity': simulation(profile), 'historical_per_trajectory_costs': costs,
            'budget_sensitivity': budget_rows,
            'budget_scope': 'September 30 observed call costs only, not current prices, not a spending commitment; no inference charges are incurred by this calculation',
            'source_audit_sha256': audit_hash, 'paid_execution_authorized': False}
