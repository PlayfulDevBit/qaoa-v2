# QAOA MaxCut v2 · IQM Garnet

Solve the [MaxCut problem](https://en.wikipedia.org/wiki/Maximum_cut) on a real quantum processor using QAOA (Quantum Approximate Optimization Algorithm), deployed as a managed pipeline on Prefect Cloud.

## What this does

**MaxCut**: Given a graph, split the nodes into two groups so that the maximum number of edges cross between the groups. It's NP-hard — brute force becomes impossible as the graph grows.

**QAOA**: A quantum algorithm that encodes MaxCut into a parameterized quantum circuit. It runs the circuit at different parameter values (γ, β), measures the output, and converges toward the best partition.

**This pipeline**: Runs the full QAOA workflow on IQM Garnet (a 20-qubit superconducting QPU), with configurable graph definition and optional error mitigation. Results are published as visual artifacts in the Prefect dashboard.

---

## Files

| File | Purpose |
|------|---------|
| `qaoa_pipeline_v2.py` | The pipeline — all stages from validation to artifact generation |
| `deploy_qaoa_v2.py` | Registers the pipeline as a Prefect Cloud deployment |
| `README.md` | This file |

---

## Quick start

### 1. Prerequisites

- Prefect Cloud account with a managed work pool
- IQM Resonance API token
- GitHub repo to host the pipeline code

### 2. Store your IQM token

In Prefect Cloud UI: **Blocks → + → Secret**
- Name: `iqm-resonance-token`
- Value: your IQM Resonance API token

### 3. Deploy

Edit `deploy_qaoa_v2.py` — set `GITHUB_REPO` to your repo URL, then:

```bash
python deploy_qaoa_v2.py
```

### 4. Run

From Prefect Cloud UI: **Deployments → qaoa-maxcut-v2-garnet → Run**

Or via CLI:

```bash
prefect deployment run "QAOA MaxCut v2 · IQM Garnet/qaoa-maxcut-v2-garnet"
```

---

## Parameters

All parameters have defaults and can be overridden at run time via the Prefect UI (Custom Run → JSON editor) or CLI.

### Graph definition

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `num_nodes` | int | 5 | Number of graph nodes |
| `node_coordinates` | list of [x, y] | Circular layout | 2D positions for visualization |
| `edge_list` | list of [i, j] | Random ~60% | Which node pairs are connected |

**Validation rules:**
- `num_nodes` must equal `len(node_coordinates)`
- Every index in `edge_list` must be `< num_nodes`
- No self-loops allowed

### QAOA parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `num_iterations` | int | 6 | Number of (γ, β) parameter points to try |
| `shots` | int | 4096 | Measurement shots per circuit execution |
| `qaoa_depth` | int | 1 | QAOA circuit depth (p). Higher = more expressive but deeper circuit |

### Error mitigation

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `error_mitigation` | str | "none" | Mitigation technique to use |

Available options:

| Value | What it does |
|-------|-------------|
| `none` | Raw QPU counts, no correction |
| `zne` | **Zero Noise Extrapolation** — runs the circuit at noise factors [1, 3, 5] via global unitary folding, then extrapolates the expectation value to zero noise using a linear fit |
| `readout` | **Readout Error Mitigation** — runs calibration circuits (all-0 and all-1) to measure per-qubit readout fidelity, then corrects the output distribution |
| `dd` | **Dynamical Decoupling** — inserts XX pulse sequences during idle qubit periods to suppress coherent errors from unwanted qubit interactions |
| `all` | Applies all three: DD + ZNE + Readout |

---

## Override examples

### Custom Run in Prefect UI (JSON editor)

A triangle graph with ZNE:
```json
{
  "num_nodes": 3,
  "node_coordinates": [[0, 0], [2, 0], [1, 1.7]],
  "edge_list": [[0, 1], [1, 2], [0, 2]],
  "error_mitigation": "zne"
}
```

A 4-node square with one diagonal:
```json
{
  "num_nodes": 4,
  "node_coordinates": [[0, 0], [3, 0], [3, 3], [0, 3]],
  "edge_list": [[0, 1], [1, 2], [2, 3], [0, 3], [0, 2]],
  "error_mitigation": "readout",
  "num_iterations": 10
}
```

### CLI overrides

```bash
prefect deployment run "QAOA MaxCut v2 · IQM Garnet/qaoa-maxcut-v2-garnet" \
  --param num_nodes=4 \
  --param 'node_coordinates=[[0,0],[3,0],[3,3],[0,3]]' \
  --param 'edge_list=[[0,1],[1,2],[2,3],[0,3],[0,2]]' \
  --param error_mitigation=zne
```

---

## Pipeline stages

```
1 · Validate Inputs
    ↓
2 · Build Graph (+ brute-force optimal MaxCut)
    ↓
3 · Build QAOA Circuit (for each γ, β point)
    ↓
4 · Transpile for IQM Garnet (real backend coupling map + optional DD)
    ↓
5 · Execute on QPU (+ optional ZNE / readout mitigation)
    ↓
6 · Analyze Results
    ↓
7 · Publish Artifacts
    ├── 7.1 Convergence chart (SVG) — cut performance vs iteration
    ├── 7.2 Graph visualization (SVG) — nodes colored by partition
    └── 7.3 Experiment report — full summary with parameter sweep table
```

---

## Artifacts

After a run completes, check the **Artifacts** tab in the Prefect dashboard.

### Convergence chart
SVG chart with:
- **Green dashed line** — theoretical optimal MaxCut value
- **Blue solid line** — QAOA best-so-far at each iteration
- **Light blue dots** — per-iteration cut value

### Graph visualization
SVG showing the graph with:
- Nodes colored by QAOA partition (blue = Set 0, red = Set 1)
- Cut edges in solid red, uncut edges in dashed grey
- Nodes positioned according to your input coordinates

### Experiment report
Full summary including problem definition, coordinates, edge list, mitigation details, parameter sweep table, and quality assessment.

---

## Dependencies

Installed automatically by Prefect Serverless at runtime:

| Package | Version | Purpose |
|---------|---------|---------|
| qiskit | 2.1.2 | Quantum circuit construction and transpilation |
| iqm-client[qiskit] | 33.0.5 | IQM Garnet backend provider |
| networkx | latest | Graph utilities |
| numpy | latest | Numerical computation (ZNE extrapolation) |

For local development:
```bash
pip install prefect "qiskit>=1.0,<2.2" "iqm-client[qiskit]==33.0.5" networkx numpy
```

---

## Tips

- **Start small**: Run with 3-5 nodes first to verify the pipeline works end-to-end.
- **Compare mitigations**: Run the same graph with `none`, then `zne`, then `all` — compare convergence charts to see mitigation impact.
- **Scaling**: Brute-force optimal MaxCut is computed for comparison. This becomes slow above ~18 nodes (2^n complexity). The QAOA part itself scales with circuit depth, not exponentially.
- **Approximation ratio**: A ratio of 0.8+ is good for QAOA p=1 on noisy hardware. Increase `qaoa_depth` for potentially better results at the cost of deeper (noisier) circuits.
