"""STT local para Windows: mesmo contrato do whisper-server do whisper.cpp (POST /inference, multipart -> {"text": ...}).

    python whisper_server.py            # porta 8178, modelo WHISPER_MODEL (padrao: small)
    python whisper_server.py --pai PID  # encerra sozinho quando o processo PID (o server.py) morrer
    python whisper_server.py --teste    # autoteste (sem modelo, sem rede)

faster-whisper (CTranslate2) em CPU/int8: o whisper.cpp nao tem binario pronto para Windows.
"""
import email
import email.policy
import io
import json
import os
import sys
import threading
import time
import wave

import numpy as np
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("WHISPER_PORT", "8178"))
MODEL = os.environ.get("WHISPER_MODEL", "small")
LANG = os.environ.get("WHISPER_LANG", "pt")
_model = None
# Segmento descartado quando o modelo acha que nao havia fala (no_speech_prob alto) E nao confia no texto (avg_logprob
# baixo): e assim que nascem "Obrigado por assistir" e "Legendas pela comunidade" em silencio/ruido. Os dois valores
# sao os padroes do faster-whisper (no_speech_threshold=0.6, log_prob_threshold=-1.0); o transcribe() abaixo so aplica
# essa regra por janela de 30 s, aqui ela vale por segmento. Os dois juntos: fala baixa mas clara tem logprob bom e fica.
NO_SPEECH_MAX = 0.6
LOGPROB_MIN = -1.0


def texto(segs):
    """Os segmentos que parecem fala de verdade, juntos."""
    return " ".join(s.text.strip() for s in segs
                    if not (s.no_speech_prob > NO_SPEECH_MAX and s.avg_logprob < LOGPROB_MIN)).strip()


def vigiar_pai(pid, ao_morrer=lambda: os._exit(0), intervalo=2.0):
    """Encerra o STT quando o processo pai (server.py) sumir: sem isso ele fica orfao segurando a porta e ~1 GB de RAM."""
    import psutil
    try:
        pai = psutil.Process(pid)
    except psutil.Error:
        return ao_morrer()

    def laco():
        while True:
            try:
                vivo = pai.is_running() and pai.status() != psutil.STATUS_ZOMBIE   # is_running confere o create_time
            except psutil.Error:
                vivo = False
            if not vivo:
                return ao_morrer()
            time.sleep(intervalo)
    th = threading.Thread(target=laco, daemon=True, name="vigia-pai")
    th.start()
    return th


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
                                     initial_prompt=prompt, beam_size=5, vad_filter=True, condition_on_previous_text=False,
                                     no_speech_threshold=0.5, log_prob_threshold=-1.0, temperature=0.0)
        out = json.dumps({"text": texto(segs)}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)


if __name__ == "__main__":
    if "--teste" in sys.argv:
        import subprocess
        from types import SimpleNamespace as S
        segs = [S(text=" abre o youtube ", no_speech_prob=0.1, avg_logprob=-0.3),
                S(text="Obrigado por assistir!", no_speech_prob=0.9, avg_logprob=-1.4),   # silencio: fora
                S(text="que horas sao", no_speech_prob=0.7, avg_logprob=-0.5),            # baixo mas confiante: fica
                S(text="hmm", no_speech_prob=0.2, avg_logprob=-1.6)]                      # fala, so incerta: fica
        assert texto(segs) == "abre o youtube que horas sao hmm", texto(segs)
        assert texto([]) == ""
        pai = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(1.5)"])
        morreu = threading.Event()
        vigiar_pai(pai.pid, morreu.set, intervalo=0.2)
        assert not morreu.wait(0.5), "pai vivo: nao pode encerrar"
        assert morreu.wait(10), "pai morreu e o vigia nao percebeu"
        print("whisper_server ok")
        sys.exit(0)
    if "--pai" in sys.argv:   # iniciar.ps1 passa o PID do server.py
        vigiar_pai(int(sys.argv[sys.argv.index("--pai") + 1]))
    model()
    print(f"whisper ({MODEL}) em 127.0.0.1:{PORT}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
