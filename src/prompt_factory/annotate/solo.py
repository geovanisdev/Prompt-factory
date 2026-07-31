"""MODO SOLO — uma pessoa, os três chapéus.

``[annotate] permitir_autorrevisao``. Desligado (o default), a plataforma se
comporta como uma operação com equipe:

* cada persona tem UM papel, e as rotas de outro papel recusam com 403;
* **o revisor não vê as próprias submissões** — a regra de QC sem a qual a taxa
  de aprovação do painel do admin não mede coisa nenhuma.

Ligado, ela se comporta como uma bancada de uma pessoa só. **As duas coisas
acima caem juntas, e caem pela mesma razão**, que é o ponto deste módulo:

Quem monta o próprio portfólio anota, revisa e administra — a mesma pessoa. Se
só a segunda regra caísse, o caminho continuaria travado de um jeito pior: ao
trocar para o papel de revisor, a interface teria de vestir OUTRA persona (uma
das fixtures), e o trabalho do autor apareceria revisado por "Diego Prado". Isso
não é uma limitação técnica, é uma atribuição falsa — e atribuição falsa num
portfólio é exatamente o defeito que este projeto inteiro existe para não ter.

Então, no modo solo, **o papel deixa de ser autorização e passa a ser vista**: a
mesma persona atravessa as três telas, e quem responde por cada linha do banco
continua sendo ela.

NADA DISSO ACONTECE EM SILÊNCIO
===============================
* a fila de revisão carrega uma faixa permanente ("modo solo: você está
  revisando trabalho seu"), que não se fecha;
* cada item da fila diz se é seu;
* ``revisoes.autorrevisao`` e ``avaliacoes.autorrevisao`` guardam a marca, para
  o export e o painel poderem declarar o número separado — uma taxa de aprovação
  calculada sobre autorrevisão não é a mesma coisa que uma calculada sobre
  revisão cruzada, e apresentar as duas com o mesmo nome seria a mentira que
  este projeto não conta;
* ``/api/health`` e ``pf annotate status`` dizem qual dos dois modos está ativo.
"""

from __future__ import annotations

from ..config import get as _cfg


def ligado() -> bool:
    """``[annotate] permitir_autorrevisao`` — lido na hora, não no import.

    É a chave que um usuário mexe entre duas subidas para ver o efeito na tela,
    e a tela precisa concordar com o servidor dentro do mesmo request.
    """
    return bool(_cfg("annotate", "permitir_autorrevisao", default=False))


#: A frase que a interface e a CLI mostram. Uma constante porque ela aparece em
#: três lugares e os três precisam dizer a MESMA coisa.
AVISO = (
    "modo solo: uma pessoa nos três papéis. O papel é uma vista, não uma "
    "autorização, e cada revisão fica gravada como autorrevisão."
)

AVISO_DESLIGADO = "quem revisa nunca é quem anotou"


__all__ = ["AVISO", "AVISO_DESLIGADO", "ligado"]
