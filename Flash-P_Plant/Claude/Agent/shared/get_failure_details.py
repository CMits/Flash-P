import json

# Get detailed failure info for ODE failures
with open('../../networks/Grain_Volume_Sorghum/validation/ode_validation_results.json') as f:
    ode_data = json.load(f)

print("=== ODE FAILURES ===")
for t in ode_data['detailed_results']:
    if not t['correct']:
        print('Test %s: %s -> expected %s, predicted %s' % (
            t['test_id'], t['gene'], t['expected_direction'], t['predicted_direction']))
        print('  Modifier: %.2f, Ratio: %.4f' % (t['gene_modifier'], t['ratio']))

# Get detailed failure info for RWR failures
with open('../../networks/Grain_Volume_Sorghum/validation/rwr_validation_results.json') as f:
    rwr_data = json.load(f)

print("\n=== RWR FAILURES ===")
for t in rwr_data['detailed_results']:
    if not t['correct']:
        print('Test %s: %s -> expected %s, predicted %s' % (
            t['test_id'], t['gene'], t['expected_direction'], t['predicted_direction']))
        print('  Modifier: %.2f' % t['gene_modifier'])
