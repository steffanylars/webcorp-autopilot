"""
Test del orquestador SIN necesitar la API de Qwen: un cliente simulado
reproduce el protocolo exacto de function calling (tool_calls -> tool
results -> respuesta final). Prueba la logica completa del loop.

Correr:  python test_agente_mock.py
"""
import os
import json

# Aislar artefactos del test
os.environ["AUDIT_LOG_PATH"] = "/tmp/test_audit.jsonl"
os.environ["PENDIENTES_DB_PATH"] = "/tmp/test_pendientes.sqlite"
os.environ["DEDUPE_DB_PATH"] = "/tmp/test_dedupe.sqlite"
os.environ["ARTEFACTOS_DIR"] = "/tmp/test_artefactos"
# Backend muerto a proposito: las tools con fallback deben usar el
# dataset sintetico — el test es deterministico, no depende de MySQL.
os.environ["WEBCORP_BACKEND_URL"] = "http://localhost:1"
for f in ["/tmp/test_audit.jsonl", "/tmp/test_pendientes.sqlite", "/tmp/test_dedupe.sqlite"]:
    if os.path.exists(f):
        os.remove(f)
import glob, shutil
if os.path.isdir("/tmp/test_artefactos"):
    shutil.rmtree("/tmp/test_artefactos")

import agente


# --- Cliente simulado que habla el protocolo OpenAI/Qwen ---
class _Func:
    def __init__(self, name, arguments):
        self.name, self.arguments = name, arguments

class _ToolCall:
    def __init__(self, id, name, arguments):
        self.id, self.type = id, "function"
        self.function = _Func(name, arguments)

class _Msg:
    def __init__(self, content=None, tool_calls=None):
        self.content, self.tool_calls = content, tool_calls

class _Choice:
    def __init__(self, msg):
        self.message = msg

class _Resp:
    def __init__(self, msg):
        self.choices = [_Choice(msg)]


class ClienteSimulado:
    """Guion: (1) pide estimador_wilson, (2) pide notificar, (3) redacta final."""
    def __init__(self):
        self.turno = 0
        self.chat = self
        self.completions = self

    def create(self, model, messages, tools, **kw):
        self.turno += 1
        if self.turno == 1:
            return _Resp(_Msg(tool_calls=[
                _ToolCall("c1", "estimador_wilson",
                          json.dumps({"carrier": "FORZA", "min_n": 30, "top": 3}))
            ]))
        if self.turno == 2:
            # verifica que recibio el resultado de la tool en messages
            assert any(m.get("role") == "tool" for m in messages), \
                "el loop no devolvio el resultado de la tool a Qwen"
            return _Resp(_Msg(tool_calls=[
                _ToolCall("c2", "notificar_operaciones", json.dumps({
                    "asunto": "FORZA critico en Escuintla",
                    "mensaje": "Wilson 24.9 con n=39, muy por debajo del promedio nacional de FORZA (54.7%).",
                    "origen_id": "wilson#Escuintla-FORZA",
                }))
            ]))
        return _Resp(_Msg(content=(
            "FORZA en Nueva Concepcion, Escuintla es el peor par con evidencia "
            "suficiente (Wilson 24.9, n=39). Propuse notificar a operaciones; "
            "quedo pendiente de tu aprobacion."
        )))


def main():
    print("=== TEST 1: loop completo con HITL ===")
    r = agente.responder("Por que FORZA esta mal en Escuintla?", client=ClienteSimulado())

    assert not r["fallback"], "no debio activar fallback"
    assert [t["tool"] for t in r["tools_usadas"]] == ["estimador_wilson"], \
        f"tools ejecutadas incorrectas: {r['tools_usadas']}"
    assert len(r["pendientes"]) == 1, "notificar debio quedar PENDIENTE, no ejecutarse"
    assert "pendiente" in r["respuesta"].lower()
    print("OK — estimador ejecutado, notificacion NO ejecutada (en cola HITL)")

    print("\n=== TEST 2: la cola de pendientes persiste y se aprueba ===")
    pend = agente.listar_pendientes()
    assert len(pend) == 1 and pend[0]["tool"] == "notificar_operaciones"
    pid = pend[0]["id"]
    res = agente.resolver_pendiente(pid, aprobado=True)
    assert res["ok"] and res["ejecutado"], f"la aprobacion no ejecuto: {res}"
    assert agente.listar_pendientes() == [], "el pendiente no salio de la cola"
    print(f"OK — pendiente {pid} aprobado y ejecutado (canal demo)")

    print("\n=== TEST 3: doble aprobacion es rechazada ===")
    res2 = agente.resolver_pendiente(pid, aprobado=True)
    assert not res2["ok"], "no debe permitir aprobar dos veces el mismo pendiente"
    print("OK — segunda aprobacion del mismo id rechazada")

    print("\n=== TEST 4: fallback cuando Qwen no responde ===")
    class ClienteRoto:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    raise ConnectionError("Qwen Cloud caido (simulado)")
    r = agente.responder("hay algo raro en la operacion?", client=ClienteRoto())
    assert r["fallback"], "debio activar el fallback de reglas"
    assert "FALLBACK" in r["respuesta"]
    print("OK — sin LLM, las reglas respondieron igual")

    print("\n=== TEST 5: audit log registra la cadena completa ===")
    entradas = agente.leer_audit(50)
    tipos = [e["tipo"] for e in entradas]
    for esperado in ["pregunta", "tool_ejecutada", "hitl_pendiente",
                     "respuesta", "hitl_aprobado_ejecutado", "fallback_activado"]:
        assert esperado in tipos, f"falta '{esperado}' en el audit log: {tipos}"
    print(f"OK — {len(entradas)} entradas, cadena completa trazable")

    print("\n=== TEST 6: contexto externo bloqueado si es la primera tool ===")
    class ClienteContextoPrimero:
        """Guion malicioso: intenta buscar_contexto_externo SIN evidencia previa."""
        def __init__(self):
            self.turno = 0
            self.chat = self; self.completions = self
        def create(self, model, messages, tools, **kw):
            self.turno += 1
            if self.turno == 1:
                return _Resp(_Msg(tool_calls=[
                    _ToolCall("x1", "buscar_contexto_externo", json.dumps({
                        "zona": "Escuintla, Guatemala", "periodo": "junio 2026",
                        "alerta_confirmada": "(ninguna — es el primer paso)",
                    }))
                ]))
            # el loop debe haberle devuelto el bloqueo de la capa Trust
            bloqueos = [m for m in messages if m.get("role") == "tool"
                        and "Bloqueado por la capa Trust" in m.get("content", "")]
            assert bloqueos, "el guard no devolvio el bloqueo a Qwen"
            return _Resp(_Msg(content="Entendido, primero confirmo con datos internos."))
    r = agente.responder("por que esta mal Escuintla?", client=ClienteContextoPrimero())
    assert not r["fallback"]
    assert r["tools_usadas"] == [], f"no debio ejecutar ninguna tool: {r['tools_usadas']}"
    print("OK — sin evidencia interna previa, la capa Trust bloqueo la busqueda externa")

    print("\n=== TEST 7: contexto externo corre DESPUES de evidencia interna ===")
    import agent_tools
    def _contexto_fake(zona, periodo, alerta_confirmada):
        return {"hallazgo": "Reporte de bloqueo en CA-1 (simulado)",
                "fuentes": [{"titulo": "demo", "url": "https://ejemplo.gt/x", "sitio": "demo"}],
                "confianza": agent_tools.ETIQUETA_NO_VERIFICADO,
                "alerta_origen": alerta_confirmada}
    agent_tools.TOOL_HANDLERS["buscar_contexto_externo"] = _contexto_fake
    class ClienteEvidenciaLuegoContexto:
        def __init__(self):
            self.turno = 0
            self.chat = self; self.completions = self
        def create(self, model, messages, tools, **kw):
            self.turno += 1
            if self.turno == 1:
                return _Resp(_Msg(tool_calls=[
                    _ToolCall("e1", "estimador_wilson",
                              json.dumps({"carrier": "FORZA", "min_n": 30, "top": 1}))
                ]))
            if self.turno == 2:
                return _Resp(_Msg(tool_calls=[
                    _ToolCall("e2", "buscar_contexto_externo", json.dumps({
                        "zona": "Zacatecoluca, El Salvador", "periodo": "junio 2026",
                        "alerta_confirmada": "FORZA Wilson 23.4 n=38",
                    }))
                ]))
            contexto = [m for m in messages if m.get("role") == "tool"
                        and "NO VERIFICADO" in m.get("content", "")]
            assert contexto, "el resultado del contexto externo no llego etiquetado a Qwen"
            return _Resp(_Msg(content=(
                "FORZA en Zacatecoluca esta critico (Wilson 23.4, n=38). "
                "Posible contexto externo (NO VERIFICADO): reporte de bloqueo en CA-1."
            )))
    r = agente.responder("explica la caida de Zacatecoluca", client=ClienteEvidenciaLuegoContexto())
    assert [t["tool"] for t in r["tools_usadas"]] == ["estimador_wilson", "buscar_contexto_externo"], \
        f"orden de tools incorrecto: {r['tools_usadas']}"
    print("OK — con evidencia interna previa, la busqueda externa corrio y llego etiquetada")
    agent_tools.TOOL_HANDLERS["buscar_contexto_externo"] = agent_tools.buscar_contexto_externo

    print("\n=== TEST 8: aprobar genera artefactos de remediacion (PDF/Excel/correo) ===")
    class ClienteRemediacion:
        def __init__(self):
            self.turno = 0
            self.chat = self; self.completions = self
        def create(self, model, messages, tools, **kw):
            self.turno += 1
            if self.turno == 1:
                return _Resp(_Msg(tool_calls=[
                    _ToolCall("r1", "estimador_wilson",
                              json.dumps({"municipio": "Escuintla", "carrier": "FORZA", "min_n": 1, "top": 1}))
                ]))
            if self.turno == 2:
                return _Resp(_Msg(tool_calls=[
                    _ToolCall("r2", "notificar_operaciones", json.dumps({
                        "asunto": "FORZA critico en Escuintla — remediacion",
                        "mensaje": "Auditar rutas y reasignar carrier en la zona.",
                        "origen_id": "wilson#Escuintla-FORZA-test",
                        "caso": {"municipio": "Escuintla", "depto": "Escuintla",
                                 "carrier": "FORZA", "pais": "GT"},
                    }))
                ]))
            return _Resp(_Msg(content="Notificacion propuesta, pendiente de tu aprobacion."))
    r = agente.responder("FORZA en Escuintla necesita remediacion", client=ClienteRemediacion())
    assert len(r["pendientes"]) == 1
    pid8 = r["pendientes"][0]["id"]
    res8 = agente.resolver_pendiente(pid8, aprobado=True)
    rem = res8["resultado"].get("remediacion")
    assert rem and rem["generado"], f"la remediacion no se genero: {rem}"
    tipos = {a["tipo"] for a in rem["artefactos"]}
    assert tipos & {"pdf", "html_imprimible"}, f"falta el PDF (o su fallback html): {tipos}"
    assert "excel" in tipos, f"falta el Excel de call center: {tipos}"
    areas = {c["area"] for c in rem["correos"]}
    assert areas == {"mensajeria", "callcenter"}, f"borradores de correo incorrectos: {areas}"
    for a in rem["artefactos"]:
        ruta = os.path.join("/tmp/test_artefactos", a["archivo"])
        assert os.path.exists(ruta) and os.path.getsize(ruta) > 0, f"artefacto vacio o inexistente: {ruta}"
    assert all(c["mailto"].startswith("mailto:") for c in rem["correos"]), \
        "el correo debe ser borrador mailto, nunca envio automatico"
    print(f"OK — {len(rem['artefactos'])} artefactos + {len(rem['correos'])} borradores; "
          f"tipos: {sorted(tipos)} (ordenes sinteticas: backend apagado a proposito)")

    print("\nTODOS LOS TESTS PASARON — la logica del orquestador esta lista.")
    print("Siguiente: correrlo con Qwen real -> python agente.py \"tu pregunta\"")


if __name__ == "__main__":
    main()
