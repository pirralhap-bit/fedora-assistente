"""Run every saved clip through small/medium/turbo with Fedora's own whisper() call; print text and time."""
import os, pathlib, time, wave, sys
import listener
URLS = {"small": "http://127.0.0.1:8178/inference", "medium": "http://127.0.0.1:8179/inference", "turbo": "http://127.0.0.1:8180/inference"}
clips = sorted((pathlib.Path(os.environ["XDG_RUNTIME_DIR"]) / "fedora-ouvido").glob("*.wav"))
tot = {k: 0.0 for k in URLS}
for c in clips:
    w = wave.open(str(c)); pcm = w.readframes(w.getnframes())
    print(f"\n{c.name} ({len(pcm)/32000:.1f}s)")
    for k, u in URLS.items():
        t = time.perf_counter(); out = listener.whisper(u, pcm); dt = time.perf_counter() - t; tot[k] += dt
        print(f"  {k:6} {dt:5.2f}s  {out!r}")
print("\ntempo total:", {k: round(v, 1) for k, v in tot.items()})
