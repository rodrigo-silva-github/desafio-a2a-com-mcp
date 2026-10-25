#!/usr/bin/env python3
"""Servidor MCP "Central de Salas" — Streamable HTTP (2026-07-28).

Expoe tres tools (listar_salas, consultar_disponibilidade, reservar_sala) e um
resource (politica://uso), com o ciclo completo de MRTR na reserva:
input_required com elicitation em form mode e requestState assinado, protegido
pela chave REQUEST_STATE_SECRET (>= 32 bytes) e com expiracao em 15 minutos.

Endpoint unico em http://127.0.0.1:7301/mcp (default; HOST/PORT configuram).

Subir:
    export REQUEST_STATE_SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
    python3 servidor-mcp/server.py
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo

from mcp.server.mcpserver import (
    AcceptedElicitation,
    CancelledElicitation,
    DeclinedElicitation,
    Elicit,
    ElicitationResult,
    MCPServer,
    RequestStateSecurity,
    Resolve,
)
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field, create_model

FUSO_SP = ZoneInfo("America/Sao_Paulo")

RAIZ = Path(__file__).resolve().parent.parent
ARQUIVO_SALAS = RAIZ / "dados" / "salas.json"
ARQUIVO_RESERVAS = RAIZ / "dados" / "reservas.json"
ARQUIVO_POLITICA = RAIZ / "dados" / "politica-de-uso.md"

JANELA_INICIO = 8  # 08:00 horario de Sao Paulo
JANELA_FIM = 20  # 20:00
DURACAO_MAXIMA = timedelta(hours=2)
TTL_REQUEST_STATE = 15 * 60  # 15 minutos (faixa exigida: 5 a 30 min)

# Mensagens exatas exigidas pelo enunciado (fonte unica de verdade).
MSG_SALA = "Sala inexistente: {id}"
MSG_JANELA = "Fora da janela de uso: a politica permite reservas entre 08:00 e 20:00"
MSG_DURACAO = "Duracao acima do limite: a politica permite no maximo 2 horas"
MSG_INTERVALO = "Intervalo invalido: fim deve ser posterior a inicio"
MSG_SEM_ALTERNATIVA = "Sem alternativas disponiveis no intervalo"
MSG_ELICITACAO = "A sala pedida esta ocupada nesse intervalo. Escolha uma alternativa."


# ---------------------------------------------------------------------------
# Modelos de saida (os titles batem com exemplos/wire/01-tools-list.json)
# ---------------------------------------------------------------------------


class SalaOut(BaseModel):
    id: str
    nome: str
    capacidade: int
    recursos: list[str]


class ListaDeSalas(BaseModel):
    salas: list[SalaOut]


class ConflitoOut(BaseModel):
    id: str
    inicio: str
    fim: str
    responsavel: str


class Disponibilidade(BaseModel):
    sala: str
    livre: bool
    conflitos: list[ConflitoOut]


class ReservaOut(BaseModel):
    reserva: str | None = None
    reservado: bool = True
    sala: str | None = None
    inicio: str | None = None
    fim: str | None = None
    responsavel: str | None = None
    politica: str | None = None
    motivo: str | None = None


class EscolhaSala(BaseModel):
    """Modelo base da resposta a elicitation; o enum das alternativas e
    adicionado dinamicamente por chamada (depende do conflito)."""

    sala: str = Field(description="Sala alternativa escolhida")


# ---------------------------------------------------------------------------
# Estado em memoria (reservas nao precisam sobreviver a restart; o
# requestState, assinado e autocontido, e quem sobrevive)
# ---------------------------------------------------------------------------


def _carregar_salas() -> list[SalaOut]:
    return [SalaOut(**s) for s in json.loads(ARQUIVO_SALAS.read_text(encoding="utf-8"))]


SALAS: list[SalaOut] = _carregar_salas()


def _carregar_reservas() -> list[dict[str, Any]]:
    return json.loads(ARQUIVO_RESERVAS.read_text(encoding="utf-8"))


RESERVAS: list[dict[str, Any]] = _carregar_reservas()


def _proximo_id_reserva() -> str:
    maior = 0
    for r in RESERVAS:
        m = re.fullmatch(r"res-(\d+)", r["id"])
        if m:
            maior = max(maior, int(m.group(1)))
    return f"res-{maior + 1:04d}"


def _versao_politica() -> str:
    primeira = ARQUIVO_POLITICA.read_text(encoding="utf-8").splitlines()[0]
    m = re.match(r"versao:\s*(\S+)", primeira)
    if not m:
        raise RuntimeError(f"politica-de-uso.md nao declara a versao: {primeira!r}")
    return m.group(1)


VERSAO_POLITICA = _versao_politica()


# ---------------------------------------------------------------------------
# Regras de negocio (toda a politica vive no servidor; o agente nao decide nada)
# ---------------------------------------------------------------------------


def _parse_iso(valor: str) -> datetime:
    texto = valor[:-1] + "+00:00" if valor.endswith("Z") else valor
    dt = datetime.fromisoformat(texto)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=FUSO_SP)
    return dt


def _validar_pedido(sala: str, inicio: str, fim: str) -> tuple[datetime, datetime]:
    """Valida sala e politica na ordem que define qual mensagem de erro sai.

    Levanta ToolError com a mensagem exata do enunciado. Retorna o intervalo
    como datetimes aware.
    """
    if not any(s.id == sala for s in SALAS):
        raise ToolError(MSG_SALA.format(id=sala))
    dt_inicio = _parse_iso(inicio)
    dt_fim = _parse_iso(fim)
    local_inicio = dt_inicio.astimezone(FUSO_SP)
    local_fim = dt_fim.astimezone(FUSO_SP)
    if not (JANELA_INICIO <= local_inicio.hour < JANELA_FIM and JANELA_INICIO <= local_fim.hour < JANELA_FIM):
        raise ToolError(MSG_JANELA)
    if dt_fim - dt_inicio > DURACAO_MAXIMA:
        raise ToolError(MSG_DURACAO)
    if dt_fim <= dt_inicio:
        raise ToolError(MSG_INTERVALO)
    return dt_inicio, dt_fim


def _sobrepoe(a_inicio: datetime, a_fim: datetime, b_inicio: datetime, b_fim: datetime) -> bool:
    return a_inicio < b_fim and b_inicio < a_fim


def _conflitos(sala: str, dt_inicio: datetime, dt_fim: datetime) -> list[dict[str, Any]]:
    return [
        r
        for r in RESERVAS
        if r["sala"] == sala
        and _sobrepoe(dt_inicio, dt_fim, _parse_iso(r["inicio"]), _parse_iso(r["fim"]))
    ]


def _livre_no_intervalo(sala_id: str, dt_inicio: datetime, dt_fim: datetime) -> bool:
    return not _conflitos(sala_id, dt_inicio, dt_fim)


def _alternativas(sala: str, dt_inicio: datetime, dt_fim: datetime) -> list[str]:
    """Salas livres no intervalo, capacidade >= a da sala pedida, no maximo 3,
    por capacidade crescente e, em empate, por id em ordem alfabetica."""
    pedida = next(s for s in SALAS if s.id == sala)
    candidatas = [
        s
        for s in SALAS
        if s.capacidade >= pedida.capacidade and s.id != sala and _livre_no_intervalo(s.id, dt_inicio, dt_fim)
    ]
    candidatas.sort(key=lambda s: (s.capacidade, s.id))
    return [s.id for s in candidatas[:3]]


def _criar_escolha_modelo(alternativas: list[str]) -> type[EscolhaSala]:
    """Modelo com o campo sala restrito (Literal/enum) as alternativas do conflito."""
    return create_model(
        "EscolhaSala",
        __base__=EscolhaSala,
        sala=(Literal[tuple(alternativas)], Field(description="Sala alternativa escolhida")),
    )


# ---------------------------------------------------------------------------
# Ferramentas
# ---------------------------------------------------------------------------


async def listar_salas() -> ListaDeSalas:
    """Lista todas as salas com capacidade e recursos."""
    return ListaDeSalas(salas=SALAS)


async def consultar_disponibilidade(sala: str, inicio: str, fim: str) -> Disponibilidade:
    """Diz se uma sala esta livre no intervalo, e quais reservas conflitam."""
    dt_inicio, dt_fim = _validar_pedido(sala, inicio, fim)
    conflitos = _conflitos(sala, dt_inicio, dt_fim)
    return Disponibilidade(
        sala=sala,
        livre=not conflitos,
        conflitos=[
            ConflitoOut(id=c["id"], inicio=c["inicio"], fim=c["fim"], responsavel=c["responsavel"]) for c in conflitos
        ],
    )


async def escolha_de_sala(sala: str, inicio: str, fim: str) -> Elicit[EscolhaSala] | EscolhaSala:
    """Resolver: pede a escolha de alternativa somente quando ha conflito.

    Caminho feliz -> retorna a propria sala (sem pergunta). Conflito com
    alternativas -> Elicit em form mode. Conflito sem alternativa -> erro de
    execucao com a mensagem exata.
    """
    dt_inicio, dt_fim = _validar_pedido(sala, inicio, fim)
    if not _conflitos(sala, dt_inicio, dt_fim):
        return EscolhaSala(sala=sala)
    alternativas = _alternativas(sala, dt_inicio, dt_fim)
    if not alternativas:
        raise ToolError(MSG_SEM_ALTERNATIVA)
    return Elicit(MSG_ELICITACAO, _criar_escolha_modelo(alternativas))


async def reservar_sala(
    sala: str,
    inicio: str,
    fim: str,
    responsavel: str,
    escolha: Annotated[ElicitationResult[EscolhaSala], Resolve(escolha_de_sala)],
) -> ReservaOut:
    """Reserva uma sala. Se o intervalo estiver ocupado, pergunta qual alternativa usar."""
    if isinstance(escolha, (DeclinedElicitation, CancelledElicitation)):
        return ReservaOut(reservado=False, motivo="recusado")
    sala_final = escolha.data.sala
    dt_inicio, dt_fim = _validar_pedido(sala_final, inicio, fim)
    if not _livre_no_intervalo(sala_final, dt_inicio, dt_fim):
        # Defensivo (o enum da elicitation so lista salas livres); nunca sobrepoe.
        raise ToolError(MSG_SEM_ALTERNATIVA)
    reserva = {
        "id": _proximo_id_reserva(),
        "sala": sala_final,
        "inicio": inicio,
        "fim": fim,
        "responsavel": responsavel,
    }
    RESERVAS.append(reserva)
    return ReservaOut(
        reserva=reserva["id"],
        reservado=True,
        sala=sala_final,
        inicio=inicio,
        fim=fim,
        responsavel=responsavel,
        politica=VERSAO_POLITICA,
    )


# ---------------------------------------------------------------------------
# Middleware de log: metodo, id e traceparent de cada request, no stderr
# ---------------------------------------------------------------------------


async def log_middleware(ctx: Any, call_next: Any):
    meta = (ctx.params or {}).get("_meta") if isinstance(ctx.params, dict) else {}
    traceparent = (meta or {}).get("traceparent") or "-"
    print(f"request method={ctx.method} id={ctx.request_id} traceparent={traceparent}", file=sys.stderr, flush=True)
    return await call_next(ctx)


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------


def _segredo_request_state() -> str:
    segredo = os.environ.get("REQUEST_STATE_SECRET", "")
    if len(segredo.encode("utf-8")) < 32:
        print(
            "ERRO: REQUEST_STATE_SECRET ausente ou com menos de 32 bytes. Gere com: "
            'python3 -c "import secrets; print(secrets.token_hex(32))"',
            file=sys.stderr,
        )
        sys.exit(1)
    return segredo


def _criar_servidor() -> MCPServer:
    servidor = MCPServer(
        name="central-de-salas",
        version="1.0.0",
        request_state_security=RequestStateSecurity(keys=[_segredo_request_state()], ttl=TTL_REQUEST_STATE),
        middleware=[log_middleware],
    )

    @servidor.resource("politica://uso", mime_type="text/markdown")
    def politica() -> str:
        return ARQUIVO_POLITICA.read_text(encoding="utf-8")

    servidor.add_tool(listar_salas)
    servidor.add_tool(consultar_disponibilidade)
    servidor.add_tool(reservar_sala)
    return servidor


async def _main() -> None:
    import anyio

    host = os.environ.get("HOST", "127.0.0.1")
    porta = int(os.environ.get("PORT", "7301"))
    servidor = _criar_servidor()
    await servidor.run_streamable_http_async(
        host=host,
        port=porta,
        streamable_http_path="/mcp",
        json_response=True,
        stateless_http=True,
    )


if __name__ == "__main__":
    import anyio

    anyio.run(_main)
