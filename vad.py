"""Silero VAD (ONNX): probability that a 32 ms block of 16 kHz mono s16 audio is speech. None when unavailable."""
import pathlib

MODEL = pathlib.Path(__file__).with_name("modelos") / "silero_vad.onnx"
BLOCK = 512          # samples per call at 16 kHz (32 ms), what the model was trained on
_CONTEXT = 64        # samples carried over between calls, as silero's own wrapper does


class Silero:
    def __init__(self):
        import numpy as np
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = opts.inter_op_num_threads = 1   # tiny model: 1 thread, leave the CPU to whisper
        self.np = np
        self.s = ort.InferenceSession(str(MODEL), opts, providers=["CPUExecutionProvider"])
        self.reset()

    def reset(self):
        np = self.np
        self.state = np.zeros((2, 1, 128), dtype=np.float32)
        self.ctx = np.zeros((1, _CONTEXT), dtype=np.float32)

    def prob(self, chunk):
        np = self.np
        x = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)[None, :] / 32768.0
        out, self.state = self.s.run(None, {"input": np.concatenate([self.ctx, x], axis=1),
                                            "state": self.state, "sr": np.array(16000, dtype=np.int64)})
        self.ctx = x[:, -_CONTEXT:]
        return float(out[0, 0])


def load():
    """The VAD, or None (no .venv / no model): the ear then falls back to its loudness detector."""
    try:
        return Silero()
    except (ImportError, OSError, RuntimeError) as e:
        print(f"[vad] Silero indisponível ({e}); usando detecção por volume", flush=True)
        return None


if __name__ == "__main__":
    import math
    import random
    import struct
    import subprocess
    import sys
    v = load()
    assert v, "Silero não carregou"
    silencio = b"\0" * BLOCK * 2
    ruido = struct.pack(f"<{BLOCK}h", *(random.randint(-1500, 1500) for _ in range(BLOCK)))
    tom = struct.pack(f"<{BLOCK}h", *(int(8000 * math.sin(i / 8)) for i in range(BLOCK)))
    for nome, c in (("silêncio", silencio), ("ruído", ruido), ("tom", tom)):
        v.reset()
        p = max(v.prob(c) for _ in range(10))
        print(f"{nome:9} {p:.2f}")
        assert p < 0.5, nome
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import speech
    raw = subprocess.run([speech.PIPER, "-m", speech.VOICE, "--output_raw"], input="Ok, Fedora, abre a Steam.".encode(),
                         capture_output=True).stdout
    a = struct.unpack(f"<{len(raw) // 2}h", raw)
    pcm = struct.pack(f"<{int(len(a) * 16000 / 22050)}h", *(a[int(i * 22050 / 16000)] for i in range(int(len(a) * 16000 / 22050))))
    v.reset()
    ps = [v.prob(pcm[i:i + BLOCK * 2]) for i in range(0, len(pcm) - BLOCK * 2, BLOCK * 2)]
    fala = sum(p > 0.5 for p in ps) / len(ps)
    print(f"voz      {fala:.0%} dos blocos com fala")
    assert fala > 0.5
    print("vad ok")
