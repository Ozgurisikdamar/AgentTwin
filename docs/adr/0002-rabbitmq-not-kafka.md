# ADR-0002 — RabbitMQ instead of Kafka

* Status: accepted · Date: 2026-09-24

## Context
Asynchronous work in AgentTwin is *job-shaped*: request a simulation, evaluate a
run, mine a failure, record an audit event. Volumes are modest; per-message
acknowledgement, dead-lettering and routing matter more than log replay.

## Decision
RabbitMQ with one durable topic exchange `agenttwin.events`, one durable queue per
consumer service, a TTL retry queue and a parking DLQ per consumer queue.
Semantics: at-least-once, publisher confirms, idempotent consumers
(`processed_event(consumer, event_id)` written in the handling transaction), bounded
retry (default 5 attempts with increasing TTL), explicit poison-message parking.
A transactional outbox is used only where losing an event would corrupt state
(audit events, run requests, run completions).

## Upgrade trigger
Kafka (or another log) when we need multi-day replay of the raw event stream,
per-partition ordering at high throughput, or stream processing over telemetry.

## Consequences
No exactly-once assumptions anywhere; every consumer is written to tolerate
duplicates and out-of-order delivery (tested).
