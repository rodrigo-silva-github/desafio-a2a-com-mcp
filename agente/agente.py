#!/usr/bin/env python3
"""Agente A2A "Central de Salas" — host MCP por dentro, servidor A2A por fora.

Porta 7300 (default; HOST/PORT configuram):
  - GET  /.well-known/agent-card.json  -> Agent Card v1.0
  - POST /a2a                          -> JSON-RPC 2.0: SendMessage e GetTask

Por dentro, o agente e um cliente MCP de verdade (Streamable HTTP) que
descobre as tools por tools/list, le o resource politica://uso e chama o
servidor em http://127.0.0.1:7301/mcp (URL em MCP_URL).

A ponte: quando o servidor responde input_required, a Task A2A pausa em
TASK_STATE_INPUT_REQUIRED com a linha "alternativas: <ids>", o requestState
fica guardado apenas na memoria da Task, e a continuacao (escolha=<id>)
repite o tools/call com inputResponses + requestState, com id JSON-RPC novo.

Sem LLM: o mesmo pedido produz sempre o mesmo resultado.

Subir (depois do servidor MCP):
    python3 agente/agente.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import secrets
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from mcp import Client
from mcp.types import ErrorData, ElicitResult, Implementation

# ---------------------------------------------------------------------------
# Configuracao
# ---------------------------------------------------------------------------

HOST = os.environ.get("HOST", "127.0.0.1")
PORT = int(os.environ.get("PORT", "7300"))
AGENTE_BASE = os.environ.get("AGENTE_BASE", f"http://{HOST}:{PORT}")
RPC_URL = f"{AGENTE_BASE}/a2a"
MCP_URL = os.environ.get("MCP_URL", "http://127.0.0.1:7301/mcp")

NOME = "agente-central-de-salas"
VERSAO = "1.0.0"

_PEDIDO = re.compile(r"^reservar sala=(\S+) inicio=(\S+) fim=(\S+) responsavel=(.+)$")
_ESCOLHA = re.compile(r"^escolha=(.+)$")

# Estados A2A v1.0 (terminais sao definitivos).
SUBMITTED = "TASK_STATE_SUBMITTED"
WORKING = "TASK_STATE_WORKING"
INPUT_REQUIRED = "TASK_STATE_INPUT_REQUIRED"
COMPLETED = "TASK_STATE_COMPLETED"
CANCELED = "TASK_STATE_CANCELED"
FAILED = "TASK_STATE_FAILED"
TERMINAIS = {COMPLETED, CANCELED, FAILED}

# ---------------------------------------------------------------------------
# Estado das Tasks (em memoria; o requestState de cada Task vive aqui, preso
# ao id da Task, e nunca sai em resposta A2A)
# ---------------------------------------------------------------------------

TASKS: dict[str, dict[str, Any]] = {}


def _proximo(prefixo: str) -> str:
    return f"{prefixo}-{secrets.token_hex(6)}"


def _msg_json(role: str, texto: str, task_id: str, context_id: str) -> dict[str, Any]:
    return {
        "messageId": _proximo("msg"),
        "role": role,
        "parts": [{"text": texto}],
        "taskId": task_id,
        "contextId": context_id,
    }


def _criar_task() -> dict[str, Any]:
    task_id = _proximo("task")
    context_id = _proximo("ctx")
    return {
        "id": task_id,
        "contextId": context_id,
        "state": SUBMITTED,
        "history": [],
        "artifacts": [],
        # dados privados da ponte (nunca expostos em respostas A2A):
        "args": None,
        "chave": None,
        "request_state": None,
        "enum": None,
        "traceparent": None,
    }


def _task_json(task: dict[str, Any]) -> dict[str, Any]:
    historico = list(task["history"])
    status_msg = None
    if historico and historico[-1]["role"] == "ROLE_AGENT":
        status_msg = historico[-1]
    status: dict[str, Any] = {"state": task["state"]}
    if status_msg is not None:
        status["message"] = status_msg
    return {
        "id": task["id"],
        "contextId": task["contextId"],
        "status": status,
        "history": historico,
        "artifacts": task["artifacts"],
    }


def _erro(codigo: int, mensagem: str) -> dict[str, Any]:
    return {"error": {"code": codigo, "message": mensagem}}


# ---------------------------------------------------------------------------
# Cliente MCP (host) — roda num loop asyncio proprio, vivo por todo o processo
# ---------------------------------------------------------------------------

_loop: asyncio.AbstractEventLoop | None = None
_client: Client | None = None
_versao_politica: str = ""


def _novo_traceparent(trace_id: str) -> str:
    return f"00-{trace_id}-{secrets.token_hex(8)}-01"


def _trace_id_de(traceparent: str | None) -> str:
    if traceparent and traceparent.startswith("00-") and len(traceparent.split("-")) >= 4:
        return traceparent.split("-")[1]
    return secrets.token_hex(16)


def _meta_trace(traceparent: str | None) -> dict[str, str]:
    return {"traceparent": _novo_traceparent(_trace_id_de(traceparent))}


def _run_coro(coro: Any, timeout: float = 60.0) -> Any:
    assert _loop is not None
    futuro = asyncio.run_coroutine_threadsafe(coro, _loop)
    return futuro.result(timeout=timeout)


async def _elicitation_dummy(ctx: Any, params: Any) -> ErrorData:
    """Nunca e chamado: o agente dirige o ciclo de input_required manualmente
    (session.call_tool com allow_input_required=True) para enxergar o
    input_required cru e pausar a Task A2A. Existe apenas para declarar a
    capability de elicitation em form mode, que a spec exige."""
    return ErrorData(code=-32601, message="elicitation e resolvida pela Task A2A")


async def _conectar() -> Client:
    cliente = Client(
        MCP_URL,
        client_info=Implementation(name=NOME, version=VERSAO),
        mode="2026-07-28",
        elicitation_callback=_elicitation_dummy,
        input_required_max_rounds=1,
    )
    await cliente.__aenter__()
    return cliente


async def _descoberta() -> tuple[list[str], str]:
    """tools/list antes da primeira chamada + versao da politica (primeira
    linha do resource). A lista de tools nunca fica fixa no codigo."""
    tools = await _client.list_tools()  # type: ignore[union-attr]
    resultado = await _client.read_resource("politica://uso")  # type: ignore[union-attr]
    texto = "".join(getattr(p, "text", "") for p in (resultado.contents or []))
    primeira = (texto.splitlines() or [""])[0]
    m = re.match(r"versao:\s*(\S+)", primeira)
    versao = m.group(1) if m else ""
    return [t.name for t in tools.tools], versao


def _startup() -> None:
    global _loop, _client, _versao_politica
    _loop = asyncio.new_event_loop()
    threading.Thread(target=_loop.run_forever, daemon=True, name="loop-mcp").start()

    print(f"agente: conectando ao servidor MCP em {MCP_URL} ...", file=sys.stderr, flush=True)
    for tentativa in range(30):
        try:
            _client = _run_coro(_conectar())
            break
        except Exception as exc:  # noqa: BLE001
            print(f"agente: tentativa {tentativa + 1} falhou ({exc}); aguardando...", file=sys.stderr, flush=True)
            time.sleep(2)
    if _client is None:
        print("agente: nao consegui conectar ao servidor MCP.", file=sys.stderr, flush=True)
        sys.exit(1)

    nomes, _versao_politica = _run_coro(_descoberta())
    print(f"agente: conectado. tools descobertas: {nomes}; politica: {_versao_politica or '(vazia)'}", file=sys.stderr, flush=True)


# ---------------------------------------------------------------------------
# Maquina da ponte: pedido fixo -> tools/call -> Task
# ---------------------------------------------------------------------------


async def _chamar_mcp(
    args: dict[str, Any],
    traceparent: str | None,
    input_responses: dict[str, Any] | None = None,
    request_state: str | None = None,
) -> Any:
    """Um tools/call com _meta completo (version, capabilities e traceparent).
    Com allow_input_required=True o agente recebe o InputRequiredResult cru,
    sem callback: e ele quem decide pausar a Task."""
    return await _client.session.call_tool(  # type: ignore[union-attr]
        "reservar_sala",
        args,
        input_responses=input_responses,
        request_state=request_state,
        meta=_meta_trace(traceparent),
        allow_input_required=True,
    )


async def _executar_pedido(task: dict[str, Any], texto: str, traceparent: str | None) -> None:
    """Caminho inicial de uma Task nova (SendMessage sem taskId)."""
    m = _PEDIDO.match(texto.strip())
    if not m:
        task["state"] = FAILED
        task["history"].append(_msg_json("ROLE_AGENT", "Formato invalido. Use: reservar sala=<id> inicio=<iso8601> fim=<iso8601> responsavel=<nome>", task["id"], task["contextId"]))
        return
    args = {"sala": m.group(1), "inicio": m.group(2), "fim": m.group(3), "responsavel": m.group(4)}
    task["args"] = args
    task["traceparent"] = traceparent

    resultado = await _chamar_mcp(args, traceparent)
    _aplicar_resultado_mcp(task, resultado)


async def _continuar_task(task: dict[str, Any], texto: str, traceparent: str | None) -> None:
    """Continuacao de uma Task pausada (SendMessage com taskId)."""
    m = _ESCOLHA.match(texto.strip())
    if not m:
        task["state"] = INPUT_REQUIRED
        task["history"].append(_msg_json("ROLE_USER", texto.strip(), task["id"], task["contextId"]))
        task["history"].append(_msg_json("ROLE_AGENT", f"alternativas: {', '.join(task['enum'])}", task["id"], task["contextId"]))
        return
    escolha = m.group(1).strip()

    if escolha == "recusar":
        acao: dict[str, Any] = {"action": "decline"}
    elif escolha in task["enum"]:
        acao = {"action": "accept", "content": {"sala": escolha}}
    else:
        # Fora do enum: mantem a pausa e repete a lista de alternativas.
        task["state"] = INPUT_REQUIRED
        task["history"].append(_msg_json("ROLE_USER", texto.strip(), task["id"], task["contextId"]))
        task["history"].append(_msg_json("ROLE_AGENT", f"alternativas: {', '.join(task['enum'])}", task["id"], task["contextId"]))
        return

    if task["traceparent"] is None:
        task["traceparent"] = traceparent

    resultado = await _chamar_mcp(
        task["args"],
        task["traceparent"],
        input_responses={task["chave"]: acao},
        request_state=task["request_state"],
    )
    _aplicar_resultado_mcp(task, resultado)


def _aplicar_resultado_mcp(task: dict[str, Any], resultado: Any) -> None:
    """Traduz o resultado do MCP para a maquina de estados da Task (a ponte)."""
    if type(resultado).__name__ == "InputRequiredResult":
        _pausar(task, resultado)
        return
    if getattr(resultado, "is_error", False):
        texto = " ".join(getattr(p, "text", "") for p in (resultado.content or []))
        task["state"] = FAILED
        task["history"].append(_msg_json("ROLE_AGENT", texto, task["id"], task["contextId"]))
        return

    dados = dict(resultado.structured_content or {})
    if dados.get("reservado") is False:
        task["state"] = CANCELED
        task["history"].append(_msg_json("ROLE_AGENT", "Reserva recusada.", task["id"], task["contextId"]))
        return

    task["state"] = COMPLETED
    reserva_id = dados.get("reserva") or ""
    sala = dados.get("sala") or ""
    task["history"].append(
        _msg_json("ROLE_AGENT", f"Reserva {reserva_id} confirmada na sala-{sala}.", task["id"], task["contextId"])
    )
    artifact_text = json.dumps(
        {
            "reserva": reserva_id,
            "sala": sala,
            "inicio": dados.get("inicio"),
            "fim": dados.get("fim"),
            "responsavel": dados.get("responsavel"),
            "politica": dados.get("politica") or _versao_politica,
        },
        ensure_ascii=False,
    )
    task["artifacts"].append(
        {
            "artifactId": _proximo("art"),
            "name": "reserva",
            "parts": [{"text": artifact_text}],
        }
    )


def _pausar(task: dict[str, Any], resultado: Any) -> None:
    """input_required do MCP vira TASK_STATE_INPUT_REQUIRED.
    O requestState fica guardado SÓ aqui, preso a Task (nunca e ecoado)."""
    pedidos = resultado.input_requests or {}
    chave, request = next(iter(pedidos.items()))
    wire = request.model_dump(mode="json", by_alias=True, exclude_none=True)
    schema = ((wire.get("params") or {}).get("requestedSchema")) or {}
    prop = ((schema.get("properties") or {}).get("sala")) or {}
    alternativas = list(prop.get("enum") or (([prop["const"]]) if "const" in prop else []))

    task["state"] = INPUT_REQUIRED
    task["chave"] = chave
    task["request_state"] = resultado.request_state
    task["enum"] = alternativas
    task["history"].append(_msg_json("ROLE_AGENT", f"alternativas: {', '.join(alternativas)}", task["id"], task["contextId"]))


# ---------------------------------------------------------------------------
# Servidor A2A (HTTP puro, JSON-RPC 2.0)
# ---------------------------------------------------------------------------

CARD: dict[str, Any] = {
    "name": "Central de Salas",
    "description": "Reserva salas de reuniao da Hill Valley Tech.",
    "provider": {"organization": "Hill Valley Tech", "url": "https://hillvalley.example"},
    "version": "1.0.0",
    "supportedInterfaces": [
        {
            "url": RPC_URL,
            "protocolBinding": "JSONRPC",
            "protocolVersion": "1.0",
        }
    ],
    "capabilities": {
        "streaming": False,
        "pushNotifications": False,
        "extendedAgentCard": False,
    },
    "defaultInputModes": ["text/plain"],
    "defaultOutputModes": ["text/plain"],
    "skills": [
        {
            "id": "reservar-sala",
            "name": "Reservar sala",
            "description": "Reserva uma sala em um intervalo. Se houver conflito, pergunta qual alternativa usar.",
            "tags": ["salas", "agenda"],
            "inputModes": ["text/plain"],
            "outputModes": ["text/plain"],
            "examples": [
                "reservar sala=sala-garagem inicio=2026-11-03T14:00:00-03:00 fim=2026-11-03T15:00:00-03:00 responsavel=Marty"
            ],
        }
    ],
}


def _send_message(params: dict[str, Any], traceparent: str | None) -> dict[str, Any]:
    mensagem = params.get("message") or {}
    task_id = (mensagem.get("taskId") or "").strip()
    texto = "".join(p.get("text", "") for p in (mensagem.get("parts") or []))

    if not task_id:
        task = _criar_task()
        task["history"].append(_msg_json("ROLE_USER", texto, task["id"], task["contextId"]))
        TASKS[task["id"]] = task
        task["state"] = SUBMITTED
        task["state"] = WORKING
        try:
            _run_coro(_executar_pedido(task, texto, traceparent))
        except Exception as exc:  # noqa: BLE001
            task["state"] = FAILED
            task["history"].append(_msg_json("ROLE_AGENT", f"Erro interno: {exc}", task["id"], task["contextId"]))
    else:
        task = TASKS.get(task_id)
        if task is None:
            return _erro(-32602, f"Task desconhecida: {task_id}")
        if task["state"] in TERMINAIS:
            return _erro(-32602, f"Task em estado terminal: {task['state']}")
        if task["state"] != INPUT_REQUIRED:
            return _erro(-32602, f"Task nao esta aguardando input: {task['state']}")
        task["state"] = WORKING
        try:
            _run_coro(_continuar_task(task, texto, traceparent))
        except Exception as exc:  # noqa: BLE001
            task["state"] = FAILED
            task["history"].append(_msg_json("ROLE_AGENT", f"Erro interno: {exc}", task["id"], task["contextId"]))

    return {"result": {"task": _task_json(task)}}


def _get_task(params: dict[str, Any]) -> dict[str, Any]:
    task = TASKS.get(str(params.get("id", "")))
    if task is None:
        return _erro(-32602, "Task nao encontrada")
    return {"result": {"task": _task_json(task)}}


def _dispatch(corpo: dict[str, Any], headers: Any) -> dict[str, Any]:
    if corpo.get("jsonrpc") != "2.0" or not isinstance(corpo.get("method"), str):
        return {"jsonrpc": "2.0", "id": corpo.get("id"), **_erro(-32600, "Invalid Request")}
    metodo = corpo["method"]
    params = corpo.get("params") or {}
    # self.headers e um email.message.Message: o get e insensivel a caixa
    # (o case do header chega variando: urllib envia "Traceparent", curl "traceparent").
    traceparent = str(headers.get("traceparent", "")) or None

    if metodo == "SendMessage":
        resultado = _send_message(params, traceparent)
    elif metodo == "GetTask":
        resultado = _get_task(params)
    else:
        resultado = _erro(-32601, f"Metodo nao suportado: {metodo}")

    return {"jsonrpc": "2.0", "id": corpo.get("id"), **resultado}


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:  # silencioso
        return

    def _json(self, status: int, corpo: dict[str, Any]) -> None:
        dados = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(dados)))
        self.end_headers()
        self.wfile.write(dados)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/.well-known/agent-card.json":
            self._json(200, CARD)
        else:
            self._json(404, {"error": {"code": -32601, "message": "Not Found"}})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/a2a":
            self._json(404, {"error": {"code": -32601, "message": "Not Found"}})
            return
        try:
            tamanho = int(self.headers.get("Content-Length") or 0)
            corpo = json.loads(self.rfile.read(tamanho) or b"{}")
        except json.JSONDecodeError:
            self._json(400, {"jsonrpc": "2.0", "id": None, **_erro(-32700, "Parse error")})
            return
        self._json(200, _dispatch(corpo, self.headers))


def _main() -> None:
    _startup()
    servidor_http = ThreadingHTTPServer((HOST, PORT), _Handler)
    print(f"agente: A2A pronto em {AGENTE_BASE} (RPC em {RPC_URL})", file=sys.stderr, flush=True)
    try:
        servidor_http.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        servidor_http.server_close()


if __name__ == "__main__":
    _main()
