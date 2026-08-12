"""Roda UM lote da campanha num rotulador Haiku headless (`claude -p`).

Existe para tirar o texto dos lotes do contexto do MAESTRO: no modo
subagente, cada lote custava ~40k tokens da janela dele (ler 80 itens e
repassá-los verbatim no despacho), o que limitava a sessão a 4-5 lotes.
Aqui o maestro só dispara este script e recebe UMA linha de resumo — o
texto viaja por stdin de um processo separado.

A garantia anti-cola vira ESTRUTURAL, e é o segundo motivo de existir:
o rotulador roda com cwd num diretório temporário VAZIO e com todas as
ferramentas proibidas por flag, então o `labeling/manifest.json` — que
carrega o gabarito em claro — fica fora de alcance por construção, não
por instrução. No modo subagente a única defesa era a frase "não use
ferramenta nenhuma".

O script NÃO valida conteúdo (uid inventado, enum errado): isso é do
`pf labels submit`, que é estrito e imprime a linha RETRY_UIDS que
dirige o retry. Aqui só se tolera a FORMA (cerca de código, prosa, BOM),
como o parser da CLI já tolera.

Uso:
  uv run python .claude/skills/rotular-prompts/rodar_lote.py <dir>/batch_0007.json
  ... --uids uid1,uid2 --nota "grafia dos enums"   # retry dirigido: mescla no jsonl
  ... --model claude-sonnet-5                      # escalada (2 falhas no Haiku)

Sai 0 quando o nº de linhas bate com o esperado; 2 quando o arquivo foi
escrito incompleto (submeta assim mesmo: o submit lista os uids que
faltam); 1 em falha dura (CLI ausente, timeout, saída sem JSON nenhum).
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

AQUI = Path(__file__).resolve().parent
TEMPLATE = AQUI / "despacho.md"
MODELO_PADRAO = "claude-haiku-4-5-20251001"
TIMEOUT_S = 900

# tudo proibido: o rotulador é puro cômputo por construção. A lista é
# explícita (e não "*") porque é o formato que o --disallowedTools aceita.
FERRAMENTAS_PROIBIDAS = [
    "Bash", "Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit",
    "WebFetch", "WebSearch", "Task", "TodoWrite",
]


def compor_despacho(lote: dict, uids: list[str] | None, nota: str | None) -> tuple[str, list[dict]]:
    template = TEMPLATE.read_text(encoding="utf-8")
    items = lote["items"]
    if uids:
        pedidos = set(uids)
        items = [it for it in items if it["uid"] in pedidos]
        faltando = pedidos - {it["uid"] for it in items}
        # retry com uid de fora do lote gravaria rótulo órfão — melhor parar
        if faltando:
            raise SystemExit(f"[lote] uids fora do lote: {sorted(faltando)}")

    prompt = (
        template
        .replace("<<BATCH_ID>>", lote["batch_id"])
        .replace("<<N_ITEMS>>", str(len(items)))
        .replace("<<ITEMS_JSON>>", json.dumps(items, ensure_ascii=False, indent=1))
    )
    if uids:
        aviso = (
            "\n\n**RETRY DIRIGIDO.** Estes itens falharam na validação da "
            "tentativa anterior. Atenção redobrada à grafia exata dos enums e "
            "às listas disjuntas de task_type e domain."
        )
        if nota:
            aviso += f" Motivo apontado: {nota}."
        prompt += aviso + "\n"
    return prompt, items


def rodar_claude(prompt: str, modelo: str) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit("[lote] claude CLI nao encontrado no PATH — use o fallback por subagente (SKILL.md)")
    cmd = [
        exe, "-p",
        "--model", modelo,
        "--output-format", "text",
        "--disallowedTools", *FERRAMENTAS_PROIBIDAS,
    ]
    # cwd num diretório temporário VAZIO: nada para um Read alcançar, e o
    # CLAUDE.md do projeto (que nada tem a dizer a um classificador) não carrega
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=td, timeout=TIMEOUT_S,
        )
    if proc.returncode != 0:
        raise SystemExit(f"[lote] claude -p saiu {proc.returncode}: {proc.stderr.strip()[:400]}")
    return proc.stdout


def extrair_linhas(saida: str) -> dict[str, str]:
    """Tolerante na forma: descarta cerca, prosa e linha quebrada; re-serializa.

    A re-serialização (json.dumps) normaliza espaçamento e escapes sem tocar
    no CONTEÚDO — valor errado continua errado e é o submit quem o recusa.
    """
    por_uid: dict[str, str] = {}
    for ln in saida.replace("﻿", "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        uid = obj.get("uid")
        if isinstance(uid, str) and uid:
            por_uid[uid] = json.dumps(obj, ensure_ascii=False)
    return por_uid


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("batch_json", type=Path)
    ap.add_argument("--uids", help="retry dirigido: so estes uids, mesclados no jsonl existente")
    ap.add_argument("--nota", help="motivo curto do retry, repassado ao rotulador")
    ap.add_argument("--model", default=MODELO_PADRAO)
    ap.add_argument("--saida", type=Path, help="default: <batch_id>.jsonl ao lado do json")
    args = ap.parse_args()

    lote = json.loads(args.batch_json.read_text(encoding="utf-8"))
    uids = [u.strip() for u in args.uids.split(",") if u.strip()] if args.uids else None
    destino = args.saida or args.batch_json.parent / f"{lote['batch_id']}.jsonl"

    t0 = time.monotonic()
    prompt, pedidos = compor_despacho(lote, uids, args.nota)
    novas = extrair_linhas(rodar_claude(prompt, args.model))
    dur = time.monotonic() - t0

    if not novas:
        print(f"[lote] {lote['batch_id']}: a saida nao trouxe NENHUMA linha de JSON — nada escrito")
        return 1

    # No retry, as linhas novas substituem as antigas pelo uid; a ordem final é
    # SEMPRE a do lote — é o contrato de saída do despacho, e o merge não pode
    # bagunçar o que a validação vai conferir.
    existentes: dict[str, str] = {}
    if uids and destino.exists():
        existentes = extrair_linhas(destino.read_text(encoding="utf-8"))
    finais = {**existentes, **novas}

    linhas = [finais[it["uid"]] for it in lote["items"] if it["uid"] in finais]
    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(linhas) + "\n")

    vieram = len(novas)
    pedidos_n = len(pedidos)
    ok = len(linhas) == len(lote["items"]) and vieram >= pedidos_n
    resumo = (
        f"[lote] {lote['batch_id']}: {len(linhas)}/{len(lote['items'])} linhas "
        f"({vieram} novas de {pedidos_n} pedidas) -> {destino} "
        f"(modelo={args.model}, {dur:.0f}s)"
    )
    print(resumo)
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
