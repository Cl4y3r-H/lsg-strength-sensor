# Sensor de Fuerza — LifeSync-Games × Project Zomboid

Sensor virtual que lee los entrenamientos registrados en **wger**, valida su
coherencia conductual (Propuesta §4.4), los convierte en puntos de la dimensión
**Físico** (GDD §2) y los reporta a **LSG-Core-API**.

Solo usa la biblioteca estándar de Python ≥ 3.11 (sin dependencias externas).

## Flujo

```
wger ──► validar ──► puntos + tope diario ──► SQLite (atómico)
                                                 │
                       ┌─────────────────────────┴───────────────┐
                       ▼                                         ▼
       POST /sensors/ingest/webhook                Creditor (estrategia configurable)
       (dato crudo, trazabilidad)                  dry_run | offline_sync | adjust
```

Todo se persiste **antes** de enviarse. Si la red falla, lo pendiente se reenvía
en la siguiente pasada; los `client_ref` deterministas evitan duplicados.

## Reglas implementadas (`rules.py`)

| Regla | Parámetro | Valor por defecto |
|---|---|---|
| Conversión `floor(S/3)·10` | `sets_per_block`, `points_per_block` | 3, 10 |
| Tope diario Físico | `daily_cap` | 30 |
| Enfriamiento por músculo (entre días) | `muscle_cooldown_hours` | 48 (**pendiente: propuesta dice 24**) |
| Plausibilidad de la serie | `max_reps`, `max_weight_kg` | 50, 500 |
| Progresión de carga realista | `max_progression_pct` | 20 % |
| Volumen diario máximo | `max_sets_per_day` | 40 |
| Frecuencia semanal | `max_training_days_per_week` | 6 |

Los bloques de 3 series y el tope se calculan **acumulando el día completo**,
así el resultado es el mismo aunque el sensor procese la sesión en varias pasadas.

## Uso

```bash
export WGER_TOKEN=...        # wger → Ajustes → API key
export LSG_PASSWORD=...      # contraseña LSG
cp config.example.toml config.toml   # completar email, IDs y estrategia

PYTHONPATH=src python -m lsg_strength_sensor -c config.toml check-lsg  # login, roles, sensores, saldo
PYTHONPATH=src python -m lsg_strength_sensor -c config.toml muscles    # para armar muscle_map
PYTHONPATH=src python -m lsg_strength_sensor -c config.toml run        # una pasada
PYTHONPATH=src python -m lsg_strength_sensor -c config.toml loop       # cada N minutos
PYTHONPATH=src python -m lsg_strength_sensor -c config.toml status     # estado local

PYTHONPATH=src python -m unittest discover -s tests -v                 # pruebas
```

## Pendientes que dependen del laboratorio

1. **Mecanismo oficial sensor → ledger.** El webhook no acredita puntos. Mientras
   se confirma, usar `credit_strategy = "dry_run"`: los créditos quedan en cola
   local y se envían al cambiar de estrategia (ojo: `offline_sync` rechaza
   eventos de más de 30 días).
2. **Alta del sensor** (`POST /sensors`, `/link`, `/link-endpoint`) para obtener
   `sensor_endpoint_id` y `players_sensor_endpoint_id`.
3. **`point_dimension_id` vigente** de FISICO_BASE.
4. **Zona horaria** del servidor (define el reinicio del tope diario).

## Pendientes de verificación contra wger

Los nombres de campo están aislados en `WgerClient._parse_log` y
`_parse_exercise`. Verificar contra `GET /api/v2/schema` de la instancia usada:
nombre del campo de repeticiones, unidad de peso (kg/lb), formato de `date`
(si es solo fecha, se asume 12:00 local y el cooldown pierde precisión horaria)
y estructura de `exerciseinfo`.

## Contexto

Parte del trabajo de título *"Aplicación del framework LifeSync-Games al videojuego Project Zomboid
a través de un mod y sensores virtuales especializados"* (Claudio Hernández Hernández, Departamento
de Ingeniería Informática, Universidad de Santiago de Chile, 2026).

| Repositorio | Contenido |
|---|---|
| [lsg-pz-mod](https://github.com/Cl4y3r-H/lsg-pz-mod) | Mod de Project Zomboid (Build 42) y prueba de concepto Java ↔ Lua |
| [lsg-strength-sensor](https://github.com/Cl4y3r-H/lsg-strength-sensor) | Sensor de entrenamiento de fuerza (wger) |
| [lsg-reading-sensor](https://github.com/Cl4y3r-H/lsg-reading-sensor) | Sensor de ocio lector (KOReader) |
