"""
deploy_qaoa_v2.py — Register QAOA MaxCut v2 on Prefect Cloud
"""

import math
import random
from prefect import flow


GITHUB_REPO = "https://github.com/YOUR_USER/YOUR_REPO.git"  # ← CHANGE THIS


def _default_coordinates(n):
    return [
        [round(4.0 * math.cos(2 * math.pi * i / n), 2),
         round(4.0 * math.sin(2 * math.pi * i / n), 2)]
        for i in range(n)
    ]


def _default_edges(n, probability=0.6, seed=42):
    rng = random.Random(seed)
    edges = []
    for i in range(n):
        for j in range(i + 1, n):
            if rng.random() < probability:
                edges.append([i, j])
    connected = set()
    for e in edges:
        connected.add(e[0])
        connected.add(e[1])
    for i in range(n):
        if i not in connected:
            edges.append([i, (i + 1) % n])
            connected.add(i)
    return edges


if __name__ == "__main__":
    n = 5
    coords = _default_coordinates(n)
    edges = _default_edges(n)

    flow.from_source(
        source=GITHUB_REPO,
        entrypoint="qaoa_pipeline_v2.py:qaoa_pipeline_v2",
    ).deploy(
        name="qaoa-maxcut-v2-garnet",
        work_pool_name="my-managed-pool",
        job_variables={
            "pip_packages": [
                "qiskit==2.1.2",
                "iqm-client[qiskit]==33.0.5",
                "networkx",
                "numpy",
            ]
        },
        parameters={
            "num_nodes": n,
            "node_coordinates": coords,
            "edge_list": edges,
            "error_mitigation": "none",
            "num_iterations": 6,
            "shots": 4096,
            "qaoa_depth": 1,
        },
        tags=["quantum", "iqm-garnet", "qaoa", "maxcut", "v2"],
    )

    print("✅ QAOA MaxCut v2 deployed!")
    print()
    print("Default graph:")
    print(f"  Nodes: {n}")
    print(f"  Coordinates: {coords}")
    print(f"  Edges: {edges}")
    print()
    print("Override at run time (Custom Run → JSON):")
    print('  {')
    print('    "num_nodes": 4,')
    print('    "node_coordinates": [[0,0], [2,0], [2,2], [0,2]],')
    print('    "edge_list": [[0,1], [1,2], [2,3], [0,3], [0,2]],')
    print('    "error_mitigation": "zne"')
    print('  }')
    print()
    print("⚠️  num_nodes must match len(node_coordinates)")
    print("⚠️  All node indices in edge_list must be < num_nodes")
