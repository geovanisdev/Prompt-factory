"""Diz POR QUE cada pedido de um lote foi recusado — sem despejar o material.

O maestro da campanha não pode ler o arquivo do lote: são ~72 mil caracteres de
material didático com copyright integral, e mantê-los fora do contexto é a razão
de o runner ser headless. Mas quando um lote reprova alguém precisa saber o que
houve, e "recorte não é substring EXATA" sozinho não diz o que corrigir.

Este script é o caminho estreito: ele roda a MESMA validação da CLI
(`destilacao.validar`, nunca uma segunda cópia da regra) e, para o erro que mais
acontece — a cópia verbatim do recorte —, mostra **onde** a cópia divergiu: o
tamanho do prefixo que ainda casava, o caractere que quebrou e algumas dezenas
de caracteres de contexto dos dois lados. Dezenas, não milhares.

A divergência é achada por busca binária sobre o comprimento do prefixo, porque
"o maior prefixo que ainda é substring" é monotônico: se um prefixo de tamanho k
não está no texto, nenhum maior está.

Uso:
  uv run python .claude/skills/destilar-pedidos/diag_lote.py <lote>.json <resposta>.json
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from prompt_factory.annotate import destilacao as dmod

CONTEXTO = 40


def _visivel(texto: str) -> str:
    """Torna visível o que engana o olho: quebra, tabulação e espaço no fim."""
    return texto.replace("\n", "⏎").replace("\t", "→").replace(" ", "·")


def divergencia(recorte: str, janela: str) -> str:
    """Onde a cópia parou de casar. Mostra dezenas de caracteres, nunca o texto."""
    if recorte in janela:
        return "(o recorte CASA nesta janela — o erro é outro)"
    baixo, alto = 0, len(recorte)
    while baixo < alto:
        meio = (baixo + alto + 1) // 2
        if recorte[:meio] in janela:
            baixo = meio
        else:
            alto = meio - 1
    k = baixo
    if k == 0:
        return "nem os primeiros caracteres casam — o recorte parece ser de outra janela"

    pos = janela.find(recorte[:k])
    esperado = janela[pos + k : pos + k + 12]
    veio = recorte[k : k + 12]
    antes = recorte[max(0, k - CONTEXTO) : k]
    return (
        f"casou {k} de {len(recorte)} caracteres e divergiu no {k + 1}º\n"
        f"        …{_visivel(antes)}\n"
        f"        o arquivo tem: {_visivel(esperado)!r}\n"
        f"        o pedido tem:  {_visivel(veio)!r}"
    )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("lote_json", type=Path)
    ap.add_argument("resposta_json", type=Path)
    args = ap.parse_args()

    lote = json.loads(args.lote_json.read_text(encoding="utf-8"))
    resposta = json.loads(args.resposta_json.read_text(encoding="utf-8"))
    janelas = {str(j["janela_id"]): str(j["texto"]) for j in lote["janelas"]}

    # A MESMA função que a CLI chama. Uma segunda implementação da validação
    # aqui diagnosticaria um erro que o import não comete (ou vice-versa).
    pedidos, erros, avisos = dmod.validar(lote, resposta)

    brutos = resposta.get("pedidos") or resposta.get("itens") or []
    print(f"[diag] {lote['lote_id']}: {len(brutos)} pedido(s) na resposta, "
          f"{len(pedidos)} passariam, {len(erros)} erro(s), {len(avisos)} aviso(s)")

    for erro in erros:
        print(f"\n[diag] ERRO {erro}")
        if "substring EXATA" not in erro:
            continue
        # O erro nomeia a janela e a posição: `ped_0001/j02, pedido 3: ...`
        try:
            cabeca = erro.split(":", 1)[0]
            jid = cabeca.split("/")[1].split(",")[0].strip()
            i = int(cabeca.rsplit("pedido", 1)[1].strip()) - 1
        except (IndexError, ValueError):
            continue
        recorte = str(brutos[i].get("recorte", ""))
        print(f"        {divergencia(recorte, janelas.get(jid, ''))}")

    for aviso in avisos:
        print(f"[diag] aviso {aviso}")

    # A distribuição do que PASSOU: é o que diz se o lote está diverso ou se o
    # destilador achou o mesmo tipo de pedido em toda janela.
    if pedidos:
        for campo in ("task_type", "papel", "serie", "dificuldade"):
            conta: dict[str, int] = {}
            for p in pedidos:
                conta[p[campo]] = conta.get(p[campo], 0) + 1
            print(f"[diag] {campo}: " + ", ".join(f"{k} {v}" for k, v in sorted(conta.items())))
    return 0


if __name__ == "__main__":
    sys.exit(main())
