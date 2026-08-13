"""Roda UM lote da campanha de destilação num destilador headless (`claude -p`).

Existe pelos DOIS motivos do runner da rotulagem (M6-B), e aqui o segundo pesa
mais que lá:

1. **O texto não entra no contexto do maestro.** Um lote de 6 janelas de 12.000
   caracteres são ~72k caracteres; lidos e repassados por um subagente, cada
   lote custaria a janela inteira do maestro.
2. **O material é de terceiros, com copyright integral.** A regra da campanha é
   que ele não entra no prompt, no corpus, no git nem em export — e "nem no
   contexto de quem opera" é a extensão natural disso. Aqui a garantia é
   ESTRUTURAL: o processo roda com cwd num diretório temporário vazio e todas as
   ferramentas proibidas por flag, então ele não alcança o material nem o
   repositório. O maestro toca só o CAMINHO do arquivo.

O script NÃO valida conteúdo (recorte adulterado, task_type inventado, código
BNCC torto): isso é do `pf annotate pedidos importar`, que é estrito e nomeia a
janela de cada erro. Aqui só se tolera a FORMA — linha que não é JSON, cerca de
código, prosa, BOM.

POR QUE JSONL NA SAÍDA E OBJETO NO ARQUIVO
==========================================
O destilador devolve **uma linha por pedido**. Um objeto JSON único de ~40 KB
truncado no fim é irrecuperável inteiro; em JSONL, a última linha cortada é
descartada e as outras sobrevivem — a mesma razão da campanha de rotulagem. O
runner é quem monta o `{"lote_id": ..., "pedidos": [...]}` que o import espera.

MODELO PADRÃO
=============
Sonnet, e não Haiku como na rotulagem. As duas tarefas não são a mesma coisa:
lá se escolhe 1 rótulo entre 16 sobre um texto curto; aqui se copia **verbatim**
um trecho de 200 a 2.000 caracteres e se escreve uma meta pedagógica defensável.
A cópia exata é o que mais falha, e é ela que a validação recusa. `--model` está
aí para medir o contrário — se o Haiku der a mesma taxa de erro, ele é melhor
escolha, e isso se mede, não se supõe.

Uso:
  uv run python .claude/skills/destilar-pedidos/rodar_lote.py <dir>/ped_0001.json
  ... --janelas j02,j05 --nota "recorte com espaço a mais no fim"  # retry dirigido
  ... --model claude-haiku-4-5-20251001

Sai 0 quando vieram pedidos; 2 quando o lote saiu VAZIO (pode ser legítimo —
capa, créditos, sumário — mas o maestro precisa olhar); 1 em falha dura.
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
MODELO_PADRAO = "claude-sonnet-5"
TIMEOUT_S = 900

# Tudo proibido: o destilador é puro cômputo por construção. A lista é explícita
# (e não "*") porque é o formato que o --disallowedTools aceita.
FERRAMENTAS_PROIBIDAS = [
    "Bash", "Read", "Glob", "Grep", "Edit", "Write", "NotebookEdit",
    "WebFetch", "WebSearch", "Task", "TodoWrite",
]


def compor_despacho(
    lote: dict, janelas_pedidas: list[str] | None, nota: str | None
) -> tuple[str, list[dict]]:
    template = TEMPLATE.read_text(encoding="utf-8")
    janelas = lote["janelas"]
    if janelas_pedidas:
        alvo = set(janelas_pedidas)
        janelas = [j for j in janelas if j["janela_id"] in alvo]
        faltando = alvo - {j["janela_id"] for j in janelas}
        # retry citando janela de fora do lote produziria pedido órfão
        if faltando:
            raise SystemExit(f"[lote] janelas fora do lote: {sorted(faltando)}")

    limites = lote["limites"]
    task_types = "\n".join(
        f"- `{op['id']}` — {op.get('definicao') or op['rotulo']}"
        for op in lote["vocabulario"]["task_type"]
    )
    excluidos = ", ".join(f"`{x}`" for x in lote["task_types_excluidos"]["ids"])

    trocas = {
        "<<LOTE_ID>>": lote["lote_id"],
        "<<ARQUIVO>>": lote["arquivo_fonte"],
        "<<COLECAO>>": lote.get("colecao") or "(não identificada)",
        "<<DISCIPLINA>>": lote.get("disciplina") or "(não identificada)",
        "<<SERIE_SUGERIDA>>": lote.get("serie_sugerida") or "(nenhuma — volume único)",
        "<<N_JANELAS>>": str(len(janelas)),
        "<<MAX_POR_JANELA>>": str(limites["max_pedidos_por_janela"]),
        "<<RECORTE_MIN>>": str(limites["recorte_min_chars"]),
        "<<RECORTE_MAX>>": str(limites["recorte_max_chars"]),
        "<<MIN_TEMA>>": str(limites["min_chars_tema"]),
        "<<MIN_META>>": str(limites["min_chars_meta_pedagogica"]),
        "<<TASK_TYPES>>": task_types,
        "<<EXCLUIDOS>>": excluidos,
        "<<PORQUE_EXCLUIDOS>>": lote["task_types_excluidos"]["porque"],
        "<<JANELAS_JSON>>": json.dumps(janelas, ensure_ascii=False, indent=1),
    }
    prompt = template
    for marca, valor in trocas.items():
        prompt = prompt.replace(marca, valor)

    # A nota vale COM ou SEM `--janelas`: uma escalada de modelo sobre o lote
    # inteiro também é um retry, e a lição da tentativa anterior é justamente o
    # que ela tem a acrescentar. Amarrar a nota ao filtro faria `--nota` sozinho
    # não fazer nada — uma flag que o usuário passa e o programa ignora em
    # silêncio é pior que uma flag que não existe.
    if janelas_pedidas or nota:
        alvo = "Estas janelas falharam" if janelas_pedidas else "Este lote falhou"
        aviso = (
            f"\n\n**RETRY.** {alvo} na validação da tentativa anterior. Atenção "
            "redobrada à cópia VERBATIM do recorte — é o que mais falha. A quebra de "
            "linha do arquivo é `\\n` e tem de sair como `\\n`: trocá-la por espaço "
            "invalida o recorte inteiro. Na dúvida sobre a cópia exata, escolha outro "
            "trecho."
        )
        if nota:
            aviso += f" Motivo apontado: {nota}."
        prompt += aviso + "\n"
    return prompt, janelas


def rodar_claude(prompt: str, modelo: str) -> str:
    exe = shutil.which("claude")
    if not exe:
        raise SystemExit(
            "[lote] claude CLI nao encontrado no PATH — use o fallback por subagente (SKILL.md)"
        )
    cmd = [
        exe, "-p",
        "--model", modelo,
        "--output-format", "text",
        "--disallowedTools", *FERRAMENTAS_PROIBIDAS,
    ]
    # cwd num diretório temporário VAZIO: nada para um Read alcançar, e o
    # CLAUDE.md do projeto (que nada tem a dizer a um destilador) não carrega.
    with tempfile.TemporaryDirectory() as td:
        proc = subprocess.run(
            cmd, input=prompt, capture_output=True, text=True,
            encoding="utf-8", errors="replace", cwd=td, timeout=TIMEOUT_S,
        )
    if proc.returncode != 0:
        raise SystemExit(f"[lote] claude -p saiu {proc.returncode}: {proc.stderr.strip()[:400]}")
    return proc.stdout


def extrair_pedidos(saida: str) -> list[dict]:
    """Tolerante na forma: descarta cerca, prosa e linha quebrada.

    NÃO re-serializa nem toca no conteúdo — ao contrário do runner da rotulagem,
    onde a re-serialização normalizava espaçamento sem risco. Aqui um dos campos
    é uma cópia VERBATIM que a validação confere caractere a caractere: qualquer
    passe de normalização, por inofensivo que pareça, poderia ser a diferença
    entre o recorte conferir e não conferir.
    """
    pedidos: list[dict] = []
    for ln in saida.replace("﻿", "").splitlines():
        ln = ln.strip()
        if not ln.startswith("{"):
            continue
        try:
            obj = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and obj.get("janela_id"):
            pedidos.append(obj)
    return pedidos


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("lote_json", type=Path)
    ap.add_argument("--janelas", help="retry dirigido: so estas janelas (ex.: j02,j05)")
    ap.add_argument("--nota", help="motivo curto do retry, repassado ao destilador")
    ap.add_argument("--model", default=MODELO_PADRAO)
    ap.add_argument("--saida", type=Path, help="default: <lote_id>.resposta.json ao lado do json")
    args = ap.parse_args()

    lote = json.loads(args.lote_json.read_text(encoding="utf-8"))
    pedidas = [j.strip() for j in args.janelas.split(",") if j.strip()] if args.janelas else None
    destino = args.saida or args.lote_json.parent / f"{lote['lote_id']}.resposta.json"

    t0 = time.monotonic()
    prompt, janelas = compor_despacho(lote, pedidas, args.nota)
    novos = extrair_pedidos(rodar_claude(prompt, args.model))
    dur = time.monotonic() - t0

    # No retry, os pedidos das janelas reprocessadas SUBSTITUEM os anteriores;
    # os das outras janelas ficam. Sem isso, um retry de uma janela jogaria fora
    # o trabalho bom das outras cinco.
    finais = novos
    if pedidas and destino.exists():
        try:
            antigos = json.loads(destino.read_text(encoding="utf-8")).get("pedidos", [])
        except (OSError, json.JSONDecodeError):
            antigos = []
        alvo = set(pedidas)
        finais = [p for p in antigos if p.get("janela_id") not in alvo] + novos

    # A ordem final é SEMPRE a das janelas do lote — o import nomeia a janela em
    # cada erro, e uma resposta fora de ordem tornaria o retry dirigido confuso.
    ordem = {j["janela_id"]: i for i, j in enumerate(lote["janelas"])}
    finais.sort(key=lambda p: ordem.get(str(p.get("janela_id")), 999))

    with open(destino, "w", encoding="utf-8", newline="\n") as f:
        json.dump(
            {"lote_id": lote["lote_id"], "pedidos": finais}, f, ensure_ascii=False, indent=1
        )
        f.write("\n")

    # A cobertura é do ARQUIVO FINAL contra o lote inteiro, não contra as janelas
    # desta chamada: num retry dirigido `janelas` tem 1 item e `finais` cobre as 8
    # do merge, e a razão saía como "7/1 janela(s)" — um número que não descreve
    # nada. O que a chamada fez fica na segunda metade da linha.
    cobertas = len({str(p.get("janela_id")) for p in finais})
    escopo = (
        f" [retry de {len(janelas)}: {len(novos)} novo(s)]" if pedidas else ""
    )
    print(
        f"[lote] {lote['lote_id']}: {len(finais)} pedido(s) em "
        f"{cobertas}/{len(lote['janelas'])} janela(s) -> {destino} "
        f"(modelo={args.model}, {dur:.0f}s){escopo}"
    )
    if not finais:
        # Vazio PODE ser legítimo (capa, créditos, sumário), mas também é como
        # uma falha silenciosa do modelo se parece. Sai 2 para o maestro olhar.
        print("[lote] nenhum pedido veio — confira se as janelas são de conteúdo didático")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
