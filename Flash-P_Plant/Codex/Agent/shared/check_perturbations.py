import json
with open('../../networks/Grain_Volume_Sorghum/data/reconciled_perturbation_dataset.json') as f:
    data = json.load(f)
# Find tests T045, T062, T092
for p in data['perturbations']:
    if p['id'] in ['T045', 'T062', 'T092']:
        print('Test %s: gene=%s, node=%s, modifier=%s, expected_direction=%s' % (
            p['id'], p['g'], p.get('ng', 'NOT SET'), p.get('m'), p.get('ed')))
