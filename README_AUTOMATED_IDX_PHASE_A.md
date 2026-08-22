# Phase A — Automated IDX Collector MVP

Implementation branch: `feat/automated-idx-collector-mvp`.

The phase adds an HTTP-only, fail-closed collector for the public IDX disclosure endpoint, incremental recent-ID checkpointing, official-host attachment caching, and a one-shot CLI wired to the existing source-neutral extraction/Gemini/Synapse pipeline.

Production scheduling is intentionally excluded until a controlled live probe passes repeatedly.
