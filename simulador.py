"""
AgentClean - Simulador consola (entrega intermedia).

Arquitectura de dos capas:
  - Capa 1 (este archivo): estado del mundo, ciclo por tick, compromisos
    persistentes, eventos del entorno, salida por consola.
  - Capa 2 (planificador_pydatalog.py): coordinacion reactiva por snapshot.

Sin PyGame ni Prolog. Los agentes se mueven con paso greedy Manhattan.
"""

from __future__ import annotations

import argparse
import json
import os
import random
from typing import Any

import modelo_datos
import planificador_pydatalog


TIPO_LIMPIEZA   = "limpieza"
TIPO_TRANSPORTE = "transporte"
TIPO_RECARGA    = "recarga"


# ---------------------------------------------------------------------------
# Simulador
# ---------------------------------------------------------------------------

class Simulador:
    def __init__(
        self,
        config_path: str,
        max_ticks_override: int | None = None,
        verbose: bool = True,
    ):
        self.cfg = modelo_datos.cargar_config(config_path)

        modelo_datos.declarar_vocabulario()
        modelo_datos.cargar_hechos_estaticos(self.cfg)
        planificador_pydatalog.definir_reglas()

        self.estado: dict = modelo_datos.estado_inicial_desde_config(self.cfg)
        self.tick_actual: int = 0
        self.verbose = verbose

        sim = self.cfg.get("simulacion", {})
        self.max_ticks    = max_ticks_override or sim.get("max_ticks", 300)
        self.tasa_recarga = sim.get("tasa_recarga", 25)
        self.semilla      = sim.get("semilla", 42)
        random.seed(self.semilla)

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

        self.compromisos: dict[str, dict] = {}

        self.traza_ticks: list[dict] = []
        self.eventos_aplicados: list[dict] = []
        self.tareas_completadas_total: list[dict] = []
        self.tareas_huerfanas_total:   list[dict] = []
        self.cooperaciones_completadas: int = 0
        self.energia_consumida_total:   int = 0

    # =========================================================
    # Helpers
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
        modelo_datos.refrescar_hechos_dinamicos(self.estado)

        decisiones = planificador_pydatalog.planificar()
        metricas   = planificador_pydatalog.calcular_metricas()

        completadas: list[dict] = []
        huerfanas:   list[dict] = []

        self._conciliar(decisiones, huerfanas)
        self._aplicar(decisiones, completadas, huerfanas)
        self._aplicar_eventos(huerfanas)

        registro = self._registrar_tick(decisiones, metricas, completadas, huerfanas)

        if self.verbose:
            self._imprimir_tick(metricas, completadas, huerfanas)

        self.tareas_completadas_total.extend(completadas)
        self.tareas_huerfanas_total.extend(huerfanas)
        self.tick_actual += 1
        return registro

    def run(self) -> dict:
        print(f"=== AgentClean — grilla {self.ancho}x{self.alto}, "
              f"{len(self.cfg['agentes'])} agentes, max {self.max_ticks} ticks ===\n")

        terminacion = "max_ticks"
        while self.tick_actual < self.max_ticks:
            self.tick()
            if not self.estado["celdas_sucias"] and not self.estado["objetos_pesados"]:
                terminacion = "limpieza_completa"
                break

        resumen = self._construir_resumen(terminacion)

        ruta_traza = _ruta_salida("traza_simulacion.json")
        traza = self.estado_para_traza()
        traza["resumen"] = resumen
        with open(ruta_traza, "w", encoding="utf-8") as f:
            json.dump(traza, f, indent=2, ensure_ascii=False, default=_json_default)

        print(f"\n[traza guardada en {ruta_traza}]")
        return resumen

    # =========================================================
    # Conciliacion
    # =========================================================

    def _conciliar(self, decisiones: dict, huerfanas: list[dict]) -> None:
        sucias = self._sucias_set()
        for aid in list(self.compromisos.keys()):
            com = self.compromisos[aid]
            ag = self._agente(aid)
            agcfg = self.agentes_cfg[aid]

            if ag["estado"] == "averiado":
                self._orfanar(aid, "averia", huerfanas)
                continue

            if (com["tipo"] in (TIPO_LIMPIEZA, TIPO_TRANSPORTE)
                    and ag["energia"] <= agcfg["umbral_recarga"]):
                self._orfanar(aid, "sin_energia", huerfanas)
                continue

            if com["tipo"] == TIPO_LIMPIEZA:
                if com["objetivo"] not in sucias:
                    self._liberar(aid)
            elif com["tipo"] == TIPO_TRANSPORTE:
                if self._objeto_pesado_pos(com["obj"]) is None:
                    self._liberar(aid)

        recargas_map  = {a: (ex, ey) for (a, ex, ey) in decisiones.get("recargas", [])}
        asig_lim_map  = {a: (x, y)   for (a, x, y)  in decisiones.get("asignaciones_limpieza", [])}
        acciones_conj = decisiones.get("acciones_conjuntas", [])

        comprom_lim_objs    = {com["objetivo"] for com in self.compromisos.values() if com["tipo"] == TIPO_LIMPIEZA}
        comprom_objs_pesados = {com["obj"]     for com in self.compromisos.values() if com["tipo"] == TIPO_TRANSPORTE}

        for ag in self.estado["agentes"]:
            aid = ag["id"]
            if aid in self.compromisos or ag["estado"] == "averiado":
                continue

            if aid in recargas_map:
                self._comprometer(aid, TIPO_RECARGA, recargas_map[aid])
                continue

            if aid in asig_lim_map and asig_lim_map[aid] not in comprom_lim_objs:
                target = asig_lim_map[aid]
                self._comprometer(aid, TIPO_LIMPIEZA, target)
                comprom_lim_objs.add(target)
                continue

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
    # Aplicacion (movimientos, acciones, energia)
    # =========================================================

    def _aplicar(self, decisiones: dict, completadas: list[dict], huerfanas: list[dict]) -> None:
        for ag in self.estado["agentes"]:
            if ag["estado"] == "averiado":
                continue
            aid = ag["id"]
            com = self.compromisos.get(aid)

            if com is None:
                if aid in decisiones.get("a_espera", []):
                    target = self._mas_cercana(ag, self.zonas_espera)
                    if target and (ag["x"], ag["y"]) != target:
                        self._mover_un_paso(ag, target)
                continue

            agcfg = self.agentes_cfg[aid]

            if com["tipo"] == TIPO_LIMPIEZA:
                target = com["objetivo"]
                if (ag["x"], ag["y"]) == target:
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
                if self._manhattan((ag["x"], ag["y"]), obj_pos) > 1:
                    self._mover_un_paso(ag, obj_pos)
                    self._verificar_averia_por_energia(ag, huerfanas)

            elif com["tipo"] == TIPO_RECARGA:
                target = com["objetivo"]
                if (ag["x"], ag["y"]) == target:
                    ag["estado"] = "recargando"
                    ag["energia"] = min(ag["energia"] + self.tasa_recarga, agcfg["energia_maxima"])
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
        return False

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
    # Eventos del entorno
    # =========================================================

    def _aplicar_eventos(self, huerfanas: list[dict]) -> None:
        for (x, y) in self.evt_nuevas_sucias.get(self.tick_actual, []):
            if (x, y) in self._sucias_set():
                continue
            if (x, y) in self.obstaculos_fijos_set:
                continue
            self.estado["celdas_sucias"].append({"x": x, "y": y})
            self.eventos_aplicados.append({
                "tipo": "nueva_sucia", "tick": self.tick_actual, "x": x, "y": y,
            })

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

        for om in self.estado["obstaculos_moviles"]:
            ruta = self.patron_om.get(om["id"])
            if not ruta:
                continue
            idx = (self.tick_actual + 1) % len(ruta)
            nueva = ruta[idx]
            destino = (nueva["x"], nueva["y"])
            if (om["x"], om["y"]) == destino:
                continue
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
    # Salida por consola
    # =========================================================

    def _imprimir_tick(
        self,
        metricas: dict,
        completadas: list[dict],
        huerfanas: list[dict],
    ) -> None:
        sep = "-" * 50
        print(f"\n{sep}")
        print(f"TICK {self.tick_actual:>4}  |  sucias: {len(self.estado['celdas_sucias'])}  "
              f"objetos: {len(self.estado['objetos_pesados'])}")
        print(sep)

        # Estado de cada agente
        for ag in self.estado["agentes"]:
            com = self.compromisos.get(ag["id"])
            if com:
                if com["tipo"] == TIPO_LIMPIEZA:
                    tarea = f"-> limpieza ({com['objetivo'][0]},{com['objetivo'][1]})"
                elif com["tipo"] == TIPO_TRANSPORTE:
                    tarea = f"-> transporte {com['obj']} c/{com.get('companero','?')}"
                elif com["tipo"] == TIPO_RECARGA:
                    tarea = f"-> recarga ({com['objetivo'][0]},{com['objetivo'][1]})"
                else:
                    tarea = f"-> {com['tipo']}"
            else:
                tarea = "(libre/espera)"

            estado_str = ag["estado"].upper() if ag["estado"] == "averiado" else ag["estado"]
            print(f"  {ag['id']}  pos=({ag['x']:2},{ag['y']:2})  "
                  f"E={ag['energia']:3}  [{estado_str}]  {tarea}")

        # Tareas completadas este tick
        if completadas:
            print("  Completadas:")
            for c in completadas:
                if c["tipo"] == "limpieza":
                    print(f"    [+] limpieza ({c['x']},{c['y']}) por {c['agente']}")
                elif c["tipo"] == "transporte":
                    print(f"    [+] transporte {c['obj']} por {c['agentes']}")
                elif c["tipo"] == "recarga":
                    print(f"    [+] recarga de {c['agente']}")

        # Tareas huerfanas este tick
        if huerfanas:
            print("  Huerfanas:")
            for h in huerfanas:
                print(f"    [!] {h['agente']} abandona {h['tarea']} "
                      f"({h['ticks_invertidos']} ticks, causa: {h['causa']})")

        # Eventos del entorno aplicados en este tick
        evts_tick = [e for e in self.eventos_aplicados if e.get("tick") == self.tick_actual]
        if evts_tick:
            print("  Eventos:")
            for e in evts_tick:
                if e["tipo"] == "nueva_sucia":
                    print(f"    [~] nueva celda sucia en ({e['x']},{e['y']})")
                elif e["tipo"] == "averia":
                    print(f"    [x] averia de {e['agente']} (causa: {e['causa']})")
                elif e["tipo"] == "obstaculo_movido":
                    print(f"    [>] obstaculo {e['id']}: "
                          f"({e['desde'][0]},{e['desde'][1]}) -> ({e['hacia'][0]},{e['hacia'][1]})")

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

    def estado_para_traza(self) -> dict:
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
            "ticks_totales":               self.tick_actual,
            "tareas_completadas":          tareas_completadas,
            "tareas_huerfanas":            len(self.tareas_huerfanas_total),
            "trabajo_desperdiciado_total": sum(
                h["ticks_invertidos"] for h in self.tareas_huerfanas_total
            ),
            "energia_consumida_total":     self.energia_consumida_total,
            "cooperaciones_completadas":   self.cooperaciones_completadas,
            "terminacion":                 terminacion,
        }


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
    base = os.path.join(os.path.dirname(os.path.abspath(__file__)), "salidas")
    os.makedirs(base, exist_ok=True)
    return os.path.join(base, nombre)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="AgentClean - simulador consola")
    parser.add_argument("--max-ticks", type=int, default=None)
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--quiet", action="store_true",
                        help="solo imprime el resumen final")
    args = parser.parse_args()

    sim = Simulador(
        args.config,
        max_ticks_override=args.max_ticks,
        verbose=not args.quiet,
    )
    resumen = sim.run()

    print("\n=== Resumen de simulacion ===")
    for k, v in resumen.items():
        print(f"  {k}: {v}")
