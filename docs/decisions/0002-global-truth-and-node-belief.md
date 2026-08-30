# ADR 0002: Separate global truth from node belief

## Context

The simulator needs omniscient facts to represent objects and the canonical
chain, but a protocol participant cannot consult those facts directly. Mixing
provenance, settlement, and local knowledge would make delayed or lost
information impossible to model and could let nodes act on evidence they never
received.

## Decision

`Arena` owns object identity, provenance, coverage, and claims. `ChainState`
owns canonical inclusion and expiry. Each `NodeView` owns only that node's
held objects, bodies, verified proofs, settlement knowledge, and per-peer
beliefs. Node decisions use the local view rather than arena provenance or
unnotified chain state.

Beliefs form only at an event that establishes the claimed fact, never when a
message is enqueued. Body beliefs update on physical delivery because arrived
bodies are retained even if proof verification is refused. Sender-side proof
belief updates only when verification is accepted or the receiver already
retains the proof; receipt-direction proof belief is justified by the sender
having supplied it.

## Consequences

Loss, CPU refusal, delayed chain news, and partial bodies remain observable.
Proof suppression self-heals after refusal because no false proof belief forms.
The arena remains a storage and accounting oracle, not knowledge available to a
node.

## Where it lives in the code

`starksim.arena.Arena`, `starksim.chain.ChainState`, and
`starksim.node.NodeView` hold the three kinds of state.
`starksim.node.PeerBodyBeliefs.confirm_delivery` and
`starksim.node.PeerProofBeliefs.note` record peer beliefs.
`starksim.netsim.NetworkSim._on_deliver` and `_deliver_wrapper` choose when
those records are justified.

## Status

Accepted and implemented.
