#!/usr/bin/env python3
"""
FLASH-P-supervised SNP-network GWAS discovery.

This pipeline intentionally does not use GRN-derived SNP-gene or eQTL tables.
FLASH-P perturbation genes are treated as supervised ground-truth labels, not
as Bayesian priors. SNPs are represented with GWAS evidence plus local genomic
SNP-network features, then trained/evaluated with grouped held-out FLASH-P
perturbation loci.
"""

from __future__ import annotations

import argparse
import gzip
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier
from sklearn.metrics import average_precision_score, roc_auc_score


DEFAULT_GWAS_ROOT = Path(
    r"C:\GWAS_Pipeline_Sorghum_MultiAgents\New_format_re_run\ldpruned_gwas"
)
DEFAULT_FLASH_ROOT = Path(r"C:\Network\FlashP\Flash-P_Plant\Claude")
DEFAULT_GFF = Path(r"C:\GWAS_Pipeline_Sorghum_MultiAgents\Sbicolor_454_v3.1.1.gene.gff3.gz")
DEFAULT_NETWORK = Path(r"C:\Network\FlashP\Flash-P_Plant\Claude\networks\Days_To_Flowering")
DEFAULT_STUDIES = ["SDPHER17", "SDPHER18", "SDPGAT19", "Hrr2502"]

MODEL_WEIGHTS = {
    "mlm": 1.0,
    "blink": 1.0,
    "xgboost": 1.0,
    "xgboost_full": 1.0,
}

# Direct or transparent high-confidence mappings from FLASH-P DTF perturbation
# nodes to Sobic genes. These are labels, so keep broad family expansion off by
# default and expose confidence filtering through the CLI.
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


def neglog10(values: pd.Series | np.ndarray, max_logp: float) -> pd.Series:
    vals = pd.to_numeric(values, errors="coerce")
    vals = vals.clip(lower=10 ** (-max_logp), upper=1.0)
    out = -np.log10(vals)
    return pd.Series(out).clip(lower=0.0, upper=max_logp)


def read_model_scores(model_file: ModelFile, max_logp: float) -> pd.DataFrame:
    if model_file.model == "mlm":
        header = read_header(model_file.result_file)
        p_col = "p_wald" if "p_wald" in header else "p_score" if "p_score" in header else "p_lrt"
        usecols = ["chr", "rs", "ps", p_col]
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
    return df[["snp", "chr", "pos", "p_value", "neglog10p", "model_score", "study", "model"]]


def update_aggregate(agg: pd.DataFrame | None, scores: pd.DataFrame, model_weight: float) -> pd.DataFrame:
    tmp = scores.set_index("snp")
    if agg is None:
        agg = tmp[["chr", "pos"]].copy()
        agg["score_sum"] = 0.0
        agg["score_sq_sum"] = 0.0
        agg["neglog10p_sum"] = 0.0
        agg["neglog10p_sq_sum"] = 0.0
        agg["weight_sum"] = 0.0
        agg["max_model_score"] = 0.0
        agg["max_neglog10p"] = 0.0
        agg["min_p_value"] = np.nan
        agg["model_support_95"] = 0
        agg["model_support_99"] = 0
        agg["best_model"] = ""
        agg["best_study"] = ""
    else:
        missing = tmp.index.difference(agg.index)
        if len(missing):
            add = tmp.loc[missing, ["chr", "pos"]].copy()
            add["score_sum"] = 0.0
            add["score_sq_sum"] = 0.0
            add["neglog10p_sum"] = 0.0
            add["neglog10p_sq_sum"] = 0.0
            add["weight_sum"] = 0.0
            add["max_model_score"] = 0.0
            add["max_neglog10p"] = 0.0
            add["min_p_value"] = np.nan
            add["model_support_95"] = 0
            add["model_support_99"] = 0
            add["best_model"] = ""
            add["best_study"] = ""
            agg = pd.concat([agg, add], axis=0)

    idx = tmp.index
    score = tmp["model_score"].astype(float)
    neglogp = tmp["neglog10p"].astype(float)
    p_value = tmp["p_value"].astype(float)

    agg.loc[idx, "score_sum"] = agg.loc[idx, "score_sum"].astype(float) + score * model_weight
    agg.loc[idx, "score_sq_sum"] = agg.loc[idx, "score_sq_sum"].astype(float) + (score**2) * model_weight
    agg.loc[idx, "neglog10p_sum"] = agg.loc[idx, "neglog10p_sum"].astype(float) + neglogp * model_weight
    agg.loc[idx, "neglog10p_sq_sum"] = agg.loc[idx, "neglog10p_sq_sum"].astype(float) + (neglogp**2) * model_weight
    agg.loc[idx, "weight_sum"] = agg.loc[idx, "weight_sum"].astype(float) + model_weight
    agg.loc[idx, "max_neglog10p"] = np.maximum(agg.loc[idx, "max_neglog10p"].astype(float), neglogp)

    existing_min = agg.loc[idx, "min_p_value"].astype(float)
    agg.loc[idx, "min_p_value"] = np.fmin(existing_min.fillna(np.inf), p_value.fillna(np.inf)).replace(np.inf, np.nan)

    hit95 = score >= 0.95
    hit99 = score >= 0.99
    agg.loc[idx, "model_support_95"] = agg.loc[idx, "model_support_95"].astype(int) + hit95.astype(int)
    agg.loc[idx, "model_support_99"] = agg.loc[idx, "model_support_99"].astype(int) + hit99.astype(int)

    for cutoff, hit in [(95, hit95), (99, hit99)]:
        study_col = f"study_hit_{cutoff}__{tmp['study'].iloc[0]}"
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


def finalize_gwas_features(agg: pd.DataFrame) -> pd.DataFrame:
    out = agg.copy()
    weight = out["weight_sum"].replace(0, np.nan).astype(float)
    out["gwas_consensus_score"] = (out["score_sum"].astype(float) / weight).fillna(0.0).astype("float32")
    out["gwas_mean_neglog10p"] = (out["neglog10p_sum"].astype(float) / weight).fillna(0.0).astype("float32")
    score_var = out["score_sq_sum"].astype(float) / weight - out["gwas_consensus_score"].astype(float) ** 2
    neg_var = out["neglog10p_sq_sum"].astype(float) / weight - out["gwas_mean_neglog10p"].astype(float) ** 2
    out["gwas_score_std"] = np.sqrt(score_var.clip(lower=0.0)).fillna(0.0).astype("float32")
    out["gwas_neglog10p_std"] = np.sqrt(neg_var.clip(lower=0.0)).fillna(0.0).astype("float32")
    out["min_p_neglog10"] = neglog10(out["min_p_value"], 20.0).astype("float32")

    for cutoff in [95, 99]:
        cols = [c for c in out.columns if c.startswith(f"study_hit_{cutoff}__")]
        out[f"study_support_{cutoff}"] = out[cols].sum(axis=1).astype("int16") if cols else 0

    out["gwas_rank"] = out["gwas_consensus_score"].rank(method="first", ascending=False).astype(int)
    out["chr"] = pd.to_numeric(out["chr"], errors="coerce").astype("Int64")
    out["pos"] = pd.to_numeric(out["pos"], errors="coerce").astype("Int64")
    return out


def parse_gff_attributes(text: str) -> dict[str, str]:
    attrs = {}
    for part in text.split(";"):
        if "=" in part:
            key, value = part.split("=", 1)
            attrs[key] = value
    return attrs


def normalize_chrom(value: object) -> int | None:
    text = str(value)
    m = re.search(r"(\d+)", text)
    if not m:
        return None
    return int(m.group(1))


def load_gene_intervals(gff_path: Path) -> pd.DataFrame:
    opener = gzip.open if str(gff_path).lower().endswith(".gz") else open
    rows = []
    with opener(gff_path, "rt", encoding="utf-8", errors="replace") as f:
        for line in f:
            if not line or line.startswith("#"):
                continue
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 9 or parts[2] != "gene":
                continue
            attrs = parse_gff_attributes(parts[8])
            gene = attrs.get("Name") or attrs.get("ID", "").split(".v")[0]
            chrom = normalize_chrom(parts[0])
            if not gene or chrom is None:
                continue
            rows.append(
                {
                    "gene": gene,
                    "chr": chrom,
                    "start": int(parts[3]),
                    "end": int(parts[4]),
                    "strand": parts[6],
                }
            )
    genes = pd.DataFrame(rows)
    if genes.empty:
        raise RuntimeError(f"No gene intervals parsed from {gff_path}")
    genes["center"] = ((genes["start"] + genes["end"]) / 2.0).astype(float)
    return genes.sort_values(["chr", "start", "end"]).reset_index(drop=True)


def intervals_by_chrom(intervals: pd.DataFrame) -> dict[int, pd.DataFrame]:
    return {int(chrom): part.sort_values(["start", "end"]).reset_index(drop=True) for chrom, part in intervals.groupby("chr")}


def annotate_nearest_interval(
    snps: pd.DataFrame,
    intervals: pd.DataFrame,
    prefix: str,
) -> pd.DataFrame:
    by_chr = intervals_by_chrom(intervals)
    n = len(snps)
    nearest_gene = np.full(n, "", dtype=object)
    nearest_dist = np.full(n, np.nan, dtype="float32")
    inside = np.zeros(n, dtype="int8")

    work = snps[["chr", "pos"]].reset_index(drop=True)
    for chrom, idx in work.groupby("chr", sort=False).groups.items():
        if pd.isna(chrom):
            continue
        chrom_int = int(chrom)
        genes = by_chr.get(chrom_int)
        if genes is None or genes.empty:
            continue
        positions = work.loc[idx, "pos"].astype(float).to_numpy()
        starts = genes["start"].astype(float).to_numpy()
        ends = genes["end"].astype(float).to_numpy()
        gene_ids = genes["gene"].astype(str).to_numpy()

        prev_idx = np.searchsorted(starts, positions, side="right") - 1
        next_idx = np.searchsorted(starts, positions, side="left")
        cand = []
        for arr in [prev_idx, prev_idx - 1, next_idx, next_idx + 1]:
            valid = (arr >= 0) & (arr < len(genes))
            dist = np.full(len(positions), np.inf, dtype=float)
            is_inside = np.zeros(len(positions), dtype=bool)
            gene_for = np.full(len(positions), "", dtype=object)
            if valid.any():
                a = arr[valid]
                pos_v = positions[valid]
                start_v = starts[a]
                end_v = ends[a]
                left = np.maximum(start_v - pos_v, 0.0)
                right = np.maximum(pos_v - end_v, 0.0)
                d = np.maximum(left, right)
                dist[valid] = d
                is_inside[valid] = d == 0.0
                gene_for[valid] = gene_ids[a]
            cand.append((dist, is_inside, gene_for))

        best_dist = cand[0][0].copy()
        best_inside = cand[0][1].copy()
        best_gene = cand[0][2].copy()
        for dist, is_inside, gene_for in cand[1:]:
            take = dist < best_dist
            best_dist[take] = dist[take]
            best_inside[take] = is_inside[take]
            best_gene[take] = gene_for[take]

        out_idx = np.asarray(list(idx), dtype=int)
        nearest_gene[out_idx] = best_gene
        nearest_dist[out_idx] = (best_dist / 1000.0).astype("float32")
        inside[out_idx] = best_inside.astype("int8")

    snps[f"{prefix}_gene"] = nearest_gene
    snps[f"{prefix}_distance_kb"] = nearest_dist
    snps[f"{prefix}_inside_gene"] = inside
    return snps


def read_flashp_perturbations(network_dir: Path) -> list[dict]:
    reconciled = network_dir / "data" / "reconciled_perturbation_dataset.json"
    raw = network_dir / "data" / "perturbation_dataset.json"
    path = reconciled if reconciled.exists() else raw
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("perturbations", [])


def normalize_node(value: object) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", str(value)).upper()


def build_label_mapping(
    network_dir: Path,
    min_confidence: float,
    genes: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    node_map = {normalize_node(row["node"]): row for row in DEFAULT_NODE_GENE_MAP}
    gene_set = set(genes["gene"].astype(str))
    perturbs = read_flashp_perturbations(network_dir)
    rows = []
    unmapped = []

    for pert in perturbs:
        nodes = pert.get("ng") or list((pert.get("m") or {}).keys()) or [pert.get("g")]
        for node_raw in nodes:
            node = normalize_node(node_raw)
            row = node_map.get(node)
            if not row or float(row["confidence"]) < min_confidence or row["gene"] not in gene_set:
                unmapped.append(
                    {
                        "test_id": pert.get("id", ""),
                        "flashp_gene": pert.get("g", ""),
                        "node": node_raw,
                        "perturbation_type": pert.get("pt", ""),
                        "expected_direction": pert.get("ed", ""),
                    }
                )
                continue
            rows.append(
                {
                    "test_id": pert.get("id", ""),
                    "flashp_gene": pert.get("g", ""),
                    "node": row["node"],
                    "sobic_gene": row["gene"],
                    "perturbation_type": pert.get("pt", ""),
                    "expected_direction": pert.get("ed", ""),
                    "confidence": row["confidence"],
                    "map_type": row["map_type"],
                    "basis": row["basis"],
                }
            )

    mapping = pd.DataFrame(rows).drop_duplicates()
    unmapped_df = pd.DataFrame(unmapped).drop_duplicates()
    if mapping.empty:
        raise RuntimeError("No FLASH-P perturbation nodes could be mapped to GFF genes.")

    truth = (
        mapping.groupby("sobic_gene", as_index=False)
        .agg(
            node=("node", "first"),
            flashp_gene=("flashp_gene", lambda x: ";".join(sorted(set(map(str, x))))),
            n_tests=("test_id", "nunique"),
            perturbation_types=("perturbation_type", lambda x: ";".join(sorted(set(map(str, x))))),
            expected_directions=("expected_direction", lambda x: ";".join(sorted(set(map(str, x))))),
            confidence=("confidence", "max"),
            map_type=("map_type", "first"),
            basis=("basis", "first"),
        )
        .sort_values(["confidence", "n_tests", "sobic_gene"], ascending=[False, False, True])
    )
    truth = truth.merge(genes[["gene", "chr", "start", "end", "strand"]], left_on="sobic_gene", right_on="gene", how="left")
    truth = truth.drop(columns=["gene"])
    return truth, pd.concat([mapping.assign(status="mapped"), unmapped_df.assign(status="unmapped")], ignore_index=True, sort=False)


def add_flashp_labels(
    snps: pd.DataFrame,
    truth_genes: pd.DataFrame,
    label_window_kb: float,
) -> pd.DataFrame:
    positive_intervals = truth_genes.rename(columns={"sobic_gene": "gene"})[
        ["gene", "chr", "start", "end", "strand"]
    ].copy()
    positive_intervals["center"] = ((positive_intervals["start"] + positive_intervals["end"]) / 2.0).astype(float)
    snps = annotate_nearest_interval(snps, positive_intervals, "flashp")
    snps["flashp_label"] = (
        pd.to_numeric(snps["flashp_distance_kb"], errors="coerce") <= float(label_window_kb)
    ).astype("int8")
    snps["flashp_positive_gene"] = np.where(snps["flashp_label"].astype(bool), snps["flashp_gene"], "")
    return snps


def add_annotation_features(snps: pd.DataFrame, genes: pd.DataFrame, gene_kernel_kb: float) -> pd.DataFrame:
    snps = annotate_nearest_interval(snps, genes, "nearest")
    dist = pd.to_numeric(snps["nearest_distance_kb"], errors="coerce").fillna(1e6)
    snps["nearest_gene_distance_kb_clipped"] = dist.clip(upper=1000.0).astype("float32")
    snps["nearest_gene_kernel"] = np.exp(-dist / max(gene_kernel_kb, 1e-9)).astype("float32")
    snps["is_genic"] = snps["nearest_inside_gene"].astype("int8")
    return snps


def add_locus_network_features(snps: pd.DataFrame, locus_kb: int) -> pd.DataFrame:
    snps["locus_bin"] = (pd.to_numeric(snps["pos"], errors="coerce") // (locus_kb * 1000)).astype("Int64")
    grouped = (
        snps.groupby(["chr", "locus_bin"], observed=True)
        .agg(
            locus_snp_count=("gwas_consensus_score", "size"),
            locus_gwas_mean=("gwas_consensus_score", "mean"),
            locus_gwas_max=("gwas_consensus_score", "max"),
            locus_neglog10p_mean=("max_neglog10p", "mean"),
            locus_neglog10p_max=("max_neglog10p", "max"),
            locus_model_support_99_max=("model_support_99", "max"),
        )
        .reset_index()
        .sort_values(["chr", "locus_bin"])
    )

    pieces = []
    for _, part in grouped.groupby("chr", sort=False):
        part = part.sort_values("locus_bin").copy()
        part["neighborhood_gwas_max"] = part["locus_gwas_max"].rolling(3, center=True, min_periods=1).max()
        part["neighborhood_gwas_mean"] = part["locus_gwas_mean"].rolling(3, center=True, min_periods=1).mean()
        part["neighborhood_neglog10p_max"] = part["locus_neglog10p_max"].rolling(3, center=True, min_periods=1).max()
        part["neighborhood_snp_count"] = part["locus_snp_count"].rolling(3, center=True, min_periods=1).sum()
        pieces.append(part)
    grouped = pd.concat(pieces, ignore_index=True)

    snps = snps.merge(grouped, on=["chr", "locus_bin"], how="left")
    for col in [
        "locus_gwas_mean",
        "locus_gwas_max",
        "locus_neglog10p_mean",
        "locus_neglog10p_max",
        "neighborhood_gwas_max",
        "neighborhood_gwas_mean",
        "neighborhood_neglog10p_max",
    ]:
        snps[col] = pd.to_numeric(snps[col], errors="coerce").fillna(0.0).astype("float32")
    for col in ["locus_snp_count", "locus_model_support_99_max", "neighborhood_snp_count"]:
        snps[col] = pd.to_numeric(snps[col], errors="coerce").fillna(0.0)
    snps["locus_snp_count_log"] = np.log1p(snps["locus_snp_count"].astype(float)).astype("float32")
    snps["neighborhood_snp_count_log"] = np.log1p(snps["neighborhood_snp_count"].astype(float)).astype("float32")
    return snps


def feature_columns() -> list[str]:
    return [
        "gwas_consensus_score",
        "gwas_score_std",
        "max_model_score",
        "max_neglog10p",
        "gwas_mean_neglog10p",
        "gwas_neglog10p_std",
        "min_p_neglog10",
        "model_support_95",
        "model_support_99",
        "study_support_95",
        "study_support_99",
        "nearest_gene_distance_kb_clipped",
        "nearest_gene_kernel",
        "is_genic",
        "locus_snp_count_log",
        "locus_gwas_mean",
        "locus_gwas_max",
        "locus_neglog10p_mean",
        "locus_neglog10p_max",
        "locus_model_support_99_max",
        "neighborhood_gwas_max",
        "neighborhood_gwas_mean",
        "neighborhood_neglog10p_max",
        "neighborhood_snp_count_log",
    ]


def make_feature_matrix(snps: pd.DataFrame, cols: list[str]) -> np.ndarray:
    x = snps.loc[:, cols].copy()
    for col in cols:
        x[col] = pd.to_numeric(x[col], errors="coerce").replace([np.inf, -np.inf], np.nan).fillna(0.0)
    return x.to_numpy(dtype=np.float32, copy=True)


def sample_training_rows(
    snps: pd.DataFrame,
    rng: np.random.Generator,
    exclude_mask: np.ndarray | None,
    max_background: int,
    neg_pos_ratio: int,
) -> tuple[np.ndarray, np.ndarray]:
    label = snps["flashp_label"].to_numpy(dtype=np.int8)
    if exclude_mask is None:
        exclude_mask = np.zeros(len(snps), dtype=bool)
    pos_idx = np.flatnonzero((label == 1) & ~exclude_mask)
    bg_idx = np.flatnonzero((label == 0) & ~exclude_mask)
    if len(pos_idx) == 0 or len(bg_idx) == 0:
        raise RuntimeError("Cannot train without both positive and background SNPs.")

    n_bg = min(len(bg_idx), max_background, max(len(pos_idx) * neg_pos_ratio, 1000))
    bg_sample = rng.choice(bg_idx, size=n_bg, replace=False)
    train_idx = np.concatenate([pos_idx, bg_sample])
    y = np.concatenate([np.ones(len(pos_idx), dtype=np.int8), np.zeros(len(bg_sample), dtype=np.int8)])
    order = rng.permutation(len(train_idx))
    return train_idx[order], y[order]


def fit_classifier(x_train: np.ndarray, y_train: np.ndarray, random_state: int) -> HistGradientBoostingClassifier:
    clf = HistGradientBoostingClassifier(
        max_iter=220,
        learning_rate=0.05,
        max_leaf_nodes=31,
        l2_regularization=0.05,
        early_stopping=True,
        random_state=random_state,
    )
    pos = max(int(y_train.sum()), 1)
    neg = max(int((y_train == 0).sum()), 1)
    weights = np.where(y_train == 1, (pos + neg) / (2 * pos), (pos + neg) / (2 * neg))
    clf.fit(x_train, y_train, sample_weight=weights)
    return clf


def predict_in_chunks(
    clf: HistGradientBoostingClassifier,
    snps: pd.DataFrame,
    cols: list[str],
    chunk_size: int,
) -> np.ndarray:
    out = np.zeros(len(snps), dtype=np.float32)
    for start in range(0, len(snps), chunk_size):
        end = min(start + chunk_size, len(snps))
        x = make_feature_matrix(snps.iloc[start:end], cols)
        out[start:end] = clf.predict_proba(x)[:, 1].astype("float32")
    return out


def rank_of_best(scores: np.ndarray, mask: np.ndarray) -> int | None:
    if not mask.any():
        return None
    best = float(np.max(scores[mask]))
    return int(1 + np.sum(scores > best))


def run_leave_one_gene_cv(
    snps: pd.DataFrame,
    cols: list[str],
    truth_genes: pd.DataFrame,
    rng: np.random.Generator,
    max_background: int,
    neg_pos_ratio: int,
    chunk_size: int,
    random_state: int,
    supervised_weight: float,
) -> pd.DataFrame:
    rows = []
    labels = snps["flashp_label"].to_numpy(dtype=np.int8)
    gwas_scores = snps["gwas_consensus_score"].to_numpy(dtype=float)

    for fold_no, gene in enumerate(truth_genes["sobic_gene"].astype(str), start=1):
        holdout = (snps["flashp_positive_gene"].astype(str).to_numpy() == gene)
        if not holdout.any():
            rows.append({"sobic_gene": gene, "status": "skipped_no_labeled_snps"})
            continue

        train_positive = (labels == 1) & ~holdout
        if train_positive.sum() == 0:
            rows.append({"sobic_gene": gene, "status": "skipped_no_train_positive"})
            continue

        train_idx, y_train = sample_training_rows(
            snps.assign(flashp_label=train_positive.astype("int8")),
            rng=rng,
            exclude_mask=holdout,
            max_background=max_background,
            neg_pos_ratio=neg_pos_ratio,
        )
        x_train = make_feature_matrix(snps.iloc[train_idx], cols)
        clf = fit_classifier(x_train, y_train, random_state + fold_no)
        pred = predict_in_chunks(clf, snps, cols, chunk_size)
        hybrid = (1.0 - supervised_weight) * gwas_scores + supervised_weight * pred

        eval_mask = ~train_positive
        y_eval = holdout[eval_mask].astype(int)
        pred_eval = pred[eval_mask]
        auc = np.nan
        ap = np.nan
        if y_eval.sum() > 0 and (y_eval == 0).sum() > 0:
            try:
                auc = float(roc_auc_score(y_eval, pred_eval))
                ap = float(average_precision_score(y_eval, pred_eval))
            except ValueError:
                pass

        best_model_rank = rank_of_best(pred, holdout)
        best_supervised_rank = rank_of_best(hybrid, holdout)
        best_gwas_rank = rank_of_best(gwas_scores, holdout)
        rows.append(
            {
                "sobic_gene": gene,
                "node": truth_genes.loc[truth_genes["sobic_gene"].astype(str) == gene, "node"].iloc[0],
                "status": "ok",
                "n_holdout_snps": int(holdout.sum()),
                "n_train_positive_snps": int(train_positive.sum()),
                "n_train_rows": int(len(train_idx)),
                "best_gwas_rank": best_gwas_rank,
                "best_model_rank": best_model_rank,
                "best_supervised_rank": best_supervised_rank,
                "rank_delta_vs_gwas": None
                if best_supervised_rank is None or best_gwas_rank is None
                else int(best_gwas_rank - best_supervised_rank),
                "hit_top_100": bool(best_supervised_rank is not None and best_supervised_rank <= 100),
                "hit_top_1000": bool(best_supervised_rank is not None and best_supervised_rank <= 1000),
                "hit_top_5000": bool(best_supervised_rank is not None and best_supervised_rank <= 5000),
                "holdout_auc_proxy": auc,
                "holdout_average_precision_proxy": ap,
            }
        )
    return pd.DataFrame(rows)


def train_final_model(
    snps: pd.DataFrame,
    cols: list[str],
    rng: np.random.Generator,
    max_background: int,
    neg_pos_ratio: int,
    chunk_size: int,
    random_state: int,
) -> tuple[HistGradientBoostingClassifier, np.ndarray, pd.DataFrame]:
    train_idx, y_train = sample_training_rows(
        snps,
        rng=rng,
        exclude_mask=None,
        max_background=max_background,
        neg_pos_ratio=neg_pos_ratio,
    )
    x_train = make_feature_matrix(snps.iloc[train_idx], cols)
    clf = fit_classifier(x_train, y_train, random_state)
    pred = predict_in_chunks(clf, snps, cols, chunk_size)
    train_preview = snps.iloc[train_idx].copy()
    train_preview["training_label"] = y_train
    return clf, pred, train_preview


def summarize_feature_signal(snps: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    pos = snps["flashp_label"].astype(bool)
    rows = []
    for col in cols:
        vals = pd.to_numeric(snps[col], errors="coerce").replace([np.inf, -np.inf], np.nan)
        pos_mean = float(vals[pos].mean()) if pos.any() else np.nan
        bg_mean = float(vals[~pos].mean()) if (~pos).any() else np.nan
        rows.append(
            {
                "feature": col,
                "positive_mean": pos_mean,
                "background_mean": bg_mean,
                "positive_minus_background": pos_mean - bg_mean,
            }
        )
    return pd.DataFrame(rows).sort_values("positive_minus_background", ascending=False)


def build_locus_summary(snps: pd.DataFrame, locus_kb: int, top_n: int) -> pd.DataFrame:
    cols = [
        "snp",
        "chr",
        "pos",
        "supervised_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "supervised_score",
        "supervised_model_score",
        "supervised_model_rank",
        "gwas_consensus_score",
        "max_neglog10p",
        "flashp_label",
        "flashp_positive_gene",
        "nearest_gene",
        "nearest_distance_kb",
        "best_model",
        "best_study",
    ]
    work = snps.sort_values("supervised_rank").drop_duplicates(["chr", "locus_bin"]).copy()
    work["locus_id"] = work["chr"].astype(str) + ":" + (work["locus_bin"].astype(int) * locus_kb).astype(str) + "kb"
    return work.loc[:, ["locus_id"] + cols].head(top_n)


def build_gene_summary(snps: pd.DataFrame, top_n: int) -> pd.DataFrame:
    cols = [
        "nearest_gene",
        "snp",
        "chr",
        "pos",
        "supervised_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "supervised_score",
        "supervised_model_score",
        "supervised_model_rank",
        "gwas_consensus_score",
        "max_neglog10p",
        "flashp_label",
        "flashp_positive_gene",
        "nearest_distance_kb",
        "best_model",
        "best_study",
    ]
    work = snps[snps["nearest_gene"].astype(str) != ""].sort_values("supervised_rank")
    return work.drop_duplicates("nearest_gene").loc[:, cols].head(top_n)


def markdown_table(df: pd.DataFrame) -> str:
    if df.empty:
        return "_No rows._"
    text = df.copy()
    for col in text.columns:
        if pd.api.types.is_float_dtype(text[col]):
            text[col] = text[col].map(lambda x: "" if pd.isna(x) else f"{x:.4g}")
        else:
            text[col] = text[col].fillna("").astype(str)
    widths = {col: max(len(col), int(text[col].map(len).max())) for col in text.columns}
    header = "| " + " | ".join(col.ljust(widths[col]) for col in text.columns) + " |"
    sep = "| " + " | ".join("-" * widths[col] for col in text.columns) + " |"
    rows = [
        "| " + " | ".join(str(row[col]).ljust(widths[col]) for col in text.columns) + " |"
        for _, row in text.iterrows()
    ]
    return "\n".join([header, sep] + rows)


def write_outputs(
    out_dir: Path,
    trait: str,
    studies: list[str],
    model_files: list[ModelFile],
    snps: pd.DataFrame,
    truth_genes: pd.DataFrame,
    label_mapping: pd.DataFrame,
    cv: pd.DataFrame,
    feature_signal: pd.DataFrame,
    top_n: int,
    locus_kb: int,
    label_window_kb: float,
    min_label_confidence: float,
    supervised_weight: float,
    discovery_exclusion_window_kb: float,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    manifest = pd.DataFrame(
        [
            {
                "study": mf.study,
                "model": mf.model,
                "result_file": str(mf.result_file),
                "aux_file": "" if mf.aux_file is None else str(mf.aux_file),
            }
            for mf in model_files
        ]
    )
    manifest.to_csv(out_dir / "model_manifest.tsv", sep="\t", index=False)
    truth_genes.to_csv(out_dir / "flashp_ground_truth_genes.tsv", sep="\t", index=False)
    label_mapping.to_csv(out_dir / "flashp_label_mapping_used.tsv", sep="\t", index=False)
    cv.to_csv(out_dir / "leave_one_gene_cv_metrics.tsv", sep="\t", index=False)
    feature_signal.to_csv(out_dir / "feature_signal_summary.tsv", sep="\t", index=False)

    snps.to_parquet(out_dir / "supervised_snp_scores.parquet", index=False)
    top_cols = [
        "snp",
        "chr",
        "pos",
        "supervised_rank",
        "gwas_rank",
        "rank_delta_vs_gwas",
        "supervised_score",
        "supervised_model_score",
        "supervised_model_rank",
        "gwas_consensus_score",
        "max_neglog10p",
        "flashp_label",
        "flashp_positive_gene",
        "nearest_gene",
        "nearest_distance_kb",
        "locus_gwas_max",
        "neighborhood_gwas_max",
        "best_model",
        "best_study",
    ]
    top_snps = snps.sort_values("supervised_rank").loc[:, top_cols].head(top_n)
    top_snps.to_csv(out_dir / f"top_{top_n}_supervised_snps.tsv", sep="\t", index=False)
    top_novel = (
        snps[snps["flashp_label"].astype(int) == 0]
        .sort_values("supervised_rank")
        .loc[:, top_cols]
        .head(top_n)
    )
    top_novel.to_csv(out_dir / f"top_{top_n}_supervised_novel_snps.tsv", sep="\t", index=False)
    flashp_dist = pd.to_numeric(snps["flashp_distance_kb"], errors="coerce")
    top_discovery = (
        snps[flashp_dist.isna() | (flashp_dist > discovery_exclusion_window_kb)]
        .sort_values("supervised_rank")
        .loc[:, top_cols]
        .head(top_n)
    )
    top_discovery.to_csv(out_dir / f"top_{top_n}_supervised_discovery_snps.tsv", sep="\t", index=False)
    build_locus_summary(snps, locus_kb, top_n).to_csv(
        out_dir / f"top_{top_n}_supervised_loci.tsv", sep="\t", index=False
    )
    build_gene_summary(snps, top_n).to_csv(out_dir / f"top_{top_n}_supervised_genes.tsv", sep="\t", index=False)

    ok_cv = cv[cv["status"] == "ok"].copy() if "status" in cv.columns else pd.DataFrame()
    summary = {
        "trait": trait,
        "studies": studies,
        "n_model_files": len(model_files),
        "n_snps_scored": int(len(snps)),
        "n_flashp_ground_truth_genes": int(len(truth_genes)),
        "n_flashp_positive_snps": int(snps["flashp_label"].sum()),
        "label_window_kb": label_window_kb,
        "min_label_confidence": min_label_confidence,
        "supervised_weight": supervised_weight,
        "discovery_exclusion_window_kb": discovery_exclusion_window_kb,
        "cv_folds_ok": int(len(ok_cv)),
        "cv_top_100_hits": int(ok_cv["hit_top_100"].sum()) if "hit_top_100" in ok_cv else 0,
        "cv_top_1000_hits": int(ok_cv["hit_top_1000"].sum()) if "hit_top_1000" in ok_cv else 0,
        "cv_top_5000_hits": int(ok_cv["hit_top_5000"].sum()) if "hit_top_5000" in ok_cv else 0,
        "median_cv_rank_delta_vs_gwas": None
        if ok_cv.empty
        else float(pd.to_numeric(ok_cv["rank_delta_vs_gwas"], errors="coerce").median()),
        "top_snp": top_snps.iloc[0].to_dict() if not top_snps.empty else None,
    }
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")

    lines = [
        f"# FLASH-P Supervised SNP-Network GWAS: {trait}",
        "",
        "Method: FLASH-P perturbation loci are supervised labels. No GRN SNP-gene or eQTL tables were used.",
        "Final `supervised_score` is a GWAS-anchored hybrid: raw GWAS evidence plus the FLASH-P-trained SNP-network model.",
        "",
        f"- Studies: {', '.join(studies)}",
        f"- Model files: {len(model_files)}",
        f"- SNPs scored: {len(snps):,}",
        f"- FLASH-P ground-truth genes: {len(truth_genes):,}",
        f"- Positive SNPs within {label_window_kb:g} kb of a FLASH-P gene: {int(snps['flashp_label'].sum()):,}",
        f"- Supervised model weight in final score: {supervised_weight:g}",
        f"- Discovery table excludes SNPs within {discovery_exclusion_window_kb:g} kb of FLASH-P genes",
        f"- Leave-one-gene-out folds: {summary['cv_folds_ok']}",
        f"- CV hits in top 100 / 1000 / 5000: {summary['cv_top_100_hits']} / {summary['cv_top_1000_hits']} / {summary['cv_top_5000_hits']}",
        "",
        "## Top Supervised SNPs",
        "",
        markdown_table(top_snps.head(20)),
        "",
        "## Top Novel SNPs Outside FLASH-P Positive Windows",
        "",
        markdown_table(top_novel.head(20)),
        "",
        "## Top Discovery SNPs Outside Extended FLASH-P Windows",
        "",
        markdown_table(top_discovery.head(20)),
        "",
        "## Leave-One-Gene-Out CV",
        "",
        markdown_table(cv.head(30)),
        "",
        "Interpretation note: `supervised_score` is a FLASH-P-trained, GWAS-anchored prioritization score, not a causal posterior inclusion probability.",
    ]
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> None:
    flash_root = resolve_path(args.flash_root)
    gwas_root = resolve_path(args.gwas_root)
    network_dir = resolve_path(args.network, flash_root)
    out_dir = resolve_path(args.out, flash_root)
    rng = np.random.default_rng(args.random_state)

    print("Discovering GWAS model files...")
    model_files = discover_model_files(gwas_root, args.studies, args.trait, args.include_xgboost_full)
    if not model_files:
        raise RuntimeError("No GWAS model files discovered.")

    print(f"Reading and harmonizing {len(model_files)} GWAS model files...")
    agg: pd.DataFrame | None = None
    for model_file in model_files:
        print(f"  {model_file.study} {model_file.model}: {model_file.result_file}")
        scores = read_model_scores(model_file, args.max_logp)
        agg = update_aggregate(agg, scores, MODEL_WEIGHTS.get(model_file.model, 1.0))
    if agg is None:
        raise RuntimeError("No SNP scores could be aggregated.")
    snps = finalize_gwas_features(agg).reset_index().rename(columns={"index": "snp"})

    print("Loading GFF3 gene intervals...")
    genes = load_gene_intervals(resolve_path(args.gff))
    print(f"  Parsed {len(genes):,} gene intervals.")

    print("Building FLASH-P supervised labels...")
    truth_genes, label_mapping = build_label_mapping(network_dir, args.min_label_confidence, genes)
    print(f"  Mapped {len(truth_genes):,} FLASH-P ground-truth genes.")

    print("Annotating nearest genes and FLASH-P positive loci...")
    snps = add_annotation_features(snps, genes, args.gene_kernel_kb)
    snps = add_flashp_labels(snps, truth_genes, args.label_window_kb)
    print(f"  Positive SNP labels: {int(snps['flashp_label'].sum()):,}")

    print("Building local SNP-network features...")
    snps = add_locus_network_features(snps, args.locus_kb)
    cols = feature_columns()
    feature_signal = summarize_feature_signal(snps, cols)

    print("Running leave-one-FLASH-P-gene-out validation...")
    cv = run_leave_one_gene_cv(
        snps=snps,
        cols=cols,
        truth_genes=truth_genes,
        rng=rng,
        max_background=args.max_background,
        neg_pos_ratio=args.neg_pos_ratio,
        chunk_size=args.chunk_size,
        random_state=args.random_state,
        supervised_weight=args.supervised_weight,
    )

    print("Training final FLASH-P-supervised SNP-network model...")
    _, supervised_model_score, _ = train_final_model(
        snps=snps,
        cols=cols,
        rng=rng,
        max_background=args.max_background,
        neg_pos_ratio=args.neg_pos_ratio,
        chunk_size=args.chunk_size,
        random_state=args.random_state,
    )
    snps["supervised_model_score"] = supervised_model_score
    snps["supervised_model_rank"] = pd.Series(supervised_model_score).rank(method="first", ascending=False).astype(int)
    snps["supervised_score"] = (
        (1.0 - args.supervised_weight) * snps["gwas_consensus_score"].astype(float)
        + args.supervised_weight * snps["supervised_model_score"].astype(float)
    ).astype("float32")
    snps["supervised_rank"] = snps["supervised_score"].rank(method="first", ascending=False).astype(int)
    snps["rank_delta_vs_gwas"] = snps["gwas_rank"].astype(int) - snps["supervised_rank"].astype(int)
    snps = snps.sort_values("supervised_rank").reset_index(drop=True)

    print(f"Writing outputs to {out_dir}...")
    write_outputs(
        out_dir=out_dir,
        trait=args.trait,
        studies=args.studies,
        model_files=model_files,
        snps=snps,
        truth_genes=truth_genes,
        label_mapping=label_mapping,
        cv=cv,
        feature_signal=feature_signal,
        top_n=args.top_n,
        locus_kb=args.locus_kb,
        label_window_kb=args.label_window_kb,
        min_label_confidence=args.min_label_confidence,
        supervised_weight=args.supervised_weight,
        discovery_exclusion_window_kb=args.discovery_exclusion_window_kb,
    )
    print("Done.")


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Train a FLASH-P-supervised SNP-network GWAS ranker without GRN inputs."
    )
    parser.add_argument("--gwas-root", default=str(DEFAULT_GWAS_ROOT), help="Root containing study GWAS folders.")
    parser.add_argument("--studies", nargs="+", default=DEFAULT_STUDIES, help="Study folder names to include.")
    parser.add_argument("--trait", default="DTF", help="Trait token used to discover GWAS result files.")
    parser.add_argument("--flash-root", default=str(DEFAULT_FLASH_ROOT), help="FLASH-P Claude workspace root.")
    parser.add_argument("--network", default=str(DEFAULT_NETWORK), help="FLASH-P network directory with perturbations.")
    parser.add_argument("--gff", default=str(DEFAULT_GFF), help="Gene GFF3 or GFF3.gz for SNP-gene annotation.")
    parser.add_argument("--out", default="analysis/flashp_supervised_snp_network_dtf", help="Output directory.")
    parser.add_argument("--top-n", type=int, default=5000, help="Number of top SNPs/loci/genes to write.")
    parser.add_argument("--max-logp", type=float, default=20.0, help="Clip -log10(p) to this value.")
    parser.add_argument("--label-window-kb", type=float, default=50.0, help="Positive label window around FLASH-P genes.")
    parser.add_argument(
        "--discovery-exclusion-window-kb",
        type=float,
        default=250.0,
        help="Window around FLASH-P genes excluded from discovery-only output tables.",
    )
    parser.add_argument("--gene-kernel-kb", type=float, default=50.0, help="Distance scale for nearest-gene feature.")
    parser.add_argument("--locus-kb", type=int, default=100, help="Physical SNP-network bin size.")
    parser.add_argument("--min-label-confidence", type=float, default=0.45, help="Minimum node-gene label confidence.")
    parser.add_argument(
        "--supervised-weight",
        type=float,
        default=0.25,
        help="Weight of the FLASH-P-trained SNP-network model in the final GWAS-anchored score.",
    )
    parser.add_argument("--max-background", type=int, default=200000, help="Maximum unlabeled/background SNPs per fit.")
    parser.add_argument("--neg-pos-ratio", type=int, default=50, help="Background-to-positive training ratio cap.")
    parser.add_argument("--chunk-size", type=int, default=250000, help="Prediction chunk size.")
    parser.add_argument("--random-state", type=int, default=42, help="Random seed.")
    parser.add_argument("--include-xgboost-full", action="store_true", help="Use xgboost_full instead of xgboost.")
    return parser.parse_args(argv)


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
