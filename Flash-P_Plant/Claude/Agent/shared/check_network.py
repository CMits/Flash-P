import json

# Check network for these genes
with open('../../networks/Grain_Volume_Sorghum/network/network.json') as f:
    net = json.load(f)

genes = ['SbTGW6', 'SbSPL16', 'SbStg1', 'Grain_Volume']

# Show first edge to understand structure
if net['edges']:
    print('Sample edge:', net['edges'][0])
    print('Edge keys:', list(net['edges'][0].keys()))

# Find nodes and edges
print("\n=== NODES ===")
for g in genes:
    nodes = [n for n in net['nodes'] if n.get('id') == g]
    if nodes:
        print('Node %s: type=%s' % (g, nodes[0].get('ty')))
    else:
        print('Node %s: NOT FOUND' % g)

print("\n=== EDGES ===")
for e in net['edges']:
    s = e.get('s')
    t = e.get('t')
    if s in genes or t in genes:
        print('Edge: %s --[%s]--> %s' % (s, e.get('x', 'unknown'), t))
