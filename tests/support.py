"""Mechanical fixtures shared by the test suite."""

import json

from starksim.chain import ChainState, InstantNotifier
from starksim.netsim import NetworkSim
from starksim.network import Overlay
from starksim.node import Node


def line_adjacency(node_count):
    """Return an undirected line with nodes numbered from left to right."""
    adjacency = []
    for node_id in range(node_count):
        peers = []
        if node_id > 0:
            peers.append(node_id - 1)
        if node_id < node_count - 1:
            peers.append(node_id + 1)
        adjacency.append(peers)
    return adjacency


def line_overlay(node_count, base_latency=10.0, **kwargs):
    return Overlay.from_adjacency(
        line_adjacency(node_count), base_latency=base_latency, **kwargs)


def two_node_overlay(base_latency=10.0, **kwargs):
    return Overlay.from_adjacency(
        [[1], [0]], base_latency=base_latency, **kwargs)


def make_node(node_id=0, peers=(1, 2), chain=None, notifier=None, **kwargs):
    """Build one node; callers that need its chain should pass one explicitly."""
    if notifier is None:
        notifier = InstantNotifier(chain if chain is not None else ChainState())
    return Node(node_id, peers, notifier, **kwargs)


def make_sim(overlay=None, *, base_latency=10.0, **kwargs):
    """Build a deterministic two-node simulation unless given an overlay."""
    if overlay is None:
        overlay = two_node_overlay(base_latency)
    return NetworkSim(overlay, seed=kwargs.pop("seed", 1), **kwargs)


def parse_jsonl(text):
    """Decode non-empty JSON Lines text into records."""
    return [json.loads(line) for line in text.splitlines() if line]
