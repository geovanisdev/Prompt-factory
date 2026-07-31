"""Export do universo curado: JSONL/CSV + manifesto, com licença por linha.

Este é o produto final do projeto. Tudo o que a pipeline fez — ingerir,
normalizar, detectar idioma, limpar PII, deduplicar, rotular, curar — existe
para que um arquivo saia daqui e possa ser entregue a uma plataforma de
anotação **com a licença em ordem**.

Três decisões que não são detalhes de formato:

* **``attribution`` não é coluna do banco.** Ela vive em ``config/sources.toml``
  e é resolvida num dict antes do laço. Sem ela o export **não cumpre a
  licença**: ODC-BY, CC-BY e CC-BY-SA exigem atribuição a cada uso, e um JSONL
  sem crédito é um problema jurídico com cara de arquivo pronto.
* **``redistributable = 0`` fica FORA por padrão**, e o manifesto diz quantas
  linhas saíram e por quê. Incluir é permitido (uso local é legítimo), mas aí o
  manifesto carimba um ``WARNING`` explícito. ``commercial_ok = 0`` NÃO é
  excluído — é contabilizado à parte, porque a decisão de usar CC-BY-NC depende
  do destino e o usuário é quem sabe qual é.
* **``profile`` e ``container`` são separados.** ``exports.format`` tem
  ``CHECK (format IN ('jsonl','csv'))`` no DDL; um perfil futuro (Label Studio)
  entra como ``profile="label-studio"`` continuando a gravar
  ``format="jsonl"``, e nenhuma migração de schema é necessária. É para isso
  que existe o ``REGISTRY``.

Toda escrita sai daqui de dentro, com ``encoding`` explícito. Redirecionar no
PowerShell grava UTF-16/BOM e quebra acentuação e JSONL.

A LÍNGUA DOS ARTEFATOS DE ENTREGA  (convenção do P3i, aplicada a partir do P5b)
===============================================================================
Ver ``IDIOMA_DOS_ARTEFATOS``, logo abaixo. Resumo: **todo texto que a plataforma
ENTREGA sai em inglês**; o dado que ela entrega sai na língua em que foi
produzido.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NamedTuple, Protocol

from . import __version__
from .schema import TAXONOMY_VERSION

#: A língua de TODO artefato de entrega — decidida no P3i, cobrada no P5b.
#:
#: O QUE ISTO COBRE
#: ================
#: Dataset card, relatório de qualidade, export de auditoria completa de um item,
#: e todo campo de prosa que a Bancada gerar dentro deles: nome de perfil,
#: descrição de coluna, motivo de exclusão, nota de licença, cabeçalho de
#: manifesto. Sai em **inglês**, sem exceção e sem versão traduzida ao lado.
#:
#: POR QUÊ
#: =======
#: É a mesma regra que a interface aplica campo a campo, pelo mesmo motivo:
#: **quem lê um artefato de entrega é o cliente**, e o cliente deste projeto é
#: uma plataforma de data annotation estrangeira. Um relatório de qualidade em
#: português obriga o avaliador a traduzir a prova de QC antes de conseguir
#: avaliá-la — e a prova de QC é o argumento inteiro do portfólio.
#:
#: O QUE ISTO **NÃO** COBRE
#: ========================
#: * O DADO. ``text``, resposta de modelo e resposta de referência saem na língua
#:   em que foram escritos, e ``lang`` por linha diz qual é. Traduzir dado
#:   inventaria um texto que ninguém escreveu.
#: * Justificativa, comentário de revisão e critério de rubrica já são
#:   produzidos em inglês pela plataforma (é a convenção de campo do P3i), então
#:   não há nada a fazer com eles no export além de deixá-los passar.
#: * Mensagem de CLI, comentário de código e docstring continuam em **pt-BR**:
#:   são internos, e essa é a convenção do repo.
IDIOMA_DOS_ARTEFATOS = "en"

#: Chaves do perfil ``flat``, **nesta ordem**. A ordem é contrato: um diff entre
#: dois exports do mesmo recorte tem de ser vazio, e chave em ordem de dict
#: mudaria a cada versão do Python.
CAMPOS_FLAT: tuple[str, ...] = (
    "uid",
    "text",
    "lang",
    "lang_variant",
    "task_type",
    "domain",
    "quality",
    "nsfw",
    "pii_found",
    "source",
    "source_id",
    "license",
    "commercial_ok",
    "redistributable",
    "attribution",
    "label_method",
    "label_confidence",
    "needs_review",
    "edited",
    "n_chars",
    "n_words",
    "n_exact_dups",
    "n_near_dups",
    "country",
    "model_family",
    "created_ts",
    "native_category",
    "taxonomy_version",
)

#: Só com ``include_text_original``. Fica fora por padrão porque dobra o
#: tamanho do arquivo para dizer "nada mudou" em 99,9% das linhas.
CAMPO_ORIGINAL = "text_original"

_NOME_SEGURO = re.compile(r"[^A-Za-z0-9._-]+")

#: Teto padrão de um campo no módulo ``csv`` do Python. O corpus tem prompts
#: bem acima disso, então todo export CSV com texto grande carrega um aviso no
#: manifesto — o arquivo está certo, o leitor é que precisa de
#: ``csv.field_size_limit()``.
LIMITE_CAMPO_CSV_PADRAO = 131_072


def nome_de_arquivo(bruto: str | None) -> str:
    """Nome vindo do usuário → nome de arquivo seguro.

    O nome chega pela API e vira caminho em disco: sem esta faxina,
    ``../../config/settings`` seria um caminho válido. Só ASCII alfanumérico,
    ponto, hífen e sublinhado sobrevivem.
    """
    if bruto:
        limpo = _NOME_SEGURO.sub("-", bruto).strip("-.")
        if limpo:
            return limpo[:80]
    return f"export_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"


# ---------------------------------------------------------------------------
# escritores
# ---------------------------------------------------------------------------


class Escritor(Protocol):
    """Contrato de um escritor de linhas (aberto pelo ``REGISTRY``)."""

    def escrever(self, registro: dict[str, Any]) -> None: ...
    def fechar(self) -> None: ...


class EscritorJsonl:
    """JSONL, uma linha por registro. ``ensure_ascii=False``: acento é dado."""

    def __init__(self, destino: Path, encoding: str, campos: tuple[str, ...]) -> None:
        self._campos = campos
        self._fh = destino.open("w", encoding=encoding, newline="\n")

    def escrever(self, registro: dict[str, Any]) -> None:
        self._fh.write(json.dumps(registro, ensure_ascii=False) + "\n")

    def fechar(self) -> None:
        self._fh.close()


class EscritorCsv:
    """CSV para o Excel em pt-BR.

    ``utf-8-sig`` (o BOM é o que faz o Excel reconhecer UTF-8 e não quebrar
    "coração"), ``QUOTE_ALL`` e ``lineterminator="\\n"``. O texto dos prompts
    tem quebras de linha DENTRO do campo: o arquivo **tem de ser lido com um
    parser de CSV de verdade**, nunca com ``split(",")``.
    """

    def __init__(self, destino: Path, encoding: str, campos: tuple[str, ...]) -> None:
        self._fh = destino.open("w", encoding=encoding, newline="")
        self._w = csv.DictWriter(
            self._fh, fieldnames=list(campos), quoting=csv.QUOTE_ALL, lineterminator="\n"
        )
        self._w.writeheader()

    def escrever(self, registro: dict[str, Any]) -> None:
        self._w.writerow({k: _celula(v) for k, v in registro.items()})

    def fechar(self) -> None:
        self._fh.close()


#: Como um valor NULO aparece como CHAVE nas contagens do manifesto. JSON não
#: aceita ``null`` como chave de objeto, e ``str(None)`` daria ``"None"`` — que
#: num manifesto em português parece nome de classe, não "sem rótulo".
CHAVE_NULA = "(nulo)"


def _chave_de_contagem(valor: Any) -> str:
    return CHAVE_NULA if valor is None else str(valor)


def _celula(valor: Any) -> str:
    """Valor Python → célula de CSV: bool vira ``true``/``false``, ``None`` vira vazio."""
    if valor is None:
        return ""
    if isinstance(valor, bool):
        return "true" if valor else "false"
    return str(valor)


# ---------------------------------------------------------------------------
# registry de formatos
# ---------------------------------------------------------------------------


class Formato(NamedTuple):
    """Um formato de saída = perfil (o QUE sai) x container (COMO sai)."""

    profile: str
    #: Vai para ``exports.format``, que tem CHECK no DDL. Só ``jsonl``/``csv``.
    container: str
    extensao: str
    encoding: str
    campos: tuple[str, ...]
    abrir: Callable[[Path, str, tuple[str, ...]], Escritor]


def _formato_flat(container: str, encoding: str, abrir: Any) -> Formato:
    return Formato("flat", container, f".{container}", encoding, CAMPOS_FLAT, abrir)


#: Formatos disponíveis, chaveados por ``"<profile>.<container>"``.
#:
#: Acrescentar ``"label-studio.jsonl"`` aqui (com outro ``campos`` e outro
#: adaptador) é toda a mudança necessária para o M10+: o container continua
#: ``jsonl``, o CHECK do DDL continua satisfeito, e o perfil real fica gravado
#: em ``manifest_json.profile``.
REGISTRY: dict[str, Formato] = {
    "flat.jsonl": _formato_flat("jsonl", "utf-8", EscritorJsonl),
    "flat.csv": _formato_flat("csv", "utf-8-sig", EscritorCsv),
}


def formato(chave: str) -> Formato:
    """Formato pelo par ``perfil.container``; erro alto se não existe."""
    try:
        return REGISTRY[chave]
    except KeyError:
        raise KeyError(
            f"formato desconhecido: {chave!r} (disponíveis: {', '.join(sorted(REGISTRY))})"
        ) from None


# ---------------------------------------------------------------------------
# adaptação de linha
# ---------------------------------------------------------------------------


def _b(valor: object) -> bool | None:
    return None if valor is None else bool(valor)


def linha_flat(
    linha: sqlite3.Row, atribuicao: str, *, incluir_original: bool
) -> dict[str, Any]:
    """Linha do SQLite → registro do perfil ``flat``, achatado e estável."""
    registro: dict[str, Any] = {
        "uid": str(linha["uid"]),
        "text": str(linha["text"]),
        "lang": linha["lang"],
        "lang_variant": linha["lang_variant"],
        "task_type": linha["task_type"],
        "domain": linha["domain"],
        "quality": None if linha["quality"] is None else int(linha["quality"]),
        "nsfw": _b(linha["nsfw"]),
        "pii_found": _b(linha["pii_found"]),
        "source": linha["source"],
        "source_id": linha["source_id"],
        "license": linha["license"],
        "commercial_ok": _b(linha["commercial_ok"]),
        "redistributable": _b(linha["redistributable"]),
        "attribution": atribuicao,
        "label_method": linha["label_method"],
        "label_confidence": linha["label_confidence"],
        "needs_review": _b(linha["needs_review"]),
        "edited": _b(linha["edited"]),
        "n_chars": int(linha["n_chars"]),
        "n_words": int(linha["n_words"]),
        "n_exact_dups": int(linha["n_exact_dups"]),
        "n_near_dups": int(linha["n_near_dups"]),
        "country": linha["country"],
        "model_family": linha["model_family"],
        "created_ts": linha["created_ts"],
        "native_category": linha["native_category"],
        "taxonomy_version": TAXONOMY_VERSION,
    }
    if incluir_original:
        # "O texto original" é sempre COALESCE: text_original é NULL enquanto
        # ninguém editou, e emitir NULL ali faria parecer que o prompt não tem
        # texto de origem.
        registro[CAMPO_ORIGINAL] = str(
            linha["text_original"] if linha["text_original"] is not None else linha["text"]
        )
    return registro


# ---------------------------------------------------------------------------
# execução
# ---------------------------------------------------------------------------


class Resultado(NamedTuple):
    """O que um export produziu."""

    arquivo: Path
    manifesto: Path
    row_count: int
    manifest: dict[str, Any]


def _sha256(caminho: Path, bloco: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with caminho.open("rb") as fh:
        for pedaco in iter(lambda: fh.read(bloco), b""):
            h.update(pedaco)
    return h.hexdigest()


def executar(
    linhas: Iterable[sqlite3.Row],
    *,
    destino_dir: Path,
    chave_formato: str,
    atribuicoes: dict[str, str],
    nome: str | None = None,
    mode: str = "filter",
    filtros: dict[str, Any] | None = None,
    include_nonredistributable: bool = False,
    include_text_original: bool = False,
    max_rows: int | None = None,
    meta_banco: dict[str, Any] | None = None,
) -> Resultado:
    """Escreve o arquivo e o manifesto. Devolve os dois caminhos e as contagens.

    A escrita é atômica (``.tmp`` + ``os.replace``): um Ctrl+C no meio deixa um
    ``.tmp`` órfão, nunca um export pela metade que parece pronto. O
    ``row_count`` do manifesto é **contado no laço**, jamais um ``count(*)``
    prévio — só o que foi escrito conta, e o filtro de redistribuição corta
    linhas depois da consulta.
    """
    fmt = formato(chave_formato)
    destino_dir.mkdir(parents=True, exist_ok=True)
    base = nome_de_arquivo(nome)
    arquivo = destino_dir / f"{base}{fmt.extensao}"
    tmp = destino_dir / f"{base}{fmt.extensao}.tmp"

    campos = fmt.campos + ((CAMPO_ORIGINAL,) if include_text_original else ())

    contagens: dict[str, Counter[str]] = {
        chave: Counter()
        for chave in ("source", "license", "lang", "lang_variant", "task_type", "domain", "quality")
    }
    n = 0
    excluidas = 0
    nao_comercial = 0
    nsfw_sim = 0
    com_pii = 0
    editadas = 0
    maior_texto = 0
    atribuicoes_usadas: dict[str, str] = {}

    escritor = fmt.abrir(tmp, fmt.encoding, campos)
    try:
        for linha in linhas:
            if not linha["redistributable"] and not include_nonredistributable:
                excluidas += 1
                continue
            if max_rows is not None and n >= max_rows:
                raise ValueError(
                    f"o recorte passa do teto de {max_rows} linhas por export "
                    "([app] export_max_rows) — refine o filtro ou aumente o teto"
                )
            fonte = str(linha["source"])
            credito = atribuicoes.get(fonte, "")
            registro = linha_flat(linha, credito, incluir_original=include_text_original)
            escritor.escrever(registro)
            n += 1

            if credito:
                atribuicoes_usadas[fonte] = credito
            for chave in contagens:
                contagens[chave][_chave_de_contagem(registro[chave])] += 1
            if not registro["commercial_ok"]:
                nao_comercial += 1
            if registro["nsfw"]:
                nsfw_sim += 1
            if registro["pii_found"]:
                com_pii += 1
            if registro["edited"]:
                editadas += 1
            maior_texto = max(maior_texto, registro["n_chars"])
    except BaseException:
        escritor.fechar()
        tmp.unlink(missing_ok=True)
        raise
    escritor.fechar()
    os.replace(tmp, arquivo)

    manifest: dict[str, Any] = {
        "created_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "file": arquivo.name,
        "sha256": _sha256(arquivo),
        "bytes": arquivo.stat().st_size,
        "format": fmt.container,
        "profile": fmt.profile,
        "encoding": fmt.encoding,
        "mode": mode,
        "filters": filtros or {},
        "row_count": n,
        "counts": {chave: dict(contador.most_common()) for chave, contador in contagens.items()},
        "nsfw_incluidos": nsfw_sim,
        "pii_found": com_pii,
        "editados": editadas,
        "nao_comercial_count": nao_comercial,
        "excluded_nonredistributable_count": excluidas,
        "include_nonredistributable": include_nonredistributable,
        "include_text_original": include_text_original,
        "attributions": dict(sorted(atribuicoes_usadas.items())),
        "taxonomy_version": TAXONOMY_VERSION,
        "app_version": __version__,
        **{f"db_{k}": v for k, v in (meta_banco or {}).items()},
    }
    if include_nonredistributable:
        manifest["WARNING"] = (
            "contém linhas não redistribuíveis (redistributable=0) — uso local "
            "apenas, NÃO publique nem entregue este arquivo a terceiros"
        )
    if nao_comercial:
        manifest["NOTA_COMERCIAL"] = (
            f"{nao_comercial} linha(s) sob licença não comercial (CC-BY-NC): o "
            "derivado não pode ser vendido"
        )
    manifest["maior_texto_chars"] = maior_texto
    if fmt.container == "csv" and maior_texto > LIMITE_CAMPO_CSV_PADRAO:
        # Medido: o módulo `csv` do Python recusa campo acima de 131.072
        # caracteres com `_csv.Error: field larger than field limit`, e este
        # corpus tem prompts de ~1 milhão. O arquivo está CORRETO — quem
        # precisa de ajuste é o leitor. Sem este aviso, o usuário conclui que o
        # export saiu corrompido.
        manifest["NOTA_CSV"] = (
            f"a maior célula tem {maior_texto} caracteres, acima do limite padrão "
            f"de {LIMITE_CAMPO_CSV_PADRAO} do módulo csv do Python. Para ler: "
            "csv.field_size_limit(10**7) antes do reader. O Excel corta em 32.767 "
            "por célula — para textos longos use o JSONL."
        )

    manifesto = destino_dir / f"{base}.manifest.json"
    tmp_manifesto = destino_dir / f"{base}.manifest.json.tmp"
    tmp_manifesto.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp_manifesto, manifesto)

    return Resultado(arquivo, manifesto, n, manifest)


def registrar(
    conn: sqlite3.Connection,
    resultado: Resultado,
    *,
    filtros: dict[str, Any] | None = None,
) -> int:
    """Grava a linha em ``exports`` e devolve o id.

    ``format`` recebe o **container**, nunca o perfil: a coluna tem
    ``CHECK (format IN ('jsonl','csv'))`` e ``'label-studio'`` seria rejeitado
    pelo banco na hora.
    """
    cur = conn.execute(
        "INSERT INTO exports (format, path, filters_json, row_count, manifest_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (
            resultado.manifest["format"],
            str(resultado.arquivo),
            json.dumps(filtros or {}, ensure_ascii=False),
            resultado.row_count,
            json.dumps(resultado.manifest, ensure_ascii=False),
        ),
    )
    return int(cur.lastrowid or 0)


def previa_csv(registros: Iterable[dict[str, Any]], campos: tuple[str, ...]) -> str:
    """Renderiza registros como CSV em memória — usado só por teste/prévia."""
    buffer = io.StringIO(newline="")
    escritor = csv.DictWriter(
        buffer, fieldnames=list(campos), quoting=csv.QUOTE_ALL, lineterminator="\n"
    )
    escritor.writeheader()
    for registro in registros:
        escritor.writerow({k: _celula(v) for k, v in registro.items()})
    return buffer.getvalue()


def ler_manifesto(caminho: Path) -> dict[str, Any]:
    """Manifesto de um export já gravado (``{}`` se sumiu ou está torto)."""
    try:
        return dict(json.loads(caminho.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return {}


def linhas_em_lotes(
    conn: sqlite3.Connection, sql_por_lote: Callable[[int], str], ids: list[int], lote: int = 5000
) -> Iterator[sqlite3.Row]:
    """Itera linhas por um conjunto de ids, em lotes (teto de variáveis do SQLite)."""
    for i in range(0, len(ids), lote):
        fatia = ids[i : i + lote]
        yield from conn.execute(sql_por_lote(len(fatia)), fatia)


__all__ = [
    "CAMPOS_FLAT",
    "CAMPO_ORIGINAL",
    "CHAVE_NULA",
    "IDIOMA_DOS_ARTEFATOS",
    "LIMITE_CAMPO_CSV_PADRAO",
    "REGISTRY",
    "Escritor",
    "EscritorCsv",
    "EscritorJsonl",
    "Formato",
    "Resultado",
    "executar",
    "formato",
    "ler_manifesto",
    "linha_flat",
    "linhas_em_lotes",
    "nome_de_arquivo",
    "previa_csv",
    "registrar",
]
