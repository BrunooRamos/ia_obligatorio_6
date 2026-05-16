"""
AgentClean - Simulador y estado persistente (SPEC 03).

Capa 1 de la arquitectura: fuente unica de la verdad del estado del mundo.
Orquesta el ciclo por tick, mantiene los compromisos persistentes que evitan
oscilacion, aplica eventos del entorno, escribe la traza para Prolog y
renderiza con PyGame (opcional; el modo headless es obligatorio).

Dependencias: modelo_datos.py (SPEC 01), planificador_pydatalog.py (SPEC 02).
"""

from __future__ import annotations

import argparse
import json
import os
import random
from typing import Any

import modelo_datos
import planificador_pydatalog
import puente_prolog

try:
    import pygame  # type: ignore
    _HAS_PYGAME = True
except ImportError:  # pragma: no cover
    pygame = None  # type: ignore
    _HAS_PYGAME = False


# Tipos de compromiso persistente.
TIPO_LIMPIEZA   = "limpieza"
TIPO_TRANSPORTE = "transporte"
TIPO_RECARGA    = "recarga"


# ---------------------------------------------------------------------------
# Simulador
# ---------------------------------------------------------------------------

class Simulador:
    """Motor de simulacion por tick con compromisos persistentes.

    Decisiones clave (SPEC 03 §1):
      - El planificador propone (snapshot, sin estado).
      - El simulador compromete: un compromiso solo se rompe ante un evento
        real (tarea completada, desaparecida, o huerfana por averia/sin
        energia). Eso anula la oscilacion del snapshot.
      - La accion conjunta se compromete y se libera atomicamente para los
        dos agentes.
    """

    def __init__(
        self,
        config_path: str,
        headless: bool = False,
        max_ticks_override: int | None = None,
    ):
        self.cfg = modelo_datos.cargar_config(config_path)

        modelo_datos.declarar_vocabulario()
        modelo_datos.cargar_hechos_estaticos(self.cfg)
        planificador_pydatalog.definir_reglas()

        self.estado: dict = modelo_datos.estado_inicial_desde_config(self.cfg)
        self.tick_actual: int = 0

        # --- Parametros de simulacion ---
        sim = self.cfg.get("simulacion", {})
        self.max_ticks           = max_ticks_override or sim.get("max_ticks", 300)
        self.tasa_recarga        = sim.get("tasa_recarga", 25)
        self.prolog_cada_n_ticks = sim.get("prolog_cada_n_ticks", 50)
        self.semilla             = sim.get("semilla", 42)
        self._fps                = max(1, int(sim.get("fps", 4)))
        random.seed(self.semilla)

        # --- Estado de UI (solo aplica si !headless) ---
        self._paused = False
        self._step_once = False
        self._clock = None

        # --- Eventos del entorno ---
        eventos = self.cfg.get("eventos", {})
        self.evt_nuevas_sucias: dict[int, list[tuple[int, int]]] = {}
        for e in eventos.get("nuevas_sucias", []):
            self.evt_nuevas_sucias.setdefault(e["tick"], []).append((e["x"], e["y"]))
        self.evt_averias: dict[int, list[str]] = {}
        for e in eventos.get("averias", []):
            self.evt_averias.setdefault(e["tick"], []).append(e["agente"])
        self.patron_om: dict[str, list[dict]] = {
            p["id"]: p["ruta"] for p in eventos.get("obstaculos_moviles_patron", [])
        }

        # --- Lookups derivados de la config ---
        self.ancho = self.cfg["grilla"]["ancho"]
        self.alto  = self.cfg["grilla"]["alto"]
        self.obstaculos_fijos_set = {
            (c["x"], c["y"]) for c in self.cfg.get("obstaculos_fijos", [])
        }
        self.estaciones = [
            (c["x"], c["y"]) for c in self.cfg.get("estaciones_recarga", [])
        ]
        self.zonas_espera = [
            (c["x"], c["y"]) for c in self.cfg.get("zonas_espera", [])
        ]
        self.agentes_cfg = {a["id"]: a for a in self.cfg["agentes"]}

        # --- Estado persistente del simulador ---
        # compromisos[agente_id] = {tipo, objetivo, tick_inicio, [obj], [companero]}
        self.compromisos: dict[str, dict] = {}

        # --- Acumuladores para la traza ---
        self.traza_ticks: list[dict] = []
        self.eventos_aplicados: list[dict] = []
        self.tareas_completadas_total: list[dict] = []
        self.tareas_huerfanas_total: list[dict] = []
        self.cooperaciones_completadas: int = 0
        self.energia_consumida_total: int = 0

        # --- Rutas opcionales del hook Prolog (SPEC 04) ---
        # {agente_id: [(x,y), ...]} - si esta presente, sobre-escribe el paso greedy.
        self._rutas_optimizadas: dict[str, list[tuple[int, int]]] = {}

        # --- PyGame ---
        self.headless = headless or os.environ.get("SDL_VIDEODRIVER") == "dummy"
        self._pygame_ready = False
        self._screen = None
        self._cell_px = 28
        self._panel_w = 280
        if not self.headless:
            self._init_pygame()

    # =========================================================
    # Helpers de consulta sobre el estado
    # =========================================================

    def _agente(self, aid: str) -> dict:
        for ag in self.estado["agentes"]:
            if ag["id"] == aid:
                return ag
        raise KeyError(f"agente desconocido: {aid}")

    def _sucias_set(self) -> set[tuple[int, int]]:
        return {(c["x"], c["y"]) for c in self.estado["celdas_sucias"]}

    def _obstaculos_moviles_set(self) -> set[tuple[int, int]]:
        return {(o["x"], o["y"]) for o in self.estado["obstaculos_moviles"]}

    def _objeto_pesado_pos(self, obj_id: str) -> tuple[int, int] | None:
        for o in self.estado["objetos_pesados"]:
            if o["id"] == obj_id:
                return (o["x"], o["y"])
        return None

    def _es_transitable(self, x: int, y: int) -> bool:
        if not (0 <= x < self.ancho and 0 <= y < self.alto):
            return False
        if (x, y) in self.obstaculos_fijos_set:
            return False
        if (x, y) in self._obstaculos_moviles_set():
            return False
        return True

    @staticmethod
    def _manhattan(p: tuple[int, int], q: tuple[int, int]) -> int:
        return abs(p[0] - q[0]) + abs(p[1] - q[1])

    def _mas_cercana(self, ag: dict, opciones: list[tuple[int, int]]):
        if not opciones:
            return None
        pos = (ag["x"], ag["y"])
        return min(opciones, key=lambda p: (self._manhattan(pos, p), p))

    # =========================================================
    # Ciclo principal
    # =========================================================

    def tick(self) -> dict:
        """Ejecuta UN tick completo (SPEC 03 §3)."""
        # 1. Snapshot del mundo -> hechos dinamicos pyDatalog.
        modelo_datos.refrescar_hechos_dinamicos(self.estado)

        # 2. Planificacion (capa 2).
        decisiones = planificador_pydatalog.planificar()
        metricas   = planificador_pydatalog.calcular_metricas()

        completadas: list[dict] = []
        huerfanas:   list[dict] = []

        # 3. Conciliacion (compromisos vs propuesta).
        self._conciliar(decisiones, huerfanas)

        # 4. Aplicar movimientos / acciones / energia.
        self._aplicar(decisiones, completadas, huerfanas)

        # 5. Eventos del entorno programados para este tick.
        self._aplicar_eventos(huerfanas)

        # 6. Registrar el tick en la traza.
        registro = self._registrar_tick(decisiones, metricas, completadas, huerfanas)

        # 7. Render (salvo headless).
        if not self.headless:
            self._render(metricas)

        # 8. Hook Prolog periodico (la primera invocacion es en tick
        #    prolog_cada_n_ticks - 1 para que coincida con multiplos al final).
        if (self.prolog_cada_n_ticks > 0
                and (self.tick_actual + 1) % self.prolog_cada_n_ticks == 0):
            rutas = self.hook_prolog(self.estado_para_prolog())
            if rutas:
                self._rutas_optimizadas.update(rutas)

        self.tareas_completadas_total.extend(completadas)
        self.tareas_huerfanas_total.extend(huerfanas)
        self.tick_actual += 1
        return registro

    def run(self) -> dict:
        """Corre hasta terminacion; escribe traza_simulacion.json."""
        terminacion = "max_ticks"
        while self.tick_actual < self.max_ticks:
            # Si !headless: bombear eventos, esperar si esta pausado, throttle.
            self._bombear_eventos()
            if self.headless:
                # ESC cierra la ventana -> seguimos corriendo headless.
                pass
            else:
                self._esperar_si_pausa()
            self.tick()
            if self._pygame_ready and self._clock and not self.headless:
                self._clock.tick(self._fps)
            if not self.estado["celdas_sucias"] and not self.estado["objetos_pesados"]:
                terminacion = "limpieza_completa"
                break

        resumen = self._construir_resumen(terminacion)
        traza = self.estado_para_prolog()
        traza["resumen"] = resumen

        ruta_traza = _ruta_salida("traza_simulacion.json")
        with open(ruta_traza, "w", encoding="utf-8") as f:
            json.dump(traza, f, indent=2, ensure_ascii=False, default=_json_default)

        # Analisis post-mortem (SPEC 04 F3). Escribe analisis_prolog.json y
        # agrega el resultado al resumen para visualizacion en el dashboard.
        try:
            analisis = puente_prolog.analizar(
                traza, config=self.cfg, timeout=20.0,
                salida=_ruta_salida("analisis_prolog.json"),
            )
            resumen["analisis_prolog"] = analisis
        except Exception as e:  # pragma: no cover
            resumen["analisis_prolog"] = {"error": str(e)}

        if not self.headless:
            self._cerrar_pygame()

        return resumen

    # =========================================================
    # 3.1 - Conciliacion
    # =========================================================

    def _conciliar(self, decisiones: dict, huerfanas: list[dict]) -> None:
        # --- (a) Agentes con compromiso vigente: validar o liberar. ---
        sucias = self._sucias_set()
        for aid in list(self.compromisos.keys()):
            com = self.compromisos[aid]
            ag = self._agente(aid)
            agcfg = self.agentes_cfg[aid]

            # Averiado -> huerfana (causa: averia).
            if ag["estado"] == "averiado":
                self._orfanar(aid, "averia", huerfanas)
                continue

            # Sin energia (bajo umbral) durante un compromiso de trabajo
            # -> huerfana (causa: sin_energia).
            if (com["tipo"] in (TIPO_LIMPIEZA, TIPO_TRANSPORTE)
                    and ag["energia"] <= agcfg["umbral_recarga"]):
                self._orfanar(aid, "sin_energia", huerfanas)
                continue

            # Objetivo ya no existe (otro lo completo o evento lo retiro).
            if com["tipo"] == TIPO_LIMPIEZA:
                if com["objetivo"] not in sucias:
                    self._liberar(aid)
            elif com["tipo"] == TIPO_TRANSPORTE:
                if self._objeto_pesado_pos(com["obj"]) is None:
                    self._liberar(aid)
            # Recarga: se gestiona en _aplicar (se libera al cargar lo suficiente).

        # --- (b) Agentes sin compromiso: tomar nueva asignacion. ---
        recargas_map = {a: (ex, ey) for (a, ex, ey) in decisiones.get("recargas", [])}
        asig_lim_map = {a: (x, y) for (a, x, y) in decisiones.get("asignaciones_limpieza", [])}
        acciones_conj = decisiones.get("acciones_conjuntas", [])  # [(obj, a1, a2)]

        comprom_lim_objs = {
            com["objetivo"] for com in self.compromisos.values()
            if com["tipo"] == TIPO_LIMPIEZA
        }
        comprom_objs_pesados = {
            com["obj"] for com in self.compromisos.values()
            if com["tipo"] == TIPO_TRANSPORTE
        }

        for ag in self.estado["agentes"]:
            aid = ag["id"]
            if aid in self.compromisos or ag["estado"] == "averiado":
                continue

            # Recarga tiene prioridad.
            if aid in recargas_map:
                self._comprometer(aid, TIPO_RECARGA, recargas_map[aid])
                continue

            # Limpieza si el planificador asigno y la celda no esta tomada.
            if aid in asig_lim_map and asig_lim_map[aid] not in comprom_lim_objs:
                target = asig_lim_map[aid]
                self._comprometer(aid, TIPO_LIMPIEZA, target)
                comprom_lim_objs.add(target)
                continue

            # Accion conjunta: comprometer la pareja atomicamente.
            for (obj, a1, a2) in acciones_conj:
                if obj in comprom_objs_pesados:
                    continue
                if aid not in (a1, a2):
                    continue
                companero = a2 if aid == a1 else a1
                if companero in self.compromisos:
                    break
                comp_ag = self._agente(companero)
                if comp_ag["estado"] == "averiado":
                    break
                obj_pos = self._objeto_pesado_pos(obj)
                if obj_pos is None:
                    break
                self._comprometer(a1, TIPO_TRANSPORTE, obj_pos, obj=obj, companero=a2)
                self._comprometer(a2, TIPO_TRANSPORTE, obj_pos, obj=obj, companero=a1)
                comprom_objs_pesados.add(obj)
                break
            # Si quedo sin compromiso y a_zona_espera, el movimiento se
            # gestiona en _aplicar sin comprometer (compromiso "blando").

    def _comprometer(self, aid: str, tipo: str, objetivo, **extra) -> None:
        com = {"tipo": tipo, "objetivo": objetivo, "tick_inicio": self.tick_actual}
        com.update(extra)
        self.compromisos[aid] = com
        ag = self._agente(aid)
        if ag["estado"] != "averiado":
            ag["estado"] = "ocupado"

    def _liberar(self, aid: str) -> None:
        self.compromisos.pop(aid, None)
        ag = self._agente(aid)
        if ag["estado"] not in ("averiado",):
            ag["estado"] = "libre"

    def _orfanar(self, aid: str, causa: str, huerfanas: list[dict]) -> None:
        com = self.compromisos.get(aid)
        if not com:
            return
        ticks_inv = self.tick_actual - com["tick_inicio"]
        huerfanas.append({
            "tarea":            self._descripcion_objetivo(com),
            "agente":           aid,
            "ticks_invertidos": ticks_inv,
            "causa":            causa,
        })
        # Pareja del transporte: tambien queda huerfana.
        if com["tipo"] == TIPO_TRANSPORTE and "companero" in com:
            cid = com["companero"]
            comp_com = self.compromisos.get(cid)
            if comp_com is not None:
                huerfanas.append({
                    "tarea":            self._descripcion_objetivo(comp_com),
                    "agente":           cid,
                    "ticks_invertidos": self.tick_actual - comp_com["tick_inicio"],
                    "causa":            causa,
                })
                self.compromisos.pop(cid, None)
                cag = self._agente(cid)
                if cag["estado"] != "averiado":
                    cag["estado"] = "libre"
        self.compromisos.pop(aid, None)
        ag = self._agente(aid)
        if ag["estado"] != "averiado":
            ag["estado"] = "libre"

    @staticmethod
    def _descripcion_objetivo(com: dict) -> dict:
        if com["tipo"] == TIPO_LIMPIEZA:
            return {"tipo": "limpieza", "x": com["objetivo"][0], "y": com["objetivo"][1]}
        if com["tipo"] == TIPO_TRANSPORTE:
            return {"tipo": "transporte", "obj": com.get("obj")}
        if com["tipo"] == TIPO_RECARGA:
            return {"tipo": "recarga", "x": com["objetivo"][0], "y": com["objetivo"][1]}
        return {"tipo": com["tipo"]}

    # =========================================================
    # 3.2 - Aplicacion (movimientos, acciones, energia)
    # =========================================================

    def _aplicar(
        self,
        decisiones: dict,
        completadas: list[dict],
        huerfanas: list[dict],
    ) -> None:
        # Fase 1: movimientos y acciones individuales.
        for ag in self.estado["agentes"]:
            if ag["estado"] == "averiado":
                continue
            aid = ag["id"]
            com = self.compromisos.get(aid)

            if com is None:
                # Compromiso "blando" de espera (planner -> a_espera).
                if aid in decisiones.get("a_espera", []):
                    target = self._mas_cercana(ag, self.zonas_espera)
                    if target and (ag["x"], ag["y"]) != target:
                        self._mover_un_paso(ag, target)
                continue

            agcfg = self.agentes_cfg[aid]

            if com["tipo"] == TIPO_LIMPIEZA:
                target = com["objetivo"]
                if (ag["x"], ag["y"]) == target:
                    # Limpiar.
                    self.estado["celdas_sucias"] = [
                        c for c in self.estado["celdas_sucias"]
                        if (c["x"], c["y"]) != target
                    ]
                    self._descontar_energia(ag, agcfg["costo_accion"])
                    completadas.append({
                        "tipo": "limpieza", "x": target[0], "y": target[1],
                        "agente": aid, "tick": self.tick_actual,
                    })
                    self._liberar(aid)
                else:
                    self._mover_un_paso(ag, target)
                    self._verificar_averia_por_energia(ag, huerfanas)

            elif com["tipo"] == TIPO_TRANSPORTE:
                obj_pos = self._objeto_pesado_pos(com["obj"])
                if obj_pos is None:
                    self._liberar(aid)
                    continue
                # Avanza hacia el objeto si todavia no esta sobre/adyacente.
                if self._manhattan((ag["x"], ag["y"]), obj_pos) > 1:
                    self._mover_un_paso(ag, obj_pos)
                    self._verificar_averia_por_energia(ag, huerfanas)

            elif com["tipo"] == TIPO_RECARGA:
                target = com["objetivo"]
                if (ag["x"], ag["y"]) == target:
                    ag["estado"] = "recargando"
                    ag["energia"] = min(
                        ag["energia"] + self.tasa_recarga,
                        agcfg["energia_maxima"],
                    )
                    # Termina cuando supera el umbral o llena al maximo.
                    if (ag["energia"] >= agcfg["energia_maxima"]
                            or ag["energia"] > agcfg["umbral_recarga"]):
                        completadas.append({
                            "tipo": "recarga", "x": target[0], "y": target[1],
                            "agente": aid, "tick": self.tick_actual,
                        })
                        self._liberar(aid)
                else:
                    self._mover_un_paso(ag, target)
                    self._verificar_averia_por_energia(ag, huerfanas)

        # Fase 2: transporte cooperativo - se completa cuando AMBOS estan
        # sobre/adyacentes al objeto en el mismo tick (despues de moverse).
        ya_transportados: set[str] = set()
        for aid, com in list(self.compromisos.items()):
            if com["tipo"] != TIPO_TRANSPORTE:
                continue
            obj = com["obj"]
            if obj in ya_transportados:
                continue
            obj_pos = self._objeto_pesado_pos(obj)
            if obj_pos is None:
                continue
            companero = com.get("companero")
            if not companero or companero not in self.compromisos:
                continue
            ag1, ag2 = self._agente(aid), self._agente(companero)
            d1 = self._manhattan((ag1["x"], ag1["y"]), obj_pos)
            d2 = self._manhattan((ag2["x"], ag2["y"]), obj_pos)
            if d1 <= 1 and d2 <= 1:
                ya_transportados.add(obj)
                self.estado["objetos_pesados"] = [
                    o for o in self.estado["objetos_pesados"] if o["id"] != obj
                ]
                for a in (ag1, ag2):
                    self._descontar_energia(a, self.agentes_cfg[a["id"]]["costo_accion"])
                completadas.append({
                    "tipo": "transporte", "obj": obj,
                    "agentes": sorted([ag1["id"], ag2["id"]]),
                    "tick": self.tick_actual,
                })
                self.cooperaciones_completadas += 1
                self._liberar(aid)
                self._liberar(companero)

    def _mover_un_paso(self, ag: dict, target: tuple[int, int]) -> bool:
        """Paso greedy de 1 celda hacia target (o por ruta optimizada de Prolog)."""
        # Ruta optimizada (hook Prolog) tiene prioridad.
        ruta = self._rutas_optimizadas.get(ag["id"])
        if ruta:
            siguiente = ruta[0]
            if self._es_transitable(*siguiente):
                self._aplicar_paso(ag, siguiente)
                self._rutas_optimizadas[ag["id"]] = ruta[1:]
                if not self._rutas_optimizadas[ag["id"]]:
                    del self._rutas_optimizadas[ag["id"]]
                return True
            # Si la ruta ya no es transitable, caer al greedy.

        cx, cy = ag["x"], ag["y"]
        dx, dy = target[0] - cx, target[1] - cy

        candidatos: list[tuple[int, int]] = []
        if abs(dx) >= abs(dy):
            if dx != 0:
                candidatos.append((cx + (1 if dx > 0 else -1), cy))
            if dy != 0:
                candidatos.append((cx, cy + (1 if dy > 0 else -1)))
        else:
            if dy != 0:
                candidatos.append((cx, cy + (1 if dy > 0 else -1)))
            if dx != 0:
                candidatos.append((cx + (1 if dx > 0 else -1), cy))

        for nxny in candidatos:
            if self._es_transitable(*nxny):
                self._aplicar_paso(ag, nxny)
                return True
        return False  # bloqueado: espera este tick

    def _aplicar_paso(self, ag: dict, destino: tuple[int, int]) -> None:
        agcfg = self.agentes_cfg[ag["id"]]
        ag["x"], ag["y"] = destino
        self._descontar_energia(ag, agcfg["costo_mover"])

    def _descontar_energia(self, ag: dict, costo: int) -> None:
        gastado = min(ag["energia"], costo)
        ag["energia"] -= gastado
        if ag["energia"] < 0:
            ag["energia"] = 0
        self.energia_consumida_total += gastado

    def _verificar_averia_por_energia(self, ag: dict, huerfanas: list[dict]) -> None:
        if ag["energia"] <= 0 and ag["estado"] != "averiado":
            ag["estado"] = "averiado"
            self.eventos_aplicados.append({
                "tipo": "averia", "tick": self.tick_actual,
                "agente": ag["id"], "causa": "agotamiento",
            })
            if ag["id"] in self.compromisos:
                self._orfanar(ag["id"], "sin_energia", huerfanas)

    # =========================================================
    # 3.3 - Eventos del entorno
    # =========================================================

    def _aplicar_eventos(self, huerfanas: list[dict]) -> None:
        # Nuevas sucias programadas para este tick.
        for (x, y) in self.evt_nuevas_sucias.get(self.tick_actual, []):
            if (x, y) in self._sucias_set():
                continue
            if (x, y) in self.obstaculos_fijos_set:
                continue
            self.estado["celdas_sucias"].append({"x": x, "y": y})
            self.eventos_aplicados.append({
                "tipo": "nueva_sucia", "tick": self.tick_actual, "x": x, "y": y,
            })

        # Averias programadas.
        for aid in self.evt_averias.get(self.tick_actual, []):
            ag = self._agente(aid)
            if ag["estado"] == "averiado":
                continue
            ag["estado"] = "averiado"
            self.eventos_aplicados.append({
                "tipo": "averia", "tick": self.tick_actual,
                "agente": aid, "causa": "evento",
            })
            if aid in self.compromisos:
                self._orfanar(aid, "averia", huerfanas)

        # Obstaculos moviles segun su patron (proxima posicion).
        for om in self.estado["obstaculos_moviles"]:
            ruta = self.patron_om.get(om["id"])
            if not ruta:
                continue
            idx = (self.tick_actual + 1) % len(ruta)
            nueva = ruta[idx]
            destino = (nueva["x"], nueva["y"])
            if (om["x"], om["y"]) == destino:
                continue
            # No pisar agentes.
            if destino in {(a["x"], a["y"]) for a in self.estado["agentes"]}:
                continue
            self.eventos_aplicados.append({
                "tipo": "obstaculo_movido", "tick": self.tick_actual,
                "id": om["id"],
                "desde": [om["x"], om["y"]],
                "hacia": [destino[0], destino[1]],
            })
            om["x"], om["y"] = destino

    # =========================================================
    # Registro / traza
    # =========================================================

    def _registrar_tick(
        self,
        decisiones: dict,
        metricas: dict,
        completadas: list[dict],
        huerfanas: list[dict],
    ) -> dict:
        asignaciones = {
            aid: self._descripcion_objetivo(com)
            for aid, com in self.compromisos.items()
        }

        acciones_conj: list[dict] = []
        vistos: set[str] = set()
        for aid, com in self.compromisos.items():
            if com["tipo"] != TIPO_TRANSPORTE:
                continue
            obj = com.get("obj")
            if not obj or obj in vistos:
                continue
            vistos.add(obj)
            acciones_conj.append({
                "obj": obj,
                "agentes": sorted([aid, com.get("companero")]),
            })

        recargas = [
            {"agente": aid, "x": com["objetivo"][0], "y": com["objetivo"][1]}
            for aid, com in self.compromisos.items()
            if com["tipo"] == TIPO_RECARGA
        ]

        registro = {
            "tick":                   self.tick_actual,
            "asignaciones":           asignaciones,
            "acciones_conjuntas":     acciones_conj,
            "recargas":               recargas,
            "completadas":            list(completadas),
            "huerfanas":              list(huerfanas),
            "conflictos_potenciales": metricas.get("conflictos_potenciales", 0),
            "energia_por_agente":     {a["id"]: a["energia"] for a in self.estado["agentes"]},
            "metricas":               metricas,
        }
        self.traza_ticks.append(registro)
        return registro

    def estado_para_prolog(self) -> dict:
        """Devuelve la traza acumulada en el formato contractual de §2.3."""
        return {
            "escenario": {
                "ancho":              self.ancho,
                "alto":               self.alto,
                "n_agentes":          len(self.cfg["agentes"]),
                "n_sucias_iniciales": len(self.cfg.get("celdas_sucias_iniciales", [])),
                "n_objetos_pesados":  len(self.cfg.get("objetos_pesados", [])),
            },
            "ticks":   self.traza_ticks,
            "eventos": self.eventos_aplicados,
        }

    def _construir_resumen(self, terminacion: str) -> dict:
        tareas_completadas = sum(
            1 for c in self.tareas_completadas_total
            if c["tipo"] in ("limpieza", "transporte")
        )
        return {
            "ticks_totales":              self.tick_actual,
            "tareas_completadas":         tareas_completadas,
            "tareas_huerfanas":           len(self.tareas_huerfanas_total),
            "trabajo_desperdiciado_total": sum(
                h["ticks_invertidos"] for h in self.tareas_huerfanas_total
            ),
            "energia_consumida_total":    self.energia_consumida_total,
            "cooperaciones_completadas":  self.cooperaciones_completadas,
            "terminacion":                terminacion,
        }

    # =========================================================
    # Hook Prolog (stub - SPEC 04)
    # =========================================================

    def hook_prolog(self, traza: dict) -> dict | None:
        """Costura periodica hacia la capa Prolog (SPEC 04, modo F1).

        Delega en puente_prolog.optimizar: dado el estado y los compromisos
        vigentes, devuelve rutas optimizadas {agente: [(x,y), ...]}. El
        simulador las consume en _mover_un_paso prioritariamente sobre el
        paso greedy. Si PySWIP falta o el timeout vence, el puente cae a
        un BFS Python equivalente sin romper el ciclo.
        """
        try:
            return puente_prolog.optimizar(
                estado=self.estado,
                compromisos=self.compromisos,
                obstaculos_fijos=self.obstaculos_fijos_set,
                obstaculos_moviles=self._obstaculos_moviles_set(),
                ancho=self.ancho,
                alto=self.alto,
                timeout=10.0,
            )
        except Exception:
            return None

    # =========================================================
    # PyGame (render minimo - el dashboard es bonus de la letra)
    # =========================================================

    def _init_pygame(self) -> None:  # pragma: no cover
        if not _HAS_PYGAME:
            raise RuntimeError("PyGame no disponible; correr en headless")
        pygame.init()
        ancho_px = self.ancho * self._cell_px + self._panel_w
        alto_px  = max(self.alto * self._cell_px, 400)
        self._screen = pygame.display.set_mode((ancho_px, alto_px))
        pygame.display.set_caption("AgentClean - Simulador")
        self._font = pygame.font.SysFont("monospace", 14)
        self._clock = pygame.time.Clock()
        self._pygame_ready = True
        print("[controles] ESPACIO: pausa | -> : paso a paso | "
              "+/- : velocidad | ESC: salir")

    def _procesar_evento(self, ev) -> None:  # pragma: no cover
        if ev.type == pygame.QUIT:
            self.headless = True
            return
        if ev.type != pygame.KEYDOWN:
            return
        if ev.key == pygame.K_SPACE:
            self._paused = not self._paused
        elif ev.key == pygame.K_RIGHT:
            self._step_once = True
        elif ev.key in (pygame.K_PLUS, pygame.K_EQUALS, pygame.K_KP_PLUS):
            self._fps = min(60, self._fps + 2)
        elif ev.key in (pygame.K_MINUS, pygame.K_KP_MINUS):
            self._fps = max(1, self._fps - 1)
        elif ev.key == pygame.K_ESCAPE:
            self.headless = True

    def _bombear_eventos(self) -> None:  # pragma: no cover
        if not self._pygame_ready:
            return
        for ev in pygame.event.get():
            self._procesar_evento(ev)

    def _esperar_si_pausa(self) -> None:  # pragma: no cover
        """Bloquea (con event polling) mientras este pausado."""
        if self.headless or not self._pygame_ready:
            return
        while self._paused and not self._step_once and not self.headless:
            self._bombear_eventos()
            if self._clock:
                self._clock.tick(30)
        self._step_once = False

    def _render(self, metricas: dict) -> None:  # pragma: no cover
        if not self._pygame_ready:
            return
        self._bombear_eventos()
        if self.headless:
            return

        s = self._screen
        s.fill((30, 30, 30))
        cp = self._cell_px

        # Celdas.
        for x in range(self.ancho):
            for y in range(self.alto):
                rect = (x * cp, y * cp, cp - 1, cp - 1)
                color = (60, 60, 60)
                if (x, y) in self.obstaculos_fijos_set:        color = (90, 90, 90)
                if (x, y) in self._obstaculos_moviles_set():    color = (160, 90, 30)
                if (x, y) in {(c["x"], c["y"]) for c in self.estado["celdas_sucias"]}:
                    color = (140, 100, 30)
                if (x, y) in self.estaciones:                   color = (40, 140, 90)
                if (x, y) in self.zonas_espera:                 color = (50, 80, 140)
                pygame.draw.rect(s, color, rect)

        # Objetos pesados.
        for o in self.estado["objetos_pesados"]:
            cx, cy = o["x"] * cp + cp // 2, o["y"] * cp + cp // 2
            pygame.draw.circle(s, (200, 200, 50), (cx, cy), cp // 3, 3)

        # Agentes.
        colores_cap = {
            "limpieza":    (200,  80,  80),
            "transporte":  ( 80, 160, 200),
            "supervision": (180, 130, 220),
        }
        for ag in self.estado["agentes"]:
            cx, cy = ag["x"] * cp + cp // 2, ag["y"] * cp + cp // 2
            caps = self.agentes_cfg[ag["id"]]["capacidades"]
            color = colores_cap.get(caps[0], (220, 220, 220))
            if ag["estado"] == "averiado":
                color = (60, 60, 60)
            pygame.draw.circle(s, color, (cx, cy), cp // 3)
            label = self._font.render(ag["id"], True, (240, 240, 240))
            s.blit(label, (cx - cp // 4, cy - cp // 2))
            # Barra de energia.
            ratio = ag["energia"] / max(1, self.agentes_cfg[ag["id"]]["energia_maxima"])
            bar_w = int(cp * ratio)
            pygame.draw.rect(s, (50, 200, 50),
                             (ag["x"] * cp, ag["y"] * cp + cp - 3, bar_w, 2))

        # Panel lateral.
        px = self.ancho * cp + 10
        y0 = 10
        for k, v in [
            ("tick",                   self.tick_actual),
            ("tareas pendientes",      metricas.get("tareas_pendientes", 0)),
            ("agentes trabajando",     metricas.get("agentes_trabajando", 0)),
            ("tasa ocupacion",         f"{metricas.get('tasa_ocupacion', 0):.2f}"),
            ("energia promedio",       f"{metricas.get('energia_promedio', 0):.1f}"),
            ("cooperaciones activas",  metricas.get("cooperaciones_activas", 0)),
            ("conflictos potenciales", metricas.get("conflictos_potenciales", 0)),
            ("huerfanas acumuladas",   len(self.tareas_huerfanas_total)),
            ("fps",                    self._fps),
            ("estado",                 "PAUSA" if self._paused else "corriendo"),
        ]:
            txt = self._font.render(f"{k}: {v}", True, (220, 220, 220))
            s.blit(txt, (px, y0))
            y0 += 20

        pygame.display.flip()

    def _cerrar_pygame(self) -> None:  # pragma: no cover
        if self._pygame_ready:
            pygame.quit()
            self._pygame_ready = False


# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------

def _json_default(o: Any):
    if isinstance(o, tuple):
        return list(o)
    if isinstance(o, set):
        return sorted(o)
    raise TypeError(f"no serializable: {type(o).__name__}")


def _ruta_salida(nombre: str) -> str:
    """Resuelve el nombre dentro de la carpeta `salidas/` del proyecto.

    La carpeta se crea si no existe. Mantiene los JSONs generados fuera
    del codigo fuente para que el directorio raiz quede limpio.
    """
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "salidas")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, nombre)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentClean - simulador (SPEC 03)")
    parser.add_argument("--headless", action="store_true", help="sin ventana PyGame")
    parser.add_argument("--max-ticks", type=int, default=None,
                        help="sobreescribe max_ticks del config")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args()

    sim = Simulador(
        args.config,
        headless=args.headless,
        max_ticks_override=args.max_ticks,
    )
    resumen = sim.run()

    print("=== Resumen de simulacion ===")
    for k, v in resumen.items():
        print(f"  {k}: {v}")
