"""
deploy_qaoa_v2.py — Register QAOA MaxCut v2 on Prefect Cloud
=============================================================
Parameters exposed in the Prefect UI:
  - num_nodes:        Graph size (default 5)
  - node_coordinates: 2D positions — must match num_nodes
  - error_mitigation: none | zne | readout | dd | all
  - num_iterations:   Optimization iterations
  - shots:            Measurement shots
  - qaoa_depth:       QAOA circuit depth p
"""

from prefect import flow


GITHUB_REPO = "https://github.com/PlayfulDevBit/qaoa-v2.git"  # ← CHANGE THIS


if __name__ == "__main__":
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
                "matplotlib",
                "networkx",
                "mitiq",
                "mthree",
            ]
        },
        parameters={
            # ── Graph defaults (user can override at run time) ──
            "num_nodes": 5,
            "node_coordinates": [
                [0.0, 4.0],
                [3.8, 1.2],
                [2.4, -3.2],
                [-2.4, -3.2],
                [-3.8, 1.2],
            ],
            # ── Error mitigation ──
            "error_mitigation": "none",
            # ── QAOA parameters ──
            "num_iterations": 6,
            "shots": 4096,
            "qaoa_depth": 1,
        },
        tags=["quantum", "iqm-garnet", "qaoa", "maxcut", "v2", "mitigation"],
    )

    print("✅ QAOA MaxCut v2 deployment registered!")
    print()
    print("Parameters visible in Prefect UI:")
    print("  num_nodes ........... 5       (change graph size)")
    print("  node_coordinates .... [5 pts] (change node positions)")
    print("  error_mitigation .... none    (none|zne|readout|dd|all)")
    print("  num_iterations ...... 6       (optimization steps)")
    print("  shots ............... 4096    (measurement shots)")
    print("  qaoa_depth .......... 1       (QAOA p layers)")
    print()
    print("⚠️  If you change num_nodes, you MUST also change node_coordinates")
    print("   to have the same number of [x,y] pairs — the pipeline validates this.")
    print()
    print("Run with:")
    print('  prefect deployment run "QAOA MaxCut v2 · IQM Garnet/qaoa-maxcut-v2-garnet"')
    print()
    print("Override example (CLI):")
    print('  prefect deployment run "QAOA MaxCut v2 · IQM Garnet/qaoa-maxcut-v2-garnet" \\')
    print('    --param num_nodes=3 \\')
    print('    --param \'node_coordinates=[[0,0],[2,0],[1,1.7]]\' \\')
    print('    --param error_mitigation=zne')
