"""One-time seed script for the insurance-product knowledge tree
(docs/exclusion-scenario-plan.md P3): reads data/insurance_product/*.yaml
and writes it into semantic long-term memory under
`_global/semantic/insurance_product/<product>/...` so
mcp_servers/memory/server.py's browse_semantic_memory (P2) has something
real to walk. Deliberately not wired into orchestrator/memory_writer.py --
a policy document is static reference knowledge, not something distilled
from a run.

Idempotent: remember()'s aput() upserts on (namespace, key), so re-running
this after editing a yaml file just resyncs it -- no separate migration
step. Run with:
    uv run python -m scripts.seed_insurance_memory
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import yaml

from persistence.call_log import current_node_name
from persistence.memory import GLOBAL_TENANT, INDEX_KEY, MemoryKind, remember
from persistence.memory_policy import load_memory_policy
from persistence.memory_store import get_memory_store

_REPO_ROOT = Path(__file__).resolve().parents[1]
_POLICY_PATH = _REPO_ROOT / "mcp_servers" / "policy.yaml"
_DATA_DIR = _REPO_ROOT / "data" / "insurance_product"


async def _seed_node(store, policy, scope: tuple[str, ...], node: dict) -> int:
    """Writes one node's own _index, then recurses into `children` (a
    branch) or `articles` (a leaf) -- see data/insurance_product/kgi_ltc.yaml's
    header comment for the node shape. Returns how many remember() calls
    were made, purely for this script's own summary line."""
    written = 0
    await remember(
        store,
        policy,
        MemoryKind.SEMANTIC,
        tenant=GLOBAL_TENANT,
        scope=scope,
        key=INDEX_KEY,
        content={"title": node["title"], "summary": node["summary"]},
    )
    written += 1

    for child in node.get("children", []):
        written += await _seed_node(store, policy, (*scope, child["segment"]), child)

    for article in node.get("articles", []):
        await remember(
            store,
            policy,
            MemoryKind.SEMANTIC,
            tenant=GLOBAL_TENANT,
            scope=scope,
            key=article["key"],
            content={k: v for k, v in article.items() if k != "key"},
        )
        written += 1

    return written


async def main() -> None:
    policy = load_memory_policy(str(_POLICY_PATH))
    # the only principal policy.yaml grants semantic write access to (see
    # mcp_servers/policy.yaml's memory_writer entry) -- reused here rather
    # than inventing a new principal, since this script fills the same role
    # (the trusted writer of long-term memory) that orchestrator/memory_writer.py
    # plays for distilled episodic memory.
    current_node_name.set("memory_writer")

    async with get_memory_store() as store:
        await store.setup()
        total = 0
        for path in sorted(_DATA_DIR.glob("*.yaml")):
            product_id = path.stem
            node = yaml.safe_load(path.read_text())
            count = await _seed_node(store, policy, ("insurance_product", product_id), node)
            print(f"[seed] {path.name} -> {count} item(s) under insurance_product/{product_id}")
            total += count

    print(f"\nSeeded {total} item(s) total.")


if __name__ == "__main__":
    asyncio.run(main())
