# ADR 0001: Meter bandwidth on node egress

## Context

Sending the same payload to several peers consumes one node's uplink several
times. A capacity attached only to each directed link would let all peer sends
serialize in parallel and would hide the protocol's fan-out cost. Propagation
latency and bandwidth capacity are separate effects.

## Decision

Every node has one egress server. Each accepted message occupies that server
for `size / capacity`, so all traffic from the node competes for the same
uplink. Links normally contribute latency only. `link_capacity`, when set, adds
a second per-direction serialization server after node egress; it models a
tandem bottleneck rather than replacing the uplink.

## Consequences

Sending to degree `d` costs `d` copies on one uplink. Offered, completed-egress,
and delivered bytes are distinct quantities. A link cap can expose a downstream
bottleneck, but it cannot make fan-out free. Ingress is measured but is not a
metered resource.

## Where it lives in the code

`starksim.netsim.NetworkSim.__init__` creates `egress` and `link_servers`.
`starksim.netsim.NetworkSim._send` charges node egress.
`starksim.netsim.NetworkSim._egress_done` and `_link_server` implement the
optional tandem stage. `starksim.network.Overlay.latency` supplies propagation
latency independently.

## Status

Accepted and implemented.
