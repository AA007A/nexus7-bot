# Seven safety corrections

Base reviewed: d3d326f (includes upstream PRs 354–357). These patches do not
change the configured 50x leverage or the 50% available-margin sizing target.
They do not release the pilot budget or change drawdown overrides.

1. LIVE RR exits persist one intent before submission, recover by clientOid,
   require fill confirmation and exchange-flat evidence, and leave accounting
   to position sync. An ACK is never treated as a fill.
2. LIVE partial TP persists one intent per opening-order identity. A confirmed
   fill is persisted before break-even stop repair. Restart/timeout/stop failure
   never blindly repeats the reduction. Exchange quantity has explicit units;
   partial quantities use native contract lots. Pending partials block RR exits
   and new entries. Native protection and emergency reduction remain separate.
3. Administrative pause blocks entries at engine and HTTP dispatch boundaries.
   Position management continues even with active=false. Worker tasks are
   cancelled and awaited on shutdown. See ENTRY_PAUSE.md for restart semantics.
4. Ownership-fence storage failures can permit only a freshly verified,
   correctly sided, bounded reduceOnly order. Another healthy owner still
   blocks dispatch. Unknown exchange exposure blocks the exception.
5. Daily PnL uses a policy-independent namespace. Legacy ledgers are merged
   non-destructively by event identity; duplicates deduplicate and conflicts
   block. Old writers are re-read on each checkpoint. This is not a new
   cross-process transactional ledger; existing single-owner assumptions apply.
6. Optimizer CPU work runs in a separate process with no inherited credentials,
   network disabled and bounded lifetime. Cancellation kills and reaps the
   process. The parent alone saves research candidates; runtime application
   remains false. This does not move the separate weekly backtester.
7. Final quantity and fresh executable price must satisfy estimated stop loss
   plus round-trip cost <= 50% of entry margin. Invalid/missing stops block.
   Quantity and technical stop are not silently changed. Gaps, funding and
   execution beyond cost estimates can exceed this projected ceiling.

## Recovery and limits

An intent persisted immediately before a crash may never have reached the
exchange. Absence from lookup is not proof of rejection: it remains unresolved
and is not resent automatically. Protection/reconciliation must establish the
outcome; manual investigation may be needed. Exactly-once execution across a
network partition is not claimed. A missing opening lineage blocks that
optional exit path rather than inventing an identity.

Confirmed RR orders with residual exposure remain pending, with native
protection retained. These changes do not replace exchange-native SL/TP or
emergency close policies. Partial cancellation and late fills need operational
reconciliation; no automatic second partial is authorized.

Test coverage includes ACK without fill, ambiguous submission across restart,
stop-update exception, residual/invalid position reads, native minimum lots,
DB ownership outage versus another owner, pause lifecycle, legacy ledger import,
projected-loss cost boundary, worker timeout/cancellation and a real isolated
research child with an event-loop heartbeat. All exchanges are mocked or
network-blocked during validation.

Production certification still requires CI at the exact published commit and
controlled runtime evidence. No profitability or 10/10 claim is made.
