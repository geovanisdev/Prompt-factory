"""lmsys — `lmsys/lmsys-chat-1m`, DESLIGADA por padrão (esqueleto opt-in).

É o maior pool de português que existe fora do WildChat (pt é o 2º idioma do
dataset), e mesmo assim fica fora do corpus por três motivos independentes:

1. **Gated.** Exige `hf auth login`, aceite do *LMSYS-Chat-1M Dataset License
   Agreement* no site do Hub e um e-mail de contato. Não dá para automatizar.
2. **A licença PROÍBE redistribuir.** No `sources.toml` isto está codificado
   como `redistributable = false`, e o `pf export` (M9) exclui essas fontes por
   padrão — habilitar aqui só faz sentido para uso estritamente local.
3. **Texto anonimizado destrutivamente.** Nomes próprios viram `NAME_1`,
   `NAME_2`... Para um banco de *prompts reais* isso é ruído permanente, não
   PII removida (o s03 faria a remoção do nosso jeito, reversível na origem).

Com `enabled = false` este módulo faz **zero rede**: imprime o aviso e sai com
código 2. O corpo do download fica documentado abaixo para quem ligar a fonte um
dia — mas ele levanta `NotImplementedError` atrás do guard, de propósito: um
esqueleto que "quase funciona" é como se baixa 1M de linhas sem querer.

Forma prevista (verificada no card do Hub, para não se descobrir na hora):

* colunas: `conversation_id`, `model`, `conversation` (list<{role, content}>),
  `turn`, `language`, `openai_moderation`, `redacted`;
* **não existem `country` nem `timestamp`** — as colunas `country`/`created_ts`
  do `RAW_SCHEMA` ficariam `""` (nunca nulas), e o s01 tem de tolerar isso;
* filtro `language == "Portuguese"` (por extenso, como no aya e no WildChat);
* `source_id = conversation_id`; `model_family = model` cru;
* `nsfw_hint` derivável de `openai_moderation` (aqui existe de verdade, ao
  contrário do WildChat-4.8M, onde `toxic` é constante False);
* extração idêntica à do WildChat: primeiro turno com `role == "user"` —
  reusar `wildchat.first_user_text`, não reescrever.
"""

from __future__ import annotations

import argparse
from collections.abc import Iterator, Mapping
from typing import Any

SOURCE = "lmsys"
SPLIT = "train"
LANGUAGE = "Portuguese"
NEEDED = (
    "conversation_id",
    "model",
    "conversation",
    "turn",
    "language",
    "openai_moderation",
    "redacted",
)

_AVISO = """\
[lmsys] fonte DESLIGADA em config/sources.toml (enabled = false). Nada foi baixado.
[lmsys] Para ligar (uso estritamente LOCAL), nesta ordem:
[lmsys]   1. aceite o "LMSYS-Chat-1M Dataset License Agreement" em
[lmsys]      https://huggingface.co/datasets/lmsys/lmsys-chat-1m (pede e-mail de contato);
[lmsys]   2. `hf auth login` com uma conta que teve o acesso aprovado;
[lmsys]   3. troque enabled = false por true na seção [lmsys] do config/sources.toml.
[lmsys] Antes disso, saiba o que muda:
[lmsys]   * a licença PROIBE redistribuir o texto -> redistributable = false, e o
[lmsys]     `pf export` sempre exclui a fonte (só entra com --include-nc no M9);
[lmsys]   * os nomes próprios vêm anonimizados como NAME_1/NAME_2 e NÃO dá para desfazer;
[lmsys]   * a fonte não tem country nem timestamp: essas colunas saem vazias no raw."""


def iter_rows(cfg: Mapping[str, Any], max_rows: int | None = None) -> Iterator[dict[str, Any]]:
    """Ingester real — deliberadamente NÃO implementado (ver docstring do módulo)."""
    raise NotImplementedError(
        "lmsys: ingester previsto mas não implementado. Ligar a fonte exige aceitar "
        "os termos gated e assumir redistributable=false; a implementação sai do "
        "esqueleto quando (e se) alguém precisar dela localmente."
    )


def run_ingest(source_name: str, cfg: Mapping[str, Any], args: argparse.Namespace) -> int:
    """`pf ingest lmsys` — imprime o aviso e sai 2 enquanto `enabled = false`."""
    if not cfg.get("enabled", False):
        print(_AVISO)
        return 2
    # Guard passado: quem chegou aqui editou o toml de propósito.
    raise NotImplementedError(
        "[lmsys] enabled = true, mas o ingester ainda é esqueleto. "
        "Implemente iter_rows() (o shape previsto está na docstring do módulo) "
        "e lembre que a licença proíbe redistribuir o resultado."
    )


__all__ = ["LANGUAGE", "NEEDED", "SOURCE", "SPLIT", "iter_rows", "run_ingest"]
