# Fedora — guia para o Claude neste repositório

Assistente de voz local. `master` = Windows 11; Linux está na branch `linux` (congelada). Instalar: roteiro "Versões e instalação" do `README.md`, configurando conforme o SO da pessoa. Python: `server.py`, `commands.py`, `atalhos.py`, `conversa.py`, `runtime.py`, `motor_agy.py`, `motor_regra.py`, `agentes.py`,
`dupla.py`, `contexto.py`, `memory.py`, `listener.py`, `speech.py`, `tidy.py`, `llm.py`, `turnolog.py`, `retro.py`, `despachante.py`,
`notes.py`, `sysinfo.py`, `vad.py`, `orb.py`, `whisper_server.py`; UI em `ui/index.html`. Mapa completo no `README.md`.

- Nunca desligue, reinicie nem apague de vez: valem a lista `DENY` (`runtime.py`) e as `PC_RULES` (`commands.py`). Não afrouxe nenhuma.
- Testes: `tools\testa.ps1` (offline). Depois de mexer num módulo, rode o autoteste dele.
- Subagentes (`agentes/`: junior, writer, design, arquiteto, senior, qa; chamados por `agentes.py`) e níveis N1–N3: `docs/EQUIPE.md`. Motores, agentes e `dupla`: `docs/MOTORES.md`.
