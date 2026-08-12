"""Mostra, para um lote, só os OUROS e onde o agente discordou do gabarito.

Existe porque `pf labels submit` devolve um número (o agreement) e o número
não diz QUAL fronteira falhou. Diagnosticar antes de repetir é a regra que a
campanha extraiu do episódio do gabarito corrompido: reprovação se lê item a
item, e a primeira suspeita é o gabarito, não o agente. Foi este diagnóstico
que transformou o 0,67 do batch_0005 em UMA convenção nova (material colado
manda mais que o tom da pergunta) em vez de um retry cego.

Uso: uv run python .claude/skills/rotular-prompts/diag_lote.py <batch_id> <resposta.jsonl> <lote.json>
"""

import json
import sys
from pathlib import Path

RAIZ = Path(__file__).resolve().parents[3]

batch_id, resp_path, lote_path = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])

manifest = json.loads((RAIZ / "labeling" / "manifest.json").read_text(encoding="utf-8"))
gold = manifest["gold"]
gold_uids = manifest["batches"][batch_id]["gold_uids"]

# texto dos itens, para o diagnóstico não ser só um par de rótulos soltos
textos = {
    it["uid"]: it["text"]
    for it in json.loads(lote_path.read_text(encoding="utf-8"))["items"]
}
agente = {
    json.loads(ln)["uid"]: json.loads(ln)
    for ln in resp_path.read_text(encoding="utf-8").splitlines()
    if ln.strip()
}

acertos = 0
for uid in gold_uids:
    g, a = gold[uid], agente[uid]
    for campo in ("task_type", "domain"):
        igual = g[campo] == a[campo]
        acertos += igual
        marca = "  ok " if igual else "  XX "
        print(f"{marca}{uid} {campo:14s} ouro={g[campo]:24s} agente={a[campo]}")
    print(f"      texto: {textos[uid][:260]!r}")
    print()

print(f"agreement = {acertos}/6 = {acertos / 6:.2f}")
