from __future__ import annotations

import unittest
from unittest.mock import Mock, call, patch

from memory.domain.models import ActivationOptions, MemoryEdge, MemoryNode
from memory.engines import ActivationEngine, SalienceEngine


class EngineBehaviorTests(unittest.TestCase):
    def test_activation_preserves_filters_scores_and_result_order(self) -> None:
        seed = MemoryNode(id="seed", type="Function")
        latent = MemoryNode(id="latent", type="Function", status="latent")
        archived = MemoryNode(id="archived", type="Function", status="archived")
        nodes = {node.id: node for node in (seed, latent, archived)}
        propagated = MemoryEdge(id="propagated", from_id="seed", to_id="latent", type="CALLS", weight=0.8)
        blocked = MemoryEdge(id="blocked", from_id="seed", to_id="archived", type="EXTRACTED_FROM")

        store = Mock()
        store.get_node.side_effect = nodes.get
        store.neighbors.side_effect = lambda node_id, **_: (
            [(propagated, latent), (blocked, archived)] if node_id == "seed" else []
        )
        store.get_nodes.side_effect = lambda node_ids: [nodes[node_id] for node_id in node_ids]

        result = ActivationEngine(store).activate(
            ["missing", "archived", "seed"],
            ActivationOptions(max_depth=1, update_store=False),
        )

        expected_latent_activation = 1.0 * 0.8 * 1.0 * (0.55 ** 1) * 0.35
        self.assertEqual(result.activation_by_node, {"seed": 1.0, "latent": expected_latent_activation})
        self.assertEqual([node.id for node in result.active_nodes], ["seed", "latent"])
        self.assertEqual([edge.id for edge in result.fired_edges], ["propagated"])
        store.neighbors.assert_called_once_with(
            "seed", direction="both", edge_types=None, min_weight=0.01, limit=120
        )

    @patch("memory.engines.salience.seconds_since", return_value=7 * 86400.0)
    def test_salience_preserves_scores_and_recompute_calls(self, _seconds_since: Mock) -> None:
        first = MemoryNode(
            id="first",
            type="Function",
            properties={"novelty": 0.5, "priority": 1.5},
            confidence=0.8,
            volatility=0.25,
            evidence_count=2,
        )
        second = MemoryNode(id="second", type="Unknown")
        store = Mock()
        store.degree.return_value = 6
        store.find_nodes.return_value = [first, second]
        engine = SalienceEngine(store)

        signal = engine.compute_salience_signal(first)
        expected_recency = 0.5 ** (7.0 / 14.0)
        expected_signal = (
            0.16 * 0.5
            + 0.12 * expected_recency
            + 0.24 * 0.5
            + 0.18 * 0.5
            + 0.12 * 0.4
            + 0.18 * 0.8
        )
        self.assertEqual(signal["output"]["salience_score"], expected_signal)

        expected_scores = [engine.compute_node_salience(first), engine.compute_node_salience(second)]
        self.assertEqual(engine.recompute_user(limit=2), 2)
        store.find_nodes.assert_called_once_with(limit=2, order_by="updated_at")
        self.assertEqual(
            store.update_node_fields.call_args_list,
            [
                call("first", salience=expected_scores[0]),
                call("second", salience=expected_scores[1]),
            ],
        )


if __name__ == "__main__":
    unittest.main()
