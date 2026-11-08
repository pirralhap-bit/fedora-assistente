"""Compare speech models on saved clips: python3 tools/compara-voz.py <pasta-com-wav>. Text + time per clip and model.
Servers: small :8178 (whisper_server.py), medium :8179, turbo :8180 (start them by hand with whisper_server.py, WHISPER_MODEL and WHISPER_PORT)."""
import pathlib, sys, time, wave
import listener

VARIANTES = {"small": ("http://127.0.0.1:8178/inference", None), "medium": ("http://127.0.0.1:8179/inference", None),
             "turbo": ("http://127.0.0.1:8180/inference", None), "turbo-cheio": ("http://127.0.0.1:8180/inference", 0)}
orig = listener.whisper
tot = {k: 0.0 for k in VARIANTES}
for c in sorted(pathlib.Path(sys.argv[1]).glob("*.wav")):
    w = wave.open(str(c)); pcm = w.readframes(w.getnframes())
    print(f"\n{c.name} ({len(pcm)/32000:.1f}s)")
    for k, (url, ctx) in VARIANTES.items():
        if ctx is None:
            fn = orig
        else:   # full 30 s window: listener's short-clip audio_ctx trick switched off
            sec = listener.RATE * 2
            fn = lambda u, p: orig(u, p + b"\0" * max(0, 26 * sec - len(p)))   # >25 s of audio makes whisper() pick ctx=0
        t = time.perf_counter(); out = fn(url, pcm); dt = time.perf_counter() - t; tot[k] += dt
        print(f"  {k:11} {dt:5.2f}s  {out!r}")
print("\ntempo total:", {k: round(v, 1) for k, v in tot.items()})
