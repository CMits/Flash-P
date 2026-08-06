import json

# Read flashp results
with open('../../networks/Grain_Volume_Sorghum/validation/script_validation_results.json') as f:
    flashp = json.load(f)
print('FLASHP: accuracy=%.3f, kappa=%.3f, mcc=%.3f, correct=%d/%d' % (
    flashp['metrics']['overall_accuracy'],
    flashp['metrics']['cohens_kappa'],
    flashp['metrics']['mcc'],
    flashp['metrics']['correct'],
    flashp['metrics']['total_tested']
))

# Read ODE results
with open('../../networks/Grain_Volume_Sorghum/validation/ode_validation_results.json') as f:
    ode = json.load(f)
print('ODE: accuracy=%.3f, kappa=%.3f, mcc=%.3f, correct=%d/%d, K=%s, n=%s' % (
    ode['metrics']['overall_accuracy'],
    ode['metrics']['cohens_kappa'],
    ode['metrics']['mcc'],
    ode['metrics']['correct'],
    ode['metrics']['total_tested'],
    ode.get('best_K', 'N/A'),
    ode.get('best_n', 'N/A')
))

# Read RWR results
with open('../../networks/Grain_Volume_Sorghum/validation/rwr_validation_results.json') as f:
    rwr = json.load(f)
print('RWR: accuracy=%.3f, kappa=%.3f, mcc=%.3f, correct=%d/%d, alpha=%s' % (
    rwr['metrics']['overall_accuracy'],
    rwr['metrics']['cohens_kappa'],
    rwr['metrics']['mcc'],
    rwr['metrics']['correct'],
    rwr['metrics']['total_tested'],
    rwr.get('best_alpha', 'N/A')
))
