#!/bin/sh
# Modo gamer: libera CPU, GPU e microfone para o jogo. Uso: tools/modo-gamer.sh liga|desliga
# Liga: fecha a janela, para a Fedora (ouvido e orb juntos) e o whisper.
# Desliga: sobe tudo de novo e reabre a janela. Com a Fedora parada, sair do modo é só por aqui (ela não ouve).
case "$1" in
  liga)
    pkill -f -- "--app=http://localhost:8720" || true
    systemctl --user stop fedora-assistente whisper
    notify-send -i input-gaming "Modo gamer ligado" "Fedora e whisper parados." ;;
  desliga)
    systemctl --user start whisper fedora-assistente
    setsid "$(dirname "$(readlink -f "$0")")/../abrir-janela.sh" >/dev/null 2>&1 &
    notify-send -i input-gaming "Modo gamer desligado" "Fedora de volta." ;;
  *) echo "uso: $0 liga|desliga" >&2; exit 2 ;;
esac
