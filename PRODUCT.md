# Product

## Register

product

## Users

Dos audiencias sobre la misma pantalla:
1. **Operadores de logística de WebCorp** (3PL, 6 países de Centroamérica): personal de operaciones no técnico que necesita saber *dónde* está fallando la entrega y *aprobar o descartar* acciones propuestas por el agente. Contexto de uso: escritorio, jornada operativa, decisiones rápidas con dinero real en juego (contra-reembolso no cobrado).
2. **Jueces del hackathon Qwen Cloud (Track 4)**: evalúan en 10–15 segundos de video/demo si esto es un producto serio o un wrapper. Muchos no leen español técnico; la jerarquía visual debe contar la historia sola.

Trabajo a realizar: pregunta ambigua de negocio → evidencia estadísticamente honesta → decisión humana → acción auditada con artefactos de remediación.

## Product Purpose

WebCorp Autopilot: agente de decisión operativa sobre datos reales de una 3PL. Detecta anomalías de entrega (reactivo y proactivo), recomienda con estadística honesta (Wilson LCB, nunca tasa cruda; el LLM nunca calcula), y ejecuta acciones solo tras aprobación humana — con generación determinística de artefactos de remediación (PDF a mensajerías, Excel a call center, borradores de correo). Éxito = que un operador confíe en actuar sobre lo que el panel le dice, y que un juez identifique el checkpoint HITL sin explicación.

## Brand Personality

**Honesto, riguroso, confiable.** La honestidad es feature visible: cobertura de datos declarada en pantalla, fuentes sintéticas etiquetadas, números con su n al lado, marca "ƒ calculado" en todo dato que produce una tool determinística. Tono directo en español, sin relleno, sin promesas.

## Anti-references

- **Template admin genérico**: Bootstrap/admin de stock, grids de cards idénticas, hero-metric con gradiente.
- **Chatbot juguete**: burbujas de chat estilo consumer, emojis, tono casual, mascotas.
- **Terminal hacker oscuro**: fondo negro con verde neón, estética de película. El mono (IBM Plex Mono) es voz de datos, no disfraz de terminal.

## Design Principles

1. **La evidencia manda la jerarquía**: lo más grande en pantalla es siempre la conclusión con sus números, nunca el chrome de la UI.
2. **El humano decide, y se nota**: el checkpoint de aprobación es el único bloque de advertencia con peso visual; no compite con nada.
3. **Dos lectores, una pantalla**: lenguaje humano al frente (títulos de eventos, etiquetas), lo técnico como subtítulo pequeño — nunca eliminado, nunca dominante.
4. **Declarar, no aparentar**: límites de datos, fuentes sintéticas y fechas de snapshot se muestran en la superficie, no se esconden en tooltips.
5. **Familia del producto real**: paleta y patrones heredan del dashboard corporativo WebCorp existente; el panel debe sentirse parte del mismo producto, no un anexo de hackathon.

## Accessibility & Inclusion

WCAG AA estricto: contraste ≥4.5:1 en texto de cuerpo (≥3:1 en texto grande), estados de foco visibles, `prefers-reduced-motion` respetado en toda animación, semántica correcta en botones/controles. Auditoría formal pendiente antes del submit (9 de julio).
