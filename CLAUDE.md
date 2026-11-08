# Fedora — guia para o Claude neste repositório

Assistente de voz local. `master` = Windows 11; Linux está na branch `linux` (congelada). Instalar: roteiro "Versões e instalação" do `README.md`, configurando conforme o SO da pessoa. Python: `server.py`, `commands.py`, `atalhos.py`, `conversa.py`, `runtime.py`, `memory.py`,
`listener.py`, `speech.py`, `tidy.py`, `llm.py`, `turnolog.py`, `retro.py`; UI em `ui/index.html`. Mapa completo no `README.md`.

- Nunca desligue, reinicie nem apague de vez: valem a lista `DENY` (`runtime.py`) e as `PC_RULES` (`commands.py`). Não afrouxe nenhuma.
- Testes: `tools\testa.ps1` (offline). Depois de mexer num módulo, rode o autoteste dele.
- Subagentes (`.claude/agents/`: junior, senior, qa) e níveis N1–N3: `docs/EQUIPE.md`.
