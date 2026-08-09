"""Livro-caixa da campanha de GERAÇÃO (P4c): material e anotações sintéticas.

O QUE ESTE MÓDULO EXISTE PARA RESOLVER
======================================
Duas das quatro abas da Bancada têm **teto estrutural**: ``avaliar_rubrica``
precisa de rubrica ativa **e** de uma resposta de modelo; ``comparar_ab``
precisa de **duas** respostas. Um prompt cru do corpus não tem nada disso, e o
pacote de demonstração cobre 11 itens. Sem material, as duas telas mais caras da
plataforma abrem vazias.

E há um segundo teto, menos óbvio e pior: **se o dono anotar bem — e vai, é o
portfólio dele —, as escalas do Rate and Review nunca são exercitadas.** Tudo
cairia em ``adequado``/``excepcional`` e a tela mais sofisticada da plataforma
mostraria dois dos quatro botões nunca usados. Por isso a campanha B produz
anotações **de qualidade deliberadamente variada**, cada uma com a nota que ela
deveria receber gravada escondida em ``anotacoes.gabarito_avaliacao_json``.

A DIVISÃO DE TRABALHO, E ELA NÃO SE NEGOCIA
===========================================
É a mesma de ``labeling_io`` + ``.claude/skills/rotular-prompts``:

* o **agente gerador é puro cômputo** — lê o JSON de um lote, devolve JSON como
  texto, e não escreve arquivo nenhum do repositório;
* **só o fio principal grava**, pela CLI (``pf annotate gerar``), com validação
  estrita antes de encostar no banco.

Se o agente escrevesse no banco, duas sessões paralelas se sobrescreveriam e a
validação estrita seria contornável — o que é o mesmo que não existir.

O SINAL DE "SINTÉTICA" É UM SÓ
==============================
``anotacoes.gabarito_avaliacao_json IS NOT NULL``. Não há segunda marca, e a
ausência de uma segunda marca é a decisão: duas marcas divergem, e no dia em que
divergissem ninguém saberia qual acreditar. NULL significa **trabalho humano**,
e é isso que o export do P5b e o painel do P5a leem.

MÁQUINA DE ESTADOS DE UM LOTE
=============================
::

    pending --preparar--> claimed --importar--> done
       ^                     |
       |                     +--N falhas / TTL--> failed
       |                                            |
       +------------ preparar --lote (revive) ------+

Falha de VALIDAÇÃO não muda o estado — o lote segue ``claimed`` e o maestro faz
um retry dirigido, exatamente como no ``pf labels submit``. O que muda o estado
é a contagem: ao atingir ``[geracao] max_tentativas`` o lote cai em ``failed``,
para não travar o ciclo num item só.

POR QUE O LOTE VAI PARA UM ARQUIVO
==================================
A saída de ferramenta do harness trunca em ~30k caracteres e um lote de dezenas
de prompts do corpus passa disso com folga. É a mesma pegadinha que o M5 já
documentou, e a razão de ``--out-dir`` existir aqui desde o primeiro dia.
"""

from __future__ import annotations

import json
import random
import re
import sqlite3
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .. import paths
from ..config import get as _cfg
from ..labeling_io import escrever_json_atomico, escrever_texto_atomico
from . import briefs as briefmod
from . import catalogo as catmod
from . import db as adb
from . import diretrizes as dirmod
from . import eventos as evmod
from . import payloads
from . import seed as seedmod
from . import tarefas as tmod

#: Contrato do arquivo de lote e do arquivo de resposta. Viaja DENTRO dos dois
#: arquivos, pela razão do ``payload_schema``: a versão anda com o dado, e um
#: lote preparado hoje continua legível quando o formato evoluir.
CONTRATO = "geracao@1"

#: As duas campanhas. ``material`` = rubrica + duas respostas por prompt (o que
#: levanta o teto das abas); ``anotacoes`` = anotações sintéticas com nota-alvo
#: escondida (o que exercita as escalas do Rate and Review).
CAMPANHAS: tuple[str, ...] = ("material", "anotacoes")

#: Prefixo do id de lote por campanha. Legível de relance no manifest e no
#: nome do arquivo: ``mat_0003.json`` diz o que é sem abrir.
PREFIXO_LOTE: dict[str, str] = {"material": "mat", "anotacoes": "anot"}

#: Estados de um lote. Os mesmos nomes do ``labeling_io`` de propósito: quem
#: opera as duas campanhas não precisa aprender dois vocabulários.
PENDING, CLAIMED, DONE, FAILED = "pending", "claimed", "done", "failed"

#: Transições permitidas. Fora desta tabela, ``ValueError``.
TRANSICOES: frozenset[tuple[str, str]] = frozenset(
    {
        (PENDING, CLAIMED),
        (CLAIMED, DONE),
        (CLAIMED, PENDING),  # TTL de claim órfão
        (CLAIMED, FAILED),   # tentativas esgotadas
        (FAILED, PENDING),   # revival explícito (`preparar --lote`)
    }
)

#: Corte do texto do prompt dentro do lote. O agente precisa do prompt INTEIRO
#: para escrever uma resposta defensável — ao contrário da rotulagem, onde 1.900
#: caracteres bastavam para classificar. O pool já limita a 2.000 caracteres
#: (``[annotate] pool_fallback_max_chars``), então este teto quase nunca morde;
#: ele existe para o caso de o pool vir de uma coleção curada à mão.
MAX_CHARS_PROMPT = 6_000


# ---------------------------------------------------------------------------
# catálogos ABERTOS de defeito
# ---------------------------------------------------------------------------
#
# Abertos, e não CHECK no banco (ver o comentário de `gabarito_avaliacao_json`
# no DDL): o catálogo cresce a cada rodada, e migrar um banco com trabalho
# humano dentro só para registrar o NOME de uma família nova seria absurdo. A
# validação confere contra a lista conhecida e **avisa**; nome novo passa.

#: Defeitos de RESPOSTA DE MODELO — as quatro famílias herdadas do
#: ``fixtures/demo_pack.json``, mais ``nenhum`` para a resposta defensável do
#: par. São as que um linguista pega e um leitor apressado não.
DEFEITOS_RESPOSTA: dict[str, str] = {
    "fato-inventado-com-precisao": (
        "afirma um dado técnico inventado com cara de conferido (casa decimal, "
        "marca, artigo de lei, limiar) e apoia a resposta inteira nele"
    ),
    "restricao-ignorada": (
        "desobedece a uma restrição que o pedido enumerou — o ingrediente que a "
        "pessoa não tem, o número de itens, o formato, o registro pedido"
    ),
    "registro-errado": (
        "acerta o conteúdo e erra o tom: informal onde o pedido era formal, "
        "jargão onde a pessoa pediu simples, ou o contrário"
    ),
    "fluencia-cobrindo-vazio": (
        "prosa bem construída, cortês e organizada que não entrega nada: "
        "reafirma o problema com vocabulário melhor e devolve a pergunta"
    ),
    "nenhum": "a resposta defensável do par — a que deve vencer o A/B",
}

#: Defeitos de ANOTAÇÃO. **São outros**, e é isso que faz o Rate and Review
#: valer: um revisor treinado a pegar resposta ruim não é o mesmo que um revisor
#: treinado a pegar avaliação ruim. ``nenhum`` cobre a anotação boa (as fatias
#: ``adequado`` e ``excepcional`` da distribuição).
DEFEITOS_ANOTACAO: dict[str, str] = {
    "nota-nao-bate-com-justificativa": (
        "a nota diz uma coisa e o texto ao lado dela diz outra — nota alta com "
        "justificativa que descreve um problema, ou o inverso"
    ),
    "justificativa-generica": (
        "uma justificativa que serviria para QUALQUER item: 'a resposta atende "
        "parcialmente ao critério', sem citar nada do texto julgado"
    ),
    "criterio-nao-observavel": (
        "julga o que não está no texto — a intenção de quem escreveu, o que a "
        "resposta 'provavelmente faria', o modelo que a produziu"
    ),
    "restricao-do-prompt-ignorada": (
        "avalia sem levar em conta uma restrição que o PROMPT enumerou, e por "
        "isso premia uma resposta que a violou"
    ),
    "nota-inflada-em-bloco": (
        "notas altas em todos os critérios, sem discriminar — a rubrica deixa "
        "de medir e vira um carimbo"
    ),
    "nota-deflacionada-em-bloco": (
        "notas baixas em todos os critérios, sem discriminar; o espelho do "
        "anterior, e igualmente inútil"
    ),
    "justificativa-na-lingua-errada": (
        "escreve a justificativa em português onde a convenção da plataforma "
        "pede inglês (metadado é o que o cliente lê) — violação de convenção é "
        "defeito real e é ótimo material de calibração"
    ),
    "nenhum": "anotação sem defeito plantado — as fatias adequado/excepcional",
}


def familias_conhecidas(campanha: str) -> dict[str, str]:
    """O catálogo da campanha. Aberto: nome fora dele é AVISO, nunca erro."""
    return DEFEITOS_RESPOSTA if campanha == "material" else DEFEITOS_ANOTACAO


# ---------------------------------------------------------------------------
# configuração
# ---------------------------------------------------------------------------


def distribuicao() -> dict[str, float]:
    """A distribuição de qualidade-alvo das sintéticas, normalizada em 1,0.

    Default (``[annotate] sinteticas_distribuicao``): 15% excepcional, 30%
    adequado, 40% ajustável, 15% inutilizável. Exercita as QUATRO posições da
    escala 1 do Rate and Review — que é o motivo inteiro da campanha B.

    Uma chave fora de ``db.AVALIACOES_ANTES`` é erro de configuração e para
    aqui: um alvo que a escala não conhece produziria uma sintética impossível
    de avaliar, e o defeito só apareceria na tela.
    """
    bruto = _cfg(
        "annotate",
        "sinteticas_distribuicao",
        default={
            "excepcional": 0.15,
            "adequado": 0.30,
            "ajustavel": 0.40,
            "inutilizavel": 0.15,
        },
    )
    if not isinstance(bruto, Mapping) or not bruto:
        raise ValueError("[annotate] sinteticas_distribuicao precisa ser uma tabela não vazia")
    desconhecidas = sorted(set(bruto) - set(adb.AVALIACOES_ANTES))
    if desconhecidas:
        raise ValueError(
            f"[annotate] sinteticas_distribuicao cita avaliação fora da escala: "
            f"{', '.join(desconhecidas)} (conhecidas: {', '.join(adb.AVALIACOES_ANTES)})"
        )
    pesos = {k: float(v) for k, v in bruto.items() if float(v) > 0}
    total = sum(pesos.values())
    if total <= 0:
        raise ValueError("[annotate] sinteticas_distribuicao só tem pesos zerados")
    return {k: v / total for k, v in pesos.items()}


def status_inicial_padrao() -> str:
    """Onde a sintética ENTRA: ``pendente_triagem`` ou ``pendente_avaliacao``.

    Escolhível porque o dono vai querer exercitar os dois caminhos: triar (e
    decidir se aprova) ou cair direto no Rate and Review. O default é a triagem,
    que é o caminho completo — pular a passagem 1 numa demonstração de QC seria
    esconder metade do aparato.
    """
    valor = str(_cfg("annotate", "sinteticas_status", default="pendente_triagem"))
    return validar_status_inicial(valor)


def validar_status_inicial(valor: str) -> str:
    if valor not in STATUS_INICIAIS:
        raise ValueError(
            f"status inicial inválido: {valor!r} (aceitos: {', '.join(STATUS_INICIAIS)})"
        )
    return valor


#: Os dois pontos de entrada de uma sintética. ``avaliada`` e companhia ficam de
#: fora: uma anotação que nasce no fim do funil não atravessou nenhuma passagem
#: e mediria um QC que não aconteceu.
STATUS_INICIAIS: tuple[str, ...] = ("pendente_triagem", "pendente_avaliacao")


def max_tentativas() -> int:
    return int(_cfg("geracao", "max_tentativas", default=3))


def claim_ttl_horas() -> float:
    return float(_cfg("geracao", "claim_ttl_horas", default=6))


def min_chars_resposta() -> int:
    return int(_cfg("geracao", "min_chars_resposta", default=200))


def lang_confianca_minima() -> float:
    """Piso de confiança do detector para RECUSAR por idioma. Ver ``_lingua``."""
    return float(_cfg("geracao", "lang_confianca_min", default=0.50))


# ---------------------------------------------------------------------------
# caminhos e manifest
# ---------------------------------------------------------------------------


class GeracaoPaths:
    """Onde mora cada artefato da campanha.

    Existe pela mesma razão de ``LabelingPaths``: os testes rodam a campanha
    inteira dentro de um ``tmp_path`` sem encostar em ``geracao/``.
    """

    __slots__ = ("lotes", "manifest", "raiz", "respostas")

    def __init__(self, raiz: Path | str | None = None) -> None:
        self.raiz = Path(raiz) if raiz is not None else paths.GERACAO
        self.lotes = self.raiz / "lotes"
        self.respostas = self.raiz / "respostas"
        self.manifest = self.raiz / "manifest.json"

    def preparar(self) -> None:
        for d in (self.lotes, self.respostas):
            d.mkdir(parents=True, exist_ok=True)

    def lote(self, lote_id: str) -> Path:
        return self.lotes / f"{lote_id}.json"

    def resposta(self, lote_id: str) -> Path:
        return self.respostas / f"{lote_id}.json"

    def __repr__(self) -> str:  # pragma: no cover - conveniência de depuração
        return f"GeracaoPaths({self.raiz})"


def agora() -> str:
    """Instante atual em ISO-8601 UTC. Separado para o teste congelar o relógio."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ler_iso(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def manifest_vazio() -> dict[str, Any]:
    return {"contrato": CONTRATO, "criado_em": agora(), "lotes": {}}


def carregar_manifest(gp: GeracaoPaths | None = None) -> dict[str, Any]:
    """Lê o manifest, ou devolve um vazio.

    Ao contrário do ``labeling_io``, manifest ausente **não** é erro: a campanha
    de geração começa pelo ``preparar``, e não por um ``make-seed`` que precisa
    existir antes. Um ``pf annotate gerar status`` num clone limpo responde
    "nenhum lote", que é a verdade.
    """
    gp = gp or GeracaoPaths()
    if not gp.manifest.is_file():
        return manifest_vazio()
    with gp.manifest.open(encoding="utf-8") as fh:
        dados: dict[str, Any] = json.load(fh)
    contrato = dados.get("contrato")
    if contrato != CONTRATO:
        raise SystemExit(
            f"[pf] manifest da geração está no contrato {contrato!r} e o código está em "
            f"{CONTRATO!r} — o formato do lote mudou no meio da campanha, e isso é "
            "decisão humana"
        )
    return dados


def salvar_manifest(dados: Mapping[str, Any], gp: GeracaoPaths | None = None) -> Path:
    gp = gp or GeracaoPaths()
    gp.preparar()
    return escrever_json_atomico(gp.manifest, dados)


def _transicionar(registro: dict[str, Any], destino: str, lote_id: str) -> None:
    origem = str(registro.get("status", PENDING))
    if origem == destino:
        raise ValueError(f"{lote_id}: já está em {destino!r}")
    if (origem, destino) not in TRANSICOES:
        permitidas = sorted(d for o, d in TRANSICOES if o == origem)
        raise ValueError(
            f"{lote_id}: transição {origem!r} -> {destino!r} não é permitida "
            f"(permitidas de {origem!r}: {', '.join(permitidas) or 'nenhuma'})"
        )
    registro["status"] = destino


def varrer_orfaos(
    manifest: dict[str, Any], ttl_horas: float | None = None
) -> list[str]:
    """Devolve a ``pending`` os lotes ``claimed`` que passaram do TTL.

    Uma sessão que morreu (cota, Ctrl+C, janela fechada) deixa lotes
    reivindicados e nunca importados. Sem esta varredura eles ficariam presos e
    o ``preparar`` seguinte escolheria prompts NOVOS, deixando um buraco
    permanente no meio da campanha. Muta o manifest em memória; quem salva é
    quem chamou.
    """
    limite = datetime.now(UTC) - timedelta(
        hours=claim_ttl_horas() if ttl_horas is None else ttl_horas
    )
    devolvidos: list[str] = []
    for lote_id, registro in sorted(manifest.get("lotes", {}).items()):
        if registro.get("status") != CLAIMED:
            continue
        quando = _ler_iso(registro.get("claimed_at"))
        if quando is not None and quando > limite:
            continue
        _transicionar(registro, PENDING, lote_id)
        registro["claimed_at"] = None
        registro["motivo"] = f"claim órfão (TTL de {claim_ttl_horas():g} h)"
        devolvidos.append(lote_id)
    return devolvidos


def proximo_id(manifest: Mapping[str, Any], campanha: str) -> str:
    """``mat_0004`` — o sucessor, contado pelo MANIFEST e nunca por ``listdir``.

    A mesma disciplina do índice de part-file do WildChat: um arquivo órfão no
    disco (um lote preparado numa sessão que caiu antes de salvar o manifest)
    faria ``listdir`` pular um número e o lote seguinte sobrescrever o anterior.
    """
    prefixo = PREFIXO_LOTE[campanha]
    usados = [
        int(m.group(1))
        for lote_id in manifest.get("lotes", {})
        if (m := re.fullmatch(rf"{prefixo}_(\d{{4}})", lote_id))
    ]
    return f"{prefixo}_{(max(usados) + 1) if usados else 1:04d}"


def uids_ja_na_campanha(manifest: Mapping[str, Any]) -> set[str]:
    """Os uids que algum lote de MATERIAL vivo já cobre.

    ``failed`` fica de fora de propósito: um lote que esgotou as tentativas
    liberou os prompts dele, e um ``preparar`` seguinte pode tentar de novo com
    outros itens. ``pending``, ``claimed`` e ``done`` seguram.
    """
    reservados: set[str] = set()
    for registro in manifest.get("lotes", {}).values():
        if registro.get("campanha") != "material" or registro.get("status") == FAILED:
            continue
        reservados.update(str(u) for u in registro.get("uids", []))
    return reservados


def pares_ja_na_campanha(manifest: Mapping[str, Any]) -> set[tuple[int, int]]:
    """Os pares ``(tarefa_id, anotador_id)`` que um lote de anotações já reserva.

    Sem isto, dois ``preparar`` seguidos gerariam o mesmo par e o segundo import
    morreria no ``UNIQUE(tarefa_id, anotador_id)`` de ``atribuicoes`` — depois
    de o agente ter escrito o trabalho inteiro.
    """
    pares: set[tuple[int, int]] = set()
    for registro in manifest.get("lotes", {}).values():
        if registro.get("campanha") != "anotacoes" or registro.get("status") == FAILED:
            continue
        for plano in registro.get("plano", []):
            pares.add((int(plano["tarefa_id"]), int(plano["anotador_id"])))
    return pares


# ---------------------------------------------------------------------------
# campanha A — seleção do material
# ---------------------------------------------------------------------------


def _sem_material(conn: sqlite3.Connection, uids: Sequence[str]) -> list[str]:
    """Dos ``uids``, os que ainda não têm rubrica ativa NEM resposta de modelo.

    A pergunta é feita a ``catalogo.marcar_material``, que é quem o catálogo e a
    rota do modo livre já consultam. Uma segunda consulta aqui responderia
    "precisa de material" para um prompt que o botão da tela mostra habilitado —
    e as duas discordarem é exatamente o defeito que ``material_faltando``
    existe para não ter.
    """
    itens: list[dict[str, Any]] = [{"uid": u} for u in uids]
    catmod.marcar_material(conn, itens)
    return [
        str(item["uid"])
        for item in itens
        if not item["tem_rubrica"] and int(item["n_respostas"]) == 0
    ]


def escolher_prompts(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    n: int,
    lang: str = "ambos",
    reservados: Iterable[str] = (),
) -> list[dict[str, Any]]:
    """Os ``n`` prompts do pool que ainda não têm material, na língua pedida.

    Percorre o pool na ORDEM dele (que é determinística por construção — ver
    ``pool.materializar``) e para ao completar a cota. Nada de amostragem
    aleatória: duas máquinas com o mesmo corpus e o mesmo manifest preparam o
    mesmo lote, e é isso que torna a campanha auditável.

    ``lang="ambos"`` alterna pt e en item a item enquanto os dois durarem. O pool
    é 250/250 desde o P3i e cortar ao meio devolveria um lote monolíngue — o
    MESMO defeito que ``seed.fatias_por_passo`` já consertou duas vezes neste
    repositório.
    """
    if lang not in ("pt", "en", "ambos"):
        raise ValueError(f"lang inválido: {lang!r} (aceitos: pt, en, ambos)")
    fora = set(reservados)
    candidatos = [u for u in tmod.uids_do_pool(conn, conn_corpus) if u not in fora]
    if not candidatos:
        return []

    livres = set(_sem_material(conn, candidatos))
    por_lingua: dict[str, list[dict[str, Any]]] = {"pt": [], "en": []}
    for uid in candidatos:
        if uid not in livres:
            continue
        prompt = tmod.resolver_prompt(conn, conn_corpus, uid)
        # Uid sumido do corpus entre duas recargas: pula, não explode. O
        # universo desta máquina já foi de 144.754 para 159.733 numa recarga.
        if prompt is None:
            continue
        idioma = str(prompt.get("lang") or "")
        if idioma in por_lingua and (lang == "ambos" or lang == idioma):
            por_lingua[idioma].append(prompt)
        # Parada antecipada: `resolver_prompt` é uma ida ao corpus por uid, e sem
        # isto um lote de 8 pagaria uma ida para cada prompt livre do pool
        # (`[annotate] pool_max`, hoje 500 e configurável). Em `ambos`, ter `n`
        # de CADA língua já basta para qualquer intercalação — o teto é 2n, e
        # não o pool inteiro.
        if lang != "ambos":
            if len(por_lingua[lang]) >= n:
                break
        elif len(por_lingua["pt"]) >= n and len(por_lingua["en"]) >= n:
            break

    if lang != "ambos":
        return por_lingua[lang][:n]

    # Intercalado, não concatenado: `zip` pararia na língua mais curta e
    # descartaria o excedente da outra, entregando menos itens do que existem.
    saida: list[dict[str, Any]] = []
    pt, en = por_lingua["pt"], por_lingua["en"]
    for i in range(max(len(pt), len(en))):
        if i < len(pt):
            saida.append(pt[i])
        if i < len(en):
            saida.append(en[i])
        if len(saida) >= n:
            break
    return saida[:n]


def montar_lote_material(prompts: Sequence[Mapping[str, Any]], lote_id: str) -> dict[str, Any]:
    """O arquivo que o agente lê. Prompt inteiro, língua e nada de gabarito."""
    return {
        "contrato": CONTRATO,
        "campanha": "material",
        "lote_id": lote_id,
        "gerado_em": agora(),
        "n_items": len(prompts),
        "instrucoes": (
            "Para CADA item: uma rubrica de 3 a 5 critérios com escala ancorada "
            "(as pontas descritas) e DUAS respostas de modelo na LÍNGUA DO PROMPT. "
            "Nunca as duas igualmente boas: uma tem defeito plantado e a outra é a "
            "defensável. Critérios da rubrica em INGLÊS (metadado é o que o cliente lê); "
            "respostas na língua do prompt. Protocolo completo na skill `gerar-material`."
        ),
        "catalogo_defeitos": DEFEITOS_RESPOSTA,
        "rotulos_modelo": list(adb.ROTULOS_MODELO),
        "items": [
            {
                "uid": str(p["uid"]),
                "lang": str(p["lang"]),
                "fonte": str(p.get("fonte") or ""),
                "text": str(p["text"])[:MAX_CHARS_PROMPT],
            }
            for p in prompts
        ],
    }


# ---------------------------------------------------------------------------
# campanha B — o plano das sintéticas
# ---------------------------------------------------------------------------


def sortear_alvos(n: int, semente: int) -> list[str]:
    """``n`` alvos de qualidade seguindo a distribuição, sem sorte no total.

    Cota inteira por faixa (``round(n * peso)``) e só então embaralhada: sortear
    ``n`` vezes de forma independente daria, com n=6 e 15% de ``inutilizavel``,
    uma chance real de a faixa não aparecer NENHUMA vez — e o lote inteiro
    deixaria de exercitar o botão que ele existe para exercitar.

    O resto da divisão vai para a faixa de maior peso, que é a que menos sofre
    com uma unidade a mais.
    """
    if n <= 0:
        return []
    pesos = distribuicao()
    ordem = sorted(pesos, key=lambda k: (-pesos[k], k))
    cotas = {k: round(n * pesos[k]) for k in ordem}
    # `round` pode somar n-1 ou n+1; o ajuste cai sempre na faixa dominante.
    sobra = n - sum(cotas.values())
    cotas[ordem[0]] = max(0, cotas[ordem[0]] + sobra)
    alvos = [k for k in ordem for _ in range(cotas[k])]
    # Uma cota pode ter zerado no arredondamento (n pequeno); completa com a
    # faixa dominante para o total bater sempre.
    while len(alvos) < n:
        alvos.append(ordem[0])
    random.Random(semente).shuffle(alvos)
    return alvos[:n]


def familia_para(alvo: str, indice: int) -> str:
    """A família de defeito que combina com o alvo de qualidade.

    ``adequado`` e ``excepcional`` não têm defeito plantado — inventar um para
    eles produziria uma anotação boa que se declara ruim, e o revisor que
    acertasse seria contado como errado pelo painel do P5a.
    """
    if alvo in ("adequado", "excepcional"):
        return "nenhum"
    plantaveis = [f for f in DEFEITOS_ANOTACAO if f != "nenhum"]
    return plantaveis[indice % len(plantaveis)]


def _personas_anotadoras(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """As personas de fixture com papel de anotador, ativas, em ordem estável.

    As sintéticas são atribuídas a ELAS, e não ao dono: uma anotação de agente
    carimbada com o nome de quem monta o portfólio é exatamente a mentira que
    este marco existe para não cometer.
    """
    return [
        {"id": int(linha["id"]), "nome": str(linha["nome"])}
        for linha in conn.execute(
            "SELECT id, nome FROM anotadores WHERE papel = 'anotador' AND ativo = 1 "
            "ORDER BY id"
        )
    ]


def _tarefas_candidatas(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection
) -> list[dict[str, Any]]:
    """Tarefas abertas cujo prompt SUSTENTA o tipo delas, com o material em pé.

    ``catalogo.material_faltando`` é quem decide — a mesma função da rota e do
    catálogo. Uma sintética sobre uma ``comparar_ab`` sem duas respostas seria
    um payload válido sobre uma tela impossível.
    """
    candidatas: list[dict[str, Any]] = []
    for linha in conn.execute(
        "SELECT id, tipo, prompt_uid FROM tarefas WHERE status = 'aberta' ORDER BY id"
    ):
        uid = str(linha["prompt_uid"])
        tipo = str(linha["tipo"])
        if catmod.material_faltando(conn, uid, tipo) is not None:
            continue
        if tmod.resolver_prompt(conn, conn_corpus, uid) is None:
            continue
        candidatas.append({"tarefa_id": int(linha["id"]), "tipo": tipo, "prompt_uid": uid})
    return candidatas


def montar_plano(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    n: int,
    semente: int,
    reservados: Iterable[tuple[int, int]] = (),
) -> list[dict[str, Any]]:
    """``n`` linhas ``(tarefa, persona, alvo, família)`` — quem escreve o quê.

    O alvo e a família saem DAQUI, e não do agente. Duas razões: a distribuição
    só é distribuição se alguém a controlar, e um agente que escolhesse o
    próprio alvo escolheria o mais fácil de escrever. Ao agente cabe **produzir**
    o defeito que o plano pede — e ecoar de volta qual era, para que o import
    prove que ele leu a linha certa.
    """
    ja = set(reservados)
    personas = _personas_anotadoras(conn)
    if not personas:
        return []
    candidatas = _tarefas_candidatas(conn, conn_corpus)
    alvos = sortear_alvos(n, semente)

    plano: list[dict[str, Any]] = []
    for tarefa in candidatas:
        if len(plano) >= n:
            break
        # RODÍZIO: a lista de personas é girada pelo tamanho do plano, em vez de
        # varrida sempre a partir da primeira. Sem isso, num banco com tarefas
        # livres o lote inteiro sairia atribuído à persona de menor id, e o
        # painel do admin mediria uma pessoa só. (Quando as outras já ocupam a
        # tarefa, o rodízio não muda nada: a trava anti-repetição decide.)
        giro = len(plano) % len(personas)
        for persona in personas[giro:] + personas[:giro]:
            par = (tarefa["tarefa_id"], persona["id"])
            if par in ja:
                continue
            # A trava anti-repetição do banco é a mesma: uma pessoa não anota a
            # mesma tarefa duas vezes. Conferida aqui para o lote nascer viável.
            if conn.execute(
                "SELECT 1 FROM atribuicoes WHERE tarefa_id = ? AND anotador_id = ?",
                par,
            ).fetchone() is not None:
                continue
            i = len(plano)
            alvo = alvos[i]
            plano.append(
                {
                    "tarefa_id": tarefa["tarefa_id"],
                    "tipo": tarefa["tipo"],
                    "prompt_uid": tarefa["prompt_uid"],
                    "anotador_id": persona["id"],
                    "anotador": persona["nome"],
                    "avaliacao_antes": alvo,
                    "familia_defeito": familia_para(alvo, i),
                }
            )
            ja.add(par)
            break
    return plano


def montar_lote_anotacoes(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    plano: Sequence[Mapping[str, Any]],
    lote_id: str,
) -> dict[str, Any]:
    """O arquivo do lote B: cada item já vem com o material NA MÃO.

    A rubrica, as respostas e o prompt viajam dentro do lote porque o agente é
    puro cômputo: ele não abre banco, não roda ``pf`` e não lê o repositório. Se
    faltasse qualquer um deles, ele inventaria — e uma anotação sobre critérios
    inventados não avalia nada.
    """
    itens: list[dict[str, Any]] = []
    for linha in plano:
        uid = str(linha["prompt_uid"])
        tipo = str(linha["tipo"])
        prompt = tmod.resolver_prompt(conn, conn_corpus, uid)
        item: dict[str, Any] = {
            "tarefa_id": int(linha["tarefa_id"]),
            "tipo": tipo,
            "anotador": str(linha["anotador"]),
            "avaliacao_antes": str(linha["avaliacao_antes"]),
            "familia_defeito": str(linha["familia_defeito"]),
            "payload_schema": payloads.nome_schema(tipo),
            "prompt": {
                "uid": uid,
                "lang": str(prompt["lang"]) if prompt else "",
                "text": (str(prompt["text"])[:MAX_CHARS_PROMPT] if prompt else ""),
            },
        }
        rubrica = tmod.rubrica_ativa(conn, uid)
        if tipo in ("avaliar_rubrica", "sft_resposta") and rubrica:
            item["rubrica"] = {
                "titulo": rubrica["titulo"],
                "criterios": rubrica["criterios"],
            }
        if tipo in ("avaliar_rubrica", "comparar_ab"):
            quais = (
                list(adb.ROTULOS_MODELO)
                if tipo == "comparar_ab"
                else _quais_respostas(conn, int(linha["tarefa_id"]))
            )
            # `_respostas` NÃO traz o meta_json: o defeito plantado na resposta
            # é o gabarito do exercício, e um agente que o lesse escreveria a
            # avaliação certa sem avaliar nada.
            item["respostas"] = tmod._respostas(conn, uid, quais)
        itens.append(item)

    return {
        "contrato": CONTRATO,
        "campanha": "anotacoes",
        "lote_id": lote_id,
        "gerado_em": agora(),
        "n_items": len(itens),
        "instrucoes": (
            "Para CADA item, escreva UMA anotação no contrato do `payload_schema` dele, "
            "com a QUALIDADE que `avaliacao_antes` pede e o defeito que `familia_defeito` "
            "nomeia. Devolva `avaliacao_antes` e `familia_defeito` de volta, iguais. "
            "Justificativas em INGLÊS — exceto quando a família for "
            "`justificativa-na-lingua-errada`, que é o defeito a plantar. "
            "Protocolo completo na skill `gerar-material`."
        ),
        "catalogo_defeitos": DEFEITOS_ANOTACAO,
        "escala_alvo": list(adb.AVALIACOES_ANTES),
        "items": itens,
    }


def _quais_respostas(conn: sqlite3.Connection, tarefa_id: int) -> list[str]:
    """Qual resposta esta ``avaliar_rubrica`` avalia (o payload da tarefa diz)."""
    linha = conn.execute(
        "SELECT payload_json FROM tarefas WHERE id = ?", (tarefa_id,)
    ).fetchone()
    payload = tmod._carregar_json(linha["payload_json"] if linha else None, {})
    qual = payload.get("resposta") if isinstance(payload, dict) else None
    return [str(qual)] if qual in adb.ROTULOS_MODELO else [adb.ROTULOS_MODELO[0]]


# ---------------------------------------------------------------------------
# validação — o portão
# ---------------------------------------------------------------------------


def _lingua(texto: str) -> tuple[str, float]:
    """``(idioma, confiança)`` pelo detector do repositório. Nunca levanta."""
    from ..langdetect import detect1

    return detect1(texto)


def conferir_lingua(texto: str, esperada: str) -> str | None:
    """A frase de erro quando o texto está na OUTRA língua — ou ``None``.

    DUAS CAMADAS, COMO O s02, E PELO MESMO MOTIVO
    =============================================
    Só recusa quando o detector aponta a outra língua de ``{pt, en}`` **com
    confiança acima do piso**. Um texto em português cheio de identificadores de
    código, um trecho curto ou uma detecção de terceira língua viram silêncio —
    o corpus deste projeto já provou que payload longo afoga a instrução e que
    um árbitro sozinho devolve CATALÃO para texto em russo.

    Ou seja: **recusar exige certeza; deixar passar, não.** O caso que importa
    (uma resposta inteira em inglês para um prompt em português) é justamente o
    inequívoco.
    """
    if esperada not in ("pt", "en"):
        return None
    limpo = texto.strip()
    if len(limpo) < 60:
        return None
    outra = "en" if esperada == "pt" else "pt"
    detectada, confianca = _lingua(limpo)
    if detectada == outra and confianca >= lang_confianca_minima():
        return (
            f"o texto está em {outra!r} (confiança {confianca:.2f}) e o prompt está em "
            f"{esperada!r} — resposta de modelo vai na LÍNGUA DO PROMPT"
        )
    return None


def _texto(valor: Any) -> str:
    return valor.strip() if isinstance(valor, str) else ""


def _carregar_resposta(texto: str, lote_id: str) -> tuple[dict[str, Any] | None, list[str]]:
    """JSON tolerante na forma: cerca de código, BOM, prosa em volta e CRLF saem.

    Mesma disciplina do ``labeling_io.parse_jsonl`` — tolerante na FORMA,
    estrito no CONTEÚDO. Um agente que embrulha a resposta em ```json não é
    motivo para jogar fora o material de um lote inteiro.
    """
    bruto = texto.lstrip("﻿").replace("\r\n", "\n").strip()
    avisos: list[str] = []
    if not bruto:
        return None, [f"{lote_id}: arquivo de resposta vazio"]
    if bruto.startswith("```"):
        linhas = bruto.split("\n")
        if linhas[-1].strip().startswith("```"):
            linhas = linhas[:-1]
        bruto = "\n".join(linhas[1:]).strip()
        avisos.append("cerca de código removida da resposta")
    if not bruto.startswith("{"):
        inicio = bruto.find("{")
        fim = bruto.rfind("}")
        if inicio < 0 or fim <= inicio:
            return None, [f"{lote_id}: a resposta não contém objeto JSON nenhum"]
        bruto = bruto[inicio : fim + 1]
        avisos.append("prosa em volta do JSON descartada")
    try:
        dados = json.loads(bruto)
    except json.JSONDecodeError as exc:
        return None, [f"{lote_id}: JSON inválido — {exc.msg} (linha {exc.lineno})"]
    if not isinstance(dados, dict):
        return None, [f"{lote_id}: o JSON de topo precisa ser um objeto, veio {type(dados).__name__}"]
    return dados, avisos


def _cobertura(
    esperados: Sequence[Any], vistos: Sequence[Any], nome: str, lote_id: str
) -> list[str]:
    """Cobertura EXATA: nada a mais, nada a menos, nada repetido."""
    erros: list[str] = []
    conjunto = list(esperados)
    faltando = [x for x in conjunto if x not in vistos]
    if faltando:
        erros.append(
            f"{lote_id}: faltam {len(faltando)} {nome}(s) do lote: "
            + ", ".join(str(x) for x in faltando[:5])
            + (" ..." if len(faltando) > 5 else "")
        )
    repetidos = sorted({str(x) for x in vistos if list(vistos).count(x) > 1})
    if repetidos:
        erros.append(f"{lote_id}: {nome}(s) repetido(s) na resposta: {', '.join(repetidos[:5])}")
    return erros


def validar_material(
    lote: Mapping[str, Any], resposta: Mapping[str, Any]
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Valida a resposta da campanha A. Devolve ``(itens, erros, avisos)``.

    Toda mensagem de erro nomeia a LINHA (o uid) — um "payload inválido" seco
    obrigaria o maestro a reler o lote inteiro para achar o item quebrado, e o
    retry dirigido deixaria de ser dirigido.
    """
    erros: list[str] = []
    avisos: list[str] = []
    lote_id = str(lote["lote_id"])
    por_uid = {str(i["uid"]): i for i in lote["items"]}

    brutos = resposta.get("itens") or resposta.get("items") or []
    if not isinstance(brutos, list):
        return [], [f"{lote_id}: 'itens' precisa ser uma lista"], avisos

    vistos = [str(b.get("uid")) for b in brutos if isinstance(b, Mapping)]
    erros += _cobertura(list(por_uid), vistos, "uid", lote_id)

    limpos: list[dict[str, Any]] = []
    for bruto in brutos:
        if not isinstance(bruto, Mapping):
            erros.append(f"{lote_id}: item que não é objeto na lista de itens")
            continue
        uid = str(bruto.get("uid") or "")
        if uid not in por_uid:
            erros.append(f"uid {uid!r}: não pertence ao lote {lote_id}")
            continue
        lang = str(por_uid[uid]["lang"])
        item, e, a = _validar_item_material(uid, lang, bruto)
        erros += e
        avisos += a
        if item is not None:
            limpos.append(item)

    return limpos, erros, avisos


def _validar_item_material(
    uid: str, lang: str, bruto: Mapping[str, Any]
) -> tuple[dict[str, Any] | None, list[str], list[str]]:
    erros: list[str] = []
    avisos: list[str] = []

    rubrica, e = _validar_rubrica(uid, bruto.get("rubrica"))
    erros += e
    respostas, e2, a2 = _validar_respostas(uid, lang, bruto.get("respostas"))
    erros += e2
    avisos += a2

    if erros or rubrica is None:
        return None, erros, avisos
    return {"uid": uid, "lang": lang, "rubrica": rubrica, "respostas": respostas}, erros, avisos


def _validar_rubrica(uid: str, bruto: Any) -> tuple[dict[str, Any] | None, list[str]]:
    """A rubrica no contrato de fixture (``rubrica@3``): escala com ÂNCORAS.

    É o formato que ``wsAvaliar`` desenha (``escala.min/max/ancoras``), e não o
    ``escrever_rubrica@1`` que um anotador submete (``escala_min``/``rotulo_min``).
    Os dois existem e são diferentes de propósito: um é o que a pessoa preenche
    num formulário, o outro é o instrumento já montado. Gerar no formato errado
    daria uma rubrica que a tela abre **sem âncora nenhuma** — funcionando, e
    medindo outra coisa.
    """
    erros: list[str] = []
    if not isinstance(bruto, Mapping):
        return None, [f"uid {uid}: falta o objeto 'rubrica'"]

    titulo = _texto(bruto.get("titulo"))
    if len(titulo) < 5:
        erros.append(f"uid {uid}: título da rubrica com menos de 5 caracteres")

    criterios = bruto.get("criterios")
    piso = int(_cfg("annotate", "min_criterios_rubrica", default=3))
    teto = int(_cfg("geracao", "max_criterios_material", default=5))
    if not isinstance(criterios, list) or not (piso <= len(criterios) <= teto):
        n = len(criterios) if isinstance(criterios, list) else 0
        erros.append(f"uid {uid}: a rubrica precisa de {piso} a {teto} critérios (veio {n})")
        return None, erros

    limpos: list[dict[str, Any]] = []
    nomes: list[str] = []
    for i, c in enumerate(criterios):
        rotulo = f"uid {uid}, critério {i + 1}"
        if not isinstance(c, Mapping):
            erros.append(f"{rotulo}: não é objeto")
            continue
        nome = _texto(c.get("nome"))
        descricao = _texto(c.get("descricao"))
        if len(nome) < 3:
            erros.append(f"{rotulo}: 'nome' com menos de 3 caracteres")
        if len(descricao) < 10:
            erros.append(f"{rotulo}: 'descricao' com menos de 10 caracteres")
        nomes.append(nome.casefold())

        escala = c.get("escala")
        if not isinstance(escala, Mapping):
            erros.append(f"{rotulo}: falta 'escala' com min, max e ancoras")
            continue
        try:
            emin, emax = int(escala["min"]), int(escala["max"])
        except (KeyError, TypeError, ValueError):
            erros.append(f"{rotulo}: 'escala.min' e 'escala.max' precisam ser inteiros")
            continue
        if isinstance(escala.get("min"), bool) or isinstance(escala.get("max"), bool):
            # bool é subclasse de int, quarta vez neste repositório.
            erros.append(f"{rotulo}: escala booleana — mande os números")
            continue
        if not (1 <= emin < emax <= 9) or emax - emin < 2:
            erros.append(
                f"{rotulo}: escala {emin}..{emax} inválida — precisa caber em 1..9 e ter "
                "ao menos 3 posições"
            )
            continue

        ancoras = escala.get("ancoras")
        if not isinstance(ancoras, list) or len(ancoras) < 2:
            erros.append(f"{rotulo}: a escala precisa de ao menos 2 âncoras (as duas pontas)")
            continue
        valores = []
        limpas: list[dict[str, Any]] = []
        for a in ancoras:
            if not isinstance(a, Mapping):
                erros.append(f"{rotulo}: âncora que não é objeto")
                continue
            try:
                valor = int(a["valor"])
            except (KeyError, TypeError, ValueError):
                erros.append(f"{rotulo}: âncora sem 'valor' inteiro")
                continue
            texto_ancora = _texto(a.get("rotulo"))
            if len(texto_ancora) < 3:
                erros.append(f"{rotulo}: âncora {valor} sem rótulo legível")
                continue
            if not emin <= valor <= emax:
                erros.append(f"{rotulo}: âncora {valor} fora da escala {emin}..{emax}")
                continue
            valores.append(valor)
            limpas.append({"valor": valor, "rotulo": texto_ancora})
        # As PONTAS são o que faz uma escala ancorada ser ancorada: sem elas,
        # "clareza de 1 a 5" significa cinco coisas para cinco anotadores.
        if emin not in valores or emax not in valores:
            erros.append(f"{rotulo}: as duas PONTAS ({emin} e {emax}) precisam estar ancoradas")
            continue
        limpos.append(
            {
                "nome": nome,
                "descricao": descricao,
                "escala": {"min": emin, "max": emax, "ancoras": limpas},
            }
        )

    if len(set(nomes)) != len(nomes):
        erros.append(f"uid {uid}: dois critérios com o mesmo nome")
    if erros:
        return None, erros
    return {"titulo": titulo, "criterios": limpos}, erros


def _validar_respostas(
    uid: str, lang: str, bruto: Any
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Duas respostas, rótulos cegos, e NUNCA as duas igualmente boas."""
    erros: list[str] = []
    avisos: list[str] = []
    if not isinstance(bruto, list) or len(bruto) != len(adb.ROTULOS_MODELO):
        n = len(bruto) if isinstance(bruto, list) else 0
        return [], [f"uid {uid}: precisa de exatamente {len(adb.ROTULOS_MODELO)} respostas (veio {n})"], avisos

    piso = min_chars_resposta()
    limpas: list[dict[str, Any]] = []
    rotulos: list[str] = []
    corretas = 0
    for r in bruto:
        if not isinstance(r, Mapping):
            erros.append(f"uid {uid}: resposta que não é objeto")
            continue
        rotulo = str(r.get("rotulo_modelo") or "")
        if rotulo not in adb.ROTULOS_MODELO:
            erros.append(
                f"uid {uid}: rotulo_modelo {rotulo!r} fora de {list(adb.ROTULOS_MODELO)}"
            )
            continue
        rotulos.append(rotulo)
        texto = _texto(r.get("texto"))
        if len(texto) < piso:
            # O piso é contra resposta VAZIA, não contra resposta CURTA: piada tem
            # desfecho de 38 caracteres, pergunta factual tem resposta de 86, e nos
            # dois casos a curta é a CORRETA. Piso alto codificaria "mais longo é
            # melhor" — o viés de comprimento que a anotação existe para combater.
            erros.append(
                f"uid {uid}, {rotulo}: resposta com {len(texto)} caracteres "
                f"(mínimo {piso}) — o modelo não devolveu resposta"
            )
            continue
        problema = conferir_lingua(texto, lang)
        if problema:
            erros.append(f"uid {uid}, {rotulo}: {problema}")
            continue

        meta = r.get("meta")
        if not isinstance(meta, Mapping):
            erros.append(f"uid {uid}, {rotulo}: falta o objeto 'meta'")
            continue
        classe = _texto(meta.get("defeito_classe"))
        plantado = _texto(meta.get("defeito_plantado"))
        correta = meta.get("correta")
        if not isinstance(correta, bool):
            erros.append(f"uid {uid}, {rotulo}: 'meta.correta' precisa ser booleano JSON")
            continue
        if not classe:
            erros.append(f"uid {uid}, {rotulo}: 'meta.defeito_classe' vazio")
            continue
        if len(plantado) < 20:
            erros.append(
                f"uid {uid}, {rotulo}: 'meta.defeito_plantado' precisa DIZER o que foi "
                "plantado (ou por que esta é a defensável) — é o gabarito do exercício"
            )
            continue
        if classe not in DEFEITOS_RESPOSTA:
            # AVISO, não erro: o catálogo é aberto de propósito e cresce a cada
            # rodada. Barrar aqui obrigaria a editar o código para escrever um
            # defeito novo, e o defeito novo é o que a campanha descobre.
            avisos.append(
                f"uid {uid}, {rotulo}: defeito_classe {classe!r} não está no catálogo conhecido "
                f"({', '.join(sorted(DEFEITOS_RESPOSTA))}) — aceito como família nova"
            )
        if correta and classe != "nenhum":
            erros.append(
                f"uid {uid}, {rotulo}: 'correta' é true mas defeito_classe é {classe!r} — "
                "a resposta defensável do par não tem defeito plantado"
            )
            continue
        corretas += int(correta)
        limpas.append(
            {
                "rotulo_modelo": rotulo,
                "texto": texto,
                "meta": {
                    "modelo": _texto(meta.get("modelo")) or f"campanha P4c — {rotulo}",
                    "defeito_classe": classe,
                    "defeito_plantado": plantado,
                    "correta": correta,
                },
            }
        )

    if sorted(rotulos) != sorted(adb.ROTULOS_MODELO):
        erros.append(
            f"uid {uid}: os rótulos precisam ser exatamente {sorted(adb.ROTULOS_MODELO)}, "
            f"vieram {sorted(rotulos)}"
        )
    if not erros and corretas != 1:
        # É a regra do pacote de demonstração, e é a regra do produto: um A/B
        # sem resposta defensável não mede preferência, mede ruído. Duas boas é
        # o mesmo problema pelo outro lado.
        erros.append(
            f"uid {uid}: exatamente UMA das duas respostas tem de ter 'correta': true "
            f"(vieram {corretas}) — nunca as duas igualmente boas"
        )
    return limpas, erros, avisos


def validar_anotacoes(
    lote: Mapping[str, Any],
    plano: Sequence[Mapping[str, Any]],
    resposta: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    """Valida a resposta da campanha B contra o PLANO (não contra o arquivo).

    O plano mora no manifest. Validar contra ele — e não contra o lote no disco —
    é o que impede alguém de reescrever o arquivo do lote e importar um alvo
    diferente do que a campanha planejou. A RUBRICA, essa sim, sai do arquivo do
    lote: é a mesma que o agente teve em mãos, e conferir contra outra acusaria
    um erro que ele não cometeu.
    """
    erros: list[str] = []
    avisos: list[str] = []
    lote_id = str(lote["lote_id"])
    por_tarefa = {int(p["tarefa_id"]): p for p in plano}
    rubricas = {
        int(i["tarefa_id"]): i.get("rubrica")
        for i in lote.get("items", [])
        if isinstance(i, Mapping)
    }

    brutos = resposta.get("itens") or resposta.get("items") or []
    if not isinstance(brutos, list):
        return [], [f"{lote_id}: 'itens' precisa ser uma lista"], avisos

    vistos: list[int] = []
    for b in brutos:
        if isinstance(b, Mapping):
            try:
                vistos.append(int(b.get("tarefa_id")))
            except (TypeError, ValueError):
                erros.append(f"{lote_id}: item sem 'tarefa_id' inteiro")
    erros += _cobertura(list(por_tarefa), vistos, "tarefa_id", lote_id)

    limpos: list[dict[str, Any]] = []
    for bruto in brutos:
        if not isinstance(bruto, Mapping):
            erros.append(f"{lote_id}: item que não é objeto na lista de itens")
            continue
        try:
            tid = int(bruto.get("tarefa_id"))
        except (TypeError, ValueError):
            continue
        if tid not in por_tarefa:
            erros.append(f"tarefa {tid}: não pertence ao lote {lote_id}")
            continue
        item, e = _validar_item_anotacao(por_tarefa[tid], bruto, rubricas.get(tid))
        erros += e
        if item is not None:
            limpos.append(item)
    return limpos, erros, avisos


def _validar_item_anotacao(
    plano: Mapping[str, Any],
    bruto: Mapping[str, Any],
    rubrica: Mapping[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[str]]:
    erros: list[str] = []
    tid = int(plano["tarefa_id"])
    tipo = str(plano["tipo"])

    # O ECO. O agente devolve o alvo que recebeu; divergência é erro. Com
    # dezenas de itens num lote, um agente que processa fora de ordem produz
    # trabalho perfeitamente válido casado com o alvo ERRADO — e o painel do
    # P5a passaria a medir o revisor contra uma expectativa que ninguém tinha.
    for campo in ("avaliacao_antes", "familia_defeito"):
        veio = _texto(bruto.get(campo))
        if veio != str(plano[campo]):
            erros.append(
                f"tarefa {tid}: {campo} devolvido é {veio!r} e o plano pede "
                f"{plano[campo]!r} — o item foi casado com o alvo errado"
            )

    nota = _texto(bruto.get("nota"))
    if len(nota) < 20:
        erros.append(
            f"tarefa {tid}: 'nota' precisa DIZER o que foi plantado (mínimo 20 caracteres) — "
            "é ela que um humano lê para auditar o alvo escondido"
        )

    payload_bruto = bruto.get("payload")
    if not isinstance(payload_bruto, Mapping):
        erros.append(f"tarefa {tid}: falta o objeto 'payload'")
        return None, erros

    # O CONTRATO DO PAYLOAD NÃO É REIMPLEMENTADO AQUI. `payloads.escolher` é o
    # mesmo despachante que a rota de submissão usa, com o tipo lido do PLANO
    # (que veio do banco), nunca do que o agente afirmou estar mandando.
    from pydantic import ValidationError

    modelo = payloads.escolher(tipo)
    try:
        dados = modelo.model_validate(dict(payload_bruto))
    except ValidationError as exc:
        for erro in exc.errors():
            caminho = ".".join(str(p) for p in erro["loc"]) or "payload"
            erros.append(f"tarefa {tid}: payload.{caminho}: {erro['msg']}")
        return None, erros

    # A ESCALA REAL É A DA RUBRICA, e o Pydantic não a conhece: ele só barra
    # fora de 1..9. Esta é a MESMA função que a rota de submissão chama — sem
    # ela, uma sintética entraria com nota 7 num critério de 1..5, e a tela
    # desenharia cinco botões e um valor que não é nenhum deles.
    if isinstance(dados, payloads.AvaliarRubrica):
        problema = tmod.erro_contra_a_rubrica(rubrica, dados.notas, dados.respostas)
        if problema:
            erros.append(f"tarefa {tid}: {problema}")

    if erros:
        return None, erros
    return {
        "tarefa_id": tid,
        "anotador_id": int(plano["anotador_id"]),
        "tipo": tipo,
        "payload": dados.model_dump(),
        "gabarito": {
            "avaliacao_antes": str(plano["avaliacao_antes"]),
            "familia_defeito": str(plano["familia_defeito"]),
            "nota": nota,
        },
    }, erros


def validar_gabarito(bruto: Any) -> list[str]:
    """As chaves de ``CHAVES_GABARITO_AVALIACAO``, exatas, com valores usáveis.

    Exportada porque o P5a vai LER esta coluna e precisa poder confiar na forma.
    Um gabarito malformado não quebra nada na hora: ele quebra o painel meses
    depois, quando ninguém lembra de onde ele veio.
    """
    erros: list[str] = []
    if not isinstance(bruto, Mapping):
        return [f"gabarito precisa ser objeto, veio {type(bruto).__name__}"]
    faltando = [k for k in adb.CHAVES_GABARITO_AVALIACAO if k not in bruto]
    if faltando:
        erros.append(f"gabarito sem as chaves: {', '.join(faltando)}")
    sobrando = [k for k in bruto if k not in adb.CHAVES_GABARITO_AVALIACAO]
    if sobrando:
        erros.append(f"gabarito com chave desconhecida: {', '.join(sorted(sobrando))}")
    alvo = bruto.get("avaliacao_antes")
    if alvo not in adb.AVALIACOES_ANTES:
        erros.append(
            f"gabarito.avaliacao_antes {alvo!r} fora de {list(adb.AVALIACOES_ANTES)}"
        )
    for campo in ("familia_defeito", "nota"):
        if not _texto(bruto.get(campo)):
            erros.append(f"gabarito.{campo} vazio")
    return erros


# ---------------------------------------------------------------------------
# gravação
# ---------------------------------------------------------------------------


def gravar_material(
    conn: sqlite3.Connection, itens: Sequence[Mapping[str, Any]], lote_id: str
) -> dict[str, int]:
    """Grava rubricas e respostas com ``origem='importada'``. Idempotente.

    A idempotência é por chave natural — ``(prompt_uid, titulo)`` para a rubrica
    e ``(prompt_uid, rotulo_modelo)`` para a resposta, as mesmas do ``seed`` —
    porque um import repetido depois de uma queda no meio não pode duplicar
    material. Duas rubricas ativas no mesmo prompt fariam ``rubrica_ativa``
    devolver a mais nova e a avaliação anterior passaria a citar critérios que a
    tela não mostra mais.
    """
    conta = dict.fromkeys(("rubricas", "respostas_modelo", "ja_existiam"), 0)
    for item in itens:
        uid = str(item["uid"])
        rub = item["rubrica"]
        criterios_json = json.dumps(
            {"schema": seedmod.SCHEMA_RUBRICA, "criterios": rub["criterios"]},
            ensure_ascii=False,
        )
        ja = conn.execute(
            "SELECT id FROM rubricas WHERE prompt_uid = ? AND titulo = ?",
            (uid, rub["titulo"]),
        ).fetchone()
        if ja is None:
            conn.execute(
                "INSERT INTO rubricas (prompt_uid, titulo, criterios_json, origem, status) "
                "VALUES (?, ?, ?, 'importada', 'ativa')",
                (uid, rub["titulo"], criterios_json),
            )
            conta["rubricas"] += 1
        else:
            conta["ja_existiam"] += 1

        for resposta in item["respostas"]:
            meta = dict(resposta["meta"])
            meta["lote"] = lote_id
            ja = conn.execute(
                "SELECT id FROM respostas_modelo WHERE prompt_uid = ? AND rotulo_modelo = ?",
                (uid, resposta["rotulo_modelo"]),
            ).fetchone()
            if ja is None:
                conn.execute(
                    "INSERT INTO respostas_modelo "
                    "(prompt_uid, rotulo_modelo, texto, origem, meta_json) "
                    "VALUES (?, ?, ?, 'importada', ?)",
                    (
                        uid,
                        resposta["rotulo_modelo"],
                        resposta["texto"],
                        json.dumps(meta, ensure_ascii=False),
                    ),
                )
                conta["respostas_modelo"] += 1
            else:
                conta["ja_existiam"] += 1

    evmod.registrar(
        conn,
        acao="material_importado",
        entidade="lote",
        lote=lote_id,
        prompts=len(itens),
        **conta,
    )
    return conta


def gravar_anotacoes(
    conn: sqlite3.Connection,
    itens: Sequence[Mapping[str, Any]],
    lote_id: str,
    *,
    status: str,
) -> dict[str, int]:
    """Cria atribuição + anotação sintética por item, numa transação só.

    A anotação nasce com ``gabarito_avaliacao_json`` preenchido — e é ele, e
    NADA mais, que a distingue de trabalho humano. ``versao_diretriz`` é lida do
    banco pelo servidor, como na rota de submissão: uma sintética feita sob a
    regra v1 tem de dizer v1, senão a coluna que separa "antes" de "depois"
    passa a mentir justamente nas linhas geradas.
    """
    validar_status_inicial(status)
    conta = dict.fromkeys(("atribuicoes", "anotacoes"), 0)
    # `aprovada` quando a sintética entra direto no Rate and Review: é o estado
    # em que a triagem deixaria a atribuição, e escrever `submetida` faria a
    # aba "minhas tarefas" da persona mostrar um item eternamente em revisão
    # numa passagem que ele já pulou.
    status_atribuicao = "submetida" if status == "pendente_triagem" else "aprovada"

    for item in itens:
        cur = conn.execute(
            "INSERT INTO atribuicoes (tarefa_id, anotador_id, status, expira_em, terminada_em) "
            f"VALUES (?, ?, ?, NULL, {tmod.SQL_AGORA})",
            (int(item["tarefa_id"]), int(item["anotador_id"]), status_atribuicao),
        )
        atribuicao_id = int(cur.lastrowid or 0)
        conta["atribuicoes"] += 1

        tipo = str(item["tipo"])
        # O projeto sai da TAREFA, e a versão do brief sai da mesma função que a
        # rota de submissão usa. Um `max(versao)` escrito em SQL aqui seria uma
        # segunda implementação da mesma regra, e as duas divergiriam no dia em
        # que "vigente" deixasse de ser simplesmente o maior número.
        projeto = conn.execute(
            "SELECT projeto_id FROM tarefas WHERE id = ?", (int(item["tarefa_id"]),)
        ).fetchone()
        cur = conn.execute(
            "INSERT INTO anotacoes (atribuicao_id, versao, payload_schema, versao_diretriz, "
            "                       versao_brief, "
            f"                      payload_json, {adb.COLUNA_GABARITO_AVALIACAO}, status) "
            "VALUES (?, 1, ?, ?, ?, ?, ?, ?)",
            (
                atribuicao_id,
                payloads.nome_schema(tipo),
                dirmod.versao_vigente(conn, tipo),
                briefmod.versao_vigente(
                    conn,
                    None
                    if projeto is None or projeto["projeto_id"] is None
                    else int(projeto["projeto_id"]),
                ),
                json.dumps(item["payload"], ensure_ascii=False),
                json.dumps(item["gabarito"], ensure_ascii=False),
                status,
            ),
        )
        anotacao_id = int(cur.lastrowid or 0)
        conta["anotacoes"] += 1
        # O evento entra na MESMA transação da linha que ele registra. Ele NÃO
        # carrega o gabarito: `eventos` é lido pela trilha de auditoria e o alvo
        # escondido não pode vazar por uma porta lateral.
        evmod.registrar(
            conn,
            acao="anotacao_sintetica_importada",
            entidade="anotacao",
            entidade_id=anotacao_id,
            ator_id=int(item["anotador_id"]),
            atribuicao_id=atribuicao_id,
            tarefa_id=int(item["tarefa_id"]),
            tipo=tipo,
            lote=lote_id,
            status=status,
        )
    return conta


# ---------------------------------------------------------------------------
# operações da CLI
# ---------------------------------------------------------------------------


def preparar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    campanha: str,
    n: int,
    lang: str = "ambos",
    gp: GeracaoPaths | None = None,
    semente: int | None = None,
) -> dict[str, Any]:
    """Monta UM lote, escreve o arquivo e o registra no manifest como ``claimed``.

    ORDEM OBRIGATÓRIA: o arquivo do lote primeiro, o manifest depois. Invertida,
    um crash entre as duas escritas deixaria um lote ``claimed`` sem arquivo
    nenhum — e o maestro não teria o que despachar nem como descobrir o que
    faltou. Do jeito certo o pior caso é um arquivo órfão, que o ``preparar``
    seguinte sobrescreve.
    """
    if campanha not in CAMPANHAS:
        raise ValueError(f"campanha inválida: {campanha!r} (aceitas: {', '.join(CAMPANHAS)})")
    gp = gp or GeracaoPaths()
    manifest = carregar_manifest(gp)
    varrer_orfaos(manifest)
    lote_id = proximo_id(manifest, campanha)
    if semente is None:
        semente = int(_cfg("general", "seed", default=42)) + len(manifest.get("lotes", {}))

    if campanha == "material":
        prompts = escolher_prompts(
            conn, conn_corpus, n=n, lang=lang, reservados=uids_ja_na_campanha(manifest)
        )
        if not prompts:
            return {"lote_id": None, "n": 0, "motivo": "nenhum prompt do pool está sem material"}
        arquivo = montar_lote_material(prompts, lote_id)
        registro = {"uids": [str(p["uid"]) for p in prompts], "lang": lang}
    else:
        plano = montar_plano(
            conn, conn_corpus, n=n, semente=semente, reservados=pares_ja_na_campanha(manifest)
        )
        if not plano:
            return {
                "lote_id": None,
                "n": 0,
                "motivo": (
                    "nenhuma tarefa aberta com material em pé e persona livre — "
                    "prepare um lote de `material` antes"
                ),
            }
        # Sem `lang`: a campanha B trabalha sobre TAREFAS que já existem, e a
        # língua delas é a do prompt que já foi escolhido. Gravar aqui o
        # `--lang` pedido registraria um filtro que não foi aplicado.
        arquivo = montar_lote_anotacoes(conn, conn_corpus, plano, lote_id)
        registro = {"plano": plano}

    gp.preparar()
    escrever_json_atomico(gp.lote(lote_id), arquivo)

    manifest.setdefault("lotes", {})[lote_id] = {
        "campanha": campanha,
        "status": CLAIMED,
        "n_items": int(arquivo["n_items"]),
        "claimed_at": agora(),
        "done_at": None,
        "tentativas": 0,
        "modelo": None,
        "motivo": None,
        "resultado": None,
        **registro,
    }
    salvar_manifest(manifest, gp)
    return {
        "lote_id": lote_id,
        "n": int(arquivo["n_items"]),
        "arquivo": gp.lote(lote_id),
        "campanha": campanha,
    }


def reemitir(lote_id: str, gp: GeracaoPaths | None = None) -> dict[str, Any]:
    """Reivindica de novo um lote existente (retomar sessão, reviver ``failed``).

    O arquivo do lote continua no disco: não há o que regerar, e regerá-lo
    escolheria prompts diferentes, o que faria a resposta que o agente já
    produziu deixar de casar com o lote.
    """
    gp = gp or GeracaoPaths()
    manifest = carregar_manifest(gp)
    registro = manifest.get("lotes", {}).get(lote_id)
    if registro is None:
        raise ValueError(f"lote desconhecido: {lote_id!r}")
    if not gp.lote(lote_id).is_file():
        raise SystemExit(
            f"[pf] o arquivo do lote {lote_id} sumiu de {gp.lote(lote_id)} — "
            "prepare um lote novo; o antigo não é reconstruível"
        )
    estado = str(registro.get("status"))
    if estado == DONE:
        raise ValueError(f"{lote_id}: já foi importado (done) — prepare um lote novo")
    if estado == FAILED:
        _transicionar(registro, PENDING, lote_id)
        registro["motivo"] = None
        registro["tentativas"] = 0
        estado = PENDING
    if estado == PENDING:
        _transicionar(registro, CLAIMED, lote_id)
    registro["claimed_at"] = agora()
    salvar_manifest(manifest, gp)
    return {"lote_id": lote_id, "arquivo": gp.lote(lote_id), "n": int(registro["n_items"])}


def importar(
    conn: sqlite3.Connection,
    lote_id: str,
    texto: str,
    *,
    gp: GeracaoPaths | None = None,
    status: str | None = None,
    modelo: str | None = None,
) -> dict[str, Any]:
    """Valida ESTRITAMENTE e, só então, grava. Devolve o relatório da operação.

    ``ok=False`` **não muda o estado do lote** (segue ``claimed``), pela mesma
    razão do ``pf labels submit``: o maestro corrige o arquivo e submete de
    novo, sem perder o material bom que veio junto. O que muda é a contagem de
    tentativas — esgotada, o lote cai em ``failed`` e para de travar o ciclo.
    """
    gp = gp or GeracaoPaths()
    manifest = carregar_manifest(gp)
    registro = manifest.get("lotes", {}).get(lote_id)
    if registro is None:
        raise ValueError(f"lote desconhecido: {lote_id!r}")
    if str(registro.get("status")) == DONE:
        raise ValueError(f"{lote_id}: já importado — importar de novo duplicaria o material")

    lote = json.loads(gp.lote(lote_id).read_text(encoding="utf-8"))
    resposta, avisos_forma = _carregar_resposta(texto, lote_id)
    if resposta is None:
        return _falhar_import(manifest, registro, lote_id, avisos_forma, [], gp)

    declarado = str(resposta.get("lote_id") or lote_id)
    if declarado != lote_id:
        return _falhar_import(
            manifest,
            registro,
            lote_id,
            [f"{lote_id}: a resposta declara lote_id {declarado!r} — arquivo trocado?"],
            avisos_forma,
            gp,
        )

    campanha = str(registro["campanha"])
    # Resolvido ANTES da validação e fora do ramo: um `--status` inválido tem de
    # falhar antes de o agente ter o trabalho aceito, não depois.
    alvo = validar_status_inicial(status or status_inicial_padrao())
    if campanha == "material":
        itens, erros, avisos = validar_material(lote, resposta)
    else:
        itens, erros, avisos = validar_anotacoes(lote, registro.get("plano", []), resposta)
    if erros:
        return _falhar_import(manifest, registro, lote_id, erros, avisos_forma + avisos, gp)

    # Uma transação para o lote inteiro: meio-lote gravado é o estado que
    # obrigaria alguém a descobrir à mão o que entrou e o que não entrou.
    conn.execute("BEGIN IMMEDIATE")
    try:
        if campanha == "material":
            conta = gravar_material(conn, itens, lote_id)
        else:
            conta = gravar_anotacoes(conn, itens, lote_id, status=alvo)
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    escrever_texto_atomico(
        gp.resposta(lote_id), json.dumps(resposta, ensure_ascii=False, indent=2) + "\n"
    )
    _transicionar(registro, DONE, lote_id)
    registro["done_at"] = agora()
    registro["modelo"] = modelo
    registro["motivo"] = None
    registro["resultado"] = conta
    if campanha == "anotacoes":
        registro["status_inicial"] = alvo
    salvar_manifest(manifest, gp)
    return {
        "ok": True,
        "lote_id": lote_id,
        "campanha": campanha,
        "n_itens": len(itens),
        "gravado": conta,
        "avisos": avisos_forma + avisos,
    }


def _falhar_import(
    manifest: dict[str, Any],
    registro: dict[str, Any],
    lote_id: str,
    erros: Sequence[str],
    avisos: Sequence[str],
    gp: GeracaoPaths,
) -> dict[str, Any]:
    """Conta a tentativa, quarentena no limite, e devolve os erros nomeados."""
    registro["tentativas"] = int(registro.get("tentativas", 0)) + 1
    registro["motivo"] = str(erros[0])[:200] if erros else None
    teto = max_tentativas()
    if registro["tentativas"] >= teto and str(registro.get("status")) == CLAIMED:
        _transicionar(registro, FAILED, lote_id)
        registro["claimed_at"] = None
    salvar_manifest(manifest, gp)
    return {
        "ok": False,
        "lote_id": lote_id,
        "erros": list(erros),
        "avisos": list(avisos),
        "tentativas": registro["tentativas"],
        "max_tentativas": teto,
        "status": registro["status"],
    }


# ---------------------------------------------------------------------------
# painel e composição
# ---------------------------------------------------------------------------


def composicao(conn: sqlite3.Connection) -> dict[str, Any]:
    """Quantas anotações são humanas e quantas são sintéticas, e por alvo.

    **O SINAL É UM SÓ**: ``gabarito_avaliacao_json IS NOT NULL``. Esta função é
    onde ele vira número, e é ela que o painel do admin (P5a) e o dataset card
    (P5b) chamam — não uma segunda contagem escrita do lado de lá, que
    divergiria desta na primeira mudança.

    O projeto não usa prompt de IA para treinar IA e não entrega anotação de IA
    como humana: por isso a composição é declarada, e não deduzida por quem lê.
    """
    coluna = adb.COLUNA_GABARITO_AVALIACAO
    total = int(conn.execute("SELECT count(*) AS n FROM anotacoes").fetchone()["n"])
    sinteticas = int(
        conn.execute(
            f"SELECT count(*) AS n FROM anotacoes WHERE {coluna} IS NOT NULL"
        ).fetchone()["n"]
    )
    por_alvo: dict[str, int] = {}
    por_familia: dict[str, int] = {}
    for linha in conn.execute(
        f"SELECT {coluna} AS g FROM anotacoes WHERE {coluna} IS NOT NULL"
    ):
        try:
            alvo = json.loads(str(linha["g"]))
        except (TypeError, ValueError):
            continue
        if not isinstance(alvo, dict):
            continue
        chave = str(alvo.get("avaliacao_antes") or "?")
        por_alvo[chave] = por_alvo.get(chave, 0) + 1
        familia = str(alvo.get("familia_defeito") or "?")
        por_familia[familia] = por_familia.get(familia, 0) + 1

    material = {
        tabela: {
            str(linha["origem"]): int(linha["n"])
            for linha in conn.execute(
                f"SELECT origem, count(*) AS n FROM {tabela} GROUP BY origem ORDER BY origem"
            )
        }
        for tabela in ("rubricas", "respostas_modelo")
    }
    return {
        "anotacoes": total,
        "sinteticas": sinteticas,
        "humanas": total - sinteticas,
        "sinteticas_por_alvo": dict(sorted(por_alvo.items())),
        "sinteticas_por_familia": dict(sorted(por_familia.items())),
        "material_por_origem": material,
        "sinal": f"anotacoes.{coluna} IS NOT NULL",
    }


def cobertura_do_pool(conn: sqlite3.Connection) -> dict[str, int]:
    """Quantos prompts do pool já sustentam cada tipo de tarefa.

    É o número que o marco existe para mover: ``avaliar_rubrica`` e
    ``comparar_ab`` saindo de 8 (o pacote de demonstração) para centenas.
    """
    linha = conn.execute(
        "SELECT count(*) AS n_pool, "
        "  sum(CASE WHEN r.uid IS NOT NULL AND resp.n >= 1 THEN 1 ELSE 0 END) AS n_avaliar, "
        "  sum(CASE WHEN resp.n >= 2 THEN 1 ELSE 0 END) AS n_ab "
        "FROM pool p "
        "LEFT JOIN (SELECT DISTINCT prompt_uid AS uid FROM rubricas WHERE status = 'ativa') r "
        "       ON r.uid = p.uid "
        "LEFT JOIN (SELECT prompt_uid AS uid, count(DISTINCT rotulo_modelo) AS n "
        "           FROM respostas_modelo GROUP BY prompt_uid) resp ON resp.uid = p.uid"
    ).fetchone()
    demo = conn.execute(
        "SELECT count(*) AS n FROM prompts_demo pd "
        "WHERE (SELECT count(DISTINCT rotulo_modelo) FROM respostas_modelo "
        "       WHERE prompt_uid = pd.uid) >= 2"
    ).fetchone()
    return {
        "pool": int(linha["n_pool"] or 0),
        "avaliar_rubrica": int(linha["n_avaliar"] or 0),
        "comparar_ab": int(linha["n_ab"] or 0),
        "comparar_ab_demo": int(demo["n"] or 0),
    }


def painel(gp: GeracaoPaths | None = None) -> dict[str, Any]:
    """Números da campanha, para o ``pf annotate gerar status``."""
    gp = gp or GeracaoPaths()
    manifest = carregar_manifest(gp)
    lotes = manifest.get("lotes", {})
    por_campanha: dict[str, dict[str, int]] = {
        c: dict.fromkeys((PENDING, CLAIMED, DONE, FAILED), 0) for c in CAMPANHAS
    }
    itens = dict.fromkeys(CAMPANHAS, 0)
    importados = dict.fromkeys(CAMPANHAS, 0)
    limite = datetime.now(UTC) - timedelta(hours=claim_ttl_horas())
    orfaos: list[str] = []
    for lote_id, registro in sorted(lotes.items()):
        campanha = str(registro.get("campanha", "material"))
        estado = str(registro.get("status", PENDING))
        if campanha in por_campanha:
            por_campanha[campanha][estado] = por_campanha[campanha].get(estado, 0) + 1
            itens[campanha] += int(registro.get("n_items", 0))
            if estado == DONE:
                importados[campanha] += int(registro.get("n_items", 0))
        if estado == CLAIMED and (_ler_iso(registro.get("claimed_at")) or limite) <= limite:
            orfaos.append(lote_id)
    return {
        "contrato": manifest.get("contrato"),
        "n_lotes": len(lotes),
        "por_campanha": por_campanha,
        "itens": itens,
        "importados": importados,
        "orfaos": orfaos,
        "ttl_horas": claim_ttl_horas(),
        "distribuicao": distribuicao(),
        "status_inicial": status_inicial_padrao(),
        "lotes": {
            lote_id: {
                "campanha": r.get("campanha"),
                "status": r.get("status"),
                "n_items": r.get("n_items"),
                "tentativas": r.get("tentativas", 0),
                "motivo": r.get("motivo"),
                "modelo": r.get("modelo"),
            }
            for lote_id, r in sorted(lotes.items())
        },
    }


__all__ = [
    "CAMPANHAS",
    "CLAIMED",
    "CONTRATO",
    "DEFEITOS_ANOTACAO",
    "DEFEITOS_RESPOSTA",
    "DONE",
    "FAILED",
    "MAX_CHARS_PROMPT",
    "PENDING",
    "PREFIXO_LOTE",
    "STATUS_INICIAIS",
    "TRANSICOES",
    "GeracaoPaths",
    "agora",
    "carregar_manifest",
    "cobertura_do_pool",
    "composicao",
    "conferir_lingua",
    "distribuicao",
    "escolher_prompts",
    "familia_para",
    "familias_conhecidas",
    "gravar_anotacoes",
    "gravar_material",
    "importar",
    "manifest_vazio",
    "montar_lote_anotacoes",
    "montar_lote_material",
    "montar_plano",
    "painel",
    "pares_ja_na_campanha",
    "preparar",
    "proximo_id",
    "reemitir",
    "salvar_manifest",
    "sortear_alvos",
    "status_inicial_padrao",
    "uids_ja_na_campanha",
    "validar_anotacoes",
    "validar_gabarito",
    "validar_material",
    "validar_status_inicial",
    "varrer_orfaos",
]
