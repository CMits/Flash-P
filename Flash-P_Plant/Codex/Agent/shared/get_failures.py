import json

# Get failures from each method
with open('../../networks/Grain_Volume_Sorghum/validation/script_validation_results.json') as f:
    flashp_data = json.load(f)
    flashp_failures = [t['test_id'] for t in flashp_data['detailed_results'] if not t['correct']]
    print('FLASHP failures (%d):' % len(flashp_failures), flashp_failures[:10], '...' if len(flashp_failures) > 10 else '')

with open('../../networks/Grain_Volume_Sorghum/validation/ode_validation_results.json') as f:
    ode_data = json.load(f)
    ode_failures = [t['test_id'] for t in ode_data['detailed_results'] if not t['correct']]
    print('ODE failures (%d):' % len(ode_failures), ode_failures)

with open('../../networks/Grain_Volume_Sorghum/validation/rwr_validation_results.json') as f:
    rwr_data = json.load(f)
    rwr_failures = [t['test_id'] for t in rwr_data['detailed_results'] if not t['correct']]
    print('RWR failures (%d):' % len(rwr_failures), rwr_failures)
