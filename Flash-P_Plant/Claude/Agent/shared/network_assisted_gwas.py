#!/usr/bin/env python3
"""
Network-assisted GWAS prioritization for FLASH-P trait networks.

This is an empirical-Bayes/rank-fusion prototype:
1. Harmonize MLM, BLINK, and XGBoost evidence across studies.
2. Build a trait-specific prior from a FLASH-P network.
3. Map SNPs to nearby genes with the GRN SNP-near-gene bridge.
4. Reweight GWAS evidence by the network prior and optional eQTL support.

The posterior is a transparent ranking proxy, not a formal fine-mapping PIP.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import networkx as nx
import numpy as np
import pandas as pd


DEFAULT_GWAS_ROOT = Path(
    r"C:\GWAS_Pipeline_Sorghum_MultiAgents\New_format_re_run\ldpruned_gwas"
)
DEFAULT_FLASH_ROOT = Path(r"C:\Network\FlashP\Flash-P_Plant\Claude")
DEFAULT_SNP_GENE = Path(r"C:\GRN\derived\edges_snp_near_gene.parquet")
DEFAULT_EQTL_TOP1 = Path(r"C:\GRN\derived\edges_snp_eqtl_top1.parquet")
DEFAULT_GENE_DESCRIPTIONS = Path(
    r"C:\GWAS_Pipeline_Sorghum_MultiAgents\PLAZA5_gene_description.sbi.csv"
)

MODEL_WEIGHTS = {
    "mlm": 1.0,
    "blink": 1.0,
    "xgboost": 1.0,
    "xgboost_full": 1.0,
}

# High-confidence or transparent proxy mappings from FLASH-P DTF nodes to Sobic genes.
# Confidence scales the network prior before SNP projection.
DEFAULT_NODE_GENE_MAP = [
    {
        "node": "SBPHYB",
        "gene": "Sobic.001G394400",
        "confidence": 1.00,
        "map_type": "exact_symbol",
        "basis": "Sbi_ID_mapping symbol PHYB",
    },
    {
        "node": "SBPHYC",
        "gene": "Sobic.001G087100",
        "confidence": 1.00,
        "map_type": "exact_symbol",
        "basis": "Sbi_ID_mapping symbol PHYC",
    },
    {
        "node": "SBPRR37",
        "gene": "Sobic.006G057866",
        "confidence": 1.00,
        "map_type": "exact_symbol",
        "basis": "Sbi_ID_mapping symbol PRR37/Ma1",
    },
    {
        "node": "SBGI",
        "gene": "Sobic.003G040900",
        "confidence": 0.95,
        "map_type": "description_exact",
        "basis": "PLAZA description Protein GIGANTEA",
    },
    {
        "node": "SBELF3",
        "gene": "Sobic.009G257300",
        "confidence": 0.95,
        "map_type": "description_exact",
        "basis": "PLAZA description ELF3 protein",
    },
    {
        "node": "SBCO",
        "gene": "Sobic.003G347680",
        "confidence": 0.80,
        "map_type": "description_proxy",
        "basis": "PLAZA description Zinc finger CONSTANS-like protein",
    },
    {
        "node": "DELLA",
        "gene": "Sobic.001G120900",
        "confidence": 0.75,
        "map_type": "description_proxy",
        "basis": "PLAZA description DELLA protein DWARF8",
    },
    {
        "node": "SBEHD1",
        "gene": "Sobic.001G227900",
        "confidence": 0.55,
        "map_type": "family_proxy",
        "basis": "PLAZA description B-type response regulator; EHD1-like proxy",
    },
    {
        "node": "SBCN8",
        "gene": "Sobic.010G045100",
        "confidence": 0.65,
        "map_type": "family_proxy",
        "basis": "PLAZA description Flowering locus T; FT/SbCN florigen proxy",
    },
    {
        "node": "SBID1",
        "gene": "Sobic.001G036800",
        "confidence": 0.45,
        "map_type": "family_proxy",
        "basis": "PLAZA description Indeterminate spikelet 1; ID-like proxy",
    },
    {
        "node": "SBLFY",
        "gene": "Sobic.006G201600",
        "confidence": 0.45,
        "map_type": "family_proxy",
        "basis": "PLAZA description Floricaula/leafy-like 2",
    },
]

FAMILY_PROXY_PATTERNS = {
    "SBSPL": {
        "patterns": ["Squamosa promoter-binding-like"],
        "confidence": 0.25,
        "basis": "PLAZA description family proxy for SPL",
    },
    "SBAP2": {
        "patterns": ["AP2 domain"],
        "confidence": 0.20,
        "basis": "PLAZA description family proxy for AP2",
    },
    "SBAP1": {
        "patterns": ["MADS box", "MADS-box"],
        "confidence": 0.18,
        "basis": "PLAZA description family proxy for AP1/MADS",
    },
    "SBSOC1": {
        "patterns": ["MADS box", "MADS-box"],
        "confidence": 0.18,
        "basis": "PLAZA description family proxy for SOC1/MADS",
    },
    "SBFKF1": {
        "patterns": ["Kelch repeat-containing F-box"],
        "confidence": 0.18,
        "basis": "PLAZA description family proxy for FKF1/F-box",
    },
}


@dataclass
class ModelFile:
    study: str
    model: str
    result_file: Path
    aux_file: Path | None = None


def resolve_path(path: str | Path, base: Path | None = None) -> Path:
    p = Path(path)
    if not p.is_absolute() and base is not None:
        p = base / p
    return p


def canonical_trait_tokens(trait: str) -> set[str]:
    t = trait.lower()
    compact = re.sub(r"[^a-z0-9]+", "", t)
    tokens = {t, compact}
    if compact in {"dtf", "daystoflowering", "daysflowering"}:
        tokens.update({"dtf", "days_to_flowering", "daystoflowering", "flowering"})
    return tokens


def path_has_trait(path: Path, trait: str) -> bool:
    lowered = str(path).lower()
    compact = re.sub(r"[^a-z0-9]+", "", lowered)
    return any(tok in lowered or tok in compact for tok in canonical_trait_tokens(trait))


def score_path(path: Path, trait: str) -> tuple[int, int, int, str]:
    parts = [p.lower() for p in path.parts]
    trait_tokens = canonical_trait_tokens(trait)
    exact_dir = int(any(p in trait_tokens for p in parts))
    name_hit = int(any(tok in path.name.lower() for tok in trait_tokens))
    shorter = -len(str(path))
    return (exact_dir, name_hit, shorter, str(path))


def choose_one(paths: list[Path], trait: str) -> Path | None:
    if not paths:
        return None
    return sorted(paths, key=lambda p: score_path(p, trait), reverse=True)[0]


def discover_model_files(
    gwas_root: Path,
    studies: list[str],
    trait: str,
    include_xgboost_full: bool = False,
) -> list[ModelFile]:
    files: list[ModelFile] = []
    for study in studies:
        study_dir = gwas_root / study
        if not study_dir.exists():
            raise FileNotFoundError(f"Study directory not found: {study_dir}")

        all_files = [p for p in study_dir.rglob("*") if p.is_file() and path_has_trait(p, trait)]

        mlm = [
            p
            for p in all_files
            if "\\mlm\\" in str(p).lower()
            and p.name.lower().endswith(".assoc.txt")
            and "output" in [x.lower() for x in p.parts]
        ]
        blink = [
            p
            for p in all_files
            if "\\blink\\" in str(p).lower()
            and p.name.lower().endswith("_blink_results.txt")
        ]
        xgb_root_token = "\\xgboost_full\\" if include_xgboost_full else "\\xgboost\\"
        xgb_model = "xgboost_full" if include_xgboost_full else "xgboost"
        xgb = [
            p
            for p in all_files
            if xgb_root_token in str(p).lower()
            and p.name.lower().endswith("_xgb_pc3_kin_results.txt")
        ]
        xgb_aux = [
            p
            for p in all_files
            if xgb_root_token in str(p).lower()
            and p.name.lower().endswith("_xgb_pc3_kin_permapprox.tsv")
        ]

        chosen_mlm = choose_one(mlm, trait)
        chosen_blink = choose_one(blink, trait)
        chosen_xgb = choose_one(xgb, trait)
        chosen_xgb_aux = choose_one(xgb_aux, trait)

        if chosen_mlm:
            files.append(ModelFile(study, "mlm", chosen_mlm))
        if chosen_blink:
            files.append(ModelFile(study, "blink", chosen_blink))
        if chosen_xgb:
            files.append(ModelFile(study, xgb_model, chosen_xgb, chosen_xgb_aux))
    return files


def read_header(path: Path) -> list[str]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        return f.readline().strip().split("\t")


def read_model_scores(model_file: ModelFile, max_logp: float) -> pd.DataFrame:
    if model_file.model == "mlm":
        header = read_header(model_file.result_file)
        p_col = "p_wald" if "p_wald" in header else "p_score" if "p_score" in header else "p_lrt"
        usecols = ["chr", "rs", "ps", p_col]
        if "beta" in header:
            usecols.append("beta")
        if "se" in header:
            usecols.append("se")
        df = pd.read_csv(model_file.result_file, sep="\t", usecols=usecols)
        df = df.rename(columns={"rs": "snp", "ps": "pos", p_col: "p_value"})
        df["rank_metric"] = neglog10(df["p_value"], max_logp)

    elif model_file.model == "blink":
        usecols = ["chr", "snp", "pos", "p_value"]
        df = pd.read_csv(model_file.result_file, sep="\t", usecols=usecols)
        df["rank_metric"] = neglog10(df["p_value"], max_logp)

    elif model_file.model in {"xgboost", "xgboost_full"}:
        usecols = ["chr", "snp", "pos", "importance", "rank", "perm_p"]
        df = pd.read_csv(model_file.result_file, sep="\t", usecols=usecols, na_values=["NA", "NaN"])
        rank = pd.to_numeric(df["rank"], errors="coerce")
        n = max(float(rank.max(skipna=True) or len(df)), 1.0)
        empirical_p = (rank.clip(lower=1) / (n + 1.0)).clip(lower=1e-300, upper=1.0)
        p_value = pd.to_numeric(df["perm_p"], errors="coerce")

        if model_file.aux_file and model_file.aux_file.exists():
            aux = pd.read_csv(model_file.aux_file, sep="\t", na_values=["NA", "NaN"])
            aux_cols = [c for c in ["snp", "p_hybrid", "p_gpd", "p_emp"] if c in aux.columns]
            aux = aux[aux_cols].copy()
            aux_p = None
            for c in ["p_hybrid", "p_gpd", "p_emp"]:
                if c in aux.columns:
                    vals = pd.to_numeric(aux[c], errors="coerce")
                    aux_p = vals if aux_p is None else aux_p.combine_first(vals)
            if aux_p is not None:
                aux["xgb_aux_p"] = aux_p
                df = df.merge(aux[["snp", "xgb_aux_p"]], on="snp", how="left")
                p_value = pd.to_numeric(df["xgb_aux_p"], errors="coerce").combine_first(p_value)

        p_value = p_value.combine_first(empirical_p)
        df["p_value"] = p_value
        df["rank_metric"] = np.maximum(neglog10(df["p_value"], max_logp), neglog10(empirical_p, max_logp))
    else:
        raise ValueError(f"Unsupported model: {model_file.model}")

    df["snp"] = df["snp"].astype(str)
    df["chr"] = pd.to_numeric(df["chr"], errors="coerce").astype("Int64")
    df["pos"] = pd.to_numeric(df["pos"], errors="coerce").astype("Int64")
    df["p_value"] = pd.to_numeric(df["p_value"], errors="coerce")
    df["rank_metric"] = pd.to_numeric(df["rank_metric"], errors="coerce").fillna(0.0)
    df["model_score"] = df["rank_metric"].rank(method="average", pct=True).astype("float32")
    df["neglog10p"] = neglog10(df["p_value"], max_logp).astype("float32")
    df["study"] = model_file.study
    df["model"] = model_file.model
    return df[["snp", "chr", "pos", "p_value", "neglog10p", "rank_metric", "model_score", "study", "model"]]


def neglog10(values: pd.Series | np.ndarray, max_logp: float) -> pd.Series:
    vals = pd.to_numeric(values, errors="coerce")
    vals = vals.clip(lower=10 ** (-max_logp), upper=1.0)
    out = -np.log10(vals)
    return pd.Series(out).clip(lower=0.0, upper=max_logp)


def update_aggregate(agg: pd.DataFrame | None, scores: pd.DataFrame, model_weight: float) -> pd.DataFrame:
    tmp = scores.set_index("snp")
    if agg is None:
        agg = tmp[["chr", "pos"]].copy()
        agg["score_sum"] = 0.0
        agg["weight_sum"] = 0.0
        agg["max_model_score"] = 0.0
        agg["max_neglog10p"] = 0.0
        agg["min_p_value"] = np.nan
        agg["model_support_99"] = 0
        agg["best_model"] = ""
        agg["best_study"] = ""
    else:
        missing = tmp.index.difference(agg.index)
        if len(missing):
            add = tmp.loc[missing, ["chr", "pos"]].copy()
            add["score_sum"] = 0.0
            add["weight_sum"] = 0.0
            add["max_model_score"] = 0.0
            add["max_neglog10p"] = 0.0
            add["min_p_value"] = np.nan
            add["model_support_99"] = 0
            add["best_model"] = ""
            add["best_study"] = ""
            agg = pd.concat([agg, add], axis=0)

    idx = tmp.index
    score = tmp["model_score"].astype(float)
    neglogp = tmp["neglog10p"].astype(float)
    p_value = tmp["p_value"].astype(float)
    agg.loc[idx, "score_sum"] = agg.loc[idx, "score_sum"].astype(float) + score * model_weight
    agg.loc[idx, "weight_sum"] = agg.loc[idx, "weight_sum"].astype(float) + model_weight
    agg.loc[idx, "max_neglog10p"] = np.maximum(agg.loc[idx, "max_neglog10p"].astype(float), neglogp)

    existing_min = agg.loc[idx, "min_p_value"].astype(float)
    agg.loc[idx, "min_p_value"] = np.fmin(existing_min.fillna(np.inf), p_value.fillna(np.inf)).replace(np.inf, np.nan)

    hit = score >= 0.99
    agg.loc[idx, "model_support_99"] = agg.loc[idx, "model_support_99"].astype(int) + hit.astype(int)

    study_col = f"study_hit_99__{tmp['study'].iloc[0]}"
    if study_col not in agg.columns:
        agg[study_col] = False
    agg.loc[idx, study_col] = agg.loc[idx, study_col].astype(bool) | hit

    better = score > agg.loc[idx, "max_model_score"].astype(float)
    better_idx = idx[better.to_numpy()]
    if len(better_idx):
        agg.loc[better_idx, "max_model_score"] = score.loc[better_idx]
        agg.loc[better_idx, "best_model"] = tmp.loc[better_idx, "model"]
        agg.loc[better_idx, "best_study"] = tmp.loc[better_idx, "study"]

    return agg


def parse_network(path: Path) -> tuple[nx.DiGraph, dict[str, dict], list[str]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    nodes_raw = data.get("nodes", [])
    edges_raw = data.get("edges", [])
    graph = nx.DiGraph()
    node_meta: dict[str, dict] = {}

    for node in nodes_raw:
        node_id = node.get("id") or node.get("n")
        if not node_id:
            continue
        node_type = node.get("ty") or node.get("type") or ""
        node_meta[node_id] = node
        graph.add_node(node_id, node_type=node_type)

    for edge in edges_raw:
        source = edge.get("s") or edge.get("source")
        target = edge.get("t") or edge.get("target")
        if source and target:
            sign = edge.get("x", edge.get("sign", 1))
            graph.add_edge(source, target, sign=sign)

    phenotype_nodes = [
        n
        for n, meta in node_meta.items()
        if (meta.get("ty") or meta.get("type") or "").upper() in {"P", "PHENOTYPE"}
    ]
    return graph, node_meta, phenotype_nodes


def compute_node_scores(network_path: Path, phenotype_node: str) -> pd.DataFrame:
    graph, node_meta, phenotype_nodes = parse_network(network_path)
    if phenotype_node not in graph:
        raise ValueError(f"Phenotype node {phenotype_node!r} is not in {network_path}")

    reverse_graph = graph.reverse(copy=True)
    distances = nx.single_source_shortest_path_length(reverse_graph, phenotype_node)

    personalization = {n: 0.0 for n in graph.nodes}
    personalization[phenotype_node] = 1.0
    try:
        rwr = nx.pagerank(reverse_graph, alpha=0.65, personalization=personalization, max_iter=200)
    except nx.PowerIterationFailedConvergence:
        rwr = {n: 0.0 for n in graph.nodes}
        rwr[phenotype_node] = 1.0
    max_rwr = max(rwr.values()) if rwr else 1.0

    rows = []
    phenotype_set = set(phenotype_nodes)
    for node in graph.nodes:
        dist = distances.get(node)
        dist_score = math.exp(-0.75 * dist) if dist is not None else 0.0
        rwr_score = (rwr.get(node, 0.0) / max_rwr) if max_rwr else 0.0
        reachable_phenotypes = 0
        for pheno in phenotype_set:
            if node == pheno:
                reachable_phenotypes += 1
            else:
                try:
                    if nx.has_path(graph, node, pheno):
                        reachable_phenotypes += 1
                except nx.NetworkXError:
                    pass
        pleiotropy_score = math.log1p(reachable_phenotypes) / math.log1p(max(len(phenotype_set), 1))
        rows.append(
            {
                "node": node,
                "node_type": node_meta.get(node, {}).get("ty") or node_meta.get(node, {}).get("type") or "",
                "distance_to_trait": dist if dist is not None else np.nan,
                "distance_score": dist_score,
                "rwr_score": rwr_score,
                "pleiotropy_count": reachable_phenotypes,
                "pleiotropy_score": pleiotropy_score,
                "node_score": 0.60 * rwr_score + 0.35 * dist_score + 0.05 * pleiotropy_score,
            }
        )
    return pd.DataFrame(rows)


def read_gene_description_matches(path: Path, node: str, config: dict) -> list[dict]:
    if not path.exists():
        return []
    patterns = [p.lower() for p in config["patterns"]]
    rows: list[dict] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if line.startswith("#") or not line.strip():
                continue
            parts = line.rstrip("\n").split("\t", 2)
            if len(parts) < 3:
                continue
            gene, _, desc = parts
            desc_lower = desc.lower()
            if any(pattern in desc_lower for pattern in patterns):
                rows.append(
                    {
                        "node": node,
                        "gene": gene,
                        "confidence": config["confidence"],
                        "map_type": "family_description_proxy",
                        "basis": config["basis"],
                    }
                )
    return rows


def build_gene_priors(
    network_path: Path,
    merged_network_path: Path | None,
    phenotype_node: str,
    gene_description_path: Path,
    include_family_proxies: bool,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    single_scores = compute_node_scores(network_path, phenotype_node).rename(
        columns={
            "distance_to_trait": "single_distance_to_trait",
            "distance_score": "single_distance_score",
            "rwr_score": "single_rwr_score",
            "pleiotropy_count": "single_pleiotropy_count",
            "pleiotropy_score": "single_pleiotropy_score",
            "node_score": "single_node_score",
        }
    )

    if merged_network_path and merged_network_path.exists():
        merged_scores = compute_node_scores(merged_network_path, phenotype_node).rename(
            columns={
                "distance_to_trait": "merged_distance_to_trait",
                "distance_score": "merged_distance_score",
                "rwr_score": "merged_rwr_score",
                "pleiotropy_count": "merged_pleiotropy_count",
                "pleiotropy_score": "merged_pleiotropy_score",
                "node_score": "merged_node_score",
            }
        )
        node_scores = single_scores.merge(
            merged_scores[
                [
                    "node",
                    "merged_distance_to_trait",
                    "merged_distance_score",
                    "merged_rwr_score",
                    "merged_pleiotropy_count",
                    "merged_pleiotropy_score",
                    "merged_node_score",
                ]
            ],
            on="node",
            how="outer",
        )
    else:
        node_scores = single_scores.copy()
        node_scores["merged_node_score"] = 0.0
        node_scores["merged_pleiotropy_score"] = 0.0
        node_scores["merged_pleiotropy_count"] = 0

    node_scores[["single_node_score", "merged_node_score", "merged_pleiotropy_score"]] = node_scores[
        ["single_node_score", "merged_node_score", "merged_pleiotropy_score"]
    ].fillna(0.0)
    node_scores["trait_network_score"] = (
        0.70 * node_scores["single_node_score"]
        + 0.20 * node_scores["merged_node_score"]
        + 0.10 * node_scores["merged_pleiotropy_score"]
    )

    mappings = list(DEFAULT_NODE_GENE_MAP)
    if include_family_proxies:
        present_nodes = set(node_scores["node"].astype(str))
        for node, config in FAMILY_PROXY_PATTERNS.items():
            if node in present_nodes:
                mappings.extend(read_gene_description_matches(gene_description_path, node, config))

    mapping_df = pd.DataFrame(mappings)
    mapping_df = mapping_df.merge(
        node_scores[
            [
                "node",
                "node_type",
                "single_distance_to_trait",
                "single_node_score",
                "merged_node_score",
                "merged_pleiotropy_count",
                "trait_network_score",
            ]
        ],
        on="node",
        how="left",
    )
    mapping_df["trait_network_score"] = mapping_df["trait_network_score"].fillna(0.0)
    mapping_df["gene_prior_raw"] = mapping_df["trait_network_score"] * mapping_df["confidence"]

    gene_priors = (
        mapping_df.sort_values(["gene_prior_raw", "confidence"], ascending=False)
        .drop_duplicates("gene")
        .copy()
    )
    max_prior = gene_priors["gene_prior_raw"].max()
    if pd.notna(max_prior) and max_prior > 0:
        gene_priors["network_gene_prior"] = gene_priors["gene_prior_raw"] / max_prior
    else:
        gene_priors["network_gene_prior"] = 0.0
    return gene_priors, mapping_df


def project_network_prior_to_snps(
    agg: pd.DataFrame,
    gene_priors: pd.DataFrame,
    snp_gene_path: Path,
    eqtl_top1_path: Path | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    prior_genes = set(gene_priors.loc[gene_priors["network_gene_prior"] > 0, "gene"].astype(str))
    if not prior_genes:
        agg["network_prior"] = 0.0
        agg["network_gene"] = ""
        agg["network_gene_distance_kb"] = np.nan
        agg["network_gene_dist_kernel"] = 0.0
        agg["eqtl_top1_log10p"] = 0.0
        agg["eqtl_coloc_like_score"] = 0.0
        return agg, pd.DataFrame()

    edges = pd.read_parquet(snp_gene_path, columns=["snp", "gene", "distance_kb", "dist_kernel"])
    edges = edges[edges["gene"].isin(prior_genes)].copy()
    if edges.empty:
        agg["network_prior"] = 0.0
        agg["network_gene"] = ""
        agg["network_gene_distance_kb"] = np.nan
        agg["network_gene_dist_kernel"] = 0.0
        agg["eqtl_top1_log10p"] = 0.0
        agg["eqtl_coloc_like_score"] = 0.0
        return agg, edges

    edges = edges.merge(
        gene_priors[
            [
                "gene",
                "node",
                "confidence",
                "map_type",
                "basis",
                "network_gene_prior",
                "gene_prior_raw",
            ]
        ],
        on="gene",
        how="left",
    )
    edges["edge_network_prior"] = edges["network_gene_prior"].fillna(0.0) * edges["dist_kernel"].fillna(0.0)

    if eqtl_top1_path and eqtl_top1_path.exists():
        eqtl = pd.read_parquet(eqtl_top1_path, columns=["gene", "snp", "log10_p"])
        eqtl = eqtl.rename(columns={"log10_p": "eqtl_top1_log10p"})
        edges = edges.merge(eqtl, on=["gene", "snp"], how="left")
    else:
        edges["eqtl_top1_log10p"] = np.nan

    edges["eqtl_top1_log10p"] = pd.to_numeric(edges["eqtl_top1_log10p"], errors="coerce").fillna(0.0)
    edges["edge_eqtl_coloc_like"] = edges["edge_network_prior"] * np.minimum(
        1.0, edges["eqtl_top1_log10p"] / 20.0
    )

    best_edges = (
        edges.sort_values(["edge_network_prior", "edge_eqtl_coloc_like"], ascending=False)
        .drop_duplicates("snp")
        .set_index("snp")
    )
    agg = agg.join(
        best_edges[
            [
                "gene",
                "node",
                "distance_kb",
                "dist_kernel",
                "edge_network_prior",
                "eqtl_top1_log10p",
                "edge_eqtl_coloc_like",
                "map_type",
                "basis",
            ]
        ],
        how="left",
    )
    agg = agg.rename(
        columns={
            "gene": "network_gene",
            "node": "network_node",
            "distance_kb": "network_gene_distance_kb",
            "dist_kernel": "network_gene_dist_kernel",
            "edge_network_prior": "network_prior",
            "edge_eqtl_coloc_like": "eqtl_coloc_like_score",
            "map_type": "network_map_type",
            "basis": "network_map_basis",
        }
    )
    agg["network_prior"] = agg["network_prior"].fillna(0.0)
    agg["network_gene"] = agg["network_gene"].fillna("")
    agg["network_node"] = agg["network_node"].fillna("")
    agg["network_gene_dist_kernel"] = agg["network_gene_dist_kernel"].fillna(0.0)
    agg["eqtl_top1_log10p"] = agg["eqtl_top1_log10p"].fillna(0.0)
    agg["eqtl_coloc_like_score"] = agg["eqtl_coloc_like_score"].fillna(0.0)
    agg["network_map_type"] = agg["network_map_type"].fillna("")
    agg["network_map_basis"] = agg["network_map_basis"].fillna("")
    return agg, edges


def finalize_scores(
    agg: pd.DataFrame,
    studies: list[str],
    prior_boost: float,
    eqtl_boost: float,
    base_prior: float,
    likelihood_scale_log10: float,
    max_logp: float,
) -> pd.DataFrame:
    agg["gwas_mean_model_score"] = agg["score_sum"] / agg["weight_sum"].replace(0, np.nan)
    agg["gwas_mean_model_score"] = agg["gwas_mean_model_score"].fillna(0.0)

    study_hit_cols = [c for c in agg.columns if c.startswith("study_hit_99__")]
    if study_hit_cols:
        agg["study_support_99"] = agg[study_hit_cols].sum(axis=1).astype(int)
    else:
        agg["study_support_99"] = 0

    denom = max(len(studies), 1)
    agg["gwas_consensus_score"] = (
        0.65 * agg["gwas_mean_model_score"].astype(float)
        + 0.25 * agg["max_model_score"].astype(float)
        + 0.10 * (agg["study_support_99"].astype(float) / denom)
    )
    agg["gwas_rank"] = agg["gwas_consensus_score"].rank(method="first", ascending=False).astype(int)
    agg["gwas_percentile"] = agg["gwas_consensus_score"].rank(method="average", pct=True)

    prior_factor = 1.0 + prior_boost * agg["network_prior"].astype(float)
    prior_factor = prior_factor + eqtl_boost * agg["eqtl_coloc_like_score"].astype(float)
    prior_factor = prior_factor.clip(lower=1e-12)
    agg["log10_prior_proxy"] = math.log10(base_prior) + np.log10(prior_factor)
    p_strength = (agg["max_neglog10p"].astype(float) / max(max_logp, 1e-9)).clip(lower=0.0, upper=1.0)
    agg["gwas_likelihood_component"] = 0.65 * agg["gwas_percentile"].astype(float) + 0.35 * p_strength
    agg["log10_likelihood_proxy"] = likelihood_scale_log10 * agg["gwas_likelihood_component"]
    agg["log10_posterior_weight"] = agg["log10_prior_proxy"] + agg["log10_likelihood_proxy"]

    max_logw = agg["log10_posterior_weight"].max()
    weights = np.power(10.0, agg["log10_posterior_weight"] - max_logw)
    denom_w = weights.sum()
    agg["posterior_prob_proxy"] = weights / denom_w if denom_w > 0 else 0.0
    agg["network_boosted_score"] = agg["log10_posterior_weight"].rank(method="average", pct=True)
    agg["network_boosted_rank"] = agg["network_boosted_score"].rank(method="first", ascending=False).astype(int)
    agg["rank_delta_vs_gwas"] = agg["gwas_rank"] - agg["network_boosted_rank"]
    return agg


def build_locus_summary(snps: pd.DataFrame, locus_kb: int, top_n: int) -> pd.DataFrame:
    work = snps.copy()
    work["locus_bin"] = (pd.to_numeric(work["pos"], errors="coerce") // (locus_kb * 1000)).astype("Int64")
    work["locus_id"] = work["chr"].astype(str) + ":" + work["locus_bin"].astype(str)
    cols = [
        "locus_id",
        "snp",
        "chr",
        "pos",
        "network_boosted_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "network_boosted_score",
        "gwas_consensus_score",
        "network_prior",
        "network_gene",
        "network_node",
        "network_gene_distance_kb",
        "eqtl_top1_log10p",
        "best_model",
        "best_study",
    ]
    return (
        work.sort_values("network_boosted_rank")
        .drop_duplicates("locus_id")
        .loc[:, cols]
        .head(top_n)
    )


def build_gene_summary(snps: pd.DataFrame, top_n: int) -> pd.DataFrame:
    work = snps[snps["network_gene"].astype(str) != ""].copy()
    if work.empty:
        return pd.DataFrame()
    cols = [
        "network_gene",
        "network_node",
        "snp",
        "chr",
        "pos",
        "network_boosted_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "network_boosted_score",
        "gwas_consensus_score",
        "network_prior",
        "network_gene_distance_kb",
        "eqtl_top1_log10p",
        "eqtl_coloc_like_score",
        "best_model",
        "best_study",
        "network_map_type",
        "network_map_basis",
    ]
    return (
        work.sort_values(["network_gene", "network_boosted_rank"])
        .drop_duplicates("network_gene")
        .sort_values("network_boosted_rank")
        .loc[:, cols]
        .head(top_n)
    )


def write_summary(
    out_dir: Path,
    studies: list[str],
    trait: str,
    model_files: list[ModelFile],
    snps: pd.DataFrame,
    gene_priors: pd.DataFrame,
    top_snps: pd.DataFrame,
) -> None:
    summary = {
        "trait": trait,
        "studies": studies,
        "model_files": [
            {
                "study": m.study,
                "model": m.model,
                "result_file": str(m.result_file),
                "aux_file": str(m.aux_file) if m.aux_file else None,
            }
            for m in model_files
        ],
        "n_snps_scored": int(len(snps)),
        "n_network_prior_genes": int((gene_priors["network_gene_prior"] > 0).sum()),
        "n_snps_with_network_prior": int((snps["network_prior"] > 0).sum()),
        "top_snp": top_snps.iloc[0].to_dict() if not top_snps.empty else None,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    lines = [
        f"# Network-assisted GWAS summary: {trait}",
        "",
        f"- Studies: {', '.join(studies)}",
        f"- Model files discovered: {len(model_files)}",
        f"- SNPs scored: {len(snps):,}",
        f"- Network-prior genes: {(gene_priors['network_gene_prior'] > 0).sum():,}",
        f"- SNPs with nonzero network prior: {(snps['network_prior'] > 0).sum():,}",
        "",
        "## Top 20 Network-Boosted SNPs",
        "",
    ]
    show_cols = [
        "snp",
        "chr",
        "pos",
        "network_boosted_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "gwas_consensus_score",
        "network_prior",
        "network_gene",
        "network_node",
        "best_model",
        "best_study",
    ]
    lines.append(markdown_table(top_snps.head(20)[show_cols]))
    lines.append("")
    lines.append(
        "Interpretation note: `posterior_prob_proxy` and `network_boosted_rank` are empirical prioritization scores, not formal fine-mapping posterior inclusion probabilities."
    )
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows._"
    text = df.copy()
    for col in text.columns:
        if pd.api.types.is_float_dtype(text[col]):
            text[col] = text[col].map(lambda x: "" if pd.isna(x) else f"{x:.6g}")
        else:
            text[col] = text[col].map(lambda x: "" if pd.isna(x) else str(x))

    headers = list(text.columns)
    rows = text.values.tolist()
    lines = [
        "| " + " | ".join(headers) + " |",
        "| " + " | ".join(["---"] * len(headers)) + " |",
    ]
    for row in rows:
        escaped = [cell.replace("|", "\\|") for cell in row]
        lines.append("| " + " | ".join(escaped) + " |")
    return "\n".join(lines)


def run(args: argparse.Namespace) -> None:
    flash_root = resolve_path(args.flash_root)
    gwas_root = resolve_path(args.gwas_root)
    network_path = resolve_path(args.network, flash_root) / "network" / "network.json" if (resolve_path(args.network, flash_root) / "network").exists() else resolve_path(args.network, flash_root)
    merged_network_path = None
    if args.merged_network:
        merged_base = resolve_path(args.merged_network, flash_root)
        merged_network_path = merged_base / "network" / "network.json" if (merged_base / "network").exists() else merged_base

    out_dir = resolve_path(args.out, flash_root)
    out_dir.mkdir(parents=True, exist_ok=True)

    model_files = discover_model_files(
        gwas_root=gwas_root,
        studies=args.studies,
        trait=args.trait,
        include_xgboost_full=args.include_xgboost_full,
    )
    if not model_files:
        raise RuntimeError("No model files were discovered.")

    manifest = pd.DataFrame(
        [
            {
                "study": m.study,
                "model": m.model,
                "result_file": str(m.result_file),
                "aux_file": str(m.aux_file) if m.aux_file else "",
            }
            for m in model_files
        ]
    )
    manifest.to_csv(out_dir / "model_manifest.tsv", sep="\t", index=False)

    gene_priors, mapping_df = build_gene_priors(
        network_path=network_path,
        merged_network_path=merged_network_path,
        phenotype_node=args.phenotype_node,
        gene_description_path=resolve_path(args.gene_descriptions),
        include_family_proxies=not args.no_family_proxies,
    )
    gene_priors.to_csv(out_dir / "network_gene_priors.tsv", sep="\t", index=False)
    mapping_df.to_csv(out_dir / "node_gene_mapping_used.tsv", sep="\t", index=False)

    agg: pd.DataFrame | None = None
    for model_file in model_files:
        print(f"Reading {model_file.study} {model_file.model}: {model_file.result_file}")
        scores = read_model_scores(model_file, args.max_logp)
        if args.write_model_parquet:
            scores.to_parquet(out_dir / f"model_scores__{model_file.study}__{model_file.model}.parquet", index=False)
        agg = update_aggregate(agg, scores, MODEL_WEIGHTS.get(model_file.model, 1.0))
        del scores

    if agg is None or agg.empty:
        raise RuntimeError("No SNP scores could be aggregated.")

    agg, edge_prior_table = project_network_prior_to_snps(
        agg,
        gene_priors=gene_priors,
        snp_gene_path=resolve_path(args.snp_gene),
        eqtl_top1_path=resolve_path(args.eqtl_top1) if args.eqtl_top1 else None,
    )
    if not edge_prior_table.empty:
        edge_prior_table.to_parquet(out_dir / "network_prior_snp_gene_edges.parquet", index=False)

    agg = finalize_scores(
        agg,
        studies=args.studies,
        prior_boost=args.prior_boost,
        eqtl_boost=args.eqtl_boost,
        base_prior=args.base_prior,
        likelihood_scale_log10=args.likelihood_scale_log10,
        max_logp=args.max_logp,
    )

    # Put SNP back as a column and keep output order stable.
    snps = agg.reset_index().rename(columns={"index": "snp"})
    snps = snps.sort_values("network_boosted_rank")

    # Avoid writing transient boolean support columns into user-facing tables.
    study_hit_cols = [c for c in snps.columns if c.startswith("study_hit_99__")]
    full_cols = [c for c in snps.columns if c not in study_hit_cols]
    snps[full_cols].to_parquet(out_dir / "snp_scores.parquet", index=False)

    top_cols = [
        "snp",
        "chr",
        "pos",
        "network_boosted_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "posterior_prob_proxy",
        "network_boosted_score",
        "gwas_consensus_score",
        "gwas_likelihood_component",
        "gwas_mean_model_score",
        "max_model_score",
        "max_neglog10p",
        "min_p_value",
        "model_support_99",
        "study_support_99",
        "network_prior",
        "network_gene",
        "network_node",
        "network_gene_distance_kb",
        "network_gene_dist_kernel",
        "eqtl_top1_log10p",
        "eqtl_coloc_like_score",
        "best_model",
        "best_study",
        "network_map_type",
        "network_map_basis",
    ]
    top_snps = snps.loc[:, top_cols].head(args.top_n)
    top_snps.to_csv(out_dir / f"top_{args.top_n}_network_boosted_snps.tsv", sep="\t", index=False)

    locus_summary = build_locus_summary(snps, args.locus_kb, args.top_n)
    locus_summary.to_csv(out_dir / f"top_{args.top_n}_network_boosted_loci.tsv", sep="\t", index=False)

    gene_summary = build_gene_summary(snps, args.top_n)
    gene_summary.to_csv(out_dir / f"top_{args.top_n}_network_boosted_genes.tsv", sep="\t", index=False)

    write_summary(out_dir, args.studies, args.trait, model_files, snps, gene_priors, top_snps)
    print(f"Done. Outputs written to: {out_dir}")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Network-assisted GWAS prioritization using FLASH-P networks and GRN SNP-gene bridges."
    )
    parser.add_argument("--gwas-root", default=str(DEFAULT_GWAS_ROOT), help="Root containing study GWAS directories.")
    parser.add_argument("--studies", nargs="+", required=True, help="Study directories under --gwas-root.")
    parser.add_argument("--trait", default="DTF", help="Trait token used to discover result files.")
    parser.add_argument("--flash-root", default=str(DEFAULT_FLASH_ROOT), help="FLASH-P Claude workspace root.")
    parser.add_argument("--network", default="networks/Days_To_Flowering", help="Single-trait FLASH-P network dir or network.json.")
    parser.add_argument("--merged-network", default="networks/merged_sorghum_network", help="Merged FLASH-P network dir or network.json.")
    parser.add_argument("--phenotype-node", default="Days_To_Flowering", help="FLASH-P phenotype node to score toward.")
    parser.add_argument("--snp-gene", default=str(DEFAULT_SNP_GENE), help="Parquet with snp,gene,distance_kb,dist_kernel.")
    parser.add_argument("--eqtl-top1", default=str(DEFAULT_EQTL_TOP1), help="Optional top eQTL SNP-gene parquet.")
    parser.add_argument("--gene-descriptions", default=str(DEFAULT_GENE_DESCRIPTIONS), help="PLAZA gene description TSV.")
    parser.add_argument("--out", default="analysis/network_gwas_dtf", help="Output directory.")
    parser.add_argument("--top-n", type=int, default=5000, help="Number of top SNPs/loci/genes to write as TSV.")
    parser.add_argument("--locus-kb", type=int, default=100, help="Window bin size for quick locus summaries.")
    parser.add_argument("--max-logp", type=float, default=20.0, help="Clip -log10(p) to this value.")
    parser.add_argument("--prior-boost", type=float, default=20.0, help="Network prior multiplier in posterior proxy.")
    parser.add_argument("--eqtl-boost", type=float, default=20.0, help="eQTL coloc-like multiplier in posterior proxy.")
    parser.add_argument("--base-prior", type=float, default=1e-5, help="Baseline SNP prior used in posterior proxy.")
    parser.add_argument(
        "--likelihood-scale-log10",
        type=float,
        default=10.0,
        help="Maximum log10 likelihood proxy contributed by empirical GWAS percentile.",
    )
    parser.add_argument("--include-xgboost-full", action="store_true", help="Use xgboost_full instead of xgboost where present.")
    parser.add_argument("--no-family-proxies", action="store_true", help="Disable low-confidence family proxy gene mappings.")
    parser.add_argument("--write-model-parquet", action="store_true", help="Write per-study/model harmonized scores.")
    return parser.parse_args(argv)


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
