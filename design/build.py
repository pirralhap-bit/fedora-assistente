"""Monta o protótipo: python3 build.py <dados.json> <saida.html>"""
import pathlib, sys
here = pathlib.Path(__file__).parent
data = pathlib.Path(sys.argv[1]).read_text().replace("</", "<\\/")
page = (here / "jarvis.src.html").read_text()
assert "/*DATA*/null" in page
pathlib.Path(sys.argv[2]).write_text(page.replace("/*DATA*/null", data, 1))
