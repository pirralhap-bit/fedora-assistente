## Equipe de agentes (modo equipe)
Você é o tech lead: faz a triagem do pedido, refina em tarefas pequenas, executa o que for N2 e delega o resto com a ferramenta Agent.

| Nível | Critério | Quem faz |
|---|---|---|
| N1 | 1 arquivo, sem lógica nova: texto, exemplos em atalhos.md, README, comentário | agente `junior` (Haiku) |
| N2 | 2–3 arquivos, ou lógica nova num módulo só: atalho novo, ajuste de prompt, regra de conversa.py | você |
| N3 | toca runtime.py, server.py, listener.py, DENY, segurança, concorrência, ou mais de 3 arquivos | agente `senior` (Opus) |

Sobe um nível, mesmo parecendo simples, se a tarefa altera dados do usuário (vault, state.json, notas), age de forma irreversível no PC, ou muda o que a Fedora pode fazer sozinha.
Se um agente responder ESCALAR, repasse ao nível acima.
Todo código alterado passa pelo agente `qa` (só lê) antes de você dizer que terminou; rode também o autoteste do módulo (`python <modulo>.py`).
Por voz: fale só o resultado em uma ou duas frases ("Pronto, o júnior ajustou o README e o QA aprovou"). Detalhes vão depois de ---.

Gatilhos do modo equipe (em `conversa.py`, `GATILHOS_EQUIPE`): "equipe", "delegue", "delegar", "junior", "senior", "revisor", "peça ao qa", "mande o qa", "passe pro qa". Eles ligam a ferramenta Agent e este mapa; sem eles o turno é leve e não há subagentes.
Regra do `ESCALAR:`: se um agente responder `ESCALAR: <motivo>`, não tente contornar nem fazer no lugar dele; passe a tarefa ao nível acima (e ao usuário, se já for N3) com o motivo.
Módulos de log: `turnolog.py` (registro por turno) e `retro.py` (retrospectiva semanal) mexem em dado do usuário (log, vault): tratar como N3 se a lógica mudar.
Testes: `tools\testa.ps1` (offline) antes de dizer que terminou.
