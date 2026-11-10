"""
Compara os modelos do agy (Gemini Flash/Argon em low, medium e high) em tarefas curtas com resposta conferivel por codigo.

Mede por modelo: acertos, tokens (total e de raciocinio) e tempo. Serve para escolher o equivalente no Google de cada modelo
do Claude (haiku, sonnet, opus) e o nivel de esforco, em vez de achar. Nao edita nada nem roda codigo gerado pelo modelo:
cada tarefa e so texto, e a conferencia e feita aqui.

    python tools/compara_modelos.py                       # os 6 Gemini, 1 rodada
    python tools/compara_modelos.py --modelos gemini-4-argon-high,gemini-3.8-flash-high --rodadas 2
    python tools/compara_modelos.py --teste               # confere so as conferencias (offline, sem gastar cota)

Roda em pasta temporaria vazia, com --mode plan. So usa modelos que existem no agy; para comparar com o Claude de verdade rode
as mesmas tarefas no Claude a mao: o script nao toca na cota dele (a regra de troca olha 55% na semana).
"""
import argparse
import json
import os
import pathlib
import re
import subprocess
import sys
import tempfile

MODELOS = ["gemini-3.8-flash-low", "gemini-3.8-flash-medium", "gemini-3.8-flash-high",
           "gemini-4-argon-low", "gemini-4-argon-medium", "gemini-4-argon-high"]

CODIGO_BUG = """def f(xs):
    r = []
    for i in range(len(xs)):
        if xs[i] % 2 == 0:
            r.append(xs.pop(i))
    return r
print(f([2, 4, 6, 1, 8]))"""


def _numero(txt):
    return [float(x.replace(",", ".")) for x in re.findall(r"-?\d+(?:[.,]\d+)?", txt)]


def _tem_numero(txt, alvo, tol=0.005):
    return any(abs(n - alvo) <= tol for n in _numero(txt.replace("R$", "")))


def _frase_7_sol(txt):
    palavras = re.findall(r"[\wÀ-ÿ]+", txt.strip().splitlines()[0] if txt.strip() else "")
    return len(palavras) == 7 and palavras[-1].lower() == "sol"


# (id, pedido, conferencia(resposta) -> bool)
TAREFAS = [
    ("conta", "Uma loja vende 3 caixas de 12 canetas a R$ 4,50 cada caneta. O total tem 15% de desconto. Quanto o cliente paga? "
              "Responda so com o numero, com duas casas decimais.", lambda t: _tem_numero(t.replace(".", ","), 137.70)),
    ("logica", "Ana e mais alta que Bia. Bia e mais alta que Caio. Duda e mais baixa que Caio. Quem e a segunda mais alta? "
               "Responda so com o nome.", lambda t: t.strip().strip(".").lower() == "bia"),
    ("codigo", f"O que este codigo Python imprime ou levanta? Responda so com o nome da excecao, ou com a saida.\n\n{CODIGO_BUG}",
     lambda t: "indexerror" in t.lower()),
    ("dias", "Quantos dias ha entre 28/02/2024 e 01/03/2025? Responda so com o numero.", lambda t: _tem_numero(t, 367, 0)),
    ("frase", "Escreva uma frase em portugues com exatamente 7 palavras que termine com a palavra sol. Responda so com a frase.",
     _frase_7_sol),
]


def agy_exe():
    return os.environ.get("AGY_CMD") or str(pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "agy" / "bin" / "agy.exe")


def roda(modelo, pedido, pasta):
    cmd = [agy_exe(), "-p", pedido, "--output-format", "json", "--model", modelo, "--disable-slash-commands"]
    try:
        p = subprocess.run(cmd, cwd=pasta, capture_output=True, text=True, encoding="utf-8", timeout=180)
    except subprocess.TimeoutExpired:
        return dict(ok=False, texto="", tokens=0, raciocinio=0, seg=180.0, erro="timeout")
    linha = next((x for x in reversed(p.stdout.splitlines()) if x.startswith("{")), "")
    try:
        j = json.loads(linha)
    except ValueError:
        return dict(ok=False, texto="", tokens=0, raciocinio=0, seg=0.0, erro=(p.stderr or p.stdout)[:200])
    u = j.get("usage") or {}
    return dict(ok=j.get("status") == "SUCCESS", texto=j.get("response") or "", tokens=u.get("total_tokens", 0),
                raciocinio=u.get("thinking_tokens", 0), seg=float(j.get("duration_seconds") or 0), erro="")


def compara(modelos, rodadas):
    res = {m: dict(acertos=0, total=0, tokens=0, raciocinio=0, seg=0.0, erros=0, falhas=[]) for m in modelos}
    with tempfile.TemporaryDirectory() as pasta:
        for _ in range(rodadas):
            for tid, pedido, confere in TAREFAS:
                for m in modelos:
                    r = roda(m, pedido, pasta)
                    d = res[m]
                    d["total"] += 1
                    d["tokens"] += r["tokens"]
                    d["raciocinio"] += r["raciocinio"]
                    d["seg"] += r["seg"]
                    if not r["ok"]:
                        d["erros"] += 1
                        d["falhas"].append(f"{tid}: {r['erro']}")
                    elif confere(r["texto"]):
                        d["acertos"] += 1
                    else:
                        d["falhas"].append(f"{tid}: {r['texto'].strip()[:60]!r}")
                    print(f"  {m:28} {tid:7} {'ok ' if r['ok'] and confere(r['texto']) else 'ERRO'} {r['tokens']:>6} tok {r['seg']:5.1f}s",
                          flush=True)
    return res


def tabela(res):
    linhas = [f"{'modelo':28} {'acertos':>8} {'tokens/tarefa':>14} {'raciocinio':>11} {'s/tarefa':>9}"]
    for m, d in sorted(res.items(), key=lambda kv: (-kv[1]["acertos"], kv[1]["tokens"])):
        n = max(d["total"], 1)
        linhas.append(f"{m:28} {d['acertos']:>3}/{d['total']:<4} {d['tokens'] // n:>14} {d['raciocinio'] // n:>11} {d['seg'] / n:>9.1f}")
    for m, d in res.items():
        for f in d["falhas"]:
            linhas.append(f"  falha {m}: {f}")
    return "\n".join(linhas)


def _teste():
    conf = {t[0]: t[2] for t in TAREFAS}
    assert conf["conta"]("137,70") and conf["conta"]("R$ 137.70") and not conf["conta"]("162,00")
    assert conf["logica"]("Bia.") and not conf["logica"]("Ana")
    assert conf["codigo"]("IndexError: list index out of range") and not conf["codigo"]("[2, 6, 8]")
    assert conf["dias"]("367") and not conf["dias"]("366")
    assert conf["frase"]("O gato dorme feliz ao calor do sol") is False   # 8 palavras
    assert conf["frase"]("O gato dorme feliz no calor sol") is True        # 7 palavras
    assert conf["frase"]("Hoje cedo acordei com o sol.") is False          # 6 palavras
    # o gabarito do codigo e do calendario, calculados aqui e nao de cabeca
    import datetime
    assert (datetime.date(2025, 3, 1) - datetime.date(2024, 2, 28)).days == 367
    assert round(3 * 12 * 4.5 * 0.85, 2) == 137.70
    try:
        exec(CODIGO_BUG, {})
        raise AssertionError("o codigo deveria levantar")
    except IndexError:
        pass
    print("compara_modelos ok")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--modelos", default=",".join(MODELOS))
    ap.add_argument("--rodadas", type=int, default=1)
    ap.add_argument("--teste", action="store_true")
    a = ap.parse_args()
    if a.teste:
        _teste()
        sys.exit(0)
    print(tabela(compara([m.strip() for m in a.modelos.split(",") if m.strip()], max(1, a.rodadas))))
