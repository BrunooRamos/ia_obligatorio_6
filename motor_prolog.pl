% AgentClean - Capa 3 (SPEC 04): optimizacion y analisis offline.
% F1: ruteo optimo (profundizacion iterativa).
% F2: redistribucion global (patron "no existe mejor") + fallback greedy.
% F3: analisis post-simulacion sobre hechos derivados de la traza.
%
% Python (puente_prolog.py) asierta los hechos dinamicos antes de cada
% consulta y retracta_all al volver. Las consultas usan findall y se
% materializan inmediatamente desde Python (no se itera el generador lazy).

:- use_module(library(lists)).

% =====================================================================
% Predicados dinamicos
% =====================================================================

% F1 / F2 - estado vigente cargado por el simulador.
:- dynamic(dim_grilla/2).
:- dynamic(bloqueada/2).
:- dynamic(origen/3).
:- dynamic(objetivo/3).
:- dynamic(agente/1).
:- dynamic(pos_agente/3).
:- dynamic(tarea/3).
:- dynamic(apto/2).

% F3 - hechos derivados de la traza.
:- dynamic(tr_huerfana/4).
:- dynamic(tr_energia/2).
:- dynamic(tr_conflictos/2).
:- dynamic(tr_completada/2).
:- dynamic(tr_completada_por/3).
:- dynamic(tr_evento/3).
:- dynamic(tr_cooperacion/4).

% =====================================================================
% F1 - Ruteo optimo (profundizacion iterativa)
% =====================================================================

manhattan(X1, Y1, X2, Y2, D) :-
    D is abs(X1 - X2) + abs(Y1 - Y2).

% Adyacencia 4-conexion, dentro de grilla y no bloqueada.
adyacente(X, Y, X2, Y) :-
    X2 is X + 1, dim_grilla(W, _), X2 < W, \+ bloqueada(X2, Y).
adyacente(X, Y, X2, Y) :-
    X2 is X - 1, X2 >= 0, \+ bloqueada(X2, Y).
adyacente(X, Y, X, Y2) :-
    Y2 is Y + 1, dim_grilla(_, H), Y2 < H, \+ bloqueada(X, Y2).
adyacente(X, Y, X, Y2) :-
    Y2 is Y - 1, Y2 >= 0, \+ bloqueada(X, Y2).

% DLS (depth-limited search) con visitados para evitar ciclos.
% buscar_dls(MetaX, MetaY, ProfRestante, X, Y, Visitados, CaminoAcc, Camino)
buscar_dls(MX, MY, _, MX, MY, _, Acc, Camino) :-
    reverse([[MX, MY] | Acc], Camino), !.
buscar_dls(MX, MY, Prof, X, Y, Vis, Acc, Camino) :-
    Prof > 0,
    adyacente(X, Y, X2, Y2),
    \+ member([X2, Y2], Vis),
    Prof1 is Prof - 1,
    buscar_dls(MX, MY, Prof1, X2, Y2,
               [[X, Y] | Vis], [[X, Y] | Acc], Camino).

% Profundizacion iterativa: arranca en Cota = Manhattan, sube de a 1 hasta MaxProf.
iddfs(Cota, MaxProf, _, _, _, _, _) :-
    Cota > MaxProf, !, fail.
iddfs(Cota, _, X, Y, MX, MY, Camino) :-
    buscar_dls(MX, MY, Cota, X, Y, [], [], Camino), !.
iddfs(Cota, MaxProf, X, Y, MX, MY, Camino) :-
    Cota1 is Cota + 1,
    iddfs(Cota1, MaxProf, X, Y, MX, MY, Camino).

ruta_optima(Ag, Camino) :-
    origen(Ag, X0, Y0),
    objetivo(Ag, MX, MY),
    manhattan(X0, Y0, MX, MY, Cota0),
    dim_grilla(W, H),
    MaxProf is W * H,
    iddfs(Cota0, MaxProf, X0, Y0, MX, MY, Camino).

% =====================================================================
% F2 - Redistribucion global
% =====================================================================

costo_asignacion(Ag, T, C) :-
    pos_agente(Ag, AX, AY),
    tarea(T, X, Y),
    manhattan(AX, AY, X, Y, C).

% asignacion_completa(Tareas, AgentesDisp, Pares, CostoTotal).
% Cada tarea recibe un agente apto distinto; suma de costos Manhattan.
asignacion_completa([], _, [], 0).
asignacion_completa([T | RestoT], Disp, [T-Ag | RestoA], Costo) :-
    member(Ag, Disp),
    apto(Ag, T),
    costo_asignacion(Ag, T, C),
    select(Ag, Disp, Disp2),
    asignacion_completa(RestoT, Disp2, RestoA, RestoC),
    Costo is C + RestoC.

asignacion_global(Asig, Costo) :-
    findall(T, tarea(T, _, _), Tareas),
    findall(Ag, agente(Ag), Agentes),
    asignacion_completa(Tareas, Agentes, Asig, Costo).

% Patron "no existe mejor" (sin operadores de agregacion).
mejor_redistribucion(Asig, Costo) :-
    asignacion_global(Asig, Costo),
    \+ ( asignacion_global(_, C2), C2 < Costo ).

% Greedy: cada tarea al agente apto mas cercano (sin garantia de optimo global).
% Usado como fallback cuando hay demasiadas tareas para enumerar exhaustivamente.
redistribucion_greedy(Asig, Costo) :-
    findall(T, tarea(T, _, _), Tareas),
    findall(Ag, agente(Ag), Agentes),
    greedy_pasos(Tareas, Agentes, Asig, Costo).

greedy_pasos([], _, [], 0).
greedy_pasos([T | Resto], Disp, [T-Ag | RestoA], Costo) :-
    findall(C-Cand,
            ( member(Cand, Disp), apto(Cand, T),
              costo_asignacion(Cand, T, C) ),
            Pares),
    Pares = [_ | _],
    sort(Pares, [MinC-Ag | _]),
    select(Ag, Disp, Disp2),
    greedy_pasos(Resto, Disp2, RestoA, RC),
    Costo is MinC + RC.
% Si una tarea no tiene agente apto disponible, queda sin asignar.
greedy_pasos([_ | Resto], Disp, RestoA, Costo) :-
    greedy_pasos(Resto, Disp, RestoA, Costo).

% =====================================================================
% F3 - Analisis post-simulacion
% =====================================================================

% Desperdicio total = suma de ticks_invertidos de todas las huerfanas.
desperdicio_total(D) :-
    findall(T, tr_huerfana(_, _, T, _), Ts),
    sum_list(Ts, D).

% Tareas completadas y huerfanas totales.
total_completadas(N) :-
    findall(1, tr_completada(_, _), L),
    length(L, N).

total_huerfanas(N) :-
    findall(1, tr_huerfana(_, _, _, _), L),
    length(L, N).

total_cooperaciones(N) :-
    findall(1, tr_cooperacion(_, _, _, _), L),
    length(L, N).

% Eficiencia = completadas / (completadas + huerfanas).
eficiencia_global(R) :-
    total_completadas(NC),
    total_huerfanas(NH),
    Total is NC + NH,
    ( Total > 0 -> R is NC / Total ; R = 1.0 ).

tareas_por_agente(Ag, N) :-
    findall(1, tr_completada_por(_, _, Ag), L),
    length(L, N).

% Agente mas eficiente = mayor (tareas completadas / energia consumida).
% Patron "no existe mejor".
agente_mas_eficiente(Ag, Ratio) :-
    agente(Ag),
    tareas_por_agente(Ag, N),
    tr_energia(Ag, E),
    E > 0,
    Ratio is N / E,
    \+ ( agente(Ag2),
         Ag2 \= Ag,
         tareas_por_agente(Ag2, N2),
         tr_energia(Ag2, E2),
         E2 > 0,
         R2 is N2 / E2,
         R2 > Ratio ).

% Tick con mayor numero de conflictos potenciales.
pico_conflictos(Tick, N) :-
    tr_conflictos(Tick, N),
    \+ ( tr_conflictos(_, N2), N2 > N ).

% Correlacion averia -> huerfana del mismo agente.
averia_causo_desperdicio(Ag, Tarea, Ticks) :-
    tr_evento(_, averia, Ag),
    tr_huerfana(Tarea, Ag, Ticks, averia).

% Recomendaciones legibles a partir de los hechos.
recomendacion(R) :-
    findall(Ag, tr_huerfana(_, Ag, _, sin_energia), Ags),
    Ags = [_ | _],
    sort(Ags, [A | _]),
    format(atom(R),
           'el umbral de recarga de ~w parece insuficiente (genero huerfana por agotamiento)',
           [A]).

recomendacion(R) :-
    averia_causo_desperdicio(Ag, _, Ticks),
    Ticks > 5,
    format(atom(R),
           'la averia de ~w en plena tarea desperdicio ~w ticks; conviene redundancia o roles cooperativos',
           [Ag, Ticks]).

recomendacion(R) :-
    pico_conflictos(Tick, N),
    N >= 3,
    format(atom(R),
           'pico de ~w conflictos potenciales en tick ~w sugiere demasiados agentes apuntando a la misma zona',
           [N, Tick]).

recomendacion('sin recomendaciones criticas: el sistema operó dentro de parametros esperados') :-
    \+ tr_huerfana(_, _, _, _),
    \+ ( tr_conflictos(_, N), N >= 3 ).
