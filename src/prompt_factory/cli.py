"""Entrypoint `pf` — CLI única do Prompt Factory (argparse, só stdlib).

Cada subcomando corresponde a um estágio da pipeline e vai saindo do stub no
marco indicado em `_Cmd.milestone` (implementados: `db-check` no M1; `ingest` e
`report raw` no M2; `run` s01-s06 e `report universe`/`dedup-sample` no M4;
`make-seed`, `labels` e `merge-labels` no M5; `load-db` e `db-check --bench` no
M8).
Enquanto é stub, o comando imprime em que marco chega e sai com código 2.
`pf --help` e `pf <cmd> --help` funcionam e saem 0 — é isso que a DoD do M0
exige.

O `main()` faz o *bootstrap* de ambiente ANTES de qualquer import pesado:
UTF-8 forçado (Windows) e ``HF_HOME`` vindo do ``config/settings.toml``, para
que datasets/torch nunca escrevam cache no C:.
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence

from . import __version__

PROG = "pf"

# (flags, kwargs) — só o suficiente para `pf <cmd> --help` já documentar a forma
# final do comando. Os handlers reais chegam nos marcos indicados.
_Option = tuple[tuple[str, ...], dict[str, object]]

_MAX_ROWS: _Option = (
    ("--max-rows",),
    {
        "type": int,
        "metavar": "N",
        "help": "processa no máximo N linhas (smoke test; no wildchat conta linhas VARRIDAS, não mantidas)",
    },
)
_FORCE: _Option = (
    ("--force",),
    {"action": "store_true", "help": "reescreve saídas já existentes"},
)
_DATA_DIR: _Option = (
    ("--data-dir",),
    {
        "metavar": "DIR",
        "help": "redireciona a árvore de dados (padrão: data/) — é assim que o smoke roda sem tocar no corpus real",
    },
)
_LABELING_DIR: _Option = (
    ("--labeling-dir",),
    {
        "metavar": "DIR",
        "help": "redireciona a árvore da campanha (padrão: labeling/): semente, lotes, manifest e rótulos",
    },
)


class _Cmd:
    """Descrição declarativa de um subcomando.

    Ao implementar um comando (ex.: ``db-check`` no M1): escreva o handler,
    passe-o em ``handler=`` e marque ``implemented=True``. Só isso — o help, o
    dispatch e a parametrização dos testes seguem essa flag automaticamente.
    """

    __slots__ = ("handler", "help", "implemented", "milestone", "name", "options")

    def __init__(
        self,
        name: str,
        milestone: str,
        help: str,
        options: Sequence[_Option] = (),
        *,
        implemented: bool = False,
        handler: object | None = None,
    ) -> None:
        self.name = name
        self.milestone = milestone
        self.help = help
        self.options = tuple(options)
        self.implemented = implemented
        self.handler = handler


def _db_check(args: argparse.Namespace) -> int:
    """``pf db-check`` (M1) — autoteste do SQLite num banco temporário.

    Com ``--bench`` (M8), depois do autoteste ele mede o banco REAL em somente
    leitura: sem banco carregado, avisa e sai 0.

    Import lazy de propósito: ``pf --help`` não precisa carregar o DDL nem o
    módulo ``sqlite3``.
    """
    from .db import run_db_check

    return run_db_check(bench=args.bench, query=args.query, db_path=args.db)


def _ingest(args: argparse.Namespace) -> int:
    """``pf ingest [fonte...]`` (M2/M3) — baixa fontes e grava ``data/raw/<fonte>.parquet``.

    Sequencial e fail-fast: a primeira fonte que explodir aborta a rodada, com o
    traceback inteiro. As fontes já gravadas ficam válidas (cada parquet é
    escrito atomicamente), então basta re-rodar o mesmo comando — os downloads
    do HuggingFace retomam do cache e o WildChat retoma do checkpoint.

    Dois contratos de ingester (ver ``ingest/__init__.py``): ``run_ingest`` para
    quem controla a própria escrita (WildChat, streaming com checkpoint) e
    ``iter_rows`` + ``write_raw`` para as fontes pequenas do M2.

    ``datasets``/``pyarrow`` entram só aqui dentro: ``pf --help`` não paga por eles.
    """
    from . import paths
    from .ingest import REGISTRY, resolve_names
    from .ingest.base import get_source_cfg, write_raw

    paths.ensure_dirs()
    try:
        fontes = resolve_names(args.source)
    except ValueError as exc:
        print(f"[pf] {exc}", file=sys.stderr)
        return 2

    if args.resume:
        # Retomar já é o padrão: a flag existe só para quem espera digitá-la.
        print("[pf] --resume é o comportamento PADRÃO (existindo checkpoint válido, o passe continua)")
    if args.force:
        print("[pf] --force não tem efeito: raw é regenerável, a ingestão sempre reescreve")
    if args.downsample and fontes != ["wildchat_en"]:
        print("[pf] --downsample só vale para `pf ingest wildchat-en`", file=sys.stderr)
        return 2

    print(f"[pf] ingerindo {len(fontes)} fonte(s): {', '.join(fontes)}")
    for nome in fontes:
        try:
            modulo = REGISTRY[nome]
            runner = getattr(modulo, "run_ingest", None)
            if runner is not None:
                codigo = int(runner(nome, get_source_cfg(nome), args))
                if codigo != 0:
                    return codigo
            else:
                write_raw(
                    nome,
                    modulo.iter_rows(get_source_cfg(nome), args.max_rows),
                    args.max_rows,
                )
        except Exception as exc:  # o traceback inteiro vai para o stderr logo abaixo
            import traceback

            traceback.print_exc()
            print(f"[{nome}] FALHOU: {exc}", file=sys.stderr)
            return 1
    return 0


def expandir_estagios(
    tokens: Sequence[str], validos: Sequence[str], todos: Sequence[str] | None = None
) -> list[str]:
    """``["s01", "s03-s05"]`` → ``["s01", "s03", "s04", "s05"]``.

    Aceita ``all``, nomes soltos e intervalos ``sNN-sMM``. Exige ordem
    **crescente** no resultado: pedir ``s04 s02`` é quase sempre engano de quem
    digitou, e rodar fora de ordem produziria um universo montado com insumo
    velho, em silêncio.

    ``todos`` é o que ``all`` significa, quando isso não é ``validos`` inteiro:
    o s07 e o s08 são estágios legítimos de ``pf run s07``, mas ficam fora do
    ``all`` porque entre eles existe uma campanha de rotulagem com revisão
    humana (ver ``stages.CADEIA``).
    """
    ordem = list(validos)
    padrao = list(todos) if todos is not None else ordem
    escolhidos: list[str] = []
    for token in tokens:
        bruto = token.strip().lower()
        if bruto == "all":
            escolhidos.extend(padrao)
            continue
        if "-" in bruto:
            inicio, _, fim = bruto.partition("-")
            for ponta in (inicio, fim):
                if ponta not in ordem:
                    raise ValueError(f"estágio desconhecido: {ponta!r} (conhecidos: {', '.join(ordem)})")
            i, j = ordem.index(inicio), ordem.index(fim)
            if i > j:
                raise ValueError(f"intervalo invertido: {bruto!r}")
            escolhidos.extend(ordem[i : j + 1])
            continue
        if bruto not in ordem:
            raise ValueError(f"estágio desconhecido: {bruto!r} (conhecidos: {', '.join(ordem)})")
        escolhidos.append(bruto)

    vistos: list[str] = []
    for nome in escolhidos:
        if nome not in vistos:
            vistos.append(nome)
    posicoes = [ordem.index(n) for n in vistos]
    if posicoes != sorted(posicoes):
        raise ValueError(
            f"estágios fora de ordem: {' '.join(vistos)} — a pipeline é uma cadeia, rode em ordem crescente"
        )
    return vistos


def _run(args: argparse.Namespace) -> int:
    """``pf run [estágios]`` (M4) — roda s01..s06 em cadeia, parando no primeiro erro.

    O único import do módulo de estágios acontece aqui dentro: ``stages`` puxa
    pyarrow e, mais fundo, torch/lingua — ``pf --help`` não paga por isso.
    """
    from pathlib import Path

    from . import paths
    from .stages import CADEIA, DESCRICOES, STAGES, StageConfig

    try:
        nomes = expandir_estagios(args.stages or ["all"], list(STAGES), CADEIA)
    except ValueError as exc:
        print(f"[pf] {exc}", file=sys.stderr)
        return 2

    if args.force:
        print("[pf] --force não tem efeito: todo estágio reescreve a própria saída")
    cfg = StageConfig(
        data_dir=Path(args.data_dir).resolve() if args.data_dir else paths.DATA,
        max_rows=args.max_rows,
    )
    cfg.preparar_dirs()
    limite = f", max_rows={args.max_rows} (só o s01 corta)" if args.max_rows else ""
    print(f"[pf] data_dir={cfg.data_dir}{limite}")
    for nome in nomes:
        print(f"[pf] === {nome}: {DESCRICOES.get(nome, '')} ===")
        codigo = int(STAGES[nome](cfg))
        if codigo != 0:
            print(f"[pf] {nome} falhou (código {codigo}) — cadeia interrompida", file=sys.stderr)
            return codigo
    return 0


def _report(args: argparse.Namespace) -> int:
    """``pf report <alvo>`` — ``raw`` (M2), ``universe`` e ``dedup-sample`` (M4)."""
    from pathlib import Path

    raiz = Path(args.data_dir).resolve() if args.data_dir else None
    if args.target == "raw":
        from .report import report_raw

        return report_raw(sources=args.sources, head=args.head)
    if args.target == "universe":
        from .report import report_universe

        return report_universe((raiz / "final" / "universe.parquet") if raiz else None)
    if args.target == "dedup-sample":
        from .report import report_dedup_sample

        return report_dedup_sample(data_dir=raiz)
    if args.target is None:
        print("[pf] 'report' precisa de um alvo: raw | universe | dedup-sample")
        return 2
    print(f"[pf] alvo desconhecido: {args.target!r} (use raw | universe | dedup-sample)")
    return 2


def _stage_config(args: argparse.Namespace, **extra: object) -> object:
    """``StageConfig`` a partir das flags comuns (import lazy do módulo)."""
    from pathlib import Path

    from . import paths
    from .stages import StageConfig

    cfg = StageConfig(
        data_dir=Path(args.data_dir).resolve() if getattr(args, "data_dir", None) else paths.DATA,
        labeling_dir=(
            Path(args.labeling_dir).resolve()
            if getattr(args, "labeling_dir", None)
            else paths.LABELING
        ),
        **extra,  # type: ignore[arg-type]
    )
    cfg.preparar_dirs()
    return cfg


def _make_seed(args: argparse.Namespace) -> int:
    """``pf make-seed`` (M5) — s07: amostra-semente + lotes + manifest."""
    from .stages import STAGES

    if args.total:
        print(f"[pf] --total={args.total} sobrescreve [seed] size só nesta execução")
    cfg = _stage_config(args, force=bool(args.force))
    return int(STAGES["s07"](cfg))  # type: ignore[arg-type]


def _merge_labels(args: argparse.Namespace) -> int:
    """``pf merge-labels`` (M5) — s08: funde rótulos de agente e nativos."""
    from .stages import STAGES

    cfg = _stage_config(args, strict=bool(args.strict))
    return int(STAGES["s08"](cfg))  # type: ignore[arg-type]


def _load_db(args: argparse.Namespace) -> int:
    """``pf load-db`` (M8) — s11: constrói o SQLite do zero e troca por swap.

    Sai **3** quando o banco novo está pronto mas a troca foi recusada (destino
    aberto por outro processo). É um código distinto de propósito: um script que
    veja 3 sabe que basta parar o servidor e rodar ``--swap-only``, sem
    reconstruir nada.
    """
    from .stages import STAGES

    if args.swap:
        print("[pf] --swap é o comportamento PADRÃO (use --no-swap para só construir)")
    cfg = _stage_config(
        args,
        allow_unlabeled_pct=args.allow_unlabeled_pct,
        swap=not args.no_swap,
        swap_only=bool(args.swap_only),
        backup=bool(args.backup),
        deep=bool(args.deep),
    )
    return int(STAGES["s11"](cfg))  # type: ignore[arg-type]


def _labels(args: argparse.Namespace) -> int:
    """``pf labels <ação>`` (M5) — opera o livro-caixa da campanha.

    Toda escrita de estado da campanha passa por aqui: é o único lugar com
    validação estrita e escrita atômica do manifest. Editar
    ``labeling/manifest.json`` à mão é como editar um journal de banco de dados
    à mão — funciona até a primeira vez que não funciona.

    ``labeling_io`` é stdlib puro e entra por import lazy: um ``pf labels
    status`` não carrega pyarrow nem torch.
    """
    from pathlib import Path

    from . import labeling_io as lio
    from .stages import imprimir_funil

    lp = lio.LabelingPaths(Path(args.labeling_dir).resolve() if args.labeling_dir else None)
    acao = args.action or "status"

    if acao == "status":
        p = lio.painel(lp)
        if args.json:
            import json

            print(json.dumps(p, ensure_ascii=False, indent=2))
            return 0
        c = p["contagem"]
        medio = p["agreement_medio"]
        imprimir_funil(
            "labels",
            ("item", "valor"),
            [
                ["taxonomia", p["taxonomy_version"]],
                [
                    "calibração",
                    f"{p['calibracao_status']} ({p['calibracao_n']} itens)",
                ],
                ["lotes pendentes", c.get(lio.PENDING, 0)],
                ["lotes reivindicados", c.get(lio.CLAIMED, 0)],
                ["lotes concluídos", c.get(lio.DONE, 0)],
                ["lotes falhados", c.get(lio.FAILED, 0)],
                ["claims órfãos (voltam no próximo claim)", len(p["orfaos"])],
                [
                    "agreement médio",
                    f"{medio:.3f}" if medio is not None else "não medido (sem ouro)",
                ],
                [
                    f"lotes abaixo de {p['agreement_min']:.2f}",
                    len(p["lotes_baixos"]),
                ],
                ["rótulos", f"{p['n_rotulos']} / {p['n_seed']}"],
            ],
        )
        if p["calibracao_status"] != lio.GOLD:
            print(
                "[labels] a calibração ainda não virou ouro: rotule "
                f"{lio.BATCH_CALIBRACAO}, revise à mão e rode `pf labels gold --file <jsonl>`"
            )
        return 0

    if acao == "list":
        manifest = lio.carregar_manifest(lp)
        filtro = args.status
        linhas = [
            [
                b,
                r.get("status"),
                r.get("n_items"),
                r.get("tentativas", 0),
                (f"{r['agreement']:.2f}" if isinstance(r.get("agreement"), int | float) else "-"),
                r.get("agent_model") or "-",
                (r.get("motivo") or "")[:40],
            ]
            for b, r in sorted(manifest.get("batches", {}).items())
            if filtro is None or r.get("status") == filtro
        ]
        if not linhas:
            print(f"[labels] nenhum lote{f' em {filtro!r}' if filtro else ''}")
            return 0
        imprimir_funil(
            "labels",
            ("lote", "status", "itens", "tent.", "agree", "modelo", "motivo"),
            linhas[: args.limit] if args.limit else linhas,
        )
        return 0

    if acao == "next":
        manifest = lio.carregar_manifest(lp)
        if manifest.get("calibration", {}).get("status") != lio.GOLD:
            print(
                f"[labels] AVISO: a calibração ainda é {manifest['calibration']['status']!r} — "
                "os lotes vão rodar sem portão de agreement até o ouro ser importado"
            )
        alvos = lio.claim(args.batch, n=args.count, lp=lp, manifest=manifest)
        lio.salvar_manifest(manifest, lp)
        if not alvos:
            restam = sum(
                1 for r in manifest.get("batches", {}).values() if r.get("status") == lio.PENDING
            )
            print("[labels] nada pendente" if not restam else f"[labels] {restam} pendentes")
            return 0
        for batch_id in alvos:
            dados = lio.carregar_lote(batch_id, lp)
            if args.out_dir:
                destino = Path(args.out_dir) / f"{batch_id}.json"
                lio.escrever_json_atomico(destino, dados)
                print(f"[labels] {batch_id} claimed -> {destino}")
            elif args.out:
                lio.escrever_json_atomico(Path(args.out), dados)
                print(f"[labels] {batch_id} claimed -> {args.out}")
            else:
                import json

                print(json.dumps(dados, ensure_ascii=False))
        return 0

    if acao == "submit":
        if not args.batch or not args.file:
            print("[pf] 'labels submit' exige --batch e --file", file=sys.stderr)
            return 2
        texto = Path(args.file).read_text(encoding="utf-8")
        ok, linhas, erros = lio.validar_resposta(args.batch, texto, lp)
        if not ok:
            for erro in erros:
                print(f"[labels] {erro}", file=sys.stderr)
            faltam = lio.uids_para_retry(args.batch, linhas, lp)
            # Linha parseável: é ela que o maestro passa para `--only` no retry.
            print(f"RETRY_UIDS: {','.join(faltam)}")
            print(
                f"[labels] {args.batch} SEGUE reivindicado — corrija e submeta de novo",
                file=sys.stderr,
            )
            return 1
        for aviso in erros:  # sem erros, o que sobrou são avisos
            print(f"[labels] aviso: {aviso}")
        manifest = lio.carregar_manifest(lp)
        valor = lio.concluir(args.batch, linhas, args.model, lp=lp, manifest=manifest)
        minimo = lio.agreement_minimo()
        if valor is not None and valor < minimo:
            lio.reenfileirar(
                args.batch,
                f"agreement {valor:.2f} < {minimo:.2f}",
                lp=lp,
                manifest=manifest,
            )
            lio.salvar_manifest(manifest, lp)
            print(
                f"[labels] {args.batch}: agreement {valor:.2f} < {minimo:.2f} — "
                "lote devolvido para `pending` (portão de calibração)",
                file=sys.stderr,
            )
            return 1
        lio.salvar_manifest(manifest, lp)
        medida = f"{valor:.2f}" if valor is not None else "não medido (sem ouro)"
        print(f"[labels] {args.batch}: {len(linhas)} rótulos, agreement {medida} -> done")
        return 0

    if acao == "gold":
        if not args.file:
            print("[pf] 'labels gold' exige --file com o gabarito revisado", file=sys.stderr)
            return 2
        ok, _, erros = lio.importar_ouro(Path(args.file).read_text(encoding="utf-8"), lp)
        for erro in erros:
            print(f"[labels] {erro}", file=sys.stderr if not ok else sys.stdout)
        if not ok:
            print("[labels] ouro RECUSADO — nada foi gravado", file=sys.stderr)
            return 1
        print("[labels] ouro importado; agreement dos lotes já concluídos recalculado")
        return 0

    if acao == "requeue":
        manifest = lio.carregar_manifest(lp)
        if args.failed:
            alvos = [
                b
                for b, r in sorted(manifest.get("batches", {}).items())
                if r.get("status") == lio.FAILED
            ]
        elif args.batch:
            alvos = [args.batch]
        else:
            print("[pf] 'labels requeue' exige --batch ou --failed", file=sys.stderr)
            return 2
        for batch_id in alvos:
            lio.reenfileirar(batch_id, args.reason or "requeue manual", lp=lp, manifest=manifest)
        lio.salvar_manifest(manifest, lp)
        print(f"[labels] {len(alvos)} lote(s) de volta em pending: {', '.join(alvos) or '-'}")
        return 0

    print(f"[pf] ação desconhecida: {acao!r}", file=sys.stderr)
    return 2


COMMANDS: tuple[_Cmd, ...] = (
    _Cmd(
        "ingest",
        "M2",
        "baixa fontes do HuggingFace e grava data/raw/<fonte>.parquet",
        [
            (
                ("source",),
                {
                    "nargs": "*",
                    "metavar": "FONTE",
                    "help": "seções de config/sources.toml (hífen vale por _), ou 'all' (padrão: todas as default_on)",
                },
            ),
            (
                ("--resume",),
                {"action": "store_true", "help": "alias explícito do padrão: retomar do checkpoint (wildchat)"},
            ),
            (
                ("--restart",),
                {"action": "store_true", "help": "apaga checkpoint e part-files e refaz o passe do zero (wildchat)"},
            ),
            (
                ("--downsample",),
                {"action": "store_true", "help": "só wildchat-en: pool -> 100.000 estratificadas, sem rede"},
            ),
            _MAX_ROWS,
            _FORCE,
        ],
        implemented=True,
        handler=_ingest,
    ),
    _Cmd(
        "run",
        "M4",
        "roda estágios da pipeline (s01 normalize .. s06 dedup próximo)",
        [
            (("stages",), {"nargs": "*", "metavar": "STAGE", "help": "ex.: s01 s02, s01-s06 ou all (padrão: all)"}),
            _MAX_ROWS,
            _DATA_DIR,
            _FORCE,
        ],
        implemented=True,
        handler=_run,
    ),
    _Cmd(
        "report",
        "M2",
        "resumo legível de um parquet/estágio (NUNCA abra parquet com cat)",
        [
            (
                ("target",),
                {
                    "nargs": "?",
                    "choices": ["raw", "universe", "dedup-sample"],
                    "help": "'raw' (M2), 'universe' ou 'dedup-sample' (M4)",
                },
            ),
            (("--head",), {"type": int, "default": 2, "metavar": "N", "help": "mostra N linhas de exemplo por fonte"}),
            (("--sources",), {"nargs": "+", "metavar": "FONTE", "help": "restringe o `report raw` a estas fontes"}),
            _DATA_DIR,
        ],
        implemented=True,
        handler=_report,
    ),
    _Cmd(
        "db-check",
        "M1",
        "sanidade do SQLite: DDL, WAL e busca FTS sem acento ('coracao' acha 'coração')",
        [
            (("--query",), {"metavar": "Q", "help": "termo de teste do FTS5"}),
            (
                ("--bench",),
                {"action": "store_true", "help": "mede as consultas da interface no banco carregado (só leitura)"},
            ),
            (("--db",), {"metavar": "PATH", "help": "banco a medir no --bench (padrão: data/db/prompts.sqlite)"}),
        ],
        implemented=True,
        handler=_db_check,
    ),
    _Cmd(
        "make-seed",
        "M5",
        "s07: amostra-semente estratificada (fonte x tamanho) + lotes de rotulagem",
        [
            (("--total",), {"type": int, "metavar": "N", "help": "sobrescreve [seed] size"}),
            _DATA_DIR,
            _LABELING_DIR,
            (
                ("--force",),
                {
                    "action": "store_true",
                    "help": "APAGA a campanha em andamento e refaz a semente do zero",
                },
            ),
        ],
        implemented=True,
        handler=_make_seed,
    ),
    _Cmd(
        "labels",
        "M5",
        "opera a campanha: reivindica lotes, valida respostas e escreve o manifest",
        [
            (
                ("action",),
                {
                    "nargs": "?",
                    "choices": ["status", "list", "next", "submit", "gold", "requeue"],
                    "help": "status (padrão) | list | next (claim+imprime) | submit | gold | requeue",
                },
            ),
            (("--batch",), {"metavar": "ID", "help": "um lote específico (ex.: batch_0007)"}),
            (("--file",), {"metavar": "PATH", "help": "JSONL da resposta (submit) ou do ouro (gold)"}),
            (
                ("--out",),
                {
                    "metavar": "PATH",
                    "help": "grava o JSON do lote em arquivo (a saída de ferramenta do agente trunca ~30k)",
                },
            ),
            (("--out-dir",), {"metavar": "DIR", "help": "um <batch_id>.json por lote reivindicado"}),
            (
                ("-n", "--count"),
                {"type": int, "default": 1, "metavar": "N", "help": "quantos lotes reivindicar (next)"},
            ),
            (("--model",), {"metavar": "NOME", "help": "modelo que rotulou (registrado no manifest)"}),
            (("--status",), {"metavar": "S", "help": "filtra o `list` por status"}),
            (("--limit",), {"type": int, "metavar": "N", "help": "corta o `list` em N linhas"}),
            (("--failed",), {"action": "store_true", "help": "requeue: revive todos os lotes failed"}),
            (("--reason",), {"metavar": "TEXTO", "help": "motivo registrado no requeue"}),
            (("--json",), {"action": "store_true", "help": "status em JSON, para script"}),
            _LABELING_DIR,
        ],
        implemented=True,
        handler=_labels,
    ),
    _Cmd(
        "merge-labels",
        "M5",
        "s08: consolida rótulos dos agentes + rótulos nativos mapeados",
        [
            (
                ("--strict",),
                {"action": "store_true", "help": "falha se um lote done estiver sem arquivo de rótulos"},
            ),
            _DATA_DIR,
            _LABELING_DIR,
        ],
        implemented=True,
        handler=_merge_labels,
    ),
    _Cmd(
        "train",
        "M7",
        "s09: treina o classificador (LogReg sobre embeddings) por eixo",
        [
            (("--axis",), {"choices": ["task_type", "domain", "quality"], "help": "treina só um eixo"}),
            (("--folds",), {"type": int, "default": 5, "metavar": "K", "help": "folds da busca de C"}),
        ],
    ),
    _Cmd(
        "apply",
        "M7",
        "s10: aplica o classificador ao universo, com limiares de confiança",
        [_MAX_ROWS, _FORCE],
    ),
    _Cmd(
        "load-db",
        "M8",
        "s11: carga bulk no SQLite (build + rebuild do FTS + swap de arquivo)",
        [
            (
                ("--swap",),
                {"action": "store_true", "help": "alias explícito do padrão: trocar o banco no fim"},
            ),
            (
                ("--no-swap",),
                {"action": "store_true", "help": "constrói db/prompts.build.sqlite e PARA (não troca)"},
            ),
            (
                ("--swap-only",),
                {
                    "action": "store_true",
                    "help": "só a troca, sem reconstruir — é o retry de um swap recusado por lock",
                },
            ),
            (
                ("--allow-unlabeled-pct",),
                {
                    "type": float,
                    "metavar": "PCT",
                    "help": "teto de linhas sem task_type (padrão: [loaddb] allow_unlabeled_pct = 1)",
                },
            ),
            (("--backup",), {"action": "store_true", "help": "grava um .bak do banco anterior antes de trocar"}),
            (
                ("--deep",),
                {"action": "store_true", "help": "PRAGMA integrity_check (varre tudo) no lugar do quick_check"},
            ),
            _DATA_DIR,
        ],
        implemented=True,
        handler=_load_db,
    ),
    _Cmd(
        "export",
        "M9",
        "s12: exporta JSONL/CSV com licença e atribuição por linha + manifest",
        [
            (("--format",), {"choices": ["jsonl", "csv"], "default": "jsonl", "help": "formato de saída"}),
            (("--collection",), {"metavar": "NOME", "help": "exporta uma coleção específica"}),
            (("--include-nc",), {"action": "store_true", "help": "inclui fontes não comerciais (CC-BY-NC)"}),
        ],
    ),
    _Cmd(
        "serve",
        "M9",
        "sobe a interface local (FastAPI + static/index.html)",
        [
            (("--host",), {"metavar": "HOST", "help": "padrão: [app] host do settings.toml"}),
            (("--port",), {"type": int, "metavar": "PORT", "help": "padrão: [app] port do settings.toml"}),
            (("--reload",), {"action": "store_true", "help": "auto-reload do uvicorn (dev)"}),
        ],
    ),
)


def _bootstrap_env() -> None:
    """UTF-8, HF_HOME e CA bundle antes de tudo. Idempotente e à prova de falha."""
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        from .config import hf_home

        os.environ.setdefault("HF_HOME", hf_home())
    except (OSError, KeyError, ValueError) as exc:
        # settings.toml ausente, ilegível ou malformado (TOMLDecodeError é
        # ValueError) não pode derrubar um `pf --help`. Avisa e segue.
        print(f"[pf] aviso: HF_HOME não configurado ({exc})", file=sys.stderr)
    try:
        # TLS interceptado nesta máquina: o requests (huggingface_hub) precisa
        # do armazenamento de certificados do Windows. Ver certs.py.
        from .certs import ensure_ca_bundle

        ensure_ca_bundle()
    except Exception as exc:  # pragma: no cover - bootstrap nunca derruba a CLI
        print(f"[pf] aviso: CA bundle do sistema não gerado ({exc})", file=sys.stderr)
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # stream capturado (pytest) ou já fechado
            pass


def build_parser() -> argparse.ArgumentParser:
    """Monta o parser completo — usado pela CLI e pelos testes."""
    parser = argparse.ArgumentParser(
        prog=PROG,
        description=(
            "Prompt Factory — banco de prompts reais (pt-BR + EN). "
            "Estágios leem/escrevem Parquet imutável em data/; o banco final é SQLite."
        ),
        epilog="Detalhes de arquitetura e pegadinhas: ver CLAUDE.md.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subparsers = parser.add_subparsers(dest="cmd", metavar="<comando>")
    for cmd in COMMANDS:
        nota = (
            "" if cmd.implemented else f" [implementação prevista para o {cmd.milestone}]"
        )
        sub = subparsers.add_parser(
            cmd.name,
            help=cmd.help,
            description=f"{cmd.help}{nota}",
        )
        for flags, kwargs in cmd.options:
            sub.add_argument(*flags, **kwargs)  # type: ignore[arg-type]
        sub.set_defaults(_cmd_spec=cmd)
    return parser


def stub_commands() -> tuple[_Cmd, ...]:
    """Comandos ainda não implementados — usado pelos testes."""
    return tuple(c for c in COMMANDS if not c.implemented)


def main(argv: Sequence[str] | None = None) -> int:
    """Ponto de entrada do console script ``pf``. Retorna o exit code."""
    _bootstrap_env()
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.cmd is None:
        parser.print_help()
        return 2

    spec: _Cmd = args._cmd_spec
    if not spec.implemented or spec.handler is None:
        print(f"[pf] {args.cmd!r} ainda não implementado (chega no {spec.milestone})")
        return 2

    return int(spec.handler(args))  # type: ignore[operator]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
