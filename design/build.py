"""Monta os dois protótipos do HUD (painel limpo e filme) a partir de body.html + hud.js + CSS.
Uso: python3 build.py <graph.json> <pasta-de-saida>"""
import pathlib, sys

here = pathlib.Path(__file__).parent
data = pathlib.Path(sys.argv[1]).read_text().replace("</", "<\\/")
out = pathlib.Path(sys.argv[2])
body = (here / "body.html").read_text()
js = (here / "hud.js").read_text()
clean = (here / "clean.css").read_text()
FONTS = '<link rel="preconnect" href="https://fonts.googleapis.com">\n<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>\n<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family={}&display=swap">'

variants = {
    "jarvis-hud.html": ("Jarvis HUD", clean,
        FONTS.format("Geist:wght@400;500;600&family=Geist+Mono:wght@400;500")
        + '\n<meta name="theme-color" content="#f6f7f5" media="(prefers-color-scheme: light)">'
        + '\n<meta name="theme-color" content="#0e1011" media="(prefers-color-scheme: dark)">'),
    "jarvis-hud-filme.html": ("Jarvis HUD Filme", clean + "\n" + (here / "film.css").read_text(),
        FONTS.format("Rajdhani:wght@500;600&family=Share+Tech+Mono")
        + '\n<meta name="theme-color" content="#02070b">'),
}
for name, (title, css, head) in variants.items():
    page = body
    for k, v in (("/*TITLE*/", title), ("/*HEAD*/", head), ("/*CSS*/", css), ("/*JS*/", js), ("/*DATA*/null", data)):
        assert k in page, k
        page = page.replace(k, v, 1)
    (out / name).write_text(page)
    print(name, len(page))
