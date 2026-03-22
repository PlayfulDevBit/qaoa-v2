"""
QAOA MaxCut Pipeline v2 · IQM Garnet — Prefect Serverless
==========================================================
Solves unweighted MaxCut using QAOA on IQM Garnet.

User-configurable:
  - Graph size, node coordinates, and edge list
  - Error mitigation technique (none / zne / readout / dd / all)

Produces Prefect artifacts:
  - SVG convergence chart (cut performance vs iteration)
  - Graph visualization with QAOA partition
  - Experiment report

Local testing:
    pip install prefect "qiskit>=1.0,<2.2" "iqm-client[qiskit]==33.0.5" networkx
    python qaoa_pipeline_v2.py

Serverless:
    python deploy_qaoa_v2.py
    IQM token from Prefect Secret block "iqm-resonance-token"
"""

import sys
import time
import math
import argparse
import json
import random
from datetime import datetime, timezone
from itertools import combinations
from typing import Optional

from prefect import flow, task, get_run_logger
from prefect.artifacts import create_markdown_artifact


# ═══════════════════════════════════════════════════════════════════════
# CONSTANTS & DEFAULTS
# ═══════════════════════════════════════════════════════════════════════

VALID_MITIGATION = ["none", "zne", "readout", "dd", "all"]


def _default_coordinates(n: int) -> list[list[float]]:
    """Generate circular layout coordinates for n nodes."""
    return [
        [round(4.0 * math.cos(2 * math.pi * i / n), 2),
         round(4.0 * math.sin(2 * math.pi * i / n), 2)]
        for i in range(n)
    ]


def _default_edges(n: int, probability: float = 0.6, seed: int = 42) -> list[list[int]]:
    """Generate random edges (Erdős–Rényi) with given probability."""
    rng = random.Random(seed)
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < probability:
                edges.append([i, j])
    # Ensure connected: if isolated nodes exist, connect them to nearest neighbor
    connected = set()
    for e in edges:
        connected.add(e[0])
        connected.add(e[1])
    for i in range(n):
        if i not in connected:
            j = (i + 1) % n
            edges.append([i, j])
            connected.add(i)
    return edges


# Defaults for a 5-node graph
DEFAULT_NUM_NODES = 5
DEFAULT_COORDINATES = _default_coordinates(5)
DEFAULT_EDGES = _default_edges(5)


# ═══════════════════════════════════════════════════════════════════════
# TOKEN
# ═══════════════════════════════════════════════════════════════════════

def get_iqm_token() -> str:
    """Read IQM token from Prefect Secret block."""
    try:
        from prefect.blocks.system import Secret
        secret = Secret.load("iqm-resonance-token")
        return secret.get()
    except Exception:
        return ""


def _get_garnet_backend():
    """Initialize and return IQM Garnet backend."""
    import os
    token = get_iqm_token()
    if not token:
        raise RuntimeError(
            "No IQM token found. Create a Prefect Secret block named "
            "'iqm-resonance-token' with your IQM Resonance API token."
        )
    os.environ["IQM_TOKEN"] = token
    from iqm.qiskit_iqm import IQMProvider
    provider = IQMProvider("https://cocos.resonance.meetiqm.com/garnet")
    return provider.get_backend()


# ═══════════════════════════════════════════════════════════════════════
# STAGE 1 — VALIDATE INPUTS
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="1 · Validate Inputs",
    tags=["stage:1-validate", "infra:cpu"],
)
def validate_inputs(
    num_nodes: int,
    node_coordinates: list[list[float]],
    edge_list: list[list[int]],
    error_mitigation: str,
) -> dict:
    """
    Validates:
    - num_nodes == len(node_coordinates)
    - All node indices in edge_list are < num_nodes
    - error_mitigation is valid
    - Coordinates are valid [x, y] pairs
    """
    logger = get_run_logger()

    # Mitigation
    if error_mitigation not in VALID_MITIGATION:
        raise ValueError(
            f"Invalid error_mitigation='{error_mitigation}'. "
            f"Must be one of: {VALID_MITIGATION}"
        )

    # Size / coordinate match
    if len(node_coordinates) != num_nodes:
        raise ValueError(
            f"Mismatch: num_nodes={num_nodes} but "
            f"len(node_coordinates)={len(node_coordinates)}. "
            f"These must be equal."
        )

    # Coordinate format
    for idx, coord in enumerate(node_coordinates):
        if not isinstance(coord, (list, tuple)) or len(coord) != 2:
            raise ValueError(f"node_coordinates[{idx}] = {coord} is not a valid [x, y] pair.")
        try:
            float(coord[0])
            float(coord[1])
        except (TypeError, ValueError):
            raise ValueError(f"node_coordinates[{idx}] = {coord} contains non-numeric values.")

    # Edge list validation
    for idx, edge in enumerate(edge_list):
        if not isinstance(edge, (list, tuple)) or len(edge) != 2:
            raise ValueError(f"edge_list[{idx}] = {edge} is not a valid [i, j] pair.")
        i, j = int(edge[0]), int(edge[1])
        if i < 0 or i >= num_nodes or j < 0 or j >= num_nodes:
            raise ValueError(
                f"edge_list[{idx}] = [{i}, {j}] references node outside "
                f"range 0..{num_nodes - 1}."
            )
        if i == j:
            raise ValueError(f"edge_list[{idx}] = [{i}, {j}] is a self-loop.")

    # Deduplicate edges (normalize direction)
    seen = set()
    clean_edges = []
    for edge in edge_list:
        key = (min(int(edge[0]), int(edge[1])), max(int(edge[0]), int(edge[1])))
        if key not in seen:
            seen.add(key)
            clean_edges.append(list(key))

    logger.info(f"✅ Inputs valid: {num_nodes} nodes, {len(clean_edges)} edges, "
                f"mitigation='{error_mitigation}'")

    return {
        "num_nodes": num_nodes,
        "coordinates": [[float(c[0]), float(c[1])] for c in node_coordinates],
        "edges": clean_edges,
        "error_mitigation": error_mitigation,
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 2 — BUILD GRAPH + BRUTE-FORCE OPTIMAL
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="2 · Build Graph",
    tags=["stage:2-graph", "infra:cpu"],
)
def build_graph(config: dict) -> dict:
    """
    Builds the unweighted graph and brute-forces the optimal MaxCut.
    """
    logger = get_run_logger()

    n = config["num_nodes"]
    edges = [tuple(e) for e in config["edges"]]

    logger.info(f"Graph: {n} nodes, {len(edges)} edges")
    for (i, j) in edges:
        logger.info(f"  Edge ({i}, {j})")

    # Brute-force optimal MaxCut
    best_cut = 0
    best_partition = [0] * n

    for mask in range(1 << n):
        partition = [(mask >> k) & 1 for k in range(n)]
        cut = sum(1 for (i, j) in edges if partition[i] != partition[j])
        if cut > best_cut:
            best_cut = cut
            best_partition = partition[:]

    logger.info(f"Optimal MaxCut = {best_cut}")
    logger.info(f"Optimal partition: {best_partition}")

    return {
        "num_nodes": n,
        "edges": edges,
        "coordinates": config["coordinates"],
        "optimal_cut": best_cut,
        "optimal_partition": best_partition,
        "error_mitigation": config["error_mitigation"],
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 3 — QAOA CIRCUIT
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
    QAOA circuit for unweighted MaxCut.
    Cost: exp(-i * gamma * Z_i Z_j) per edge
    Mixer: exp(-i * beta * X_i) per qubit
    """
    logger = get_run_logger()
    from qiskit import QuantumCircuit

    n = graph["num_nodes"]
    edges = graph["edges"]

    qc = QuantumCircuit(n)

    # Initial superposition
    for i in range(n):
        qc.h(i)

    # QAOA layers
    for layer in range(p):
        # Cost unitary — ZZ per edge
        for (i, j) in edges:
            qc.cx(i, j)
            qc.rz(2 * gamma, j)
            qc.cx(i, j)

        # Mixer unitary — RX per qubit
        for i in range(n):
            qc.rx(2 * beta, i)

    qc.measure_all()

    logger.info(f"QAOA circuit (p={p}): γ={gamma:.4f}, β={beta:.4f}, "
                f"gates={qc.size()}, depth={qc.depth()}")

    return {
        "num_qubits": n,
        "p": p,
        "gamma": gamma,
        "beta": beta,
        "gate_count": qc.size(),
        "depth": qc.depth(),
        "_circuit_obj": qc,
    }


# ═══════════════════════════════════════════════════════════════════════
# STAGE 4 — TRANSPILE (against real Garnet backend)
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="4 · Transpile for IQM Garnet",
    tags=["stage:4-transpile", "infra:cpu"],
)
def transpile_for_garnet(circuit_meta: dict, error_mitigation: str) -> dict:
    """
    Transpile against the real IQM Garnet backend to respect its coupling map.
    Optionally insert dynamical decoupling sequences.
    """
    logger = get_run_logger()
    from qiskit import transpile as qk_transpile

    qc = circuit_meta["_circuit_obj"]
    backend = _get_garnet_backend()

    transpiled = qk_transpile(
        qc,
        backend=backend,
        optimization_level=2,
        seed_transpiler=42,
    )
    logger.info(f"  Transpiled against Garnet: gates={transpiled.size()}, "
                f"depth={transpiled.depth()}")

    # Dynamical Decoupling
    dd_applied = False
    if error_mitigation in ("dd", "all"):
        try:
            from qiskit.transpiler import PassManager
            from qiskit.transpiler.passes import PadDynamicalDecoupling
            from qiskit.circuit.library import XGate

            dd_pass = PadDynamicalDecoupling(
                durations=None,
                dd_sequence=[XGate(), XGate()],
            )
            transpiled = PassManager([dd_pass]).run(transpiled)
            dd_applied = True
            logger.info("  DD: XX sequence applied")
        except Exception as e:
            logger.warning(f"  DD: Could not apply: {e}")

    result = {
        **circuit_meta,
        "transpiled_gate_count": transpiled.size(),
        "transpiled_depth": transpiled.depth(),
        "dd_applied": dd_applied,
        "_transpiled_obj": transpiled,
    }
    result.pop("_circuit_obj", None)
    return result


# ═══════════════════════════════════════════════════════════════════════
# STAGE 5 — EXECUTE ON QPU
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
    Execute on IQM Garnet with optional error mitigation.
    """
    logger = get_run_logger()

    backend = _get_garnet_backend()
    qc = transpiled_meta["_transpiled_obj"]
    n = transpiled_meta["num_qubits"]
    edges = graph["edges"]

    t_start = time.time()

    # Base execution
    raw_counts = _run_circuit(backend, qc, shots)

    # ZNE
    zne_expectation = None
    if error_mitigation in ("zne", "all"):
        zne_expectation = _apply_zne(backend, qc, shots, n, edges, logger)

    # Readout mitigation
    mitigated_counts = raw_counts
    if error_mitigation in ("readout", "all"):
        mitigated_counts = _apply_readout_mitigation(
            backend, raw_counts, n, shots, logger
        )

    t_elapsed = time.time() - t_start

    # Expectation value
    counts_for_eval = mitigated_counts
    expectation = _compute_expectation(counts_for_eval, edges, n)

    if zne_expectation is not None:
        logger.info(f"  ZNE expectation: {zne_expectation:.4f} (raw: {expectation:.4f})")
        expectation = zne_expectation

    # Best bitstring
    best_bs = max(counts_for_eval, key=counts_for_eval.get)
    best_partition = [int(b) for b in reversed(best_bs)][:n]
    best_cut = sum(1 for (i, j) in edges if best_partition[i] != best_partition[j])

    approx_ratio = best_cut / graph["optimal_cut"] if graph["optimal_cut"] > 0 else 0.0

    logger.info(f"  ⟨C⟩={expectation:.4f}, best_cut={best_cut}/{graph['optimal_cut']}, "
                f"ratio={approx_ratio:.4f}, time={t_elapsed:.1f}s")

    top_counts = dict(sorted(counts_for_eval.items(), key=lambda x: -x[1])[:10])

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


# ───────────────────────────────────────────────────────────────────────
# Execution helpers
# ───────────────────────────────────────────────────────────────────────

def _run_circuit(backend, qc, shots):
    """Run circuit, return counts dict."""
    job = backend.run(qc, shots=shots)
    return dict(job.result().get_counts())


def _compute_expectation(counts, edges, n):
    """⟨C⟩ for unweighted MaxCut."""
    total = sum(counts.values())
    exp = 0.0
    for bitstring, count in counts.items():
        bits = [int(b) for b in reversed(bitstring)][:n]
        cut = sum(1 for (i, j) in edges if bits[i] != bits[j])
        exp += cut * count
    return exp / total


def _apply_zne(backend, qc, shots, n, edges, logger):
    """
    Zero Noise Extrapolation via global unitary folding.
    Runs circuit at noise factors [1, 3, 5], extrapolates to zero.
    """
    try:
        import numpy as np

        noise_factors = [1.0, 3.0, 5.0]
        expectations = []

        for factor in noise_factors:
            if factor == 1.0:
                circuit = qc
            else:
                circuit = _fold_circuit(qc, int(factor))

            counts = _run_circuit(backend, circuit, shots)
            exp = _compute_expectation(counts, edges, n)
            expectations.append(exp)
            logger.info(f"    ZNE factor={factor:.0f}: ⟨C⟩={exp:.4f}")

        # Linear extrapolation to zero noise
        x = np.array(noise_factors)
        y = np.array(expectations)
        coeffs = np.polyfit(x, y, 1)
        zne_val = float(np.polyval(coeffs, 0.0))
        logger.info(f"    ZNE extrapolated: ⟨C⟩={zne_val:.4f}")
        return zne_val

    except Exception as e:
        logger.warning(f"    ZNE failed: {e}")
        return None


def _fold_circuit(qc, scale_factor):
    """Global unitary folding: U → U (U† U)^((k-1)/2)."""
    qc_no_meas = qc.remove_final_measurements(inplace=False)
    inverse = qc_no_meas.inverse()

    folded = qc_no_meas.copy()
    for _ in range((scale_factor - 1) // 2):
        folded.compose(inverse, inplace=True)
        folded.compose(qc_no_meas, inplace=True)

    folded.measure_all()
    return folded


def _apply_readout_mitigation(backend, raw_counts, n, shots, logger):
    """
    Readout error mitigation using calibration circuits.

    Runs all-0 and all-1 calibration circuits to measure per-qubit
    readout fidelity P(0|0) and P(1|1), then applies a simple
    inverse correction to the output distribution.

    This replaces mthree/M3 which requires backend.configuration()
    that IQMBackend does not provide.
    """
    try:
        from qiskit import QuantumCircuit
        import numpy as np

        logger.info("    Readout: Running calibration circuits...")

        # Calibration: prepare |00...0⟩ and measure
        cal0 = QuantumCircuit(n, n)
        cal0.measure(list(range(n)), list(range(n)))

        # Calibration: prepare |11...1⟩ and measure
        cal1 = QuantumCircuit(n, n)
        for i in range(n):
            cal1.x(i)
        cal1.measure(list(range(n)), list(range(n)))

        cal_shots = min(shots, 2048)
        counts_0 = _run_circuit(backend, cal0, cal_shots)
        counts_1 = _run_circuit(backend, cal1, cal_shots)

        # Per-qubit error rates
        p0_given_0 = []
        p1_given_1 = []

        for q in range(n):
            # From all-zeros: how often does qubit q read 0?
            n0_correct = 0
            for bs, cnt in counts_0.items():
                bits = list(reversed(bs))
                if q < len(bits) and bits[q] == '0':
                    n0_correct += cnt
            p0_given_0.append(n0_correct / cal_shots)

            # From all-ones: how often does qubit q read 1?
            n1_correct = 0
            for bs, cnt in counts_1.items():
                bits = list(reversed(bs))
                if q < len(bits) and bits[q] == '1':
                    n1_correct += cnt
            p1_given_1.append(n1_correct / cal_shots)

        logger.info(f"    Readout P(0|0): {[f'{p:.3f}' for p in p0_given_0]}")
        logger.info(f"    Readout P(1|1): {[f'{p:.3f}' for p in p1_given_1]}")

        # Apply correction
        total_raw = sum(raw_counts.values())
        corrected = {}

        for bitstring, count in raw_counts.items():
            bits = list(reversed(bitstring))
            correction = 1.0
            for q in range(min(n, len(bits))):
                if bits[q] == '0':
                    if p0_given_0[q] > 0.1:
                        correction *= (1.0 / p0_given_0[q])
                else:
                    if p1_given_1[q] > 0.1:
                        correction *= (1.0 / p1_given_1[q])
            corrected[bitstring] = count * correction

        # Renormalize to integer counts
        total_corrected = sum(corrected.values())
        if total_corrected > 0:
            scale = total_raw / total_corrected
            mitigated = {
                bs: max(1, int(round(cnt * scale)))
                for bs, cnt in corrected.items()
                if cnt * scale >= 0.5
            }
        else:
            mitigated = raw_counts

        logger.info(f"    Readout: Corrected {len(mitigated)} bitstrings")
        return mitigated

    except Exception as e:
        logger.warning(f"    Readout mitigation failed: {e}. Using raw counts.")
        return raw_counts


# ═══════════════════════════════════════════════════════════════════════
# STAGE 6 — ANALYSIS
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="6 · Analyze Results",
    tags=["stage:6-analysis", "infra:cpu"],
)
def analyze_results(all_results: list[dict], graph: dict) -> dict:
    """Pick best iteration, compute summary."""
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

    logger.info(f"━━━ Result: {analysis['quality']} ━━━")
    logger.info(f"  Best ⟨C⟩={analysis['best_expectation']:.4f}, "
                f"cut={analysis['best_cut_found']}/{analysis['optimal_cut']}, "
                f"ratio={analysis['best_approximation_ratio']:.4f}")

    return analysis


# ═══════════════════════════════════════════════════════════════════════
# STAGE 7 — ARTIFACTS
# ═══════════════════════════════════════════════════════════════════════

@task(
    name="7.1 · Convergence Chart (SVG)",
    tags=["stage:7-artifacts", "reporting"],
)
def publish_convergence_chart(all_results: list[dict], graph: dict) -> None:
    """
    SVG chart: x = iteration, y = cut value.
    Blue line = QAOA running best. Green dashed = optimal.
    Light blue dots = per-iteration cut.
    """
    logger = get_run_logger()

    optimal = graph["optimal_cut"]
    n_iter = len(all_results)
    cut_values = [r["best_cut_value"] for r in all_results]

    # Running best
    running_best = []
    best_so_far = 0
    for c in cut_values:
        best_so_far = max(best_so_far, c)
        running_best.append(best_so_far)

    # SVG canvas
    w, h = 600, 350
    pl, pr, pt, pb = 60, 30, 40, 50
    pw = w - pl - pr
    ph = h - pt - pb

    y_max = optimal + 1
    y_min = 0

    def sx(i):
        if n_iter <= 1:
            return pl + pw / 2
        return pl + (i / (n_iter - 1)) * pw

    def sy(val):
        if y_max == y_min:
            return pt + ph / 2
        return pt + (1.0 - (val - y_min) / (y_max - y_min)) * ph

    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
           f'viewBox="0 0 {w} {h}">']
    svg.append(f'<rect width="{w}" height="{h}" fill="#fafafa" rx="6"/>')

    # Title
    svg.append(f'<text x="{w//2}" y="22" text-anchor="middle" '
               f'font-family="Arial" font-size="14" font-weight="bold" '
               f'fill="#333">QAOA Convergence — Cut Performance vs Iteration</text>')

    # Horizontal grid
    for val in range(0, int(y_max) + 1):
        y = sy(val)
        svg.append(f'<line x1="{pl}" y1="{y:.1f}" x2="{w - pr}" y2="{y:.1f}" '
                   f'stroke="#e0e0e0" stroke-width="1"/>')
        svg.append(f'<text x="{pl - 8}" y="{y + 4:.1f}" text-anchor="end" '
                   f'font-family="Arial" font-size="11" fill="#666">{val}</text>')

    # Axes
    svg.append(f'<line x1="{pl}" y1="{pt}" x2="{pl}" y2="{h - pb}" '
               f'stroke="#333" stroke-width="1.5"/>')
    svg.append(f'<line x1="{pl}" y1="{h - pb}" x2="{w - pr}" y2="{h - pb}" '
               f'stroke="#333" stroke-width="1.5"/>')

    # X labels
    for i in range(n_iter):
        x = sx(i)
        svg.append(f'<text x="{x:.1f}" y="{h - pb + 18}" text-anchor="middle" '
                   f'font-family="Arial" font-size="10" fill="#666">{i}</text>')

    # Axis titles
    svg.append(f'<text x="{w//2}" y="{h - 5}" text-anchor="middle" '
               f'font-family="Arial" font-size="11" fill="#555">Iteration</text>')
    svg.append(f'<text x="14" y="{h//2}" text-anchor="middle" '
               f'font-family="Arial" font-size="11" fill="#555" '
               f'transform="rotate(-90, 14, {h//2})">Cut Value</text>')

    # Optimal line (green dashed)
    oy = sy(optimal)
    svg.append(f'<line x1="{pl}" y1="{oy:.1f}" x2="{w - pr}" y2="{oy:.1f}" '
               f'stroke="#27ae60" stroke-width="2" stroke-dasharray="8,4"/>')
    svg.append(f'<text x="{w - pr + 2}" y="{oy + 4:.1f}" '
               f'font-family="Arial" font-size="10" fill="#27ae60">Optimal ({optimal})</text>')

    # Per-iteration dots (light blue)
    for i, val in enumerate(cut_values):
        x, y = sx(i), sy(val)
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                   f'fill="#85c1e9" stroke="#2980b9" stroke-width="1"/>')

    # Running best line (blue solid)
    if n_iter > 1:
        pts = " ".join(f"{sx(i):.1f},{sy(v):.1f}" for i, v in enumerate(running_best))
        svg.append(f'<polyline points="{pts}" fill="none" '
                   f'stroke="#2980b9" stroke-width="2.5"/>')

    # Running best dots
    for i, val in enumerate(running_best):
        x, y = sx(i), sy(val)
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" '
                   f'fill="#2980b9" stroke="white" stroke-width="1.5"/>')

    # Legend
    lx, ly = pl + 10, pt + 15
    svg.append(f'<line x1="{lx}" y1="{ly}" x2="{lx+20}" y2="{ly}" '
               f'stroke="#27ae60" stroke-width="2" stroke-dasharray="6,3"/>')
    svg.append(f'<text x="{lx+25}" y="{ly+4}" font-family="Arial" '
               f'font-size="10" fill="#555">Theoretical Optimal</text>')
    svg.append(f'<line x1="{lx}" y1="{ly+16}" x2="{lx+20}" y2="{ly+16}" '
               f'stroke="#2980b9" stroke-width="2.5"/>')
    svg.append(f'<text x="{lx+25}" y="{ly+20}" font-family="Arial" '
               f'font-size="10" fill="#555">QAOA Best-so-far</text>')
    svg.append(f'<circle cx="{lx+10}" cy="{ly+32}" r="3.5" '
               f'fill="#85c1e9" stroke="#2980b9" stroke-width="1"/>')
    svg.append(f'<text x="{lx+25}" y="{ly+36}" font-family="Arial" '
               f'font-size="10" fill="#555">Per-iteration cut</text>')

    svg.append("</svg>")

    md = f"""
# Convergence — QAOA MaxCut v2

{chr(10).join(svg)}

## Summary

| Metric | Value |
|--------|-------|
| Iterations | {n_iter} |
| Optimal cut | {optimal} |
| Best QAOA cut | {max(cut_values)} |
| Approximation ratio | {max(cut_values) / optimal if optimal > 0 else 0:.4f} |
| Reached optimal? | {'✅ Yes' if max(cut_values) >= optimal else '❌ No'} |
"""
    create_markdown_artifact(
        key="qaoa-v2-convergence",
        markdown=md,
        description="QAOA convergence chart — cut performance vs iteration",
    )
    logger.info("✅ Convergence chart published")


@task(
    name="7.2 · Graph Visualization",
    tags=["stage:7-artifacts", "reporting"],
)
def publish_graph_artifact(graph: dict, analysis: dict) -> None:
    """Graph SVG with QAOA partition coloring."""
    logger = get_run_logger()

    n = graph["num_nodes"]
    edges = graph["edges"]
    coords = graph["coordinates"]
    best_bs = analysis["best_bitstring"]
    partition = [int(b) for b in reversed(best_bs)][:n]

    svg = _graph_svg(n, edges, coords, partition)

    adj_lines = []
    for i in range(n):
        neighbors = []
        for (a, b) in edges:
            if a == i:
                neighbors.append(str(b))
            elif b == i:
                neighbors.append(str(a))
        adj_lines.append(f"  Node {i} @ ({coords[i][0]:.1f}, {coords[i][1]:.1f}) → "
                         f"[{', '.join(neighbors)}]")

    cut_edges = [(i, j) for (i, j) in edges if partition[i] != partition[j]]

    md = f"""
# Graph — QAOA MaxCut v2

{svg}

## Structure

| Property | Value |
|----------|-------|
| Nodes | {n} |
| Edges | {len(edges)} |
| Optimal MaxCut | {graph['optimal_cut']} |
| QAOA Best Cut | {analysis['best_cut_found']} |

## Adjacency

```
{chr(10).join(adj_lines)}
```

## QAOA Partition

- **Set 0** (blue): {[i for i in range(n) if partition[i] == 0]}
- **Set 1** (red): {[i for i in range(n) if partition[i] == 1]}
- **Cut edges** ({len(cut_edges)}): {cut_edges}

*Blue=Set 0, Red=Set 1. Solid red lines=cut, dashed grey=uncut.*
"""
    create_markdown_artifact(
        key="qaoa-v2-graph",
        markdown=md,
        description="MaxCut graph with QAOA partition",
    )
    logger.info("✅ Graph artifact published")


def _graph_svg(n, edges, coords, partition):
    """SVG of the graph using node coordinates."""
    w, h = 450, 450
    pad = 55
    nr = 20

    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    mnx, mxx = min(xs), max(xs)
    mny, mxy = min(ys), max(ys)
    rx = mxx - mnx if mxx != mnx else 1.0
    ry = mxy - mny if mxy != mny else 1.0

    def pos(c):
        return (pad + (c[0] - mnx) / rx * (w - 2 * pad),
                pad + (1.0 - (c[1] - mny) / ry) * (h - 2 * pad))

    positions = [pos(c) for c in coords]
    lines = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
             f'viewBox="0 0 {w} {h}">']
    lines.append(f'<rect width="{w}" height="{h}" fill="#f8f9fa" rx="8"/>')

    for (i, j) in edges:
        x1, y1 = positions[i]
        x2, y2 = positions[j]
        cut = partition[i] != partition[j]
        col = "#e74c3c" if cut else "#bdc3c7"
        sw = 2.5 if cut else 1.5
        dash = "" if cut else 'stroke-dasharray="6,4"'
        lines.append(f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
                     f'stroke="{col}" stroke-width="{sw}" {dash}/>')

    for i in range(n):
        x, y = positions[i]
        col = "#3498db" if partition[i] == 0 else "#e74c3c"
        lines.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{nr}" '
                     f'fill="{col}" stroke="white" stroke-width="3"/>')
        lines.append(f'<text x="{x:.1f}" y="{y + 5:.1f}" text-anchor="middle" '
                     f'fill="white" font-family="Arial" font-size="14" '
                     f'font-weight="bold">{i}</text>')

    lines.append("</svg>")
    return "\n".join(lines)


@task(
    name="7.3 · Experiment Report",
    tags=["stage:7-artifacts", "reporting"],
)
def publish_report(graph: dict, analysis: dict, all_results: list[dict]) -> None:
    """Experiment report artifact."""
    logger = get_run_logger()

    n = graph["num_nodes"]
    mit = analysis["mitigation_used"]

    mit_desc = {
        "none": "No error mitigation. Raw QPU counts.",
        "zne": "Zero Noise Extrapolation — circuit run at noise factors [1, 3, 5], "
               "extrapolated to zero noise via linear fit.",
        "readout": "Readout Error Mitigation — calibration circuits (all-0 and all-1) "
                   "measure per-qubit readout fidelity, then correct output distribution.",
        "dd": "Dynamical Decoupling — XX pulse sequences inserted during idle qubit "
              "periods to suppress coherent errors.",
        "all": "Full stack: DD + ZNE + Readout mitigation.",
    }

    total_time = sum(r["execution_time_s"] for r in all_results)

    coord_table = "\n".join(
        f"| {i} | ({graph['coordinates'][i][0]:.1f}, {graph['coordinates'][i][1]:.1f}) |"
        for i in range(n)
    )
    edge_table = "\n".join(f"| {i} | {j} |" for (i, j) in graph["edges"])

    quality_msg = {
        "EXCELLENT": "🟢 Near-optimal solution found.",
        "GOOD": "🔵 Competitive solution.",
        "FAIR": "🟡 Reasonable. Consider more iterations or higher depth.",
        "POOR": "🔴 QAOA struggled. Try enabling error mitigation or increasing p.",
    }

    param_rows = "\n".join(
        f"| {r['iteration']} | {r['gamma']:.4f} | {r['beta']:.4f} | "
        f"{r['expectation_value']:.4f} | {r['best_cut_value']}/{graph['optimal_cut']} | "
        f"{r['approximation_ratio']:.4f} | {r['execution_time_s']:.1f}s |"
        for r in all_results
    )

    report = f"""
# Experiment Report — QAOA MaxCut v2

## Problem

| | |
|-|-|
| Nodes | {n} |
| Edges | {len(graph['edges'])} |
| Optimal MaxCut | {graph['optimal_cut']} |
| Mitigation | {mit} |
| Iterations | {analysis['total_iterations']} |
| Shots | {all_results[0]['shots'] if all_results else '?'} |

## Coordinates

| Node | (x, y) |
|------|--------|
{coord_table}

## Edge List

| From | To |
|------|----|
{edge_table}

## Mitigation Details

{mit_desc.get(mit, "Unknown")}

## Parameter Sweep

| Iter | γ | β | ⟨C⟩ | Cut | Ratio | Time |
|------|---|---|-----|-----|-------|------|
{param_rows}

## Result

| | |
|-|-|
| Best ⟨C⟩ | {analysis['best_expectation']:.4f} |
| Best cut | {analysis['best_cut_found']}/{analysis['optimal_cut']} |
| Approximation ratio | {analysis['best_approximation_ratio']:.4f} |
| Quality | **{analysis['quality']}** |
| Total QPU time | {total_time:.1f}s |

{quality_msg.get(analysis['quality'], '')}

---
*Generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*
"""
    create_markdown_artifact(
        key="qaoa-v2-report",
        markdown=report,
        description="QAOA v2 experiment report",
    )
    logger.info("✅ Report published")


# ═══════════════════════════════════════════════════════════════════════
# MAIN FLOW
# ═══════════════════════════════════════════════════════════════════════

@flow(
    name="QAOA MaxCut v2 · IQM Garnet",
    description=(
        "Unweighted QAOA MaxCut with configurable graph (size, coordinates, edges) "
        "and selectable error mitigation (none/zne/readout/dd/all)."
    ),
    retries=0,
)
def qaoa_pipeline_v2(
    num_nodes: int = DEFAULT_NUM_NODES,
    node_coordinates: list[list[float]] = DEFAULT_COORDINATES,
    edge_list: list[list[int]] = DEFAULT_EDGES,
    error_mitigation: str = "none",
    num_iterations: int = 6,
    shots: int = 4096,
    qaoa_depth: int = 1,
) -> dict:
    """
    QAOA MaxCut v2 — configurable graph + error mitigation.

    Parameters
    ----------
    num_nodes : int
        Number of nodes. Must match len(node_coordinates).
    node_coordinates : list of [x, y]
        2D positions for visualization. Default: circular layout.
    edge_list : list of [i, j]
        Which node pairs are connected. Default: random ~60%.
    error_mitigation : str
        "none", "zne", "readout", "dd", or "all"
    num_iterations : int
        Number of (γ, β) points to evaluate.
    shots : int
        Measurement shots per circuit.
    qaoa_depth : int
        QAOA circuit depth p.
    """
    logger = get_run_logger()

    logger.info("=" * 60)
    logger.info("  QAOA MaxCut v2 · IQM Garnet")
    logger.info(f"  {num_nodes} nodes, {len(edge_list)} edges, "
                f"mitigation={error_mitigation}")
    logger.info("=" * 60)

    # Stage 1
    config = validate_inputs(num_nodes, node_coordinates, edge_list, error_mitigation)

    # Stage 2
    graph = build_graph(config)

    # Stages 3-5: QAOA loop
    all_results = []

    num_gamma = max(2, int(math.sqrt(num_iterations)))
    num_beta = max(2, num_iterations // num_gamma)

    gamma_vals = [math.pi * (g + 1) / (num_gamma + 1) for g in range(num_gamma)]
    beta_vals = [math.pi * (b + 1) / (2 * (num_beta + 1)) for b in range(num_beta)]

    iteration = 0
    for gamma in gamma_vals:
        for beta in beta_vals:
            if iteration >= num_iterations:
                break

            circuit = build_qaoa_circuit(graph, gamma=gamma, beta=beta, p=qaoa_depth)
            circuit["_iteration"] = iteration

            transpiled = transpile_for_garnet(circuit, error_mitigation)
            transpiled["_iteration"] = iteration

            result = execute_on_garnet(transpiled, graph, shots, error_mitigation)
            all_results.append(result)
            iteration += 1

        if iteration >= num_iterations:
            break

    # Refinement around best point
    if all_results:
        best = max(all_results, key=lambda r: r["expectation_value"])
        g0, b0 = best["gamma"], best["beta"]
        delta = 0.15

        for dg, db in [(-delta, 0), (delta, 0), (0, -delta), (0, delta)]:
            circuit = build_qaoa_circuit(graph, gamma=g0+dg, beta=b0+db, p=qaoa_depth)
            circuit["_iteration"] = iteration

            transpiled = transpile_for_garnet(circuit, error_mitigation)
            transpiled["_iteration"] = iteration

            result = execute_on_garnet(transpiled, graph, shots, error_mitigation)
            all_results.append(result)
            iteration += 1

    # Stage 6
    analysis = analyze_results(all_results, graph)

    # Stage 7
    publish_convergence_chart(all_results, graph)
    publish_graph_artifact(graph, analysis)
    publish_report(graph, analysis, all_results)

    logger.info("\n" + "=" * 60)
    logger.info(f"  Done! Quality: {analysis['quality']}")
    logger.info("  Check Prefect → Artifacts tab")
    logger.info("=" * 60)

    return analysis


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="QAOA MaxCut v2 — IQM Garnet")
    parser.add_argument("--nodes", type=int, default=5)
    parser.add_argument("--coordinates", type=str, default=None,
                        help='JSON: [[0,0],[1,0],...]')
    parser.add_argument("--edges", type=str, default=None,
                        help='JSON: [[0,1],[1,2],...]')
    parser.add_argument("--mitigation", type=str, default="none",
                        choices=VALID_MITIGATION)
    parser.add_argument("--iterations", type=int, default=6)
    parser.add_argument("--shots", type=int, default=4096)
    parser.add_argument("--depth", type=int, default=1)
    args = parser.parse_args()

    coords = json.loads(args.coordinates) if args.coordinates else _default_coordinates(args.nodes)
    edges = json.loads(args.edges) if args.edges else _default_edges(args.nodes)

    qaoa_pipeline_v2(
        num_nodes=args.nodes,
        node_coordinates=coords,
        edge_list=edges,
        error_mitigation=args.mitigation,
        num_iterations=args.iterations,
        shots=args.shots,
        qaoa_depth=args.depth,
    )
