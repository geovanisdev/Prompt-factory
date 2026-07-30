"""Entrypoint `pf` — CLI única do Prompt Factory (argparse, só stdlib).

Cada subcomando corresponde a um estágio da pipeline. No M0 todos são stubs:
imprimem em que marco chegam e saem com código 2. `pf --help` e
`pf <cmd> --help` funcionam e saem 0 — é isso que a DoD do M0 exige.

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
    {"type": int, "metavar": "N", "help": "processa no máximo N linhas (smoke test)"},
)
_FORCE: _Option = (
    ("--force",),
    {"action": "store_true", "help": "reescreve saídas já existentes"},
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


COMMANDS: tuple[_Cmd, ...] = (
    _Cmd(
        "ingest",
        "M2",
        "baixa uma fonte do HuggingFace e grava data/raw/<fonte>.parquet",
        [
            (("source",), {"nargs": "?", "help": "nome da seção em config/sources.toml (padrão: todas as habilitadas)"}),
            (("--resume",), {"action": "store_true", "help": "retoma do último checkpoint de streaming"}),
            _MAX_ROWS,
            _FORCE,
        ],
    ),
    _Cmd(
        "run",
        "M4",
        "roda estágios da pipeline (s01 normalize .. s06 dedup próximo)",
        [
            (("stages",), {"nargs": "*", "metavar": "STAGE", "help": "ex.: s01 s02 ou s01-s06 (padrão: todos)"}),
            _MAX_ROWS,
            _FORCE,
        ],
    ),
    _Cmd(
        "report",
        "M4",
        "resumo legível de um parquet/estágio (NUNCA abra parquet com cat)",
        [
            (("target",), {"nargs": "?", "help": "caminho do parquet ou nome do estágio"}),
            (("--head",), {"type": int, "default": 5, "metavar": "N", "help": "mostra N linhas de exemplo"}),
            (("--by",), {"metavar": "COL", "help": "contagens agrupadas por coluna"}),
        ],
    ),
    _Cmd(
        "db-check",
        "M1",
        "sanidade do SQLite: DDL, WAL e busca FTS sem acento ('coracao' acha 'coração')",
        [(("--query",), {"metavar": "Q", "help": "termo de teste do FTS5"})],
    ),
    _Cmd(
        "make-seed",
        "M5",
        "s07: amostra-semente estratificada (fonte x tamanho) para rotulagem",
        [
            (("--total",), {"type": int, "metavar": "N", "help": "sobrescreve [labeling] seed_total"}),
            _FORCE,
        ],
    ),
    _Cmd(
        "labels",
        "M5",
        "gera/inspeciona os lotes de rotulagem em labeling/batches/ e o manifest",
        [
            (("action",), {"nargs": "?", "choices": ["make", "status", "verify"], "help": "operação sobre os lotes"}),
            (("--batch",), {"metavar": "ID", "help": "restringe a um lote (ex.: batch_0007)"}),
        ],
    ),
    _Cmd(
        "merge-labels",
        "M5",
        "s08: consolida rótulos dos agentes + rótulos nativos mapeados",
        [(("--strict",), {"action": "store_true", "help": "falha se algum lote estiver incompleto"})],
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
        [(("--swap",), {"action": "store_true", "help": "troca o banco vivo pelo recém-construído"})],
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
    """UTF-8 e HF_HOME antes de tudo. Idempotente e à prova de falha."""
    os.environ.setdefault("PYTHONUTF8", "1")
    try:
        from .config import hf_home

        os.environ.setdefault("HF_HOME", hf_home())
    except (OSError, KeyError, ValueError) as exc:
        # settings.toml ausente, ilegível ou malformado (TOMLDecodeError é
        # ValueError) não pode derrubar um `pf --help`. Avisa e segue.
        print(f"[pf] aviso: HF_HOME não configurado ({exc})", file=sys.stderr)
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
