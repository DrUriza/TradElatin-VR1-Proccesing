# Changelog

## 4.2.3

- Restored real process-local HTTP keep-alive reuse for Emulator acquisition.
- Added one reconnect attempt after a broken Emulator connection and deterministic,
  idempotent cleanup at shutdown.
- Kept LIVE provider acquisition on its existing non-pooled path.
- Expanded lifecycle tests for reuse, reconnection, repeated cleanup, and terminal
  connection failure.
- Confirmed that `to_vr1_observation_v1()` remains the stable public projection
  for downstream consumers and that its schema stays `vr1-observation-v1`.

No market families, logical endpoint IDs, provider allowlists, observable IDs, or
public contract versions changed in this maintenance release.
