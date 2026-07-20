# WebCorp Autopilot — Dossier del proyecto

**Global AI Hackathon Series with Qwen Cloud · Track 4: Autopilot Agent**
Repo: https://github.com/steffanylars/WCP-insights · Demo vivo: http://8.219.56.30/panel/ · Licencia MIT

---

## 1. El track y cómo este proyecto le responde

Texto oficial del Track 4: *"Build an Agent that automates real-world business workflows end-to-end… from system alerts to automated remediation… demonstrate the Agent's ability to handle ambiguous inputs, invoke external tools, and incorporate human-in-the-loop checkpoints at critical decision points. Emphasis is on production-readiness over toy demos."*

Correspondencia punto por punto:

| Exigencia del track | Cómo se cumple |
|---|---|
| Real-world business workflow | Operación real de WebCorp, 3PL con 6 países activos: 318K órdenes, 1.6M eventos de tracking, 241,801 órdenes solo en 2026 |
| Ambiguous inputs | El agente recibe preguntas de negocio en lenguaje natural ("¿por qué FORZA está mal?") y las resuelve orquestando herramientas |
| Invoke external tools | 8 tools determinísticas con function calling de Qwen |
| Human-in-the-loop checkpoints | Toda acción con efecto externo queda bloqueada **en código** hasta aprobación humana explícita — no es UI, es un checkpoint en `ejecutar_tool()` que el LLM no puede saltarse |
| From system alerts to automated remediation | Al aprobar una alerta, código determinístico genera el PDF dirigido a la mensajería, el Excel de órdenes para call center y los borradores de correo — que nunca se envían solos |
| Production-readiness | Desplegado en Alibaba Cloud ECS con systemd + cron; colas y dedupe persistentes en SQLite; fallbacks en cada capa; suite de 8 tests que corre sin gastar tokens |

## 2. Qué se construyó

**Arquitectura de 3 capas** (ver `docs/architecture.png`):

1. **Reasoning** — Qwen (`qwen-plus`, function calling, `enable_thinking:false`). Clasifica la intención, decide qué tools llamar, redacta sobre los resultados. **Nunca calcula un número de negocio.**
2. **Trust layer** — chequeos en código, no en prompt: declara cobertura de datos por país, bloquea acciones con efecto externo hasta aprobación humana, bloquea la búsqueda web salvo que ya exista evidencia interna en el mismo loop.
3. **Evidence & Action** — 8 tools determinísticas:

| Tool | Función | Restricción de confianza |
|---|---|---|
| `estimador_wilson` | Rankea pares municipio×carrier por límite inferior de Wilson (95%) | Declara cobertura (solo GT y SV) y fecha de snapshot en cada llamada |
| `efectividad_semanal` | Serie semanal desde el backend de producción | Solo lectura |
| `alertas` | Anomalías por umbral con rationale pre-escrito | Solo lectura |
| `productos_real` | Volumen/efectividad/precio por producto | Solo lectura · fallback sintético declarado |
| `courier_zona` | Cruce mensajería×departamento (alimenta el mapa) | Solo lectura · fallback sintético declarado |
| `operacion_paises` | Vista regional: los 6 países | Solo lectura · fallback sintético declarado |
| `notificar_operaciones` | Notificación real + artefactos de remediación | **Bloqueada hasta aprobación humana** · dedupe persistente 6h |
| `buscar_contexto_externo` | Causas externas (bloqueos/clima/protestas) vía `enable_search` de Qwen | **Bloqueada si no corrió evidencia interna primero** · resultado siempre etiquetado NO VERIFICADO · fuentes citadas |

**Modos de operación:** reactivo (pregunta humana) y proactivo (cron 4 AM en el servidor — las propuestas amanecen en la cola de aprobación).

**Panel** (`frontend/index.html`, servido por el propio agente): dos vistas con el design system del dashboard corporativo — *Panorama* (KPIs regionales + mapa de Centroamérica con drill-down a departamentos de Guatemala + tarjeta del mecanismo Wilson) y *Agente* (conversación con jerarquía hero/body/caption, cola HITL, audit log en lenguaje humano con el evento técnico como subtítulo).

**Auditoría:** JSONL append-only; cada acción amarra su `origen_id` a la evidencia que la originó. La cadena dato → recomendación → decisión humana → acción → artefacto es reconstruible completa.

## 3. Qué suma (diferenciadores)

1. **Datos reales, no sintéticos.** El foso del proyecto: una multinacional real con problemas reales. El caso ancla en dólares: un solo par municipio-carrier acumuló ~$2,188 USD de contra-reembolso no cobrado en 90 días — y hay 700+ pares. Cita textual del dueño sobre la detección actual: *"No se dan cuenta — todo está en Excel, así que unos 3 días, o si no, nunca."* El agente baja eso a segundos.
2. **Honestidad estadística como feature.** Rankear por Wilson LCB y no por tasa cruda evita falsos héroes (Chahal/FORZA: 100% crudo con n=1 → Wilson 20.7) y detecta problemas reales (Zacatecoluca/FORZA: Wilson 23.4 con n=38, el peor par confiable del dataset). El agente declara cobertura desigual (solo GT y SV tienen evidencia municipal) en vez de fingir uniformidad.
3. **HITL que no es teatro.** El bloqueo vive en `ejecutar_tool()`: sin `aprobado_por_humano=True` —que solo el endpoint de aprobación puede poner— la tool no corre. Ni el LLM ni una inyección de prompt lo saltan.
4. **Remediación determinística.** Qué artefacto generar lo deciden reglas de código, no el modelo; los números del PDF se re-consultan al estimador en el momento de generar, no se copian del texto del LLM. Los correos son borradores (mailto): el humano envía, siempre.
5. **Uso no-obvio de Qwen Cloud:** `enable_search` + `enable_source` para contexto externo citado — verificado empíricamente que las fuentes solo las expone el endpoint nativo de DashScope, no el modo OpenAI-compatible (gotcha documentado que el resto de participantes que copie el snippet de la doc no va a descubrir).

## 4. Por qué es robusto

- **Cada capa tiene fallback:** Qwen caído → el motor de reglas responde solo (etiquetado "MODO FALLBACK"). Una tool caída → el error se le devuelve a Qwen como resultado para que use otra evidencia o declare el hueco, sin tumbar el loop. Backend MySQL inaccesible → las tools de datos caen a un dataset sintético **que se declara a sí mismo** en el campo `fuente`. WeasyPrint sin librerías de sistema → el PDF cae a HTML imprimible con la misma plantilla.
- **Estado que sobrevive reinicios:** cola HITL y dedupe en SQLite (no memoria); imposible aprobar el mismo pendiente dos veces (probado); un aviso no se reenvía dentro de su ventana de 6h.
- **Alucinación detectada y cerrada en pruebas reales:** Qwen fabricó una cita de prensa sin llamar la tool de búsqueda. Se endureció la regla del system prompt (solo puede citar URLs devueltas por la tool; si no la llamó, "no existe contexto externo") y el re-test produjo el comportamiento deseado: buscó, no encontró nada específico, y lo dijo — *"la causa es interna hasta nueva evidencia"*.
- **Suite de 8 tests sin tokens** (`test_agente_mock.py`): loop completo, HITL, doble aprobación rechazada, fallback sin LLM, cadena de auditoría, guard del contexto externo (bloqueo y camino feliz), y flujo completo de remediación con backend apagado a propósito.
- **Verificable por un juez:** todo lo afirmado se reproduce desde el repo (`git clone` → tests → servidor local) y el demo público estará vivo durante toda la ventana de judging.

## 5. Sustento de las decisiones de diseño

- **Arquitectura de 3 capas:** el patrón reasoning→trust→action converge de forma independiente en Salesforce Agentforce, Amazon Bedrock Agents y Microsoft Copilot Studio — es el estándar enterprise emergente, no una preferencia propia.
- **Workflow profundo sobre agente universal:** patrón observado en ganadores de hackathons de agentes comparables (Microsoft AI Agents Hackathon — RiskWise, WorkWizee; IBM watsonx Orchestrate — NexusGuardAI; NVIDIA NeMo — agente + solver real): gana el flujo específico y profundo, no el agente que hace de todo.
- **Wilson LCB:** estadística estándar para rankear proporciones con muestras desiguales (el mismo mecanismo del ranking de Reddit/Evan Miller); castiga incertidumbre en vez de premiar suerte.
- **"Anomaly diagnosis" está en el roadmap público de skills de Qwen Cloud** como categoría planeada no construida — este proyecto es ese patrón, hoy, para logística real.
- **Gotchas de la API verificados empíricamente** (no asumidos): thinking incompatible con tool calling; `search_info` solo en endpoint nativo; el buscador de `enable_search` es débil en noticias hiperlocales de Centroamérica ("sin hallazgos" es un resultado frecuente, válido y etiquetado).

## 6. Limitaciones conocidas (documentadas, no escondidas)

- SQLite es single-instance; multi-instancia real necesitaría Redis compartido.
- El trust layer corre en el mismo proceso; en producción sería un policy service separado.
- Sin inferencia causal: puede confundir "carrier malo" con "zona difícil" (confusión tipo Simpson).
- Wilson asume independencia entre entregas; la correlación temporal/geográfica real hace los intervalos algo más anchos de lo calculado.
- 700+ comparaciones sin corrección por multiplicidad: el peor caso del mes trae ruido. Mejora futura: shrinkage bayesiano.
- Cobertura geográfica interna desigual y **declarada**: solo Guatemala tiene geocodificación municipal utilizable (95% de sus órdenes); El Salvador 17%, el resto ~0%. El mapa lo dice en pantalla en vez de pintar datos inventados.
- Una sola DB sirve carga transaccional y analítica; producción necesitaría réplica de solo lectura.
- La instancia pública corre **datos sintéticos de demostración** cuando el backend no está disponible — decisión de privacidad (la DB de producción no se expone), siempre declarada en pantalla y en el campo `fuente`.

## 7. Privacidad de datos

Solo se publican agregados anonimizados a nivel municipio/país, con autorización del dueño de los datos. La PII (teléfonos de clientes en el Excel de call center) se maneja únicamente en generación local de artefactos, **nunca pasa por el LLM**, y el directorio de artefactos está excluido del repo. Nunca se publican clientes, órdenes individuales, montos de contrato ni credenciales.

## 8. Industrias donde aplica (replicabilidad)

El motor es agnóstico al dominio: cualquier operación con **tasas de éxito/fracaso por segmento** cabe en la misma forma. Reemplazar un CSV (`segmento, n, éxitos`) y apuntar las tools de tendencia a tu propia API basta:

- **Logística de última milla / 3PL** (el caso construido): efectividad por zona×carrier.
- **Cobranza:** tasa de recuperación por cartera×gestor.
- **Field service:** resolución en primera visita por región×técnico.
- **Seguros / claims:** aprobación por tipo×ajustador.
- **Salud (citas):** ausentismo por clínica×especialidad.
- **Retail / e-commerce COD:** entregas y rechazos por producto×región (la tool `productos_real` ya lo hace).

En todos los casos el patrón es idéntico: pregunta ambigua → evidencia estadísticamente honesta → recomendación → aprobación humana → acción auditada → artefacto de remediación.

## 9. Estado del deployment

Alibaba Cloud ECS (Singapur), Ubuntu, `systemd` con auto-restart, escaneo proactivo diario vía cron. API base de Qwen: `dashscope-intl.aliyuncs.com`. El demo permanece vivo durante toda la ventana de judging (10–31 de julio); el panel no requiere login.
