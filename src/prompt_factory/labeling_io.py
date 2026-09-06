"""Livro-caixa da campanha de rotulagem: manifest, lotes, validação e estado.

Este módulo é o que torna a campanha **retomável**. Nada do que o maestro guarda
na cabeça (ou no contexto da sessão) sobrevive a um Ctrl+C, a uma cota que
acabou ou a uma janela fechada; o que sobrevive é ``labeling/manifest.json``.
Por isso toda transição de estado passa por aqui e toda escrita é atômica
(``.tmp`` no MESMO diretório + ``os.replace``): um crash no meio deixa o
manifest velho intacto, nunca um meio-manifest.

Máquina de estados de um lote::

    pending --claim--> claimed --concluir--> done
       ^                  |                   |
       |                  +--falhar---------> failed
       |                  |
       +--- órfão (TTL) --+           done --agreement < gate--> pending

Transições fora dessa tabela são recusadas com ``ValueError``. É isso que
garante a invariante que mais importa quando há 4 agentes em paralelo: **um
``batch_id`` nunca está em ``claimed`` duas vezes**, porque só se sai de
``pending``, e sair de ``pending`` é uma escrita atômica do manifest inteiro.

O lote de calibração (``batch_0000``) tem estados próprios — ``gold_pending``
enquanto espera revisão humana e ``gold`` depois de importado. Ele não é
rotulado por agente para valer: os 100 itens dele são o gabarito, e 3 deles
viajam escondidos dentro de cada lote comum para medir o agreement.

Import barato de propósito (só stdlib no topo): ``numpy`` entra dentro de
``montar_lotes``, que é a única função que sorteia. ``pf labels status`` não
paga por pyarrow nem por torch.
"""

from __future__ import annotations

import json
import os
import random
import re
from collections.abc import Iterable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from . import paths
from .config import get
from .schema import DOMAINS, QUALITY_VALUES, TASK_TYPES, TAXONOMY_VERSION

#: Marcador que denuncia texto cortado — o agente precisa saber que o item
#: continua além do que ele está vendo, senão julga "truncado" como quality=1.
SUFIXO_TRUNCADO = " …[TRUNCADO]"

#: Id do lote de calibração. Fixo: metade do protocolo referencia esse nome.
BATCH_CALIBRACAO = "batch_0000"

#: Estados de um lote comum.
PENDING, CLAIMED, DONE, FAILED = "pending", "claimed", "done", "failed"
#: Estados do lote de calibração.
GOLD_PENDING, GOLD = "gold_pending", "gold"

#: Transições permitidas: (origem, destino). Ver a máquina de estados acima.
TRANSICOES: frozenset[tuple[str, str]] = frozenset(
    {
        (PENDING, CLAIMED),
        (CLAIMED, DONE),
        (CLAIMED, PENDING),   # requeue explícito ou varredura de órfão
        (CLAIMED, FAILED),
        (DONE, PENDING),      # agreement abaixo do portão
        (FAILED, PENDING),    # revival manual
    }
)

#: Campos obrigatórios de uma linha de rótulo.
CAMPOS_ROTULO: tuple[str, ...] = ("uid", "task_type", "domain", "quality", "nsfw")
#: Único campo extra tolerado: o agente sinalizando dúvida honesta.
CAMPO_FLAG = "flag"
VALOR_FLAG = "unsure"

_CERCA = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*$")


# ---------------------------------------------------------------------------
# caminhos e escrita atômica
# ---------------------------------------------------------------------------


class LabelingPaths:
    """Onde mora cada artefato da campanha.

    Existe para os testes (e para o smoke) rodarem a campanha inteira dentro de
    um ``tmp_path`` sem encostar em ``labeling/``. Em produção é
    ``LabelingPaths()``, que aponta para a árvore real.
    """

    __slots__ = ("batches", "labeling", "labels", "manifest", "seed")

    def __init__(self, labeling: Path | str | None = None) -> None:
        self.labeling = Path(labeling) if labeling is not None else paths.LABELING
        self.batches = self.labeling / "batches"
        self.labels = self.labeling / "labels"
        self.seed = self.labeling / "seed"
        self.manifest = self.labeling / "manifest.json"

    def preparar(self) -> None:
        for d in (self.batches, self.labels, self.seed):
            d.mkdir(parents=True, exist_ok=True)

    def lote(self, batch_id: str) -> Path:
        return self.batches / f"{batch_id}.json"

    def rotulos(self, batch_id: str) -> Path:
        return self.labels / f"{batch_id}.jsonl"

    def __repr__(self) -> str:  # pragma: no cover - conveniência de depuração
        return f"LabelingPaths({self.labeling})"


def agora() -> str:
    """Instante atual em ISO-8601 UTC com sufixo ``Z``.

    Função separada (e não ``datetime.now()`` espalhado) porque os testes
    precisam congelar o relógio para provar que o manifest sai byte a byte
    igual: basta monkeypatchar ``labeling_io.agora``.
    """
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _ler_iso(valor: str | None) -> datetime | None:
    if not valor:
        return None
    try:
        return datetime.fromisoformat(str(valor).replace("Z", "+00:00"))
    except ValueError:
        return None


def escrever_texto_atomico(destino: Path, texto: str) -> Path:
    """Grava texto UTF-8 sem passar por estado intermediário visível.

    O ``.tmp`` fica no MESMO diretório de propósito: ``os.replace`` só é atômico
    dentro do mesmo volume, e ``labeling/`` e o ``%TEMP%`` desta máquina estão em
    discos diferentes. ``fsync`` antes do replace porque o que interessa aqui é
    sobreviver a queda de energia no meio de uma campanha de horas.
    """
    destino.parent.mkdir(parents=True, exist_ok=True)
    tmp = destino.parent / f"{destino.name}.tmp"
    with tmp.open("w", encoding="utf-8", newline="\n") as fh:
        fh.write(texto)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, destino)
    return destino


def escrever_json_atomico(destino: Path, dados: Any) -> Path:
    """``escrever_texto_atomico`` com ``json.dumps`` legível (acento é acento)."""
    return escrever_texto_atomico(
        destino, json.dumps(dados, ensure_ascii=False, indent=2) + "\n"
    )


def escrever_jsonl_atomico(destino: Path, linhas: Iterable[Mapping[str, Any]]) -> Path:
    """Uma linha JSON por registro, ordem preservada."""
    corpo = "".join(
        json.dumps(linha, ensure_ascii=False, separators=(",", ":")) + "\n"
        for linha in linhas
    )
    return escrever_texto_atomico(destino, corpo)


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------


def carregar_manifest(lp: LabelingPaths | None = None) -> dict[str, Any]:
    """Lê o manifest e confere a versão da taxonomia.

    Taxonomia divergente é parada obrigatória, não aviso: rótulos de v1.0 e de
    v1.1 misturados no mesmo parquet são indistinguíveis depois, e ninguém
    descobre o estrago até o classificador sair torto.
    """
    lp = lp or LabelingPaths()
    if not lp.manifest.is_file():
        raise SystemExit(
            f"[pf] manifest ausente: {lp.manifest} — rode `pf make-seed` antes"
        )
    with lp.manifest.open(encoding="utf-8") as fh:
        dados: dict[str, Any] = json.load(fh)
    versao = dados.get("taxonomy_version")
    if versao != TAXONOMY_VERSION:
        raise SystemExit(
            f"[pf] manifest gerado na taxonomia {versao!r}, código está em "
            f"{TAXONOMY_VERSION!r} — a taxonomia mudou no meio da campanha e "
            "isso é decisão humana (refazer a semente descarta os rótulos já feitos)"
        )
    return dados


def salvar_manifest(dados: Mapping[str, Any], lp: LabelingPaths | None = None) -> Path:
    lp = lp or LabelingPaths()
    return escrever_json_atomico(lp.manifest, dados)


def _lote(manifest: Mapping[str, Any], batch_id: str) -> dict[str, Any]:
    lotes = manifest.get("batches", {})
    if batch_id not in lotes:
        conhecidos = len(lotes)
        raise ValueError(f"lote desconhecido: {batch_id!r} ({conhecidos} lotes no manifest)")
    return dict(lotes[batch_id])


def _transicionar(registro: dict[str, Any], destino: str, batch_id: str) -> None:
    origem = str(registro.get("status", PENDING))
    if origem == destino:
        raise ValueError(f"{batch_id}: já está em {destino!r}")
    if (origem, destino) not in TRANSICOES:
        raise ValueError(
            f"{batch_id}: transição {origem!r} -> {destino!r} não é permitida "
            f"(permitidas de {origem!r}: "
            f"{', '.join(sorted(d for o, d in TRANSICOES if o == origem)) or 'nenhuma'})"
        )
    registro["status"] = destino


# ---------------------------------------------------------------------------
# montagem dos lotes
# ---------------------------------------------------------------------------


def truncar(texto: str, limite: int) -> str:
    """Corta em ``limite`` caracteres e avisa que cortou.

    Sem o marcador o agente lê um prompt que acaba no meio da frase e conclui
    que o *prompt* é ruim, quando quem cortou fomos nós.
    """
    if limite <= 0 or len(texto) <= limite:
        return texto
    return texto[:limite] + SUFIXO_TRUNCADO


def _item(linha: Mapping[str, Any], truncate_chars: int) -> dict[str, Any]:
    """Um item como o agente o vê: uid, idioma e texto. Nada mais.

    Fonte, tamanho e categoria nativa ficam FORA de propósito — saber que a
    linha veio do ``no_robots`` com categoria ``Coding`` entregaria o rótulo e
    contaminaria a medição.
    """
    return {
        "uid": str(linha["uid"]),
        "lang": str(linha.get("lang") or ""),
        "text": truncar(str(linha.get("text") or ""), truncate_chars),
    }


def _linhas(seed_df: Any) -> list[dict[str, Any]]:
    """Aceita tabela pyarrow ou sequência de mappings, devolve lista de dicts."""
    if hasattr(seed_df, "to_pylist"):
        return list(seed_df.to_pylist())
    return [dict(linha) for linha in seed_df]


def montar_lotes(
    seed_df: Any,
    tamanho: int = 80,
    n_calibracao: int = 3,
    *,
    calibracao_uids: Sequence[str] | None = None,
    tamanho_calibracao: int | None = None,
    truncate_chars: int | None = None,
    rng: Any = None,
    rng_seed: int | None = None,
    lp: LabelingPaths | None = None,
    extra_manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Fatia a semente em lotes e escreve ``batches/*.json`` + o manifest.

    Um lote comum tem ``tamanho`` itens: ``tamanho - n_calibracao`` novos mais
    ``n_calibracao`` itens de calibração **embaralhados no meio e sem nenhuma
    marca** no arquivo entregue ao agente. Só o manifest sabe quais são
    (``gold_uids``). Se o agente pudesse distingui-los, mediríamos o cuidado
    dele com 3 itens marcados, não a qualidade do lote — que é o oposto do que
    o portão de agreement serve para medir.

    Os itens de calibração são reamostrados **com reposição entre lotes**: são
    100 no total e ~155 lotes, então cada um reaparece umas 4,7 vezes. Isso é de
    propósito — o custo de rotular de novo um item já conhecido é o preço da
    medição, e ele não polui o merge porque o s08 descarta rótulo de agente
    sobre uid de calibração.

    Devolve o manifest recém-escrito.
    """
    import numpy as np

    lp = lp or LabelingPaths()
    lp.preparar()
    if truncate_chars is None:
        # 1900 = a janela do CLASSIFICADOR (512 tokens do e5 ≈ 1.879 chars em pt,
        # ~2.117 em en), não um número de gosto — o porquê inteiro está em
        # `[labeling] truncate_chars` do settings.toml. Este default só entra em
        # jogo se a chave sumir de lá, e um 1.500 aqui faria o rotulador ver
        # MENOS texto que o classificador, em silêncio.
        truncate_chars = int(get("labeling", "truncate_chars", default=1900))
    if tamanho_calibracao is None:
        tamanho_calibracao = int(get("labeling", "calibration_size", default=100))
    if rng is None:
        semente = rng_seed if rng_seed is not None else int(get("seed", "rng_seed", default=42))
        rng = np.random.default_rng(semente)
    if n_calibracao >= tamanho:
        raise ValueError(f"n_calibracao={n_calibracao} não cabe num lote de {tamanho}")

    linhas = _linhas(seed_df)
    por_uid = {str(linha["uid"]): linha for linha in linhas}
    if len(por_uid) != len(linhas):
        raise ValueError(f"semente com uid repetido: {len(linhas)} linhas, {len(por_uid)} uids")

    # --- calibração ---------------------------------------------------------
    if calibracao_uids is None:
        universo = sorted(por_uid)
        n = min(tamanho_calibracao, len(universo))
        calibracao_uids = [str(u) for u in rng.choice(universo, size=n, replace=False)]
    else:
        calibracao_uids = [str(u) for u in calibracao_uids]
        faltando = [u for u in calibracao_uids if u not in por_uid]
        if faltando:
            raise ValueError(f"calibração cita uid fora da semente: {faltando[:3]}")
    cal_set = set(calibracao_uids)

    escrever_json_atomico(
        lp.lote(BATCH_CALIBRACAO),
        {
            "batch_id": BATCH_CALIBRACAO,
            "taxonomy_version": TAXONOMY_VERSION,
            "n_items": len(calibracao_uids),
            "items": [_item(por_uid[u], truncate_chars) for u in calibracao_uids],
        },
    )

    # --- lotes comuns -------------------------------------------------------
    # Embaralhar antes de fatiar mistura idiomas e fontes dentro de cada lote:
    # um lote inteiro da mesma fonte ancoraria o agente numa classe só.
    restantes = [u for u in por_uid if u not in cal_set]
    ordem = rng.permutation(len(restantes))
    restantes = [restantes[int(i)] for i in ordem]

    novos_por_lote = tamanho - n_calibracao
    blocos = [
        restantes[i : i + novos_por_lote] for i in range(0, len(restantes), novos_por_lote)
    ]

    lotes: dict[str, Any] = {}
    for indice, bloco in enumerate(blocos, start=1):
        batch_id = f"batch_{indice:04d}"
        k = min(n_calibracao, len(calibracao_uids))
        ouros = [str(u) for u in rng.choice(sorted(cal_set), size=k, replace=False)]
        itens = list(bloco)
        # Posições sorteadas uma a uma: inserir os 3 de uma vez concentraria os
        # ouros no fim (cada insert desloca as posições seguintes).
        for uid_ouro in ouros:
            pos = int(rng.integers(0, len(itens) + 1))
            itens.insert(pos, uid_ouro)
        escrever_json_atomico(
            lp.lote(batch_id),
            {
                "batch_id": batch_id,
                "taxonomy_version": TAXONOMY_VERSION,
                "n_items": len(itens),
                "items": [_item(por_uid[u], truncate_chars) for u in itens],
            },
        )
        lotes[batch_id] = {
            "status": PENDING,
            "n_items": len(itens),
            "n_novos": len(bloco),
            "gold_uids": ouros,
            "claimed_at": None,
            "done_at": None,
            "agreement": None,
            "tentativas": 0,
            "agent_model": None,
            "motivo": None,
        }

    manifest: dict[str, Any] = {
        "taxonomy_version": TAXONOMY_VERSION,
        "created_at": agora(),
        "truncate_chars": truncate_chars,
        "batch_size": tamanho,
        "gold_per_batch": n_calibracao,
        "n_seed": len(linhas),
        **dict(extra_manifest or {}),
        "calibration": {
            "batch_id": BATCH_CALIBRACAO,
            "status": GOLD_PENDING,
            "n_items": len(calibracao_uids),
            "uids": list(calibracao_uids),
            "revised_at": None,
        },
        "gold": None,
        "batches": lotes,
    }
    # O manifest é o ÚLTIMO a ser escrito: enquanto ele não existe, os lotes no
    # disco são lixo inerte; existindo, todos os lotes que ele cita já estão lá.
    salvar_manifest(manifest, lp)
    return manifest


def carregar_lote(batch_id: str, lp: LabelingPaths | None = None) -> dict[str, Any]:
    """Lê ``batches/<batch_id>.json`` (o arquivo que vai para o agente)."""
    lp = lp or LabelingPaths()
    arquivo = lp.lote(batch_id)
    if not arquivo.is_file():
        raise SystemExit(f"[pf] lote ausente: {arquivo} — rode `pf make-seed` antes")
    with arquivo.open(encoding="utf-8") as fh:
        dados: dict[str, Any] = json.load(fh)
    return dados


def uids_do_lote(batch_id: str, lp: LabelingPaths | None = None) -> list[str]:
    """Uids na ORDEM do arquivo — é essa ordem que a resposta deve espelhar."""
    return [str(item["uid"]) for item in carregar_lote(batch_id, lp).get("items", [])]


# ---------------------------------------------------------------------------
# validação
# ---------------------------------------------------------------------------


def parse_jsonl(texto: str) -> tuple[list[tuple[int, Any]], list[str]]:
    """JSONL tolerante: devolve ``([(n_linha, objeto)], avisos)``.

    Tolerante na FORMA, estrito no CONTEÚDO (isso é com ``validar_resposta``).
    Um modelo pequeno embrulha a resposta em cerca de código, começa com "Aqui
    está o JSONL:" ou repete o input; nada disso é motivo para jogar 80 rótulos
    bons fora. BOM e CRLF entram na mesma conta: são o que o Windows produz
    quando alguém salva a resposta com a ferramenta errada.

    Linha que é objeto ``{"lang":..., "text":...}`` sem nenhum eixo é ECO do
    input — descartada com aviso, senão viraria "uid repetido" mais adiante.
    """
    avisos: list[str] = []
    objetos: list[tuple[int, Any]] = []
    for n, bruta in enumerate(texto.lstrip("﻿").splitlines(), start=1):
        linha = bruta.strip().lstrip("﻿").strip()
        if not linha:
            continue
        if _CERCA.match(bruta):
            continue
        if not linha.startswith("{"):
            avisos.append(f"linha {n}: descartada, não é JSON ({linha[:60]!r})")
            continue
        try:
            obj = json.loads(linha)
        except json.JSONDecodeError as exc:
            avisos.append(f"linha {n}: JSON inválido ({exc.msg})")
            continue
        if not isinstance(obj, dict):
            avisos.append(f"linha {n}: descartada, JSON não é objeto")
            continue
        if "text" in obj and not (set(obj) & set(CAMPOS_ROTULO[1:])):
            avisos.append(f"linha {n}: descartada, é eco do input (sem eixos)")
            continue
        objetos.append((n, obj))
    return objetos, avisos


def _erros_da_linha(n: int, obj: Mapping[str, Any]) -> list[str]:
    """Erros de conteúdo de UMA linha já parseada, com o número da linha."""
    erros: list[str] = []
    uid = obj.get("uid")
    rotulo = f"linha {n}" + (f" (uid {uid})" if isinstance(uid, str) else "")

    tt = obj.get("task_type")
    if tt not in TASK_TYPES:
        erros.append(f"{rotulo}: task_type {tt!r} fora da taxonomia")
    dom = obj.get("domain")
    if dom not in DOMAINS:
        erros.append(f"{rotulo}: domain {dom!r} fora da taxonomia")

    q = obj.get("quality")
    # bool é subclasse de int em Python: sem este teste, True passaria como 1 e
    # um agente que respondeu `"quality": true` entraria em silêncio.
    if isinstance(q, bool) or not isinstance(q, int) or q not in QUALITY_VALUES:
        erros.append(
            f"{rotulo}: quality {q!r} inválido (inteiro em {list(QUALITY_VALUES)})"
        )
    if not isinstance(obj.get("nsfw"), bool):
        erros.append(f"{rotulo}: nsfw {obj.get('nsfw')!r} não é booleano JSON")

    for campo in obj:
        if campo in CAMPOS_ROTULO:
            continue
        if campo == CAMPO_FLAG and obj[campo] == VALOR_FLAG:
            continue
        erros.append(f"{rotulo}: campo desconhecido {campo!r}")
    return erros


def validar_resposta(
    batch_id: str,
    texto_jsonl: str,
    lp: LabelingPaths | None = None,
) -> tuple[bool, list[dict[str, Any]], list[str]]:
    """Valida a resposta de um agente contra o lote. É o portão da campanha.

    Devolve ``(ok, linhas_validas, erros)``. ``linhas_validas`` sai na ORDEM DO
    LOTE (não na ordem em que o agente respondeu), porque é assim que o arquivo
    de rótulos fica comparável entre lotes e entre re-runs.

    O que é conferido, em ordem: JSONL bem-formado linha a linha (com o número
    da linha no erro), cobertura EXATA dos uids do lote — nenhum a mais, nenhum
    a menos, nenhum repetido —, ``task_type``/``domain`` nos enums da taxonomia,
    ``quality`` inteiro em 1..3 e ``nsfw`` booleano de verdade.

    Ordem trocada é aviso, não erro: reordenar é barato e o conteúdo é o que
    importa. Uid inventado, sim, é erro — quase sempre significa que o agente
    alucinou um item, e aceitar isso contaminaria o treino com um uid que não
    existe no universo.
    """
    esperados = uids_do_lote(batch_id, lp)
    objetos, avisos = parse_jsonl(texto_jsonl)
    erros: list[str] = []

    validas: dict[str, dict[str, Any]] = {}
    vistos: set[str] = set()
    ordem_resposta: list[str] = []
    conjunto_esperado = set(esperados)

    for n, obj in objetos:
        uid = obj.get("uid")
        if not isinstance(uid, str) or not uid:
            erros.append(f"linha {n}: sem 'uid' (ou uid não é string)")
            continue
        if uid not in conjunto_esperado:
            erros.append(f"linha {n}: uid {uid!r} não pertence ao lote {batch_id}")
            continue
        if uid in vistos:
            erros.append(f"linha {n}: uid {uid!r} repetido")
            continue
        vistos.add(uid)
        ordem_resposta.append(uid)
        problemas = _erros_da_linha(n, obj)
        if problemas:
            erros.extend(problemas)
            continue
        registro = {
            "uid": uid,
            "task_type": str(obj["task_type"]),
            "domain": str(obj["domain"]),
            "quality": int(obj["quality"]),
            "nsfw": bool(obj["nsfw"]),
        }
        if obj.get(CAMPO_FLAG) == VALOR_FLAG:
            registro[CAMPO_FLAG] = VALOR_FLAG
        validas[uid] = registro

    faltando = [u for u in esperados if u not in vistos]
    if faltando:
        mostra = ", ".join(faltando[:5]) + (" ..." if len(faltando) > 5 else "")
        erros.append(f"faltam {len(faltando)} uid(s) do lote {batch_id}: {mostra}")

    if not erros and ordem_resposta != esperados:
        avisos.append("ordem diferente da do lote (reordenado; sem impacto no rótulo)")

    ok = not erros and len(validas) == len(esperados)
    return ok, [validas[u] for u in esperados if u in validas], erros + avisos


def uids_para_retry(
    batch_id: str,
    linhas_validas: Sequence[Mapping[str, Any]],
    lp: LabelingPaths | None = None,
) -> list[str]:
    """Uids que ainda faltam depois de uma validação — o alvo do retry dirigido.

    Repetir o lote inteiro por causa de 2 linhas erradas gasta 80 itens de
    contexto para consertar 2; o retry vai só nesses uids.
    """
    prontos = {str(linha["uid"]) for linha in linhas_validas}
    return [u for u in uids_do_lote(batch_id, lp) if u not in prontos]


# ---------------------------------------------------------------------------
# agreement
# ---------------------------------------------------------------------------


def agreement_minimo() -> float:
    """Portão de agreement por lote (``[labeling] agreement_min``)."""
    return float(get("labeling", "agreement_min", default=0.80))


def calcular_agreement(
    batch_id: str,
    respostas: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any] | None = None,
    lp: LabelingPaths | None = None,
) -> float | None:
    """Concordância com o ouro nos itens de calibração escondidos no lote.

    Compara ``task_type`` e ``domain`` dos ``gold_per_batch`` itens (3 por
    padrão) contra o gabarito revisado à mão: são 6 comparações, média simples,
    granularidade 1/6. ``quality`` e ``nsfw`` ficam de fora de propósito — são
    eixos com fronteira legitimamente borrada entre anotadores, e puni-los aqui
    reprovaria lotes bons.

    Devolve ``None`` (não 0.0) quando não há ouro importado ainda ou quando o
    lote não tem item de calibração: "não medido" e "medido e ruim" são coisas
    diferentes, e confundi-las reprovaria a campanha inteira antes da calibração.
    """
    manifest = manifest if manifest is not None else carregar_manifest(lp)
    ouro = manifest.get("gold")
    if not ouro:
        return None
    registro = manifest.get("batches", {}).get(batch_id) or {}
    gold_uids = [u for u in registro.get("gold_uids", []) if u in ouro]
    if not gold_uids:
        return None

    por_uid = {str(linha["uid"]): linha for linha in respostas}
    acertos = comparacoes = 0
    for uid in gold_uids:
        resposta = por_uid.get(uid)
        for eixo in ("task_type", "domain"):
            comparacoes += 1
            if resposta is not None and resposta.get(eixo) == ouro[uid].get(eixo):
                acertos += 1
    if not comparacoes:
        return None
    return acertos / comparacoes


# ---------------------------------------------------------------------------
# transições de estado
# ---------------------------------------------------------------------------


def varrer_orfaos(
    manifest: dict[str, Any],
    ttl_horas: float | None = None,
    max_tentativas: int | None = None,
) -> tuple[list[str], list[str]]:
    """Devolve ao pool os lotes ``claimed`` que passaram do TTL.

    Uma sessão que morreu (cota, Ctrl+C, máquina reiniciada) deixa lotes
    reivindicados e nunca submetidos. Sem esta varredura eles ficariam presos
    para sempre e a campanha travaria com "nada pendente" e 11 mil itens sem
    rótulo. Muta o manifest em memória; quem salva é o chamador (um save só).

    Devolve ``(devolvidos, quarentenados)``.
    """
    if ttl_horas is None:
        ttl_horas = float(get("labeling", "claim_ttl_hours", default=2))
    if max_tentativas is None:
        max_tentativas = int(get("labeling", "max_attempts", default=3))
    limite = datetime.now(UTC) - timedelta(hours=ttl_horas)

    devolvidos: list[str] = []
    quarentenados: list[str] = []
    for batch_id, registro in sorted(manifest.get("batches", {}).items()):
        if registro.get("status") != CLAIMED:
            continue
        quando = _ler_iso(registro.get("claimed_at"))
        if quando is not None and quando > limite:
            continue
        registro["tentativas"] = int(registro.get("tentativas", 0)) + 1
        if registro["tentativas"] >= max_tentativas:
            _transicionar(registro, FAILED, batch_id)
            registro["motivo"] = f"claim órfão após {registro['tentativas']} tentativas"
            quarentenados.append(batch_id)
        else:
            _transicionar(registro, PENDING, batch_id)
            registro["claimed_at"] = None
            devolvidos.append(batch_id)
    return devolvidos, quarentenados


def claim(
    batch_id: str | None = None,
    n: int = 1,
    lp: LabelingPaths | None = None,
    manifest: dict[str, Any] | None = None,
    salvar: bool = True,
) -> list[str]:
    """Reivindica lotes: ``pending`` → ``claimed``, com um save atômico só.

    Sem ``batch_id``, pega os ``n`` primeiros pendentes em ordem de id. A
    varredura de órfãos roda ANTES, senão uma sessão que caiu deixaria trabalho
    represado enquanto o comando responde "nada pendente".

    A invariante "nunca dois claims do mesmo lote" cai de graça da máquina de
    estados: só se sai de ``pending``, e a saída é uma escrita atômica do
    manifest inteiro.
    """
    lp = lp or LabelingPaths()
    proprio = manifest is None
    manifest = manifest if manifest is not None else carregar_manifest(lp)
    varrer_orfaos(manifest)

    lotes = manifest.get("batches", {})
    if batch_id is not None:
        alvos = [batch_id]
        if batch_id not in lotes:
            raise ValueError(f"lote desconhecido: {batch_id!r}")
    else:
        alvos = [b for b in sorted(lotes) if lotes[b].get("status") == PENDING][: max(n, 0)]

    marcados: list[str] = []
    for alvo in alvos:
        registro = lotes[alvo]
        _transicionar(registro, CLAIMED, alvo)
        registro["claimed_at"] = agora()
        marcados.append(alvo)
    if salvar and proprio:
        salvar_manifest(manifest, lp)
    return marcados


def concluir(
    batch_id: str,
    linhas: Sequence[Mapping[str, Any]],
    agent_model: str | None = None,
    lp: LabelingPaths | None = None,
    manifest: dict[str, Any] | None = None,
) -> float | None:
    """``claimed`` → ``done``: grava ``labels/<batch_id>.jsonl`` e o agreement.

    Ordem obrigatória: os rótulos primeiro, o manifest depois. Invertida, um
    crash entre as duas escritas deixaria um lote marcado ``done`` sem arquivo
    de rótulo nenhum — perda silenciosa, o pior tipo. Do jeito certo o pior caso
    é um arquivo de rótulos órfão, que o próximo submit reescreve.

    Devolve o agreement calculado (ou ``None`` se ainda não há ouro). Aplicar o
    portão é decisão de quem chamou — ver ``agreement_minimo``.
    """
    lp = lp or LabelingPaths()
    proprio = manifest is None
    manifest = manifest if manifest is not None else carregar_manifest(lp)
    registro = manifest.get("batches", {}).get(batch_id)
    if registro is None:
        raise ValueError(f"lote desconhecido: {batch_id!r}")

    _transicionar(registro, DONE, batch_id)
    valor = calcular_agreement(batch_id, linhas, manifest=manifest, lp=lp)
    escrever_jsonl_atomico(lp.rotulos(batch_id), linhas)

    registro["done_at"] = agora()
    registro["agreement"] = valor
    registro["agent_model"] = agent_model
    registro["n_rotulos"] = len(linhas)
    registro["motivo"] = None
    if proprio:
        salvar_manifest(manifest, lp)
    return valor


def falhar(
    batch_id: str,
    motivo: str,
    lp: LabelingPaths | None = None,
    manifest: dict[str, Any] | None = None,
) -> None:
    """``claimed`` → ``failed``: quarentena explícita, com o porquê registrado."""
    lp = lp or LabelingPaths()
    proprio = manifest is None
    manifest = manifest if manifest is not None else carregar_manifest(lp)
    registro = manifest.get("batches", {}).get(batch_id)
    if registro is None:
        raise ValueError(f"lote desconhecido: {batch_id!r}")
    _transicionar(registro, FAILED, batch_id)
    registro["tentativas"] = int(registro.get("tentativas", 0)) + 1
    registro["motivo"] = motivo
    registro["claimed_at"] = None
    if proprio:
        salvar_manifest(manifest, lp)


def reenfileirar(
    batch_id: str,
    motivo: str = "",
    lp: LabelingPaths | None = None,
    manifest: dict[str, Any] | None = None,
) -> None:
    """Devolve um lote para ``pending`` (de ``claimed``, ``done`` ou ``failed``).

    É por aqui que o portão de agreement age: lote com concordância abaixo do
    mínimo volta a ser trabalho pendente, e o arquivo de rótulos dele fica no
    disco até ser sobrescrito (o s08 só lê lote ``done``).
    """
    lp = lp or LabelingPaths()
    proprio = manifest is None
    manifest = manifest if manifest is not None else carregar_manifest(lp)
    registro = manifest.get("batches", {}).get(batch_id)
    if registro is None:
        raise ValueError(f"lote desconhecido: {batch_id!r}")
    _transicionar(registro, PENDING, batch_id)
    registro["tentativas"] = int(registro.get("tentativas", 0)) + 1
    registro["claimed_at"] = None
    registro["motivo"] = motivo or None
    if proprio:
        salvar_manifest(manifest, lp)


def importar_ouro(
    texto_jsonl: str,
    lp: LabelingPaths | None = None,
) -> tuple[bool, dict[str, Any], list[str]]:
    """Importa o gabarito revisado à mão e recalcula o agreement dos lotes feitos.

    Exige cobertura exata dos 100 itens da calibração e os mesmos enums de
    qualquer rótulo. Depois de gravar, faz *backfill*: lote já ``done`` antes do
    ouro existir tinha ``agreement = None``, e agora passa a ter número — sem
    isso, quem calibrou depois de rotular ficaria sem medição nenhuma.
    """
    lp = lp or LabelingPaths()
    manifest = carregar_manifest(lp)
    ok, linhas, erros = validar_resposta(BATCH_CALIBRACAO, texto_jsonl, lp)
    if not ok:
        return False, manifest, erros

    manifest["gold"] = {
        linha["uid"]: {
            "task_type": linha["task_type"],
            "domain": linha["domain"],
            "quality": linha["quality"],
            "nsfw": linha["nsfw"],
        }
        for linha in linhas
    }
    manifest["calibration"]["status"] = GOLD
    manifest["calibration"]["revised_at"] = agora()
    escrever_jsonl_atomico(lp.rotulos(BATCH_CALIBRACAO), linhas)

    for batch_id, registro in manifest.get("batches", {}).items():
        if registro.get("status") != DONE:
            continue
        arquivo = lp.rotulos(batch_id)
        if not arquivo.is_file():
            continue
        feitas = [
            json.loads(linha)
            for linha in arquivo.read_text(encoding="utf-8").splitlines()
            if linha.strip()
        ]
        registro["agreement"] = calcular_agreement(
            batch_id, feitas, manifest=manifest, lp=lp
        )
    salvar_manifest(manifest, lp)
    return True, manifest, erros


# ---------------------------------------------------------------------------
# painel
# ---------------------------------------------------------------------------


def motivo_sem_agreement(
    manifest: Mapping[str, Any], batch_id: str | None = None, *, n_done: int = 0
) -> str:
    """POR QUE não há agreement — a causa, nunca um genérico.

    ``None`` de agreement tem três causas independentes, e o painel dizia
    "sem ouro" para todas. É a mesma classe de defeito que o projeto persegue
    em outros lugares: uma mensagem que nomeia a causa errada custa mais que
    mensagem nenhuma. O caso concreto que a expôs: no dia em que o ouro foi
    importado, com 0 lotes concluídos, o painel continuou dizendo "sem ouro" —
    e quem lesse aquilo concluiria que a importação de 100 itens revisados à
    mão tinha falhado.

    ``batch_id`` responde sobre UM lote (depois do submit); sem ele, a resposta
    é sobre a média da campanha.
    """
    calibracao = manifest.get("calibration", {})
    if str(calibracao.get("status", GOLD_PENDING)) != GOLD:
        return "sem ouro importado"
    if batch_id is not None:
        return "este lote não tem item de calibração"
    if not n_done:
        return "nenhum lote concluído ainda"
    return "nenhum lote concluído tinha item de calibração"


def texto_agreement(painel: Mapping[str, Any], casas: int = 3) -> str:
    """O agreement médio formatado, ou "não medido" **com o motivo**.

    Ponto único: o ``pf labels status`` e o s08 imprimem a mesma frase, e duas
    cópias divergiriam justamente na parte que explica.
    """
    medio = painel.get("agreement_medio")
    if medio is not None:
        return f"{float(medio):.{casas}f}"
    return f"não medido ({painel.get('agreement_motivo') or 'sem ouro importado'})"


def painel(lp: LabelingPaths | None = None, manifest: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Números da campanha, para o ``pf labels status`` e para o s08."""
    lp = lp or LabelingPaths()
    manifest = manifest if manifest is not None else carregar_manifest(lp)
    lotes = manifest.get("batches", {})

    contagem = dict.fromkeys((PENDING, CLAIMED, DONE, FAILED), 0)
    agreements: list[float] = []
    baixos: list[tuple[str, float]] = []
    rotulos = 0
    minimo = agreement_minimo()
    for batch_id, registro in sorted(lotes.items()):
        status = str(registro.get("status", PENDING))
        contagem[status] = contagem.get(status, 0) + 1
        if status != DONE:
            # Lote devolvido pelo portão guarda o agreement que o reprovou (é
            # diagnóstico), mas ele NÃO entra na média da campanha: o trabalho
            # dele foi descartado e vai ser refeito.
            continue
        rotulos += int(registro.get("n_rotulos") or registro.get("n_items") or 0)
        valor = registro.get("agreement")
        if isinstance(valor, int | float):
            agreements.append(float(valor))
            if float(valor) < minimo:
                baixos.append((batch_id, float(valor)))

    ttl = float(get("labeling", "claim_ttl_hours", default=2))
    limite = datetime.now(UTC) - timedelta(hours=ttl)
    orfaos = [
        b
        for b, r in sorted(lotes.items())
        if r.get("status") == CLAIMED and (_ler_iso(r.get("claimed_at")) or limite) <= limite
    ]

    calibracao = manifest.get("calibration", {})
    return {
        "n_lotes": len(lotes),
        "contagem": contagem,
        "orfaos": orfaos,
        "agreement_medio": (sum(agreements) / len(agreements)) if agreements else None,
        "agreement_motivo": (
            None
            if agreements
            else motivo_sem_agreement(manifest, n_done=contagem.get(DONE, 0))
        ),
        "agreement_min": minimo,
        "lotes_baixos": baixos,
        "n_rotulos": rotulos,
        "n_seed": int(manifest.get("n_seed", 0)),
        "calibracao_status": str(calibracao.get("status", GOLD_PENDING)),
        "calibracao_n": int(calibracao.get("n_items", 0)),
        "taxonomy_version": manifest.get("taxonomy_version"),
    }


def amostra_estavel(itens: Sequence[Any], k: int, semente: int) -> list[Any]:
    """Amostra sem reposição reprodutível, só com stdlib (usada fora do s07)."""
    if k >= len(itens):
        return list(itens)
    return random.Random(semente).sample(list(itens), k)


__all__ = [
    "BATCH_CALIBRACAO",
    "CAMPOS_ROTULO",
    "CLAIMED",
    "DONE",
    "FAILED",
    "GOLD",
    "GOLD_PENDING",
    "PENDING",
    "SUFIXO_TRUNCADO",
    "TRANSICOES",
    "LabelingPaths",
    "agora",
    "agreement_minimo",
    "amostra_estavel",
    "calcular_agreement",
    "carregar_lote",
    "carregar_manifest",
    "claim",
    "concluir",
    "escrever_json_atomico",
    "escrever_jsonl_atomico",
    "escrever_texto_atomico",
    "falhar",
    "importar_ouro",
    "montar_lotes",
    "motivo_sem_agreement",
    "painel",
    "parse_jsonl",
    "reenfileirar",
    "salvar_manifest",
    "texto_agreement",
    "truncar",
    "uids_do_lote",
    "uids_para_retry",
    "validar_resposta",
    "varrer_orfaos",
]
