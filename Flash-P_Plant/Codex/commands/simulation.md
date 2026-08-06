# Codex Recipe: simulation

Use when the user asks for `/simulation`, ad hoc FLASH-P simulation, gene knockout/knockdown/overexpression, environmental baseline changes, treatments, or listing/searching nodes in an existing network.

## Script

```bash
python Agent/shared/simulate_custom.py <network_dir> [simulation flags]
```

## Examples

```bash
python Agent/shared/simulate_custom.py networks/Days_To_Flowering --method all --ko SBPHYB --condition Long_Day=1
python Agent/shared/simulate_custom.py networks/Days_To_Flowering --method ode --oe SBCO
python Agent/shared/simulate_custom.py networks/Days_To_Flowering --method all --baseline-condition Normal_Temperature=1 --perturbed-condition High_Temperature=1
python Agent/shared/simulate_custom.py networks/Days_To_Flowering --find temp high warm heat
python Agent/shared/simulate_custom.py networks/Days_To_Flowering --list-nodes
```

## Semantics

- `--condition NODE=VALUE` applies to both baseline and perturbed runs.
- `--baseline-condition NODE=VALUE` applies only to the baseline.
- `--perturbed-condition NODE=VALUE` applies only to the perturbed run.
- `--treatment NODE=VALUE` applies only to the perturbed run.
- `--baseline-ko`, `--baseline-kd`, and `--baseline-oe` define a custom baseline genotype.
- `--method all` runs algebraic, ODE, and RWR.

Do not rebuild or edit the network. If a gene/condition node is missing, run `--find` and explain that FLASH-P can only simulate nodes present in the network.

