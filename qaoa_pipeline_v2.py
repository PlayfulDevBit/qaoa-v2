"""
QAOA MaxCut Pipeline v2 · IQM Garnet — Prefect Serverless
==========================================================
Enhancements over v1:
  - Distance-based weighted graph from 2D node coordinates
  - User-overridable graph size + coordinates with validation
  - Selectable error mitigation: none | zne | readout | dd | all
  - Rich Prefect artifacts: energy landscape, weighted graph SVG, report

Local testing:
    pip install prefect "qiskit>=1.0,<2.2" "iqm-client[qiskit]==33.0.5" \
                matplotlib networkx mitiq mthree
    python qaoa_pipeline_v2.py

Serverless:
    Triggered from Prefect Cloud UI after running deploy_qaoa_v2.py
    IQM token from Prefect Secret block "iqm-resonance-token"
"""

import sys
import time
import math
import argparse
import json
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

from prefect import flow, task, get_run_logger
from prefect.artifacts import create_markdown_artifact


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS
# ═══════════════════════════════════════════════════════════════════════

VALID_MITIGATION = ["none", "zne", "readout", "dd", "all"]

# Default coordinates for a 5-node graph (pentagon-ish layout)
DEFAULT_COORDINATES = [
    [0.0, 4.0],
    [3.8, 1.2],
    [2.4, -3.2],
    [-2.4, -3.2],
    [-3.8, 1.2],
]


# ═══════════════════════════════════════════════════════════════════════
# TOKEN RETRIEVAL
# ═══════════════════════════════════════════════════════════════════════

def get_iqm_token() -> str:
    """Read IQM token from Prefect Secret block."""
    try:
        from prefect.blocks.system import Secret
        secret = Secret.load("iqm-resonance-token")
        return secret.get()
    except Exception:
        return ""


# ═══════════════════════════════════════════════════════════════════════
# STAGE 1 — INPUT VALIDATION
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="1 · Validate Inputs",
    tags=["stage:1-validate", "infra:cpu"],
)
def validate_inputs(
    num_nodes: int,
    node_coordinates: list[list[float]],
    error_mitigation: str,
) -> dict:
    """
    Validates that:
    - num_nodes matches len(node_coordinates)
    - error_mitigation is a valid choice
    - coordinates are valid 2D points
    Returns validated config dict.
    """
    logger = get_run_logger()

    # ── Mitigation check ──
    if error_mitigation not in VALID_MITIGATION:
        raise ValueError(
            f"Invalid error_mitigation='{error_mitigation}'. "
            f"Must be one of: {VALID_MITIGATION}"
        )

    # ── Size / coordinate match ──
    if len(node_coordinates) != num_nodes:
        raise ValueError(
            f"Mismatch: num_nodes={num_nodes} but "
            f"len(node_coordinates)={len(node_coordinates)}. "
            f"These must be equal. Either adjust num_nodes or provide "
            f"exactly {num_nodes} coordinate pairs."
        )

    # ── Coordinate format check ──
    for idx, coord in enumerate(node_coordinates):
        if not isinstance(coord, (list, tuple)) or len(coord) != 2:
            raise ValueError(
                f"node_coordinates[{idx}] = {coord} is not a valid [x, y] pair."
            )
        try:
            float(coord[0])
            float(coord[1])
        except (TypeError, ValueError):
            raise ValueError(
                f"node_coordinates[{idx}] = {coord} contains non-numeric values."
            )

    logger.info(f"✅ Inputs valid: {num_nodes} nodes, mitigation='{error_mitigation}'")
    logger.info(f"   Coordinates: {node_coordinates}")

    return {
        "num_nodes": num_nodes,
        "coordinates": [[float(c[0]), float(c[1])] for c in node_coordinates],
        "error_mitigation": error_mitigation,
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 2 — DISTANCE-BASED WEIGHTED GRAPH
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="2 · Build Weighted Graph",
    tags=["stage:2-graph", "infra:cpu"],
)
def build_weighted_graph(
    config: dict,
    distance_threshold: float = 0.0,
    max_weight: float = 1.0,
) -> dict:
    """
    Builds a fully-connected weighted graph where edge weight is
    inversely proportional to distance between nodes:

        weight(i,j) = max_weight * (1 - dist(i,j) / max_dist)

    Closer nodes → stronger coupling → higher weight.
    Optionally filters edges below a distance_threshold (0 = keep all).
    Also brute-forces optimal MaxCut for comparison.
    """
    logger = get_run_logger()
    import math as m

    coords = config["coordinates"]
    n = config["num_nodes"]

    # ── Compute pairwise distances ──
    distances = {}
    for i in range(n):
        for j in range(i + 1, n):
            dx = coords[i][0] - coords[j][0]
            dy = coords[i][1] - coords[j][1]
            distances[(i, j)] = m.sqrt(dx * dx + dy * dy)

    max_dist = max(distances.values()) if distances else 1.0

    # ── Build edges with distance-based weights ──
    edges = []
    weights = {}
    for (i, j), dist in distances.items():
        # Inverse distance weighting: closer = heavier
        w = max_weight * (1.0 - dist / max_dist) if max_dist > 0 else max_weight
        # Clamp minimum weight so even far nodes have some coupling
        w = max(0.05, w)

        if distance_threshold <= 0 or dist <= distance_threshold:
            edges.append((i, j))
            weights[(i, j)] = round(w, 4)

    logger.info(f"Graph: {n} nodes, {len(edges)} edges (fully connected)")
    for (i, j), w in sorted(weights.items()):
        d = distances[(i, j)]
        logger.info(f"  Edge ({i},{j}): dist={d:.2f}, weight={w:.4f}")

    # ── Brute-force optimal weighted MaxCut ──
    best_cut_value = 0.0
    best_partition = [0] * n

    for mask in range(1 << n):
        partition = [(mask >> k) & 1 for k in range(n)]
        cut_value = sum(
            weights[(i, j)]
            for (i, j) in edges
            if partition[i] != partition[j]
        )
        if cut_value > best_cut_value:
            best_cut_value = cut_value
            best_partition = partition[:]

    logger.info(f"Optimal weighted MaxCut = {best_cut_value:.4f}")
    logger.info(f"Optimal partition: {best_partition}")

    return {
        "num_nodes": n,
        "edges": edges,
        "weights": weights,
        "coordinates": coords,
        "distances": {f"{i}-{j}": d for (i, j), d in distances.items()},
        "optimal_cut": best_cut_value,
        "optimal_partition": best_partition,
        "error_mitigation": config["error_mitigation"],
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 3 — QAOA CIRCUIT CONSTRUCTION
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="3 · Build QAOA Circuit",
    tags=["stage:3-circuit", "infra:cpu"],
)
def build_qaoa_circuit(
    graph: dict,
    gamma: float,
    beta: float,
    p: int = 1,
) -> dict:
    """
    Builds parameterized QAOA circuit for weighted MaxCut.
    Cost unitary: exp(-i * gamma * w_ij * Z_i Z_j) for each weighted edge.
    Mixer unitary: exp(-i * beta * X_i) for each qubit.
    """
    logger = get_run_logger()
    from qiskit import QuantumCircuit

    n = graph["num_nodes"]
    edges = graph["edges"]
    weights = graph["weights"]

    qc = QuantumCircuit(n)

    # Initial superposition
    for i in range(n):
        qc.h(i)

    # QAOA layers
    for layer in range(p):
        # Cost unitary — weighted ZZ interaction for each edge
        for (i, j) in edges:
            w = weights.get((i, j), weights.get(f"{i}-{j}", 1.0))
            # ZZ gate: CNOT - Rz(2*gamma*w) - CNOT
            qc.cx(i, j)
            qc.rz(2 * gamma * w, j)
            qc.cx(i, j)

        # Mixer unitary — RX on each qubit
        for i in range(n):
            qc.rx(2 * beta, i)

    qc.measure_all()

    gate_count = qc.size()
    depth = qc.depth()

    logger.info(f"QAOA circuit (p={p}): γ={gamma:.4f}, β={beta:.4f}")
    logger.info(f"  Gates: {gate_count}, Depth: {depth}, Qubits: {n}")

    return {
        "num_qubits": n,
        "p": p,
        "gamma": gamma,
        "beta": beta,
        "gate_count": gate_count,
        "depth": depth,
        "_circuit_obj": qc,
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 4 — TRANSPILE FOR IQM GARNET
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="4 · Transpile for IQM Garnet",
    tags=["stage:4-transpile", "infra:cpu"],
)
def transpile_for_garnet(circuit_meta: dict, error_mitigation: str) -> dict:
    """
    Transpile to IQM Garnet using the actual backend so Qiskit knows the
    physical coupling map (which qubit pairs support CZ gates).
    If dynamical decoupling (dd) is selected, insert DD sequences after transpilation.
    """
    logger = get_run_logger()
    from qiskit import transpile as qk_transpile
    import os

    qc = circuit_meta["_circuit_obj"]

    # ── Get the real IQM Garnet backend for its coupling map ──
    token = get_iqm_token()
    if not token:
        raise RuntimeError(
            "No IQM token found. Create a Prefect Secret block named "
            "'iqm-resonance-token' with your IQM Resonance API token."
        )
    os.environ["IQM_TOKEN"] = token

    from iqm.qiskit_iqm import IQMProvider

    provider = IQMProvider("https://cocos.resonance.meetiqm.com/garnet")
    backend = provider.get_backend()

    # Transpile against the real backend — this ensures:
    #   - Native gate set (r, cz)
    #   - Coupling map constraints (only physically connected qubit pairs)
    #   - Qubit routing/swaps inserted where needed
    transpiled = qk_transpile(
        qc,
        backend=backend,
        optimization_level=2,
        seed_transpiler=42,
    )
    logger.info(f"  Transpiled against IQM Garnet backend (coupling map respected)")

    # ── Dynamical Decoupling insertion ──
    dd_applied = False
    if error_mitigation in ("dd", "all"):
        try:
            from qiskit.transpiler import PassManager
            from qiskit.transpiler.passes import PadDynamicalDecoupling
            from qiskit.circuit.library import XGate

            dd_sequence = [XGate(), XGate()]  # XX sequence
            dd_pass = PadDynamicalDecoupling(
                durations=None,
                dd_sequence=dd_sequence,
            )
            pm = PassManager([dd_pass])
            transpiled = pm.run(transpiled)
            dd_applied = True
            logger.info("  DD: Dynamical decoupling (XX) applied")
        except Exception as e:
            logger.warning(f"  DD: Could not apply dynamical decoupling: {e}")
            logger.warning("  DD: Proceeding without DD — circuit still valid")

    t_gates = transpiled.size()
    t_depth = transpiled.depth()

    logger.info(f"Transpiled: {t_gates} gates, depth {t_depth}")
    logger.info(f"  Original: {circuit_meta['gate_count']} gates, depth {circuit_meta['depth']}")

    result = {
        **circuit_meta,
        "transpiled_gate_count": t_gates,
        "transpiled_depth": t_depth,
        "dd_applied": dd_applied,
        "_transpiled_obj": transpiled,
    }
    # Remove original circuit to save memory
    result.pop("_circuit_obj", None)
    return result


# ═══════════════════════════════════════════════════════════════════════
# STAGE 5 — QPU EXECUTION WITH ERROR MITIGATION
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="5 · Execute on IQM Garnet",
    tags=["stage:5-execute", "infra:qpu"],
    retries=2,
    retry_delay_seconds=30,
)
def execute_on_garnet(
    transpiled_meta: dict,
    graph: dict,
    shots: int = 4096,
    error_mitigation: str = "none",
) -> dict:
    """
    Execute circuit on IQM Garnet with optional error mitigation:
      - none:     Raw counts
      - zne:      Zero Noise Extrapolation via Mitiq
      - readout:  M3 readout error mitigation
      - dd:       Already applied at transpile stage
      - all:      ZNE + readout + DD
    """
    logger = get_run_logger()
    import os
    import numpy as np

    token = get_iqm_token()
    if not token:
        raise RuntimeError(
            "No IQM token found. Create a Prefect Secret block named "
            "'iqm-resonance-token' with your IQM Resonance API token."
        )

    os.environ["IQM_TOKEN"] = token

    from iqm.qiskit_iqm import IQMProvider

    provider = IQMProvider("https://cocos.resonance.meetiqm.com/garnet")
    backend = provider.get_backend()

    qc = transpiled_meta["_transpiled_obj"]
    n = transpiled_meta["num_qubits"]
    edges = graph["edges"]
    weights = graph["weights"]

    t_start = time.time()

    # ── Base execution ──
    raw_counts = _run_circuit(backend, qc, shots)

    # ── ZNE (Zero Noise Extrapolation) ──
    zne_expectation = None
    if error_mitigation in ("zne", "all"):
        zne_expectation = _apply_zne(backend, qc, shots, n, edges, weights, logger)

    # ── Readout mitigation (M3) ──
    mitigated_counts = raw_counts
    if error_mitigation in ("readout", "all"):
        mitigated_counts = _apply_readout_mitigation(
            backend, raw_counts, n, logger
        )

    t_elapsed = time.time() - t_start

    # ── Compute expectation value from (possibly mitigated) counts ──
    counts_for_eval = mitigated_counts
    expectation = _compute_weighted_expectation(counts_for_eval, edges, weights, n)

    # If ZNE produced a result, use it as the primary expectation
    if zne_expectation is not None:
        logger.info(f"  ZNE expectation: {zne_expectation:.4f} (raw: {expectation:.4f})")
        expectation = zne_expectation

    # Best bitstring
    best_bs = max(counts_for_eval, key=counts_for_eval.get)
    best_partition = [int(b) for b in reversed(best_bs)][:n]
    best_cut = sum(
        weights.get((i, j), 0)
        for (i, j) in edges
        if best_partition[i] != best_partition[j]
    )

    approx_ratio = best_cut / graph["optimal_cut"] if graph["optimal_cut"] > 0 else 0.0

    logger.info(f"⟨C⟩ = {expectation:.4f}, best cut = {best_cut:.4f}")
    logger.info(f"Approximation ratio: {approx_ratio:.4f}")
    logger.info(f"Mitigation: {error_mitigation}, Time: {t_elapsed:.2f}s")

    top_counts = dict(
        sorted(counts_for_eval.items(), key=lambda x: -x[1])[:10]
    )

    return {
        "iteration": transpiled_meta.get("_iteration", 0),
        "gamma": transpiled_meta["gamma"],
        "beta": transpiled_meta["beta"],
        "expectation_value": expectation,
        "best_bitstring": best_bs,
        "best_cut_value": best_cut,
        "approximation_ratio": approx_ratio,
        "top_counts": top_counts,
        "shots": shots,
        "execution_time_s": t_elapsed,
        "mitigation_used": error_mitigation,
        "dd_applied": transpiled_meta.get("dd_applied", False),
        "zne_applied": zne_expectation is not None,
        "readout_mitigated": error_mitigation in ("readout", "all"),
    }


def _run_circuit(backend, qc, shots):
    """Execute a single circuit and return counts dict."""
    job = backend.run(qc, shots=shots)
    result = job.result()
    return dict(result.get_counts())


def _compute_weighted_expectation(counts, edges, weights, n):
    """Compute ⟨C⟩ = Σ_shots Σ_edges w_ij * (1 - z_i*z_j) / 2."""
    total_shots = sum(counts.values())
    expectation = 0.0
    for bitstring, count in counts.items():
        bits = [int(b) for b in reversed(bitstring)][:n]
        cut_val = sum(
            weights.get((i, j), 0)
            for (i, j) in edges
            if bits[i] != bits[j]
        )
        expectation += cut_val * count
    return expectation / total_shots


def _apply_zne(backend, qc, shots, n, edges, weights, logger):
    """
    Apply Zero Noise Extrapolation using Mitiq.
    We use unitary folding to amplify noise at scale factors [1, 3, 5]
    and extrapolate to zero noise.
    """
    try:
        from mitiq import zne
        from mitiq.zne.scaling import fold_global
        from mitiq.zne.inference import LinearFactory
        import numpy as np

        noise_factors = [1.0, 3.0, 5.0]
        expectations_at_noise = []

        for factor in noise_factors:
            if factor == 1.0:
                folded = qc
            else:
                # Mitiq fold_global works on Cirq circuits; we manually fold
                folded = _fold_circuit_qiskit(qc, int(factor))

            counts = _run_circuit(backend, folded, shots)
            exp_val = _compute_weighted_expectation(counts, edges, weights, n)
            expectations_at_noise.append(exp_val)
            logger.info(f"  ZNE noise_factor={factor:.0f}: ⟨C⟩={exp_val:.4f}")

        # Linear extrapolation to zero noise
        # E(λ) = a + b*λ → E(0) = a
        x = np.array(noise_factors)
        y = np.array(expectations_at_noise)
        coeffs = np.polyfit(x, y, 1)
        zne_value = np.polyval(coeffs, 0.0)

        logger.info(f"  ZNE extrapolated: ⟨C⟩={zne_value:.4f}")
        return float(zne_value)

    except Exception as e:
        logger.warning(f"  ZNE failed: {e}. Using raw expectation.")
        return None


def _fold_circuit_qiskit(qc, scale_factor):
    """
    Manual global unitary folding for Qiskit circuits.
    For scale_factor k: append (circuit_inverse + circuit) repeated (k-1)/2 times.
    """
    from qiskit import QuantumCircuit

    # Remove measurements for folding
    qc_no_meas = qc.remove_final_measurements(inplace=False)
    inverse = qc_no_meas.inverse()

    folded = qc_no_meas.copy()
    num_folds = (scale_factor - 1) // 2
    for _ in range(num_folds):
        folded.compose(inverse, inplace=True)
        folded.compose(qc_no_meas, inplace=True)

    folded.measure_all()
    return folded


def _apply_readout_mitigation(backend, raw_counts, n, logger):
    """
    Apply M3 (matrix-free measurement mitigation) to raw counts.
    Falls back to raw counts if M3 is unavailable.
    """
    try:
        import mthree

        mit = mthree.M3Mitigation(backend)
        # Calibrate on the qubits we used
        qubit_list = list(range(n))
        mit.cals_from_system(qubit_list)

        # Apply mitigation
        quasi_dist = mit.apply_correction(
            raw_counts, qubit_list, return_mitigation_overhead=False
        )
        # Convert quasi-distribution back to counts-like dict
        total = sum(raw_counts.values())
        mitigated = {}
        for bitstring, prob in quasi_dist.items():
            count = max(0, int(round(prob * total)))
            if count > 0:
                mitigated[bitstring] = count

        if not mitigated:
            mitigated = raw_counts

        logger.info(f"  M3 readout mitigation applied ({len(mitigated)} bitstrings)")
        return mitigated

    except Exception as e:
        logger.warning(f"  M3 readout mitigation failed: {e}. Using raw counts.")
        return raw_counts


# ═══════════════════════════════════════════════════════════════════════
# STAGE 6 — ANALYSIS
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="6 · Analyze Results",
    tags=["stage:6-analysis", "infra:cpu"],
)
def analyze_results(all_results: list[dict], graph: dict) -> dict:
    """Pick the best iteration and compute summary statistics."""
    logger = get_run_logger()

    best = max(all_results, key=lambda r: r["expectation_value"])

    analysis = {
        "best_iteration": best["iteration"],
        "best_expectation": best["expectation_value"],
        "best_bitstring": best["best_bitstring"],
        "best_cut_found": best["best_cut_value"],
        "best_approximation_ratio": best["approximation_ratio"],
        "optimal_cut": graph["optimal_cut"],
        "total_iterations": len(all_results),
        "mitigation_used": best["mitigation_used"],
    }

    if analysis["best_approximation_ratio"] >= 0.9:
        analysis["quality"] = "EXCELLENT"
    elif analysis["best_approximation_ratio"] >= 0.7:
        analysis["quality"] = "GOOD"
    elif analysis["best_approximation_ratio"] >= 0.5:
        analysis["quality"] = "FAIR"
    else:
        analysis["quality"] = "POOR"

    logger.info(f"━━━ QAOA v2 Result ━━━")
    logger.info(f"  Best ⟨C⟩ = {analysis['best_expectation']:.4f}")
    logger.info(f"  Approx ratio: {analysis['best_approximation_ratio']:.4f}")
    logger.info(f"  Quality: {analysis['quality']}")
    logger.info(f"  Mitigation: {analysis['mitigation_used']}")

    return analysis


# ═══════════════════════════════════════════════════════════════════════
# STAGE 7 — ARTIFACTS
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="7.1 · Energy Landscape Artifact",
    tags=["stage:7-artifacts", "reporting"],
)
def publish_energy_landscape(all_results: list[dict], graph: dict) -> None:
    """Convergence chart + parameter trajectory table."""
    logger = get_run_logger()

    expectations = [r["expectation_value"] for r in all_results]
    optimal = graph["optimal_cut"]
    chart_width = 40

    chart_lines = []
    for i, r in enumerate(all_results):
        e = r["expectation_value"]
        bar_len = int((e / optimal) * chart_width) if optimal > 0 else 0
        bar_len = max(1, min(chart_width, bar_len))
        bar = "█" * bar_len
        marker = " ★" if e == max(expectations) else ""
        chart_lines.append(f"  iter {i}: |{bar:<{chart_width}}| {e:.3f}{marker}")

    optimal_bar = "─" * chart_width
    chart_lines.append(f"  optimal:|{optimal_bar}| {optimal:.3f}")
    chart_text = "\n".join(chart_lines)

    # Parameter trajectory
    param_rows = "\n".join(
        f"| {r['iteration']} | {r['gamma']:.4f} | {r['beta']:.4f} | "
        f"{r['expectation_value']:.4f} | {r['approximation_ratio']:.4f} | "
        f"{r['best_cut_value']:.2f}/{graph['optimal_cut']:.2f} | "
        f"{'ZNE' if r.get('zne_applied') else ''}"
        f"{'·M3' if r.get('readout_mitigated') else ''}"
        f"{'·DD' if r.get('dd_applied') else ''} | {r['execution_time_s']:.2f}s |"
        for r in all_results
    )

    # Top bitstrings from best iteration
    best_idx = expectations.index(max(expectations))
    best_counts = all_results[best_idx]["top_counts"]
    top_bitstrings = "\n".join(
        f"| `{bs}` | {count} | {count / all_results[best_idx]['shots'] * 100:.1f}% |"
        for bs, count in sorted(best_counts.items(), key=lambda x: -x[1])[:8]
    )

    landscape = f"""
# Energy Landscape — QAOA MaxCut v2

## Convergence

```
{chart_text}
```

## Parameter Trajectory

| Iter | γ | β | ⟨C⟩ | Ratio | Cut | Mitigation | Time |
|------|---|---|-----|-------|-----|------------|------|
{param_rows}

## Top Bitstrings (Best Iteration {best_idx})

| Bitstring | Count | Probability |
|-----------|-------|-------------|
{top_bitstrings}
"""
    create_markdown_artifact(
        key="qaoa-v2-energy-landscape",
        markdown=landscape,
        description="QAOA v2 optimization convergence and parameter landscape",
    )
    logger.info("✅ Energy landscape artifact published")


@task(
    name="7.2 · Graph Visualization Artifact",
    tags=["stage:7-artifacts", "reporting"],
)
def publish_graph_artifact(graph: dict, analysis: dict) -> None:
    """
    Weighted graph SVG using actual node coordinates.
    Edge thickness ∝ weight. Node colors from QAOA partition.
    """
    logger = get_run_logger()

    n = graph["num_nodes"]
    edges = graph["edges"]
    weights = graph["weights"]
    coords = graph["coordinates"]
    best_bs = analysis["best_bitstring"]
    partition = [int(b) for b in reversed(best_bs)][:n]

    svg = _generate_weighted_graph_svg(n, edges, weights, coords, partition)

    # Adjacency with weights
    adj_lines = []
    for i in range(n):
        neighbors = []
        for (a, b) in edges:
            if a == i:
                neighbors.append(f"{b} (w={weights.get((a,b), 0):.3f})")
            elif b == i:
                neighbors.append(f"{a} (w={weights.get((a,b), 0):.3f})")
        adj_lines.append(f"  Node {i} @ ({coords[i][0]:.1f}, {coords[i][1]:.1f}) → [{', '.join(neighbors)}]")
    adj_text = "\n".join(adj_lines)

    cut_edges = [(i, j) for (i, j) in edges if partition[i] != partition[j]]
    cut_weight = sum(weights.get((i, j), 0) for (i, j) in cut_edges)

    graph_md = f"""
# Weighted Graph — QAOA MaxCut v2

## Graph Properties

| Property | Value |
|----------|-------|
| Nodes | {n} |
| Edges | {len(edges)} |
| Optimal MaxCut (weighted) | {graph['optimal_cut']:.4f} |
| QAOA Best Cut (weighted) | {analysis['best_cut_found']:.4f} |
| Approximation Ratio | {analysis['best_approximation_ratio']:.4f} |
| Error Mitigation | {analysis['mitigation_used']} |

## Node Coordinates & Adjacency

```
{adj_text}
```

## QAOA Partition

- **Set 0** (blue): nodes {[i for i in range(n) if partition[i] == 0]}
- **Set 1** (red):  nodes {[i for i in range(n) if partition[i] == 1]}
- **Cut edges** ({len(cut_edges)}): {cut_edges}  — total weight = {cut_weight:.4f}

## Graph Diagram

{svg}

*Node positions from input coordinates. Edge thickness ∝ weight. Red solid = cut edges, grey dashed = uncut.*
"""
    create_markdown_artifact(
        key="qaoa-v2-graph",
        markdown=graph_md,
        description="Weighted MaxCut graph with coordinates and QAOA partition",
    )
    logger.info("✅ Graph visualization artifact published")


def _generate_weighted_graph_svg(
    n: int,
    edges: list,
    weights: dict,
    coords: list,
    partition: list,
) -> str:
    """Generate SVG using actual node coordinates (scaled to fit)."""
    width, height = 500, 500
    padding = 60
    node_r = 22

    # Scale coordinates to fit SVG canvas
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    min_x, max_x = min(xs), max(xs)
    min_y, max_y = min(ys), max(ys)
    range_x = max_x - min_x if max_x != min_x else 1.0
    range_y = max_y - min_y if max_y != min_y else 1.0

    positions = []
    for c in coords:
        sx = padding + (c[0] - min_x) / range_x * (width - 2 * padding)
        # Flip Y axis (SVG y increases downward)
        sy = padding + (1.0 - (c[1] - min_y) / range_y) * (height - 2 * padding)
        positions.append((sx, sy))

    max_weight = max(weights.values()) if weights else 1.0

    lines = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}">'
    ]
    lines.append(f'<rect width="{width}" height="{height}" fill="#f8f9fa" rx="8"/>')

    # Title
    lines.append(
        f'<text x="{width//2}" y="25" text-anchor="middle" '
        f'font-family="Arial" font-size="14" font-weight="bold" fill="#333">'
        f'Weighted MaxCut — {n} nodes, {len(edges)} edges</text>'
    )

    # Draw edges (thickness proportional to weight)
    for (i, j) in edges:
        x1, y1 = positions[i]
        x2, y2 = positions[j]
        w = weights.get((i, j), 0.05)
        is_cut = partition[i] != partition[j]

        stroke_w = 1.0 + 4.0 * (w / max_weight)  # 1px to 5px
        color = "#e74c3c" if is_cut else "#bdc3c7"
        dash = "" if is_cut else 'stroke-dasharray="6,4"'
        opacity = "0.9" if is_cut else "0.5"

        lines.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{color}" stroke-width="{stroke_w:.1f}" {dash} opacity="{opacity}"/>'
        )

        # Weight label at midpoint
        mx = (x1 + x2) / 2
        my = (y1 + y2) / 2
        lines.append(
            f'<text x="{mx:.1f}" y="{my:.1f}" text-anchor="middle" '
            f'font-family="Arial" font-size="9" fill="#888">{w:.2f}</text>'
        )

    # Draw nodes
    for i in range(n):
        x, y = positions[i]
        color = "#3498db" if partition[i] == 0 else "#e74c3c"
        lines.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{node_r}" '
            f'fill="{color}" stroke="white" stroke-width="3"/>'
        )
        lines.append(
            f'<text x="{x:.1f}" y="{y + 5:.1f}" text-anchor="middle" '
            f'fill="white" font-family="Arial" font-size="14" font-weight="bold">{i}</text>'
        )

        # Coordinate label below node
        cx, cy = coords[i]
        lines.append(
            f'<text x="{x:.1f}" y="{y + node_r + 14:.1f}" text-anchor="middle" '
            f'font-family="Arial" font-size="8" fill="#999">({cx:.1f},{cy:.1f})</text>'
        )

    # Legend
    ly = height - 20
    lines.append(
        f'<text x="10" y="{ly}" font-family="Arial" font-size="10" fill="#666">'
        f'Blue=Set0  Red=Set1  |  Solid red=cut  Dashed grey=uncut  |  '
        f'Edge width ∝ weight</text>'
    )

    lines.append("</svg>")
    return "\n".join(lines)


@task(
    name="7.3 · Experiment Report Artifact",
    tags=["stage:7-artifacts", "reporting"],
)
def publish_experiment_report(
    graph: dict, analysis: dict, all_results: list[dict]
) -> None:
    """Comprehensive experiment report with mitigation summary."""
    logger = get_run_logger()

    n = graph["num_nodes"]
    coords = graph["coordinates"]
    mit = analysis["mitigation_used"]

    # Mitigation explanation
    mit_details = {
        "none": "No error mitigation applied. Raw QPU counts used directly.",
        "zne": "**Zero Noise Extrapolation (ZNE)**: Circuit executed at noise factors "
               "[1, 3, 5] via global unitary folding. Expectation values extrapolated "
               "to the zero-noise limit using linear regression.",
        "readout": "**M3 Readout Mitigation**: Measurement error calibration performed "
                   "on physical qubits. Quasi-probability distribution corrected using "
                   "matrix-free measurement mitigation.",
        "dd": "**Dynamical Decoupling (DD)**: XX pulse sequences inserted during idle "
              "periods of qubits to suppress coherent errors from unwanted interactions.",
        "all": "**Full mitigation stack**: DD (coherent error suppression during idle periods) "
               "+ ZNE (noise-extrapolated expectation values) "
               "+ M3 (measurement error correction).",
    }

    coord_table = "\n".join(
        f"| {i} | ({coords[i][0]:.2f}, {coords[i][1]:.2f}) |"
        for i in range(n)
    )

    total_qpu_time = sum(r["execution_time_s"] for r in all_results)

    report = f"""
# Experiment Report — QAOA MaxCut v2

## Problem Definition

| Parameter | Value |
|-----------|-------|
| Graph type | Distance-weighted (closer nodes = stronger coupling) |
| Nodes | {n} |
| Edges | {len(graph['edges'])} |
| Optimal MaxCut | {graph['optimal_cut']:.4f} |
| Error mitigation | {mit} |

## Node Coordinates

| Node | (x, y) |
|------|--------|
{coord_table}

## Error Mitigation Details

{mit_details.get(mit, "Unknown")}

## Results

| Metric | Value |
|--------|-------|
| Best ⟨C⟩ | {analysis['best_expectation']:.4f} |
| Best cut (weighted) | {analysis['best_cut_found']:.4f} |
| Approximation ratio | {analysis['best_approximation_ratio']:.4f} |
| Quality rating | **{analysis['quality']}** |
| Iterations | {analysis['total_iterations']} |
| Total QPU time | {total_qpu_time:.1f}s |

## Quality Assessment

{"🟢 EXCELLENT — QAOA found a near-optimal solution." if analysis['quality'] == "EXCELLENT" else ""}
{"🔵 GOOD — QAOA found a competitive solution." if analysis['quality'] == "GOOD" else ""}
{"🟡 FAIR — Solution is reasonable but leaves room for improvement. Consider increasing iterations or QAOA depth." if analysis['quality'] == "FAIR" else ""}
{"🔴 POOR — QAOA struggled on this instance. Potential causes: noise, insufficient iterations, or suboptimal landscape. Try enabling error mitigation or increasing p." if analysis['quality'] == "POOR" else ""}

---
*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*
"""
    create_markdown_artifact(
        key="qaoa-v2-experiment-report",
        markdown=report,
        description="QAOA v2 experiment report with mitigation details",
    )
    logger.info("✅ Experiment report artifact published")


# ═══════════════════════════════════════════════════════════════════════
# MAIN FLOW
# ═══════════════════════════════════════════════════════════════════════

@flow(
    name="QAOA MaxCut v2 · IQM Garnet",
    description=(
        "Distance-weighted QAOA MaxCut with user-configurable coordinates "
        "and selectable error mitigation (none/zne/readout/dd/all)."
    ),
    retries=0,
)
def qaoa_pipeline_v2(
    # ── Graph parameters (user-overridable at run time) ──
    num_nodes: int = 5,
    node_coordinates: list[list[float]] = DEFAULT_COORDINATES,
    # ── Error mitigation ──
    error_mitigation: str = "none",
    # ── QAOA parameters ──
    num_iterations: int = 6,
    shots: int = 4096,
    qaoa_depth: int = 1,
) -> dict:
    """
    QAOA MaxCut v2 pipeline.

    Parameters
    ----------
    num_nodes : int
        Number of graph nodes. Must match len(node_coordinates).
    node_coordinates : list of [x, y]
        2D positions for each node. Edge weights derived from distances.
        Default: 5 nodes in a pentagon layout.
    error_mitigation : str
        One of: "none", "zne", "readout", "dd", "all"
    num_iterations : int
        Number of (γ, β) parameter points to evaluate.
    shots : int
        Measurement shots per circuit execution.
    qaoa_depth : int
        QAOA circuit depth p (number of cost+mixer layers).
    """
    logger = get_run_logger()

    logger.info("=" * 60)
    logger.info("  QAOA MaxCut v2 · IQM Garnet")
    logger.info(f"  Nodes: {num_nodes}, Mitigation: {error_mitigation}")
    logger.info(f"  Iterations: {num_iterations}, Shots: {shots}, p={qaoa_depth}")
    logger.info("=" * 60)

    # ── Stage 1: Validate ──
    config = validate_inputs(num_nodes, node_coordinates, error_mitigation)

    # ── Stage 2: Build graph ──
    graph = build_weighted_graph(config)

    # ── Stages 3-5: QAOA optimization loop ──
    import math

    all_results = []

    # Grid search over (γ, β) parameter space
    num_gamma = max(2, int(math.sqrt(num_iterations)))
    num_beta = max(2, num_iterations // num_gamma)
    total_points = num_gamma * num_beta

    gamma_values = [
        math.pi * (g + 1) / (num_gamma + 1) for g in range(num_gamma)
    ]
    beta_values = [
        math.pi * (b + 1) / (2 * (num_beta + 1)) for b in range(num_beta)
    ]

    iteration = 0
    for gamma in gamma_values:
        for beta in beta_values:
            if iteration >= num_iterations:
                break

            logger.info(f"\n── Iteration {iteration}/{num_iterations} ──")

            # Build circuit
            circuit_meta = build_qaoa_circuit(
                graph, gamma=gamma, beta=beta, p=qaoa_depth
            )
            circuit_meta["_iteration"] = iteration

            # Transpile (+ DD if selected)
            transpiled = transpile_for_garnet(
                circuit_meta, error_mitigation=error_mitigation
            )
            transpiled["_iteration"] = iteration

            # Execute on QPU with mitigation
            result = execute_on_garnet(
                transpiled, graph, shots=shots,
                error_mitigation=error_mitigation,
            )

            all_results.append(result)
            iteration += 1

        if iteration >= num_iterations:
            break

    # ── Refinement pass: search around best point ──
    if all_results:
        best_so_far = max(all_results, key=lambda r: r["expectation_value"])
        g0, b0 = best_so_far["gamma"], best_so_far["beta"]
        delta = 0.15

        for dg, db in [(-delta, 0), (delta, 0), (0, -delta), (0, delta)]:
            if iteration >= num_iterations + 4:
                break

            g_ref = g0 + dg
            b_ref = b0 + db

            circuit_meta = build_qaoa_circuit(
                graph, gamma=g_ref, beta=b_ref, p=qaoa_depth
            )
            circuit_meta["_iteration"] = iteration

            transpiled = transpile_for_garnet(
                circuit_meta, error_mitigation=error_mitigation
            )
            transpiled["_iteration"] = iteration

            result = execute_on_garnet(
                transpiled, graph, shots=shots,
                error_mitigation=error_mitigation,
            )
            all_results.append(result)
            iteration += 1

    # ── Stage 6: Analysis ──
    analysis = analyze_results(all_results, graph)

    # ── Stage 7: Artifacts ──
    publish_energy_landscape(all_results, graph)
    publish_graph_artifact(graph, analysis)
    publish_experiment_report(graph, analysis, all_results)

    logger.info("\n" + "=" * 60)
    logger.info("  Pipeline complete!")
    logger.info(f"  Quality: {analysis['quality']}")
    logger.info(f"  Check Prefect dashboard → Artifacts tab")
    logger.info("=" * 60 + "\n")

    return analysis


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QAOA MaxCut v2 — IQM Garnet")
    parser.add_argument("--nodes", type=int, default=5)
    parser.add_argument(
        "--coordinates", type=str, default=None,
        help='JSON array of [x,y] pairs, e.g. \'[[0,0],[1,0],[0.5,1]]\'',
    )
    parser.add_argument(
        "--mitigation", type=str, default="none",
        choices=VALID_MITIGATION,
    )
    parser.add_argument("--iterations", type=int, default=6)
    parser.add_argument("--shots", type=int, default=4096)
    parser.add_argument("--depth", type=int, default=1)
    args = parser.parse_args()

    coords = DEFAULT_COORDINATES
    if args.coordinates:
        coords = json.loads(args.coordinates)

    # If user changed nodes but not coordinates, generate default coords
    if args.nodes != 5 and args.coordinates is None:
        import math as m
        coords = [
            [4.0 * m.cos(2 * m.pi * i / args.nodes),
             4.0 * m.sin(2 * m.pi * i / args.nodes)]
            for i in range(args.nodes)
        ]

    qaoa_pipeline_v2(
        num_nodes=args.nodes,
        node_coordinates=coords,
        error_mitigation=args.mitigation,
        num_iterations=args.iterations,
        shots=args.shots,
        qaoa_depth=args.depth,
    )
