# AgentClean

Sistema multiagente de limpieza cooperativa sobre una grilla 15×15, implementado
con arquitectura híbrida de **tres capas razonadoras articuladas**.

## Arquitectura

| Capa | Tecnología | Rol | Estado |
|---|---|---|---|
| 1 — Control y mundo | Python + PyGame | Dueña del estado, motor por *tick*, compromisos persistentes, render | Con estado |
| 2 — Coordinación reactiva | pyDatalog | Decide *qué hace cada agente cada tick* a partir del snapshot del mundo | Sin estado (por tick) |
| 3 — Optimización estratégica | Prolog (PySWIP) | Ruteo, redistribución global, análisis post-mortem | Offline / periódico |

Diseño central: **el planificador propone, el simulador compromete**. El
planner pyDatalog es snapshot-based; aplicar su propuesta directamente
produciría oscilación. El simulador mantiene un registro de compromisos
persistente que solo se rompe por completion, desaparición de la tarea, o
huérfana por avería/falta de energía. Esto habilita la métrica de *trabajo
desperdiciado* que la capa Prolog usa para análisis.

## Estructura

```
obligatorio/
├── config.json                # escenario + parámetros de simulación + eventos
├── modelo_datos.py            # SPEC 01 — vocabulario y hechos pyDatalog
├── planificador_pydatalog.py  # SPEC 02 — reglas de coordinación (familias A–H)
├── simulador.py               # SPEC 03 — motor, compromisos, render PyGame
├── puente_prolog.py           # SPEC 04 — bridge PySWIP + fallback Python (BFS)
├── motor_prolog.pl            # SPEC 04 — reglas Prolog (F1, F2, F3)
├── tests/
│   ├── verificar_simulador.py    # 10 criterios SPEC 03
│   └── verificar_prolog.py       # 10 criterios SPEC 04 + no-regresión
└── salidas/                   # auto-generado por la corrida
    ├── traza_simulacion.json     # contrato de entrada para la capa Prolog
    └── analisis_prolog.json      # análisis post-mortem
```

## Requisitos

- Python 3.10+ (las dependencias están instaladas en 3.10.14; ver `.python-version`).
- SWI-Prolog (`swipl`) — opcional pero recomendado.
- Paquetes Python: `pyDatalog`, `pygame`, `pyswip`.

Instalación:

```bash
pyenv local 3.10.14         # si usás pyenv
pip install pyDatalog pygame pyswip
brew install swi-prolog     # macOS; o el equivalente de tu sistema
```

Sin SWI-Prolog / PySWIP el sistema sigue funcionando: el puente cae al
fallback Python (BFS) automáticamente.

## Uso

Desde la raíz del proyecto:

```bash
python simulador.py                  # con ventana PyGame
python simulador.py --headless       # sin ventana (más rápido)
python simulador.py --max-ticks 100  # acotar duración
```

**Controles** durante la ejecución gráfica (también se imprimen al arrancar):

| Tecla | Acción |
|---|---|
| `ESPACIO` | pausa / reanuda |
| `→`       | avanza un tick (paso a paso, útil en pausa) |
| `+` / `-` | sube / baja FPS (rango 1–60) |
| `ESC`     | sale (sigue terminando en headless para escribir JSONs) |

La velocidad por defecto es `simulacion.fps` en `config.json`.

**Leyenda de la grilla** (colores y objetos del render): ver
[`salidas/leyenda.png`](salidas/leyenda.png). Si modificás los colores en
`simulador._render()`, regenerá la imagen con `python leyenda.py`.

## Salidas

Tras una corrida se generan dos archivos en `salidas/`:

- **`traza_simulacion.json`** — registro completo de la simulación (un
  registro por *tick*, eventos del entorno, resumen). Es el *contrato de
  entrada* de la capa Prolog (§2.3 de SPEC 03).
- **`analisis_prolog.json`** — análisis post-mortem: desperdicio total,
  eficiencia global, pico de conflictos, agente más eficiente,
  correlaciones avería→huérfana, recomendaciones en lenguaje natural.
  Incluye `cross_check` que valida que los conteos de Prolog coinciden
  con `traza.resumen` (consistencia entre capas).

## Tests

Suite de aceptación que valida los criterios de cada spec:

```bash
python tests/verificar_simulador.py   # 10 criterios SPEC 03
python tests/verificar_prolog.py      # 10 criterios SPEC 04 + no-regresión
```

`verificar_prolog.py` ejecuta también la suite de SPEC 03 al final como
chequeo de no-regresión, y prueba el fallback Python con
`AGENTCLEAN_SIN_PROLOG=1`.

## Decisiones de diseño destacadas

1. **Anti-oscilación por compromisos persistentes.** El planner pyDatalog
   es puramente snapshot; sin un registro de compromisos, los agentes
   serían reasignados antes de completar nada. El simulador es la única
   fuente de verdad y resuelve esto.
2. **Asignación determinista.** Tarea de limpieza → agente más cercano;
   ante empate de distancia, mayor energía; ante empate de energía, menor
   id lexicográfico. Garantiza unicidad y reproducibilidad.
3. **Acción conjunta atómica.** La pareja para un objeto pesado se
   compromete y se libera junto. Si uno se rompe (avería, energía),
   ambos quedan huérfanos.
4. **Patrón "no existe mejor".** Tanto pyDatalog como Prolog evitan
   operadores de agregación (que son frágiles en pyDatalog y poco
   idiomáticos en Prolog). En su lugar, una opción es óptima si no
   existe otra estrictamente mejor.
5. **Robustez de la capa Prolog.** Cada consulta PySWIP corre en hilo
   daemon con timeout. Ante timeout o PySWIP ausente, el fallback Python
   (BFS para rutas, equivalente Python para F3) provee la misma firma
   de retorno. La simulación nunca se bloquea por la capa lógica.
6. **Eventos del entorno deterministas.** Configurados por *tick* en
   `config.json` para que la corrida sea reproducible — esencial para
   la defensa y los tests de aceptación. La variante probabilística
   queda como bonus opcional detrás de `simulacion.semilla`.

## Cómo se cruzan las tres capas

Cada *tick* del simulador ejecuta 8 pasos: snapshot → planificar →
conciliar compromisos → aplicar movimientos/acciones → eventos del
entorno → registrar → render → hook Prolog (cada `prolog_cada_n_ticks`).
Al terminar, el simulador invoca `puente_prolog.analizar` con la traza
completa y agrega el análisis al `resumen`.

La capa Prolog **nunca** se invoca dentro del ciclo de coordinación: es
estratégica, no reactiva. La capa pyDatalog se invoca cada tick; el
simulador descarta los hechos dinámicos del tick anterior y vuelve a
asertarlos cada vez, manteniendo la base lógica compacta.
