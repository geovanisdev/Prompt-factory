"""P5b — os artefatos de ENTREGA da Bancada: o que sai da plataforma.

A Bancada produz trabalho de anotação. Até este marco, esse trabalho só existia
DENTRO dela: um banco SQLite que ninguém fora desta máquina abre. Um fornecedor
de data annotation não entrega um banco — entrega arquivos com o dado, a
proveniência e a **prova de que o QC aconteceu**. É isso que mora aqui.

SETE PERFIS, DOIS PRODUTOS DIFERENTES
=====================================
Quatro perfis são **dado** (``annotations``, ``sft``, ``preference`` e, desde o
F6, ``triads`` — a tríade da Central de Briefs): uma linha por item, com licença
e atribuição por linha, prontos para carregar. Três são **prosa**
(``quality-report``, ``dataset-card``, ``audit``): o que um avaliador lê para
decidir se confia no dado. O portfólio vive nos dois — dado sem prova de QC é um
arquivo; prova de QC sem dado é uma apresentação.

AS DECISÕES QUE NÃO SÃO DETALHE DE FORMATO
==========================================

* **O que conta como ENTREGUE é ``status = 'avaliada'``**, e só. É o único
  estado terminal de aceitação da máquina de ``db.TRANSICOES``: o item passou
  pela triagem E pela passagem 2 (ou pela decisão do admin, na escalação).
  ``pendente_avaliacao`` — aprovado na triagem, ainda sem Rate and Review — entra
  só com ``incluir_pendentes``, e o manifesto declara isso em letra maiúscula.
  Entregar por padrão o que passou por meia esteira faria o relatório de
  qualidade descrever um QC que não terminou.

* **O payload entregue é o CORRIGIDO**, quando a passagem 2 corrigiu
  (``avaliacoes.payload_corrigido_json``). É a razão de a passagem 2 existir:
  ela conserta no lugar em vez de devolver. Cada linha carrega
  ``corrected_by_reviewer`` e, quando true, também o texto **como submetido** —
  entregar só o corrigido esconderia metade da trilha de QC, e entregar só o
  submetido entregaria o defeito que alguém consertou.

* **Anotação sintética fica FORA dos três perfis de dado, por padrão.** O sinal
  é um só (``geracao.composicao``: ``gabarito_avaliacao_json IS NOT NULL``) e a
  política é do dono: o projeto não usa prompt de IA para treinar IA e não
  entrega anotação de IA como humana. Elas continuam contadas — no manifesto, no
  dataset card e no relatório de qualidade —, porque é justamente **calibrando o
  revisor** que elas servem, e esconder que existem seria a única forma de
  transformar um instrumento de QC em fraude.

* **Licença e atribuição por linha, resolvidas no corpus.** Mesma regra do
  ``export.py`` do universo, pelo mesmo motivo: ODC-BY, CC-BY e CC-BY-SA exigem
  crédito a cada uso. Aqui a resolução passa por ``tarefas.resolver_prompt``, que
  já sabe distinguir prompt do corpus de prompt do pacote de demonstração — e o
  pacote sai marcado ``is_demo``, nunca disfarçado de corpus.

* **Prompt do pacote de demonstração NÃO é excluído.** Ele é fixture, mas a
  anotação FEITA sobre ele é trabalho humano de verdade. Os dois eixos são
  independentes: ``synthetic`` fala de quem escreveu a ANOTAÇÃO, ``is_demo`` fala
  de quem escreveu o PROMPT. Confundi-los descartaria trabalho real.

A LÍNGUA
========
``export.IDIOMA_DOS_ARTEFATOS`` (inglês) vale para toda prosa gerada aqui:
títulos, descrições de perfil, seções do relatório, notas de licença, cabeçalho
de manifesto. **Não** vale para o dado: resposta de SFT e conversa saem na língua
em que foram escritas, e ``lang`` por linha diz qual é.

As CHAVES dos payloads continuam em pt-BR (``notas``, ``justificativa``,
``preferencia``) porque são o **schema gravado** — renomeá-las na saída faria o
arquivo entregue divergir do contrato versionado em ``payload_schema``, e um
``comparar_ab@1`` que não tem as chaves de ``comparar_ab@1`` é pior que uma
chave em português. Quem resolve a opacidade é o ``GLOSSARIO``, publicado dentro
do dataset card.
"""

from __future__ import annotations

import json
import os
import sqlite3
from collections import Counter
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple

from .. import __version__
from ..export import IDIOMA_DOS_ARTEFATOS, EscritorJsonl, nome_de_arquivo, sha256_arquivo
from . import avaliacoes as avmod
from . import db as adb
from . import geracao as germod
from . import tarefas as tmod

#: O único estado terminal de ACEITAÇÃO. Ver ``db.TRANSICOES``.
STATUS_ENTREGUE: tuple[str, ...] = ("avaliada",)

#: Passou na triagem, ainda não passou pelo Rate and Review. Entra só a pedido.
STATUS_PARCIAL: tuple[str, ...] = ("pendente_avaliacao",)

#: O que conta como entregável no perfil ``triads`` (F6). A criação tem UMA
#: passagem de revisão (decisão do P4), então ``aprovada`` já é aceitação; a
#: ``exportada`` continua entrando porque o carimbo do P6 não muda o conteúdo —
#: excluí-la faria a tríade SUMIR da entrega no dia em que a ingestão rodasse.
STATUS_TRIADE: tuple[str, ...] = ("aprovada", "exportada")

#: Chave de payload (pt-BR, do schema gravado) → o que ela significa, em inglês.
#: Vai publicado no dataset card. NÃO é uma tradução aplicada ao arquivo: o
#: arquivo carrega o contrato como ele foi gravado (ver o docstring do módulo).
GLOSSARIO: dict[str, str] = {
    "notas": "per-criterion scores (list)",
    "criterio": "criterion name, as written in the rubric being applied",
    "nota": "score on the criterion's own scale (see the rubric, not 1..9)",
    "justificativa": "rationale, written in English by convention",
    "comentario_geral": "optional overall comment on the item",
    "respostas": "answers to the questions the instrument declares, as "
    "{question_id: text}; free-text questions carry prose (the first is the user "
    "goal the project brief requires before rating), category questions carry a "
    "corpus-taxonomy id (task type / domain) — the label this work produces for "
    "the corpus row",
    "nao_aplicavel": "the criterion does not apply to this response (distinct from unrated)",
    "motivo_na": "why the criterion does not apply — required whenever nao_aplicavel is true",
    "tipos_issue": "issue types for this criterion, as {catalogue_id: picked}; every id the "
    "rubric declares is present, including the unpicked ones",
    "trecho": "verbatim span of the response that supports the finding",
    "trecho_difuso": "the annotator stated the defect has no single span",
    "criterios": "rubric criteria proposed by the annotator (list)",
    "nome": "criterion name, as proposed by the annotator",
    "descricao": "what the criterion observes",
    "escala_min": "low end of the criterion scale",
    "escala_max": "high end of the criterion scale",
    "rotulo_min": "anchor text for the low end",
    "rotulo_max": "anchor text for the high end",
    "resposta": "the reference answer (SFT target), in the prompt's language",
    "checklist": "rubric items the author claims the answer satisfies",
    "atendido": "whether that checklist item is satisfied (boolean)",
    "preferencia": "'modelo-a' | 'modelo-b' | 'empate' (tie)",
    "por_criterio": "optional per-criterion winner breakdown",
    "vencedor": "winning side for that criterion",
    "turnos": "conversation turns, in order",
    "papel": "'usuario' (human) or 'modelo' (assistant)",
    "texto": "the turn's text, in the prompt's language",
    "raciocinio": "the model's reasoning channel, when it emitted one",
    "truncado": "the model hit the token budget and was cut off",
    "rodadas": "duel rounds: both sides, the winner and why",
    "vencedora": "'A' or 'B' — which side won that round",
    "turnos_problematicos": "turns flagged by the annotator, with score and reason",
    "titulo": "rubric title",
    "rubrica": "scores for the rubric applied to the conversation as a whole",
    "notas_do_autor": "free-form notes the annotator left about the session",
    # As chaves da TRÍADE (`material_criacao@1`, perfil `triads` — F6). Mesma
    # regra dos payloads: o contrato gravado não se renomeia na saída.
    "escala": "the criterion's anchored scale: {min, max, ancoras}",
    "ancoras": "scale anchors (list of {valor, rotulo}); both endpoints are always anchored",
    "valor": "the scale position an anchor describes",
    "rotulo": "the anchor's text",
    "deve_conter": "verifiable points a gold answer must present (list)",
    "nao_pode": "errors that disqualify an answer (list)",
    "armadilhas": "what a fluent-but-empty answer would do here (list, optional)",
    "observacoes": "short free prose that does not fit the lists (optional)",
}


class Perfil(NamedTuple):
    """Um artefato de entrega. ``dado`` separa arquivo-de-dado de arquivo-de-prova."""

    chave: str
    container: str
    titulo: str
    descricao: str
    #: ``True`` = perfil de DADO: respeita a política de sintéticas e conta linhas.
    #: ``False`` = perfil de PROVA: descreve o conjunto inteiro, sintéticas
    #: incluídas, porque descrever é a função dele.
    dado: bool
    #: Tipos de tarefa que alimentam o perfil (vazio = todos).
    tipos: tuple[str, ...] = ()


PERFIS: dict[str, Perfil] = {
    "annotations": Perfil(
        "annotations",
        "jsonl",
        "Annotations",
        "One line per delivered annotation: the prompt with its license and "
        "attribution, the final payload, and the full QC trail (triage verdict, "
        "second-pass ratings, admin decision).",
        dado=True,
    ),
    "sft": Perfil(
        "sft",
        "jsonl",
        "SFT pairs",
        "Prompt and reference answer, ready for supervised fine-tuning. The "
        "answer is in the prompt's language; the surrounding metadata is English.",
        dado=True,
        tipos=("sft_resposta",),
    ),
    "preference": Perfil(
        "preference",
        "jsonl",
        "Preference pairs",
        "Chosen and rejected responses with the annotator's rationale. Includes "
        "single-turn A/B comparisons and multi-turn duel rounds, the latter "
        "carrying the conversation that preceded the choice.",
        dado=True,
        tipos=("comparar_ab", "duelo_modelos"),
    ),
    "triads": Perfil(
        "triads",
        "jsonl",
        "Created-prompt triads",
        "One line per prompt written from a pedagogical brief (Central de "
        "Briefs): the human-written prompt (CC0), the rubric to grade answers "
        "with, and the descriptive gold reference. References the request's "
        "theme, teaching goal and role — never the copyrighted excerpt.",
        dado=True,
    ),
    "quality-report": Perfil(
        "quality-report",
        "md",
        "Quality report",
        "How the work was checked: two review passes, reviewer calibration "
        "against hidden targets, and the metric that measures triage itself.",
        dado=False,
    ),
    "dataset-card": Perfil(
        "dataset-card",
        "md",
        "Dataset card",
        "Provenance, license composition, human/synthetic split, task taxonomy "
        "and intended use.",
        dado=False,
    ),
    "audit": Perfil(
        "audit",
        "md",
        "Item audit",
        "The whole chain for one item: prompt, annotation, triage, every "
        "second-pass edit with its stated reason, and the final decision.",
        dado=False,
    ),
}


def perfil(chave: str) -> Perfil:
    """Perfil pela chave; erro alto (com a lista) se não existe."""
    try:
        return PERFIS[chave]
    except KeyError:
        raise KeyError(
            f"perfil desconhecido: {chave!r} (disponíveis: {', '.join(sorted(PERFIS))})"
        ) from None


# ---------------------------------------------------------------------------
# coleta
# ---------------------------------------------------------------------------


def _json(bruto: Any, padrao: Any) -> Any:
    try:
        return json.loads(str(bruto)) if bruto is not None else padrao
    except (TypeError, ValueError):  # pragma: no cover - gravado por nós
        return padrao


_SQL_ANOTACOES = """
SELECT an.id, an.versao, an.payload_schema, an.payload_json, an.versao_diretriz,
       an.versao_brief,
       an.status, an.submetida_em, an.tempo_ativo_ms,
       an.gabarito_avaliacao_json AS gabarito,
       a.id AS atribuicao_id, a.anotador_id, au.nome AS anotador,
       t.id AS tarefa_id, t.tipo, t.prompt_uid, t.origem AS origem_tarefa,
       pj.nome AS projeto
FROM anotacoes an
JOIN atribuicoes a  ON a.id  = an.atribuicao_id
JOIN anotadores au  ON au.id = a.anotador_id
JOIN tarefas t      ON t.id  = a.tarefa_id
LEFT JOIN projetos pj ON pj.id = t.projeto_id
WHERE an.status IN ({vagas})
ORDER BY an.id
"""


def _triagem(conn: sqlite3.Connection, anotacao_id: int) -> dict[str, Any] | None:
    linha = conn.execute(
        "SELECT r.veredito, r.comentario, r.autorrevisao, r.criada_em, rv.nome AS revisor "
        "FROM revisoes r JOIN anotadores rv ON rv.id = r.revisor_id "
        "WHERE r.anotacao_id = ?",
        (anotacao_id,),
    ).fetchone()
    if linha is None:
        return None
    return {
        "verdict": str(linha["veredito"]),
        "comment": linha["comentario"],
        "reviewer": str(linha["revisor"]),
        "self_review": bool(linha["autorrevisao"]),
        "at": linha["criada_em"],
    }


def _decisao_admin(conn: sqlite3.Connection, avaliacao_id: int) -> dict[str, Any] | None:
    linha = conn.execute(
        "SELECT d.decisao, d.comentario, d.criada_em, ad.nome AS admin "
        "FROM decisoes_admin d JOIN anotadores ad ON ad.id = d.admin_id "
        "WHERE d.avaliacao_id = ?",
        (avaliacao_id,),
    ).fetchone()
    if linha is None:
        return None
    return {
        "decision": str(linha["decisao"]),
        "comment": linha["comentario"],
        "admin": str(linha["admin"]),
        "at": linha["criada_em"],
    }


def coletar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    *,
    incluir_sinteticas: bool = False,
    incluir_pendentes: bool = False,
    tipos: Iterable[str] = (),
    projeto: str | None = None,
) -> list[dict[str, Any]]:
    """As anotações entregáveis, hidratadas com prompt, licença e trilha de QC.

    Uma passagem só, e em memória: o volume aqui é o trabalho de um punhado de
    pessoas, não o corpus de 159 mil linhas. Se um dia for, o lugar de paginar é
    este — não os perfis, que já recebem a lista pronta.

    ``None`` de prompt não derruba nada: o uid pode ter sumido numa recarga do
    corpus (aconteceu — o universo foi de 144.754 para 159.733). A linha sai do
    resultado e o contador de ``prompt_sumido`` aparece no manifesto, porque uma
    entrega silenciosamente menor é pior que uma entrega que se explica.
    """
    aceitos = list(STATUS_ENTREGUE) + (list(STATUS_PARCIAL) if incluir_pendentes else [])
    sql = _SQL_ANOTACOES.format(vagas=",".join("?" * len(aceitos)))
    filtro_tipos = {str(t) for t in tipos}

    itens: list[dict[str, Any]] = []
    for linha in conn.execute(sql, aceitos).fetchall():
        tipo = str(linha["tipo"])
        if filtro_tipos and tipo not in filtro_tipos:
            continue
        nome_projeto = linha["projeto"]
        if projeto is not None and str(nome_projeto or "") != projeto:
            continue

        sintetica = linha["gabarito"] is not None
        prompt = tmod.resolver_prompt(conn, conn_corpus, str(linha["prompt_uid"]))

        parecer = avmod.parecer(conn, int(linha["id"]))
        submetido = _json(linha["payload_json"], {})
        corrigido = (parecer or {}).get("payload_corrigido")
        corrigiu = bool(parecer and parecer["edicoes"])

        itens.append(
            {
                "anotacao_id": int(linha["id"]),
                "versao": int(linha["versao"]),
                "tipo": tipo,
                "payload_schema": str(linha["payload_schema"]),
                "versao_diretriz": linha["versao_diretriz"],
                "versao_brief": linha["versao_brief"],
                "status": str(linha["status"]),
                "submetida_em": linha["submetida_em"],
                "tempo_ativo_ms": int(linha["tempo_ativo_ms"]),
                "anotador": str(linha["anotador"]),
                "anotador_id": int(linha["anotador_id"]),
                "projeto": nome_projeto,
                "origem_tarefa": str(linha["origem_tarefa"]),
                "prompt_uid": str(linha["prompt_uid"]),
                "prompt": prompt,
                "sintetica": sintetica,
                # O alvo escondido NUNCA entra num perfil de dado. Ele viaja aqui
                # porque o relatório de qualidade e a auditoria o leem — e os dois
                # só o revelam depois de a avaliação existir.
                "gabarito": _json(linha["gabarito"], None) if sintetica else None,
                "payload_submetido": submetido,
                "payload_final": corrigido if corrigido is not None else submetido,
                "corrigido": corrigiu,
                "triagem": _triagem(conn, int(linha["id"])),
                "avaliacao": parecer,
                "decisao_admin": (
                    _decisao_admin(conn, int(parecer["id"])) if parecer else None
                ),
            }
        )

    if not incluir_sinteticas:
        itens = [i for i in itens if not i["sintetica"]]
    return itens


#: O prompt do pacote de demonstração não tem fonte nem crédito de verdade: ele
#: tem um RÓTULO, e ``tarefas._do_demo`` o escreve em português porque quem o lê
#: é a interface. Num artefato de entrega esse rótulo é prosa nossa, e prosa
#: nossa sai em inglês (``export.IDIOMA_DOS_ARTEFATOS``). Traduzir aqui — e não
#: em ``_do_demo`` — mantém a tela e a API com o texto que elas já servem.
#: Nome de fonte REAL (``wildchat_pt``, ``aya``) nunca passa por aqui: é
#: identificador, e traduzir identificador quebraria o casamento com o corpus.
ROTULO_DEMO = {
    "source": "demonstration pack",
    "attribution": "hand-written for the Bancada demonstration",
}


def _licenca(item: dict[str, Any]) -> dict[str, Any]:
    """O bloco de proveniência de uma linha entregue. Sem ele o arquivo não cumpre a licença."""
    p = item["prompt"] or {}
    demo = bool(p.get("demo"))
    return {
        "uid": item["prompt_uid"],
        "lang": p.get("lang"),
        "source": ROTULO_DEMO["source"] if demo else p.get("fonte"),
        "license": p.get("licenca"),
        "license_class": p.get("licenca_classe"),
        "attribution": ROTULO_DEMO["attribution"] if demo else p.get("atribuicao", ""),
        "is_demo": demo,
    }


def _qc(item: dict[str, Any]) -> dict[str, Any]:
    """A trilha de QC de uma linha, achatada para leitura de máquina."""
    av = item["avaliacao"]
    return {
        "triage": item["triagem"],
        "rating": (
            None
            if av is None
            else {
                "before_edit": av["avaliacao_antes"],
                "after_edit": av["avaliacao_depois"],
                "rationale": av["justificativa"],
                "reviewer": av["revisor"],
                "self_review": av["autorrevisao"],
                "edits": [
                    {
                        "field": e["campo"],
                        "from": e["valor_antes"],
                        "to": e["valor_depois"],
                        "reason": e["motivo"],
                    }
                    for e in av["edicoes"]
                ],
            }
        ),
        "admin": item["decisao_admin"],
        "status": item["status"],
    }


# ---------------------------------------------------------------------------
# perfis de DADO
# ---------------------------------------------------------------------------


def registros_annotations(itens: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Uma linha por anotação: prompt + payload final + trilha de QC."""
    for item in itens:
        p = item["prompt"] or {}
        registro: dict[str, Any] = {
            "annotation_id": item["anotacao_id"],
            "task_type": item["tipo"],
            "payload_schema": item["payload_schema"],
            "guideline_version": item["versao_diretriz"],
            # O enquadramento do projeto sob o qual isto foi feito. Sem ele o
            # cliente lê o nome do projeto e não sabe QUAL versão do brief
            # dele valia — que é justamente a pergunta que aparece quando o
            # lote chega inconsistente.
            "project_brief_version": item["versao_brief"],
            "project": item["projeto"],
            "prompt": {**_licenca(item), "text": p.get("text")},
            "payload": item["payload_final"],
            "corrected_by_reviewer": item["corrigido"],
            "annotator": item["anotador"],
            "submitted_at": item["submetida_em"],
            "active_time_ms": item["tempo_ativo_ms"],
            "revision": item["versao"],
            "qc": _qc(item),
            "synthetic": item["sintetica"],
        }
        if item["corrigido"]:
            # Só quando houve correção — em 100% dos outros casos seria a mesma
            # árvore duas vezes, dobrando o arquivo para dizer "nada mudou".
            registro["payload_as_submitted"] = item["payload_submetido"]
        yield registro


def registros_sft(itens: list[dict[str, Any]]) -> Iterable[dict[str, Any]]:
    """Par prompt → resposta de referência. Só ``sft_resposta``."""
    for item in itens:
        if item["tipo"] != "sft_resposta":
            continue
        payload = item["payload_final"] or {}
        resposta = payload.get("resposta")
        if not resposta:  # pragma: no cover - o Pydantic exige na submissão
            continue
        p = item["prompt"] or {}
        yield {
            **_licenca(item),
            "prompt": p.get("text"),
            "response": resposta,
            "checklist": payload.get("checklist"),
            "notes": payload.get("notas"),
            "annotator": item["anotador"],
            "annotation_id": item["anotacao_id"],
            "corrected_by_reviewer": item["corrigido"],
        }


def _resposta_por_rotulo(conn: sqlite3.Connection, uid: str) -> dict[str, dict[str, Any]]:
    """``{'modelo-a': {...}, 'modelo-b': {...}}`` para um prompt.

    O ``meta_json`` guarda o defeito plantado e o nome real do modelo — e é ele
    que torna o par de preferência auditável. Ele sai no export porque o export
    é para o cliente, não para o anotador: o rótulo cego existe para quem JULGA,
    não para quem recebe o dado julgado.
    """
    return {
        str(linha["rotulo_modelo"]): {
            "text": str(linha["texto"]),
            "origin": str(linha["origem"]),
            "meta": _json(linha["meta_json"], {}),
        }
        for linha in conn.execute(
            "SELECT rotulo_modelo, texto, origem, meta_json FROM respostas_modelo "
            "WHERE prompt_uid = ? ORDER BY id",
            (uid,),
        )
    }


def registros_preference(
    conn: sqlite3.Connection, itens: list[dict[str, Any]]
) -> Iterable[dict[str, Any]]:
    """Pares de preferência: A/B de turno único e cada rodada de duelo.

    Empate NÃO vira par. Um par de preferência com escolhido == rejeitado não
    treina nada e envenena qualquer treino de recompensa que o leia sem conferir;
    o empate continua contado no manifesto, que é onde ele informa.

    A rodada de duelo carrega o ``context`` — os turnos ANTERIORES a ela. É o que
    distingue este dado do A/B de turno único e o que o torna raro: "escolhi B
    porque ele lembrou da restrição que eu tinha dado dois turnos atrás" só é
    verificável com a conversa junto.
    """
    for item in itens:
        p = item["prompt"] or {}
        payload = item["payload_final"] or {}
        base = {
            **_licenca(item),
            "prompt": p.get("text"),
            "annotator": item["anotador"],
            "annotation_id": item["anotacao_id"],
            "corrected_by_reviewer": item["corrigido"],
        }

        if item["tipo"] == "comparar_ab":
            preferencia = payload.get("preferencia")
            if preferencia not in adb.ROTULOS_MODELO:
                continue  # empate
            lados = _resposta_por_rotulo(conn, item["prompt_uid"])
            outro = next(r for r in adb.ROTULOS_MODELO if r != preferencia)
            escolhida, rejeitada = lados.get(preferencia), lados.get(outro)
            if escolhida is None or rejeitada is None:
                continue
            yield {
                **base,
                "multi_turn": False,
                "round": None,
                "context": [],
                "chosen": escolhida["text"],
                "rejected": rejeitada["text"],
                "chosen_label": preferencia,
                "rationale": payload.get("justificativa"),
                "per_criterion": payload.get("por_criterio"),
                "provenance": {
                    "chosen": escolhida["meta"],
                    "rejected": rejeitada["meta"],
                    "origin": escolhida["origin"],
                },
            }
            continue

        if item["tipo"] != "duelo_modelos":
            continue

        turnos = payload.get("turnos") or []
        for rodada in payload.get("rodadas") or []:
            respostas = {str(r.get("rotulo")): r for r in rodada.get("respostas") or []}
            vencedora = str(rodada.get("vencedora") or "")
            perdedora = next((r for r in adb.ROTULOS_DUELO if r != vencedora), "")
            escolhida, rejeitada = respostas.get(vencedora), respostas.get(perdedora)
            if escolhida is None or rejeitada is None:  # pragma: no cover - validado
                continue
            ordem = rodada.get("ordem")
            yield {
                **base,
                "multi_turn": True,
                "round": ordem,
                "context": [
                    {"role": t.get("papel"), "text": t.get("texto")}
                    for t in turnos
                    if isinstance(ordem, int) and int(t.get("ordem", -1)) < ordem
                ],
                "chosen": escolhida.get("texto"),
                "rejected": rejeitada.get("texto"),
                "chosen_label": vencedora,
                "rationale": rodada.get("justificativa"),
                "per_criterion": None,
                "provenance": {
                    "chosen": {
                        "model": escolhida.get("modelo"),
                        "truncated": escolhida.get("truncado"),
                    },
                    "rejected": {
                        "model": rejeitada.get("modelo"),
                        "truncated": rejeitada.get("truncado"),
                    },
                    "origin": "local-inference",
                },
            }


def registros_triades(
    conn: sqlite3.Connection, conn_corpus: sqlite3.Connection
) -> Iterable[dict[str, Any]]:
    """A tríade da Central de Briefs: prompt humano + rubrica + gold (F6).

    O registro é montado CAMPO A CAMPO, e a disciplina aqui é a mesma do
    ``gabarito_json`` do P2: ``criacoes.listar`` devolve o pedido INTEIRO
    (recorte incluído — o revisor precisa dele) e o ``anticopia`` com o TRECHO
    literal em comum. Nenhum dos dois pode sair: são texto do material de
    editora, e a §6 do plano é a razão de ser do módulo. Um ``{**item}``
    passaria no teste de hoje e vazaria no dia em que alguém acrescentasse uma
    chave.

    As chaves de ``rubrica``/``gold`` ficam em pt-BR (o contrato
    ``material_criacao@1`` gravado — ver o docstring do módulo); o bloco
    ``request`` é metadado NOSSO e sai em inglês, como toda prosa de artefato.
    ``anti_copy`` publica o NÚMERO da verificação e a régua, nunca o trecho.
    """
    from . import criacoes as crimod

    for item in crimod.listar(conn, conn_corpus):
        if item["status"] not in STATUS_TRIADE:
            continue
        if item["pedido_id"] is None or not item["material"]:
            continue
        material = item["material"]
        ped = item["pedido"] or {}
        ac = item["anticopia"] or {}
        yield {
            "creation_id": item["id"],
            "uid": item["uid_previsto"],
            "in_corpus": item["chegou_ao_corpus"],
            "lang": item["lang"],
            "source": crimod.FONTE,
            "license": item["licenca"],
            "author": item["autor"],
            "status": item["status"],
            "prompt": item["texto"],
            "material_schema": material.get("schema"),
            "rubrica": material.get("rubrica"),
            "gold": material.get("gold"),
            "request": {
                "id": item["pedido_id"],
                "role": ped.get("papel"),
                "task_type": ped.get("task_type"),
                "theme": ped.get("tema"),
                "teaching_goal": ped.get("meta_pedagogica"),
                "series": ped.get("serie"),
                "difficulty": ped.get("dificuldade"),
                "subject": ped.get("disciplina"),
                "bncc_skills": ped.get("habilidades"),
            },
            "anti_copy": (
                {"overlap_chars": ac.get("chars"), "threshold": ac.get("limiar")}
                if ac
                else None
            ),
            "reviewer": item["revisor"],
            "reviewed_at": item["revisada_em"],
        }


def resumo_central(conn: sqlite3.Connection) -> dict[str, Any]:
    """Quantas tríades entregáveis existem, para o dataset card declarar."""
    linha = conn.execute(
        "SELECT count(*) AS n, count(DISTINCT pedido_id) AS pedidos FROM criacoes "
        f"WHERE pedido_id IS NOT NULL AND material_json IS NOT NULL "
        f"  AND status IN ({','.join('?' * len(STATUS_TRIADE))})",
        STATUS_TRIADE,
    ).fetchone()
    return {"triades": int(linha["n"]), "pedidos": int(linha["pedidos"])}


# ---------------------------------------------------------------------------
# métricas (leem TUDO, sintéticas incluídas — descrever é a função delas)
# ---------------------------------------------------------------------------


def calibracao_do_revisor(itens: list[dict[str, Any]]) -> dict[str, Any]:
    """O revisor acertou o alvo escondido das sintéticas?

    Simétrico às tarefas-ouro que calibram o ANOTADOR, e é o instrumento mais
    raro do conjunto: quase nenhuma plataforma mede quem revisa.

    A escala é ORDINAL (``db.AVALIACOES_ANTES``), então a distância importa:
    chamar de ``adequado`` o que era ``excepcional`` é um erro de um passo;
    chamar de ``adequado`` o que era ``inutilizavel`` são três. Por isso saem as
    três medidas — exato, ±1 e o erro médio absoluto —, e não só a taxa de acerto
    exato, que trataria os dois casos como o mesmo erro.
    """
    escala = adb.AVALIACOES_ANTES
    indice = {nome: i for i, nome in enumerate(escala)}
    pares: list[tuple[int, int]] = []
    confusao: Counter[tuple[str, str]] = Counter()
    por_familia: Counter[str] = Counter()
    acertos_familia: Counter[str] = Counter()

    for item in itens:
        alvo, av = item["gabarito"], item["avaliacao"]
        if not item["sintetica"] or not isinstance(alvo, dict) or av is None:
            continue
        esperado, obtido = str(alvo.get("avaliacao_antes") or ""), av["avaliacao_antes"]
        if esperado not in indice or obtido not in indice:  # pragma: no cover
            continue
        pares.append((indice[esperado], indice[obtido]))
        confusao[(esperado, obtido)] += 1
        familia = str(alvo.get("familia_defeito") or "?")
        por_familia[familia] += 1
        if esperado == obtido:
            acertos_familia[familia] += 1

    n = len(pares)
    if not n:
        return {"n": 0, "scale": list(escala)}
    exatos = sum(1 for a, b in pares if a == b)
    vizinhos = sum(1 for a, b in pares if abs(a - b) <= 1)
    return {
        "n": n,
        "scale": list(escala),
        "exact": exatos,
        "exact_rate": round(exatos / n, 4),
        "within_one": vizinhos,
        "within_one_rate": round(vizinhos / n, 4),
        "mean_absolute_error": round(sum(abs(a - b) for a, b in pares) / n, 4),
        "confusion": {f"{e} -> {o}": c for (e, o), c in sorted(confusao.items())},
        "by_defect_family": {
            fam: {"n": total, "exact": acertos_familia[fam]}
            for fam, total in sorted(por_familia.items())
        },
    }


def triagem_deixou_passar(itens: list[dict[str, Any]]) -> dict[str, Any]:
    """Quantos itens a triagem aprovou e a passagem 2 julgou impróprios.

    **Esta métrica mede o revisor da TRIAGEM, não quem anotou** — e é por isso
    que ela existe nomeada em vez de diluída numa taxa de rejeição geral. Um item
    que chega ao Rate and Review como ``inutilizavel`` passou por um portão que
    devia tê-lo barrado; um que sai como ``incorrigivel`` custou o trabalho das
    duas passagens para ser descartado no fim.
    """
    aprovados = [i for i in itens if (i["triagem"] or {}).get("verdict") == "aprovada"]
    avaliados = [i for i in aprovados if i["avaliacao"]]
    inuteis = [i for i in avaliados if i["avaliacao"]["avaliacao_antes"] == "inutilizavel"]
    perdidos = [i for i in avaliados if i["avaliacao"]["avaliacao_depois"] == "incorrigivel"]
    return {
        "approved_by_triage": len(aprovados),
        "then_rated": len(avaliados),
        "arrived_unusable": len(inuteis),
        "ended_unsalvageable": len(perdidos),
        "leak_rate": round(len(inuteis) / len(avaliados), 4) if avaliados else None,
        "annotation_ids": {
            "arrived_unusable": [i["anotacao_id"] for i in inuteis],
            "ended_unsalvageable": [i["anotacao_id"] for i in perdidos],
        },
    }


def panorama(itens: list[dict[str, Any]]) -> dict[str, Any]:
    """Volumes por tipo, por pessoa, por licença e por escala das duas passagens."""

    def _conta(chave: Any) -> Counter[str]:
        return Counter(str(chave(i)) for i in itens)

    tempos = sorted(i["tempo_ativo_ms"] for i in itens if i["tempo_ativo_ms"] > 0)
    mediana = tempos[len(tempos) // 2] if tempos else 0
    edicoes = sum(len((i["avaliacao"] or {}).get("edicoes") or []) for i in itens)
    return {
        "total": len(itens),
        "human": sum(1 for i in itens if not i["sintetica"]),
        "synthetic": sum(1 for i in itens if i["sintetica"]),
        "by_task_type": dict(_conta(lambda i: i["tipo"]).most_common()),
        "by_annotator": dict(_conta(lambda i: i["anotador"]).most_common()),
        "by_project": dict(_conta(lambda i: i["projeto"] or "-").most_common()),
        "by_language": dict(_conta(lambda i: (i["prompt"] or {}).get("lang") or "?").most_common()),
        "by_license": dict(
            _conta(lambda i: (i["prompt"] or {}).get("licenca") or "?").most_common()
        ),
        # Via `_licenca`, e não direto do prompt: é lá que o rótulo do pacote de
        # demonstração vira inglês, e a tabela do dataset card cruza com este
        # nome. Duas leituras diferentes fariam a tabela ficar vazia.
        "by_source": dict(_conta(lambda i: _licenca(i)["source"] or "?").most_common()),
        "rating_before": dict(
            Counter(
                i["avaliacao"]["avaliacao_antes"] for i in itens if i["avaliacao"]
            ).most_common()
        ),
        "rating_after": dict(
            Counter(
                i["avaliacao"]["avaliacao_depois"] for i in itens if i["avaliacao"]
            ).most_common()
        ),
        "corrected_by_reviewer": sum(1 for i in itens if i["corrigido"]),
        "edits_total": edicoes,
        "self_reviewed": sum(
            1 for i in itens if (i["avaliacao"] or {}).get("autorrevisao")
        ),
        "median_active_ms": mediana,
    }


# ---------------------------------------------------------------------------
# perfis de PROVA (markdown, inglês — ver export.IDIOMA_DOS_ARTEFATOS)
# ---------------------------------------------------------------------------


def _celula_md(valor: Any) -> str:
    """Valor → célula de tabela markdown, sem quebrar a tabela.

    Dois caracteres derrubam uma tabela em silêncio, e os dois chegam aqui em
    texto que NÃO é nosso: o ``|`` (o glossário descreve um enum como
    ``'modelo-a' | 'modelo-b'``) e a quebra de linha (o motivo de cada edição da
    passagem 2 é campo livre do revisor). A tabela quebrada não dá erro: ela
    renderiza torta, e as colunas seguintes escorregam sem que nada avise.
    """
    if valor is None:
        return ""
    texto = str(valor).replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(texto.split())


def _tabela(cabecalho: tuple[str, ...], linhas: Iterable[tuple[Any, ...]]) -> list[str]:
    out = ["| " + " | ".join(cabecalho) + " |", "|" + "---|" * len(cabecalho)]
    out += ["| " + " | ".join(_celula_md(c) for c in linha) + " |" for linha in linhas]
    return out


def _contagem_md(titulo: str, mapa: dict[str, int], total: int) -> list[str]:
    if not mapa:
        return []
    return [
        "",
        f"**{titulo}**",
        "",
        *_tabela(
            ("value", "n", "share"),
            ((k, v, f"{v / total:.1%}" if total else "-") for k, v in mapa.items()),
        ),
    ]


def relatorio_qualidade(itens: list[dict[str, Any]], contexto: dict[str, Any]) -> str:
    """O relatório de QC, em inglês. Descreve o conjunto INTEIRO, sintéticas incluídas."""
    pan = panorama(itens)
    cal = calibracao_do_revisor(itens)
    vaz = triagem_deixou_passar(itens)
    linhas = [
        "# Quality report",
        "",
        f"Generated {contexto['created_at']} · Bancada {__version__} · "
        f"annotation schema v{adb.SCHEMA_VERSION_ANOTACAO}",
        "",
        "## Scope",
        "",
        f"{pan['total']} annotation(s) in delivered state "
        f"(`{'`, `'.join(contexto['statuses'])}`), of which **{pan['human']} human** "
        f"and **{pan['synthetic']} synthetic**.",
        "",
        "Synthetic annotations are machine-generated on purpose and carry a hidden "
        "target rating. They are **excluded from the data exports** and kept here "
        "because measuring the reviewer is what they are for. The signal is a "
        f"single column: `{contexto['synthetic_signal']}`.",
        "",
        "## How the work was checked",
        "",
        "Every annotation goes through two independent passes:",
        "",
        "1. **Triage** — approve or return. This is the *only* pass that sends work "
        "back to the annotator; returned work is re-submitted as a new version.",
        "2. **Rate and Review** — rate the work as it arrived, fix it in place with "
        "a stated reason for every single change, rate the result, and justify the "
        "rating. `borderline_admin` escalates to an administrator who decides and "
        "closes the item; `incorrigivel` is terminal.",
        "",
        *_tabela(
            ("measure", "value"),
            (
                ("annotations rated", sum(pan["rating_before"].values())),
                ("corrected in place by the reviewer", pan["corrected_by_reviewer"]),
                ("individual field edits, each with a reason", pan["edits_total"]),
                ("self-reviewed (solo mode)", pan["self_reviewed"]),
                ("median active time per annotation", f"{pan['median_active_ms']} ms"),
            ),
        ),
        *_contagem_md("Rating on arrival", pan["rating_before"], sum(pan["rating_before"].values())),
        *_contagem_md("Rating after correction", pan["rating_after"], sum(pan["rating_after"].values())),
        "",
        "## Did triage let anything through?",
        "",
        "This is the metric that measures **the triage reviewer**, not the "
        "annotator: an item approved in pass 1 and found unusable in pass 2 went "
        "through a gate that should have stopped it.",
        "",
        *_tabela(
            ("measure", "value"),
            (
                ("approved by triage", vaz["approved_by_triage"]),
                ("of those, reached pass 2", vaz["then_rated"]),
                ("arrived `inutilizavel`", vaz["arrived_unusable"]),
                ("ended `incorrigivel`", vaz["ended_unsalvageable"]),
                ("leak rate", "-" if vaz["leak_rate"] is None else f"{vaz['leak_rate']:.1%}"),
            ),
        ),
        "",
        "## Reviewer calibration",
        "",
    ]

    if not cal["n"]:
        linhas += [
            "No synthetic annotation has been rated yet, so reviewer calibration "
            "cannot be measured. It is not zero — it is **not measured**, which is "
            "a different statement and the honest one.",
        ]
    else:
        linhas += [
            f"{cal['n']} synthetic annotation(s) carried a hidden target rating and "
            "were reviewed blind. The scale is ordinal "
            f"(`{'` < `'.join(cal['scale'])}`), so distance matters: the mean "
            "absolute error separates a one-step miss from a three-step one, which "
            "an exact-match rate alone would flatten.",
            "",
            *_tabela(
                ("measure", "value"),
                (
                    ("exact agreement with target", f"{cal['exact']}/{cal['n']} ({cal['exact_rate']:.1%})"),
                    ("within one step", f"{cal['within_one']}/{cal['n']} ({cal['within_one_rate']:.1%})"),
                    ("mean absolute error (steps)", cal["mean_absolute_error"]),
                ),
            ),
            "",
            "**Target → reviewer**",
            "",
            *_tabela(("transition", "n"), cal["confusion"].items()),
            "",
            "**By planted defect family**",
            "",
            *_tabela(
                ("defect family", "n", "exact"),
                ((k, v["n"], v["exact"]) for k, v in cal["by_defect_family"].items()),
            ),
        ]

    linhas += [
        "",
        "## Composition",
        "",
        *_tabela(("task type", "n"), pan["by_task_type"].items()),
        *_contagem_md("By annotator", pan["by_annotator"], pan["total"]),
        *_contagem_md("By prompt language", pan["by_language"], pan["total"]),
        *_contagem_md("By prompt license", pan["by_license"], pan["total"]),
        "",
    ]
    return "\n".join(linhas) + "\n"


def dataset_card(itens: list[dict[str, Any]], contexto: dict[str, Any]) -> str:
    """O dataset card, em inglês — proveniência, licença e composição declaradas."""
    pan = panorama(itens)
    comp = contexto.get("composicao") or {}
    linhas = [
        "# Dataset card — Bancada annotations",
        "",
        f"Generated {contexto['created_at']} · Bancada {__version__} · "
        f"taxonomy {contexto['taxonomy_version']} · "
        f"annotation schema v{adb.SCHEMA_VERSION_ANOTACAO}",
        "",
        "## What this is",
        "",
        "Human annotations produced on a data-annotation platform, over prompts "
        "**written by real people** and carried through the Prompt Factory "
        "pipeline with provenance and license preserved per row.",
        "",
        "## Provenance of the prompts",
        "",
        "No prompt in this dataset was generated by a language model. That is the "
        "founding constraint of the project: prompts are ingested from public "
        "conversation corpora, deduplicated exactly and near-exactly, language-"
        "verified in two layers, and scrubbed of PII. Every row below names its "
        "source, its license and the attribution that license requires.",
        "",
        *_tabela(
            ("source", "annotations", "license"),
            (
                (
                    fonte,
                    n,
                    ", ".join(
                        sorted(
                            {
                                str(_licenca(i)["license"])
                                for i in itens
                                if _licenca(i)["source"] == fonte
                            }
                        )
                    ),
                )
                for fonte, n in pan["by_source"].items()
            ),
        ),
        "",
        "Prompts marked `is_demo` come from a hand-written demonstration pack, not "
        "from the corpus. They are labelled on every row rather than removed: the "
        "annotation made on top of one is real human work.",
        "",
        "## Composition",
        "",
        *_tabela(
            ("measure", "value"),
            (
                ("annotations delivered", pan["total"]),
                ("human", pan["human"]),
                ("synthetic (excluded from data exports)", pan["synthetic"]),
                ("distinct annotators", len(pan["by_annotator"])),
                ("task types covered", len(pan["by_task_type"])),
            ),
        ),
        *_contagem_md("By task type", pan["by_task_type"], pan["total"]),
        *_contagem_md("By language", pan["by_language"], pan["total"]),
    ]

    if comp:
        linhas += [
            "",
            "### Human vs synthetic, declared",
            "",
            "Synthetic annotations exist to calibrate the reviewer: each one carries "
            "a hidden target rating and a planted defect family, and is reviewed "
            "blind. They are declared here rather than inferred by the reader, and "
            "they do **not** appear in the data exports.",
            "",
            *_tabela(
                ("measure", "value"),
                (
                    ("annotations in the platform (all states)", comp.get("anotacoes")),
                    ("human", comp.get("humanas")),
                    ("synthetic", comp.get("sinteticas")),
                    ("signal", f"`{comp.get('sinal')}`"),
                ),
            ),
        ]

    central = contexto.get("central") or {}
    if central.get("triades"):
        linhas += [
            "",
            "## Created prompts (Central de Briefs)",
            "",
            f"{central['triades']} prompt(s) in this platform were **elicited by "
            "pedagogical briefs derived from commercial teaching material** (PNLD "
            "textbooks), **without reproducing the material**: the writer reads an "
            "excerpt locally and writes the prompt in their own words, dedicated "
            "CC0. The excerpt never enters the prompt, the platform's exports or "
            "this delivery — an automated anti-copy check (longest literal overlap "
            "against a configured threshold) enforces the rule at submission, and "
            "the reviewer judges originality with the excerpt at hand.",
            "",
            "Each such prompt ships as a **triad** in `triads.jsonl`: the prompt, "
            "the rubric to grade answers with, and a descriptive gold reference. "
            "Rows reference the request's theme, teaching goal and role — never "
            "the excerpt.",
        ]

    linhas += [
        "",
        "## Language convention",
        "",
        *_tabela(
            ("field", "language", "why"),
            (
                ("rationale, review comment, rubric criterion, metadata", "English", "the client reads it"),
                ("SFT reference answer, model conversation", "the prompt's language", "it is the data being produced"),
                ("this card, the quality report, the audit", "English", f"`IDIOMA_DOS_ARTEFATOS = {IDIOMA_DOS_ARTEFATOS!r}`"),
            ),
        ),
        "",
        "## Payload field glossary",
        "",
        "Payload keys are kept exactly as the versioned contract stores them "
        "(`payload_schema` on every row). They are in Portuguese because renaming "
        "them on export would make the delivered file disagree with the schema it "
        "declares. This is what they mean:",
        "",
        *_tabela(("key", "meaning"), GLOSSARIO.items()),
        "",
        "## Intended use and limits",
        "",
        "* Supervised fine-tuning (`sft.jsonl`), preference modelling "
        "(`preference.jsonl`) and evaluation-rubric work (`annotations.jsonl`).",
        "* Rows whose license forbids commercial use are **not** silently dropped — "
        "they carry `license` and `license_class` and the decision is the reader's. "
        "Rows that are not redistributable never enter the platform's prompt pool "
        "at all.",
        "* `nsfw` and the task taxonomy are **not yet labelled** in the underlying "
        "corpus; the platform's pool clause for NSFW currently excludes zero rows, "
        "and this card says so rather than promising a guarantee the data does not "
        "support.",
        "",
    ]
    return "\n".join(linhas) + "\n"


def auditoria(item: dict[str, Any], contexto: dict[str, Any]) -> str:
    """A cadeia inteira de UM item, em inglês. O artefato mais forte do portfólio.

    Mostra o aparato de QC completo num arquivo só: o prompt com a licença, a
    anotação como foi submetida, o veredito da triagem, **cada** edição da
    passagem 2 com o motivo declarado, a nota antes e depois, e a decisão final.
    """
    p = item["prompt"] or {}
    proc = _licenca(item)
    av = item["avaliacao"]
    linhas = [
        f"# Item audit — annotation {item['anotacao_id']}",
        "",
        f"Generated {contexto['created_at']} · task type `{item['tipo']}` · "
        f"contract `{item['payload_schema']}` · guideline v{item['versao_diretriz']}"
        + (f" · project brief v{item['versao_brief']}" if item["versao_brief"] else ""),
        "",
        "## 1. The prompt",
        "",
        *_tabela(
            ("field", "value"),
            (
                ("uid", f"`{item['prompt_uid']}`"),
                ("language", proc["lang"]),
                ("source", proc["source"]),
                ("license", proc["license"]),
                ("attribution", proc["attribution"] or "—"),
                ("from the demo pack", "yes" if proc["is_demo"] else "no"),
            ),
        ),
        "",
        "```text",
        str(p.get("text") or "(the prompt is no longer in the corpus)"),
        "```",
        "",
        "## 2. The annotation, as submitted",
        "",
        f"By **{item['anotador']}** on {item['submetida_em']} "
        f"(revision {item['versao']}, {item['tempo_ativo_ms']} ms of active time).",
        "",
        "```json",
        json.dumps(item["payload_submetido"], ensure_ascii=False, indent=2),
        "```",
        "",
        "## 3. Triage (pass 1)",
        "",
    ]

    tri = item["triagem"]
    if tri is None:
        linhas += ["Not triaged."]
    else:
        linhas += [
            f"**{tri['verdict']}** by {tri['reviewer']} on {tri['at']}"
            + (" · self-review" if tri["self_review"] else ""),
            "",
            f"> {tri['comment'] or '(no comment)'}",
        ]

    linhas += ["", "## 4. Rate and Review (pass 2)", ""]
    if av is None:
        linhas += ["Not rated yet."]
    else:
        linhas += [
            *_tabela(
                ("stage", "rating"),
                (
                    ("as it arrived", f"`{av['avaliacao_antes']}`"),
                    ("after correction", f"`{av['avaliacao_depois']}`"),
                ),
            ),
            "",
            f"Reviewer: **{av['revisor']}**"
            + (" · self-review" if av["autorrevisao"] else "")
            + f" · {av['criada_em']}",
            "",
            "### Every change, with the reason given for it",
            "",
        ]
        if not av["edicoes"]:
            linhas += ["The reviewer changed nothing."]
        else:
            linhas += _tabela(
                ("field", "from", "to", "stated reason"),
                (
                    (f"`{e['campo']}`", e["valor_antes"], e["valor_depois"], e["motivo"])
                    for e in av["edicoes"]
                ),
            )
        linhas += ["", "### Rating rationale", "", f"> {av['justificativa']}"]

    linhas += ["", "## 5. Outcome", ""]
    dec = item["decisao_admin"]
    if dec is not None:
        linhas += [
            f"Escalated to an administrator. **{dec['decision']}** by {dec['admin']} "
            f"on {dec['at']}.",
            "",
            f"> {dec['comment'] or '(no comment)'}",
            "",
        ]
    linhas += [f"Final status: **`{item['status']}`**."]

    # O alvo escondido só é revelado DEPOIS de a avaliação existir — que é a
    # mesma regra do painel. Revelá-lo antes entregaria o gabarito de um item que
    # ainda vai ser julgado, e a calibração deixaria de medir qualquer coisa.
    if item["sintetica"] and av is not None and isinstance(item["gabarito"], dict):
        alvo = item["gabarito"]
        linhas += [
            "",
            "## 6. Synthetic item — the hidden target, now revealed",
            "",
            "This annotation was machine-generated to calibrate the reviewer. The "
            "target below was written before the review and was never visible in "
            "the interface, in the API envelope or in the page source.",
            "",
            *_tabela(
                ("field", "value"),
                (
                    ("target rating on arrival", f"`{alvo.get('avaliacao_antes')}`"),
                    ("reviewer said", f"`{av['avaliacao_antes']}`"),
                    ("match", "yes" if alvo.get("avaliacao_antes") == av["avaliacao_antes"] else "no"),
                    ("planted defect family", alvo.get("familia_defeito")),
                    ("note", alvo.get("nota")),
                ),
            ),
        ]
    return "\n".join(linhas) + "\n"


# ---------------------------------------------------------------------------
# execução
# ---------------------------------------------------------------------------


class Resultado(NamedTuple):
    """O que um artefato produziu."""

    arquivo: Path
    manifesto: Path
    row_count: int
    manifest: dict[str, Any]


def _escrever_atomico(destino: Path, escrever: Any) -> None:
    """``.tmp`` + ``os.replace``: Ctrl+C deixa um ``.tmp`` órfão, nunca meio artefato."""
    tmp = destino.with_suffix(destino.suffix + ".tmp")
    try:
        escrever(tmp)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, destino)


def executar(
    conn: sqlite3.Connection,
    conn_corpus: sqlite3.Connection,
    chave_perfil: str,
    *,
    destino_dir: Path,
    nome: str | None = None,
    incluir_sinteticas: bool = False,
    incluir_pendentes: bool = False,
    tipos: Iterable[str] = (),
    projeto: str | None = None,
    anotacao_id: int | None = None,
) -> Resultado:
    """Gera um artefato + o manifesto dele. Devolve os caminhos e as contagens.

    O ``row_count`` é contado NO LAÇO, nunca por um ``count(*)`` prévio: a
    política de sintéticas, o empate do A/B e o prompt sumido do corpus todos
    cortam linhas depois da consulta, e um total prometido antes da escrita
    descreveria um arquivo diferente do que está no disco.
    """
    pf = perfil(chave_perfil)
    destino_dir.mkdir(parents=True, exist_ok=True)
    base = nome_de_arquivo(nome or pf.chave)
    arquivo = destino_dir / f"{base}.{pf.container}"

    # Os perfis de PROVA descrevem o conjunto inteiro (sintéticas incluídas):
    # descrever é a função deles, e um relatório de QC que esconde metade do
    # material sobre o qual o QC foi exercido não é um relatório de QC.
    # O `triads` (F6) não lê anotações de jeito nenhum: a fonte dele é
    # `criacoes`, e coletar aqui faria o manifesto descrever um recorte de
    # anotações que o arquivo não contém.
    itens = (
        []
        if chave_perfil == "triads"
        else coletar(
            conn,
            conn_corpus,
            incluir_sinteticas=True if not pf.dado else incluir_sinteticas,
            incluir_pendentes=incluir_pendentes,
            tipos=tipos or pf.tipos,
            projeto=projeto,
        )
    )

    agora = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    from ..schema import TAXONOMY_VERSION

    contexto = {
        "created_at": agora,
        "taxonomy_version": TAXONOMY_VERSION,
        # O `statuses` do manifesto descreve O QUE o arquivo contém — para o
        # `triads` são os estados da CRIAÇÃO, não os da anotação: dizer
        # `avaliada` num arquivo de criações afirmaria uma passagem 2 que o
        # modo criar não tem (decisão do P4).
        "statuses": (
            list(STATUS_TRIADE)
            if chave_perfil == "triads"
            else list(STATUS_ENTREGUE)
            + (list(STATUS_PARCIAL) if incluir_pendentes else [])
        ),
        "synthetic_signal": f"anotacoes.{adb.COLUNA_GABARITO_AVALIACAO} IS NOT NULL",
        "composicao": germod.composicao(conn),
        "central": resumo_central(conn),
    }

    n = 0
    creditos: dict[str, str] = {}
    extra: dict[str, Any] = {}

    if pf.container == "jsonl":
        construtor = {
            "annotations": lambda: registros_annotations(itens),
            "sft": lambda: registros_sft(itens),
            "preference": lambda: registros_preference(conn, itens),
            "triads": lambda: registros_triades(conn, conn_corpus),
        }[pf.chave]

        def _escrever_jsonl(tmp: Path) -> None:
            nonlocal n
            escritor = EscritorJsonl(tmp, "utf-8", ())
            try:
                for registro in construtor():
                    escritor.escrever(registro)
                    n += 1
                    fonte, credito = registro.get("source"), registro.get("attribution")
                    if fonte is None:  # perfil `annotations`: aninhado sob "prompt"
                        bloco = registro.get("prompt") or {}
                        fonte, credito = bloco.get("source"), bloco.get("attribution")
                    if fonte and credito:
                        creditos[str(fonte)] = str(credito)
            finally:
                escritor.fechar()

        _escrever_atomico(arquivo, _escrever_jsonl)
        if pf.chave == "preference":
            extra["empates_excluidos"] = sum(
                1
                for i in itens
                if i["tipo"] == "comparar_ab"
                and (i["payload_final"] or {}).get("preferencia") == "empate"
            )
        if pf.chave == "triads":
            extra["requests_referenced"] = contexto["central"]["pedidos"]
            extra["material_schema"] = "material_criacao@1"
    else:
        if pf.chave == "audit":
            if anotacao_id is None:
                raise ValueError("o perfil 'audit' precisa de uma anotação (--anotacao)")
            alvo = next((i for i in itens if i["anotacao_id"] == anotacao_id), None)
            if alvo is None:
                raise ValueError(
                    f"anotação {anotacao_id} não está entre as entregáveis "
                    f"(estados {', '.join(contexto['statuses'])})"
                )
            texto = auditoria(alvo, contexto)
            extra["annotation_id"] = anotacao_id
        elif pf.chave == "quality-report":
            texto = relatorio_qualidade(itens, contexto)
            extra["calibration"] = calibracao_do_revisor(itens)
            extra["triage_leak"] = triagem_deixou_passar(itens)
        else:
            texto = dataset_card(itens, contexto)
        n = 1
        _escrever_atomico(arquivo, lambda tmp: tmp.write_text(texto, encoding="utf-8"))

    sem_prompt = sum(1 for i in itens if i["prompt"] is None)
    manifest: dict[str, Any] = {
        "created_at": agora,
        "file": arquivo.name,
        "sha256": sha256_arquivo(arquivo),
        "bytes": arquivo.stat().st_size,
        "profile": pf.chave,
        "container": pf.container,
        "title": pf.titulo,
        "description": pf.descricao,
        "language": IDIOMA_DOS_ARTEFATOS,
        "row_count": n,
        "source_annotations": len(itens),
        "statuses": contexto["statuses"],
        "include_synthetic": bool(pf.dado and incluir_sinteticas),
        "include_pending_evaluation": incluir_pendentes,
        "synthetic_signal": contexto["synthetic_signal"],
        "synthetic_in_scope": sum(1 for i in itens if i["sintetica"]),
        "prompt_sumido": sem_prompt,
        "task_types": sorted({i["tipo"] for i in itens}),
        "project": projeto,
        "attributions": dict(sorted(creditos.items())),
        "taxonomy_version": TAXONOMY_VERSION,
        "app_version": __version__,
        "annotation_schema_version": adb.SCHEMA_VERSION_ANOTACAO,
        "composition": contexto["composicao"],
        **extra,
    }
    if pf.dado and incluir_sinteticas:
        manifest["WARNING"] = (
            "contains machine-generated (synthetic) annotations, included on "
            "request — every row carries synthetic=true. Do NOT deliver this file "
            "as human annotation work."
        )
    if incluir_pendentes:
        manifest["NOTE_PENDING"] = (
            "includes annotations in 'pendente_avaliacao': they passed triage but "
            "have NOT been through the second review pass."
        )
    if sem_prompt:
        manifest["NOTE_MISSING_PROMPTS"] = (
            f"{sem_prompt} annotation(s) point at a prompt uid that is no longer in "
            "the corpus (the corpus is rebuilt and swapped by `pf load-db`) and "
            "were dropped from this artifact."
        )
    if pf.chave == "triads":
        manifest["NOTE_MATERIAL"] = (
            "the excerpt from the source teaching material never leaves the "
            "machine: rows reference the request (theme, teaching goal, role) and "
            "carry the anti-copy measurement — never the excerpt itself."
        )

    manifesto = destino_dir / f"{base}.manifest.json"
    _escrever_atomico(
        manifesto,
        lambda tmp: tmp.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        ),
    )
    return Resultado(arquivo, manifesto, n, manifest)


__all__ = [
    "GLOSSARIO",
    "PERFIS",
    "STATUS_ENTREGUE",
    "STATUS_PARCIAL",
    "STATUS_TRIADE",
    "Perfil",
    "Resultado",
    "auditoria",
    "calibracao_do_revisor",
    "coletar",
    "dataset_card",
    "executar",
    "panorama",
    "perfil",
    "registros_annotations",
    "registros_preference",
    "registros_sft",
    "registros_triades",
    "relatorio_qualidade",
    "resumo_central",
    "triagem_deixou_passar",
]
