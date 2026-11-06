"""STT local para Windows: mesmo contrato do whisper-server do whisper.cpp (POST /inference, multipart -> {"text": ...}).

    python whisper_server.py            # porta 8178, modelo WHISPER_MODEL (padrao: small)

faster-whisper (CTranslate2) em CPU/int8: o whisper.cpp nao tem binario pronto para Windows.
"""
import email
import email.policy
import io
import json
import os
import sys
import wave

import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WHISPER_PORT", "8178"))
MODEL = os.environ.get("WHISPER_MODEL", "small")
LANG = os.environ.get("WHISPER_LANG", "pt")
_model = None


def model():
    global _model
    if _model is None:
        from faster_whisper import WhisperModel
        _model = WhisperModel(MODEL, device="cpu", compute_type="int8", cpu_threads=max(2, (os.cpu_count() or 8) // 2))
    return _model


def parse(ctype, body):
    msg = email.message_from_bytes(b"Content-Type: " + ctype.encode() + b"\r\n\r\n" + body, policy=email.policy.HTTP)
    return {p.get_param("name", header="content-disposition"): p.get_payload(decode=True) for p in msg.iter_parts()}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        if self.path.split("?")[0] != "/inference":
            self.send_error(404)
            return
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        f = parse(self.headers["Content-Type"], body)
        prompt = (f.get("prompt") or b"").decode("utf-8", "ignore") or None
        with wave.open(io.BytesIO(f["file"])) as w:      # 16 kHz mono s16 from the listener; no ffmpeg/av needed
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
        segs, _ = model().transcribe(audio, language=None if LANG == "auto" else LANG,
                                     initial_prompt=prompt, beam_size=1, vad_filter=False, condition_on_previous_text=False)
        out = json.dumps({"text": " ".join(s.text.strip() for s in segs)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    model()
    print(f"whisper ({MODEL}) em 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
