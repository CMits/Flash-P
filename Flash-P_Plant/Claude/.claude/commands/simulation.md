---
description: Run an ad hoc FLASH-P simulation on an existing network.
argument-hint: <network> --method <ode|algebraic|rwr|all> --ko <gene> [--condition <node=value>]
model: opus
---

# FLASH-P custom simulation

Simulation request: **$ARGUMENTS**

You are running an ad hoc perturbation simulation on an existing FLASH-P network. This is NOT the
full `/run-flashp` build pipeline. Do not rebuild the network, do not edit network files, and do not
run the literature/build/refinement agents.

Use the local simulator wrapper:

```bash
python Agent/shared/simulate_custom.py <network_dir> [simulation flags]
```

## Supported examples

```bash
/simulation networks/Days_To_Flowering --method all --ko SBPHYB --condition Long_Day=1
/simulation Days_To_Flowering --method ode --oe SBCO
/simulation Days_To_Flowering --method all --ko SBPHYB --condition High_Temperature=1
/simulation Days_To_Flowering --method all --baseline-condition Normal_Temperature=1 --perturbed-condition High_Temperature=1
/simulation Days_To_Flowering --find temp high warm heat
/simulation Days_To_Flowering --list-nodes
```

## How to execute

1. Parse `$ARGUMENTS`.
2. Resolve the network directory:
   - If the first argument starts with `networks/` or `networks\`, use it as-is.
   - If the first argument is just a network name, use `networks/<name>`.
   - If the user gives an absolute path, use that absolute path.
3. If the request already uses flags like `--ko`, `--condition`, `--method`, `--find`, or
   `--list-nodes`, pass those flags directly to `simulate_custom.py`.
4. If the request is natural language, infer the closest command:
   - "knock out X", "X KO", "delete X" -> `--ko X`
   - "knock down X", "X KD" -> `--kd X`
   - "overexpress X", "X OE" -> `--oe X`
   - "under Y", "in Y", "with Y as condition" -> `--condition Y=1`
   - "compare Y to Z", "baseline Y, perturbed Z", "change environment from Y to Z" ->
     `--baseline-condition Y=1 --perturbed-condition Z=1`
   - If no method is specified, use `--method all`.
5. Run the command from the project root (`.`), which should be the `Flash-P_Plant/Claude` folder.
6. Report the predicted direction, phenotype value/signal, fold change or RWR delta, convergence,
   and the top changed nodes. Keep the answer concise.

## Missing condition or gene nodes

If the command fails with "Node not found", do not invent a node. Immediately run a focused search:

```bash
python Agent/shared/simulate_custom.py <network_dir> --find <important search terms>
```

Then explain clearly:

- the requested node is not present in this network;
- FLASH-P can only simulate perturbations/conditions that exist as nodes;
- to simulate a missing condition such as `High_Temperature`, the network needs that environment
  node and causal edges added first.

## Semantics

- `--condition NODE=VALUE` is a background exogenous/environment condition applied to BOTH baseline
  and perturbed runs. Example: `--ko SBPHYB --condition Long_Day=1` means "SBPHYB knockout under
  Long_Day, compared against Long_Day alone."
- `--baseline-condition NODE=VALUE` is applied only to the baseline run.
- `--perturbed-condition NODE=VALUE` is applied only to the perturbed run.
- `--treatment NODE=VALUE` is applied only to the perturbed run.
- `--baseline-ko`, `--baseline-kd`, and `--baseline-oe` define a custom baseline genotype.
- `--method all` runs Algebraic, ODE, and RWR.

Begin by running the appropriate `simulate_custom.py` command now.
