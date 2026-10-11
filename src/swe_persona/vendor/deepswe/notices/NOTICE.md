# DeepSWE source and modification notices

Native task inputs originate from datacurve-ai/deep-swe at commit
0b9fabbb63b9104d678fe965e1632f2dd9eaa2ea, under the included Apache-2.0 license.
Persona adaptation and runtime originate from MatrAIx-ai/PersonaCodeBench at
commit 8885890d771a712cdc0ecac3bdc7bb85c9619c47, with its included MIT license.
The original module also records PersonaCodeBench commit
3f8d716cc96955325d5e29a845c3556f60be080b for persona and ledger provenance.

This v4 derivative adds a Harbor 0.23 CPU runtime, API routing and bounded JSON
protocol repair, host-resolved evidence references, portable image recipes,
immutable verifier image pins, and separately recorded calibration-fixture
repairs. The Prometheus public probe v2 raises only its query sample ceiling;
original inputs and its before/after controls are retained in the source package.
The unified Parquet adapter omits reference solutions and historical trajectories.
Original upstream test/grader bytes and scored source-scope restrictions remain.

The 64-task cohort was selected retrospectively (53 historical F2P regressions,
11 historical coding-time cutoffs). It is a curated diagnostic subset, not an
unbiased sample. This derivative does not grant new rights in upstream material.
Docker images retain their own upstream software licenses. The original source
notices remain verbatim in the separate source package. Source locks and the
qualification report record component identities and repair evidence.
