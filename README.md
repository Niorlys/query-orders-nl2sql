# query-orders-nl2sql

Demo de pedidos y consultas en lenguaje natural. Incluye el generador reproducible
y funciones de normalización y clasificación independientes de la base de datos,
con carga a SQLite, vistas de ventas, contratos de FastAPI y un ejecutor SQL
validado de solo lectura. La conexión al LLM y la integración del ejecutor con
el endpoint todavía no están implementadas.

## Generar datos

Requiere Python 3.11 o superior para todas las etapas, incluidos los límites de
SQLite del ejecutor. El generador usa únicamente la biblioteca
estándar y no necesita una API key ni llamadas a OpenAI.

```bash
python3 manage.py generate
```

Se crean `data/raw/pedidos.csv` y `data/raw/manifest.json`. Repetir el comando
sobrescribe esos dos archivos con el mismo contenido. La ruta predeterminada
se resuelve desde el proyecto, independientemente del directorio de ejecución.
Los resultados de limpieza se guardan en `data/processed/`; nunca reemplazan
el CSV crudo. `generator.py` contiene la generación y `manage.py` la CLI.

Opcionalmente se pueden cambiar la semilla y el directorio de salida:

```bash
python3 manage.py generate --seed 42 --output-dir /tmp/orders-demo
```

La semilla predeterminada es `20250301`. Se usa un generador aleatorio local,
sin depender del reloj. Las fechas de creación van del 1 de enero de 2024 al
15 de diciembre de 2025; la finalización ocurre entre 0 y 14 días después.
Los originales se ordenan por ID y los duplicados se añaden al final, ordenados
por la posición del original. Los archivos usan UTF-8 y saltos LF. Para
reproducción byte a byte entre entornos se recomienda la misma versión de
Python, ya que los algoritmos auxiliares de `random` pueden cambiar.

## Datos y criterio de ventas

Columnas: `pedido_id`, `fecha`, `fecha_completado`, `cliente`, `país`, `producto`,
`importe`, `moneda`, `estado` y `cliente_id`. Las variables y funciones están en inglés;
las columnas y los estados del dominio se mantienen en español.
Cada pedido tiene un producto, importe con dos decimales y moneda `USD`.
Hay 120 clientes sintéticos con ID y país estables y seis productos con precio fijo.
`cliente_id` es independiente del nombre: dos personas con el mismo nombre pueden
tener IDs diferentes. Los CSV de la versión anterior deben regenerarse con
`python3 manage.py generate` para incorporar esta columna; no se infiere el ID
a partir del nombre. Los conteos de pedidos y errores de la semilla predeterminada
se mantienen, pero el hash del CSV cambia al añadir la columna.
Los estados se sortean con pesos de 20% pendiente, 70% completado y 10% cancelado;
son probabilidades, no cuotas exactas.

| Estado | Significado | Suma en ventas |
| --- | --- | --- |
| `pendiente` | Registrado, todavía sin completar | No |
| `completado` | Producto entregado o servicio prestado | Sí |
| `cancelado` | Anulado antes de completarse | No |

Conservamos pedidos pendientes, completados y cancelados. Las métricas de ventas
y el ranking de clientes incluirán únicamente pedidos completados y usarán
`fecha_completado`, no `fecha`. Un pedido creado el 31 de marzo y completado
el 2 de abril pertenece a las ventas de abril. `fecha_completado` queda vacía
para pendientes y cancelados: eso es válido, no un error. Los cobros no se
modelan ni se infieren del estado. Esta versión no contempla devoluciones
posteriores a una venta.

Un pedido cancelado válido se conservará en `pedidos`. `pedidos_rechazados` se
reservará para problemas de calidad irrecuperables sin inventar información,
como una fecha imposible o incompleta o un importe vacío.

## Errores controlados y manifiesto

Primero se sortean cantidades enteras uniformes en estos rangos inclusivos:

| Categoría | Rango | Resultado con la semilla predeterminada |
| --- | --- | --- |
| Pedidos originales | 1.001–2.000 | 1.508 |
| Filas duplicadas adicionales | 0–40 | 36 |
| Pedidos con una fecha inválida o incompleta | 0–40 | 21 |
| Pedidos con importe vacío | 0–10 | 1 |

El CSV predeterminado contiene 1.544 filas de datos, más la cabecera.
Además, se selecciona el 25% de los originales para variantes recuperables de
países, el 20% para espacios alrededor de cliente, producto o estado, y el 30%
para formatos de fecha completos. Las cantidades se redondean hacia abajo:
377, 301 y 452, respectivamente, con la semilla predeterminada.
Los formatos válidos son `YYYY-MM-DD`, `DD/MM/YYYY` y `YYYY/MM/DD`; el formato
con día primero nunca se interpreta como mes primero. Las variantes de países
incluyen diferencias de mayúsculas, tildes y `USA` para `Estados Unidos`.

**Todos los porcentajes del manifiesto usan como denominador el número de
pedidos originales**, incluso el porcentaje de duplicados adicionales, y nunca
el total final de filas. Las muestras de cada categoría son independientes y
pueden solaparse. Dentro de cada categoría no se repite un pedido. Una fecha
inválida puede reemplazar una variante de formato aplicada previamente; las
cantidades de variantes cuentan operaciones inyectadas, no necesariamente
variantes visibles en el CSV final.

Los errores de fecha afectan `fecha` o, solo en completados, `fecha_completado`.
Se inyectan fechas imposibles (`2025-02-30`, `2025-13-01`), incompletas
(`2025-03`) o vacías. No se deben inferir partes faltantes durante la limpieza.
Los duplicados se copian después de inyectar los errores y pueden repetir
pedidos defectuosos. Por eso las filas defectuosas del CSV completo pueden ser
más que los pedidos originales defectuosos.

El manifiesto registra configuración, cantidades, porcentajes y SHA-256 del CSV.
La lista `errors` contiene solo ID, columna y tipo de los errores que provocan
rechazo. Las variaciones recuperables se contabilizan sin registrar cada cambio
ni guardar valores originales. También registra las posiciones de cada duplicado.
Los números de fila empiezan en 1 e incluyen
la cabecera: el primer pedido está en la fila 2. También contiene los IDs
rechazables y los conteos esperados de aceptados/rechazados y estados
**después de deduplicar**. Un original con ambos errores se rechaza una sola
vez. El manifiesto sirve para verificar la futura limpieza; el limpiador deberá
trabajar a partir del CSV, sin depender del manifiesto.

## Normalizar y clasificar filas

Para limpiar el CSV y guardar los resultados en SQLite:

```bash
python3 manage.py generate
python3 manage.py clean
```

Si ya generaste el CSV, basta con el segundo comando. La base se crea en
`data/processed/pedidos.sqlite3`. Ambas rutas predeterminadas se resuelven desde
el proyecto. También puedes elegir archivos explícitamente:

```bash
python3 manage.py clean --input data/raw/pedidos.csv --database data/processed/pedidos.sqlite3
```

El comando muestra los conteos y verifica que coincidan con los almacenados:

```text
Filas leídas: 1544
Cargadas (pedidos): 1486
Duplicadas (pedidos_duplicados): 36
Rechazadas (pedidos_rechazados): 22
Motivos de rechazo (una fila puede tener varios):
  fecha:invalid_date: 18
  fecha_completado:invalid_date: 3
  importe:missing_amount: 1
```

`storage.py` se encarga de leer el CSV y persistir los resultados. SQLite viene
incluido con Python, por lo que no se requiere un servidor ni otra dependencia.
Las tablas son:

- `pedidos`: pedidos aceptados de los tres estados, ID y nombre del cliente, importe en céntimos enteros,
  número de fila y datos originales en `original_json`.
- `pedidos_rechazados`: número de fila, originales, datos normalizados y todos
  los motivos en columnas JSON de texto.
- `pedidos_duplicados`: la misma auditoría y `duplicate_of`, que apunta al número
  de fila de la primera aparición, aceptada o rechazada.

**Cada carga reemplaza el contenido de estas tres tablas** con el CSV indicado;
no acumula ejecuciones ni conserva un historial de cargas. Se realiza en una
transacción: si falla la escritura, los datos anteriores permanecen intactos.
Las restricciones de SQLite exigen IDs positivos y únicos, campos obligatorios
no nulos ni vacíos, céntimos enteros positivos, países del catálogo, moneda USD
y estados permitidos. También comprueban la coherencia entre estado y fecha de
finalización. Las filas de auditoría requieren número de fila válido y datos y
motivos presentes. Los esquemas anteriores de la demo se actualizan automáticamente
en la primera carga con estas restricciones, dentro de la misma transacción;
si falla, se recuperan tanto los datos como el esquema anterior.
Repetir el comando con el mismo CSV no duplica pedidos. El CSV original no se
modifica. Los directorios generados `data/raw/` y `data/processed/` están en
`.gitignore` (esto no desversiona archivos que ya estuvieran registrados en Git).

Para comprobar la base directamente, si tienes instalado el cliente `sqlite3`:

```bash
sqlite3 -header -column data/processed/pedidos.sqlite3 "SELECT estado, COUNT(*) AS pedidos FROM pedidos GROUP BY estado;"
sqlite3 -header -column data/processed/pedidos.sqlite3 "SELECT row_number, reasons_json FROM pedidos_rechazados LIMIT 5;"
```

El primer comando debe mostrar 139 cancelados, 1.055 completados y 292 pendientes
con la semilla predeterminada. La CLI no requiere ese cliente externo.

`cleaning.py` expone funciones puras, sin acceso a archivos, al manifiesto ni a una
base de datos. `normalize_row` conserva el original, devuelve los valores
normalizados y recopila todos los problemas detectados. `classify_rows` analiza
el lote completo para detectar conflictos de ID, incluso si aparecen al final.

```python
import csv
from cleaning import classify_rows, summarize_rows

with open("data/raw/pedidos.csv", encoding="utf-8", newline="") as source:
    results = classify_rows(csv.DictReader(source))

print(summarize_rows(results))
accepted_orders = [row.normalized for row in results if row.category == "accepted"]
```

Cada resultado conserva `row_number`, `original`, `normalized`, `reasons`,
`category` y `duplicate_of` (referencia a la primera fila, cuando corresponde).
La numeración empieza en 2 por la cabecera y cuenta registros CSV, no líneas
físicas: una celda entrecomillada puede contener saltos de línea. El argumento
`first_row_number` permite cambiar el inicio. Los datos de entrada no se modifican.
Solo los resultados `accepted` contienen datos listos para cargar; los demás
conservan también los valores inválidos para auditoría.

Reglas de normalización y validación:

- Se eliminan espacios exteriores, se colapsan espacios interiores y se normaliza
  Unicode. Cliente y producto conservan mayúsculas y minúsculas.
- Países usan un catálogo explícito de los seis países del demo, incluyendo
  variantes sin tildes, códigos de dos letras y `USA`/`United States`. Los países
  desconocidos se rechazan. Estados admiten solo pendiente, completado y cancelado,
  sin distinguir mayúsculas; `pagado` no se convierte en `completado`.
- Las fechas admiten únicamente los tres formatos completos documentados y se
  convierten a ISO. No se infieren años ni se reparan fechas imposibles.
- Completados requieren `fecha_completado >= fecha`. Pendientes y cancelados
  requieren la finalización vacía, representada como `None` tras normalizar.
  No se impone a datos externos el plazo sintético de 14 días del generador.
- Importes requieren dígitos con punto decimal opcional y hasta dos decimales.
  Se convierten exactamente a `importe_centavos` mediante aritmética entera,
  sin `float`, redondeo ni imputación. Se rechazan importes vacíos, no positivos,
  comas, símbolos de moneda, notación científica y más de dos decimales. El demo
  requiere USD y limita los céntimos al rango entero positivo de 64 bits.
- `pedido_id` es un entero positivo de hasta 64 bits representado como texto
  canónico: ` 001 ` y `1` identifican el mismo pedido. IDs inválidos se rechazan
  individualmente. `cliente_id` requiere el mismo formato, pero puede aparecer en
  varios pedidos y no se usa para deduplicarlos. Cliente y producto son obligatorios; columnas faltantes o
  adicionales se registran como problemas de esquema.

Las categorías son **excluyentes**, aplicando esta prioridad:

1. **Conflicto:** si un ID válido tiene contenidos distintos después de normalizar,
   todas sus filas se rechazan con `pedido_id:conflicting_contents`, además de
   sus problemas individuales. Esto incluye versiones válidas mezcladas con
   inválidas; no se elige automáticamente la primera, la última ni la válida.
2. **Duplicado:** si el contenido coincide, las apariciones posteriores son
   `duplicate`, con referencia a la primera. También se deduplican copias de
   filas inválidas, conservando sus motivos de calidad y el motivo de duplicación.
3. **Calidad:** la primera aparición sin conflicto se acepta si no tiene problemas;
   en caso contrario se rechaza. Un mismo contenido con IDs diferentes representa
   pedidos distintos. Los valores inválidos se comparan tras quitar espacios;
   no se supone que dos errores distintos representan el mismo valor. Los
   problemas de esquema también forman parte de la comparación.

Siempre se cumple `total = accepted + duplicate + rejected`. Los conteos por
motivo se calculan solo sobre rechazadas y pueden sumar más que esas filas,
porque una fila puede tener varios problemas. Con el dataset predeterminado:
**1.544 filas = 1.486 aceptadas + 36 duplicadas + 22 rechazadas**.
Entre las aceptadas hay 1.055 completadas, 292 pendientes y 139 canceladas.
Estas funciones devuelven resultados en memoria; `storage.py` las usa para la
carga a SQLite sin introducir dependencias de persistencia en `cleaning.py`.

### Criterio respecto al libro

Se revisaron las secciones 2.2–2.4 de *Data Mining and Predictive Analytics*
(páginas impresas 21–26): validación de campos/unidades, tratamiento de faltantes
y consistencia de etiquetas. Esos principios sí aportan al demo. La decisión
del proyecto es conservar los datos originales y separar errores de calidad,
sin imputar fechas o importes. El escalado estadístico, los modelos predictivos
y el descarte automático de valores atípicos no son necesarios para esta tarea.
Un importe alto válido no se rechaza solo por ser inusual.

## Vistas de ventas

`views.sql` define las dos vistas y se ejecuta automáticamente en cada
`python3 manage.py clean`, dentro de la misma transacción que reemplaza los datos.
Si falla el SQL o alguna vista referencia una columna inexistente, se recuperan
los datos y las vistas anteriores. El cargador ejecuta las sentencias por separado
para evitar el commit implícito que puede realizar `executescript()`.

Ambas vistas incluyen solo `estado = 'completado'` y `moneda = 'USD'`. Los
importes se mantienen en **céntimos enteros** (`importe_total_centavos`), sin
convertirlos a `float` ni mezclar monedas. Cancelados y pendientes siguen en
`pedidos`, pero no suman ventas ni participan en el ranking.

- `ventas_por_mes_pais`: `anio_mes` (`YYYY-MM` de `fecha_completado`), `país`,
  `numero_pedidos`, `importe_total_centavos` y `moneda`. Distingue años; un pedido
  creado en marzo y entregado en abril pertenece a abril.
- `top_10_clientes`: `cliente_id`, `nombre`, `numero_pedidos`,
  `importe_total_centavos` y `moneda`. Agrupa por ID, no por nombre. Si un mismo
  ID aparece con nombres distintos, muestra el menor según orden `BINARY`
  (`MIN(cliente)`), sin dividir ni duplicar su total. Esto es una regla de
  presentación del demo, no una elección de cuál es el nombre más reciente.

El top 10 prioriza **importe total descendente**, luego **número de pedidos
descendente** y finalmente **ID numérico ascendente**. El desempate también se
aplica al seleccionar el décimo cliente. El `ORDER BY` dentro de la vista sirve
para elegir esos diez; las consultas deben incluir su propio `ORDER BY` cuando
importe el orden de presentación:

```bash
sqlite3 -header -column data/processed/pedidos.sqlite3 "SELECT * FROM ventas_por_mes_pais ORDER BY anio_mes, país;"
sqlite3 -header -column data/processed/pedidos.sqlite3 "SELECT * FROM top_10_clientes ORDER BY importe_total_centavos DESC, numero_pedidos DESC, cliente_id ASC;"
```

### Comprobación manual con datos conocidos

El archivo versionado `tests/fixtures/orders_views.csv` contiene 19 filas pequeñas.
Para probarlo sin reemplazar tu base habitual:

```bash
python3 manage.py clean --input tests/fixtures/orders_views.csv --database /tmp/pedidos-vistas-demo.sqlite3
sqlite3 -header -column /tmp/pedidos-vistas-demo.sqlite3 "SELECT * FROM ventas_por_mes_pais ORDER BY anio_mes, país;"
sqlite3 -header -column /tmp/pedidos-vistas-demo.sqlite3 "SELECT * FROM top_10_clientes ORDER BY importe_total_centavos DESC, numero_pedidos DESC, cliente_id ASC;"
```

La carga debe producir 16 aceptadas, 2 rechazadas (fecha imposible y moneda EUR)
y 1 duplicada. Las aceptadas incluyen 1 cancelada y 1 pendiente. Totales esperados:

| Año-mes | País | Pedidos completados | Total en céntimos USD |
| --- | --- | ---: | ---: |
| 2024-03 | España | 1 | 500 |
| 2025-03 | España | 11 | 24.000 |
| 2025-03 | México | 1 | 15.000 |
| 2025-04 | España | 1 | 10.000 |

España en marzo de 2025 suma 50 + 100 + nueve pedidos de 10 = **240 USD**.
El pedido de 100 USD creado el 31 de marzo y entregado el 2 de abril suma en
abril. El ranking empieza por ID 2 (Ana, 2 pedidos, 150 USD), ID 1 (Bruno,
1 pedido, 150 USD) e ID 3 (otra Ana, 2 pedidos, 105 USD). Les siguen los IDs
4 a 10 con 10 USD cada uno; los IDs 11 y 12 quedan fuera por el desempate.

Pruebas automáticas de estas vistas:

```bash
.venv/bin/python -m pytest tests/test_views.py -v
```

Verifican totales exactos, meses de finalización, separación de años, exclusión
de no ventas, homónimos, desempates, límite de diez, ausencia de ventas,
repetibilidad y rollback ante fallos en las vistas.

## API: contratos y errores

La aplicación está en `main.py`; los modelos Pydantic en `schemas.py` y las
excepciones y manejadores centralizados en `errors.py`. Para iniciarla:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m uvicorn main:app --reload
```

Documentación interactiva en [Swagger UI](http://127.0.0.1:8000/docs) y esquema en
[OpenAPI](http://127.0.0.1:8000/openapi.json). No hace falta una API key para esta
etapa: no se llama al proveedor ni se abre SQLite desde el endpoint.

`POST /query` recibe `QueryRequest`:

```json
{"question": "¿Cuánto vendimos en España en marzo de 2025?"}
```

La pregunta debe ser texto y tener entre 1 y 1.000 caracteres **después de quitar
espacios exteriores**. Se rechazan preguntas vacías, solo espacios, valores de
otro tipo y campos adicionales. No se adivina la intención de la pregunta en
esta etapa: fuera de alcance y ambigüedad tienen contratos de error preparados
para cuando se conecte el servicio.

El contrato de éxito `QueryResponse` es:

```json
{
  "columns": ["importe_total_centavos"],
  "rows": [[24000]],
  "truncated": false
}
```

Este es un ejemplo del formato, no un resultado que el endpoint calcule todavía.
Cada fila debe tener exactamente tantas celdas como columnas. Las celdas pueden
ser texto, entero, número finito, booleano o `null`; no se admiten objetos, listas,
fechas Python, bytes, `Decimal`, `NaN` ni infinitos. `truncated` es un booleano
obligatorio que indica si el ejecutor omitió filas por límites de cantidad o
tamaño. El ejecutor ya implementa esos límites, pero aún no está conectado al
endpoint HTTP.

Todos los errores usan `ErrorResponse`, construido con Pydantic y serializado
con `model_dump(mode="json")` mediante una única función que devuelve
`JSONResponse`. Su estructura es siempre `code`, `message` y `details`:

```json
{
  "code": "invalid_input",
  "message": "La entrada no es válida. Revisa los campos indicados.",
  "details": [
    {
      "location": ["body", "question"],
      "code": "string_too_short",
      "message": "La pregunta no puede estar vacía."
    }
  ]
}
```

| HTTP | Código | Excepción / condición |
| --- | --- | --- |
| 422 | `invalid_input` | `InvalidInputError` o validación de la petición |
| 422 | `out_of_scope` | `OutOfScopeError` |
| 422 | `ambiguous_question` | `AmbiguousQuestionError` |
| 502 | `invalid_llm_response` | `InvalidLLMResponseError` |
| 502 | `sql_policy_violation` | `SQLPolicyViolationError`: SQL generado fuera de la política |
| 502 | `query_resource_limit` | `QueryResourceLimitError`: presupuesto de ejecución o tamaño excedido |
| 503 | `provider_unavailable` | `ProviderUnavailableError` |
| 504 | `provider_timeout` | `ProviderTimeoutError` |
| 500 | `internal_error` | `InternalServerError`, excepción inesperada o respuesta interna mal formada |
| 503 | `service_not_ready` | Servicio de consultas aún no conectado en esta etapa |
| 404 / 405 y otros | `http_error` | Errores HTTP del framework con el mismo formato |

Las excepciones concretas heredan de `AppError`. Sus mensajes públicos son
controlados; los diagnósticos internos no se devuelven al cliente. Los errores
500 siempre tienen el mensaje genérico `Ocurrió un error interno inesperado.`
La validación no devuelve los valores recibidos ni el cuerpo completo de la
petición. `HTTPException` conserva su estado y cabeceras (por ejemplo, `Allow`
en un 405), pero no expone su `detail` arbitrario. Los estados HTTP 422, 502,
503 y 504 se asignan a sus códigos correspondientes en la tabla; otros 5xx
usan `internal_error`. Los fallos inesperados se registran en el servidor.

Una respuesta del LLM identificada como inválida será **502**; una respuesta
interna que viole `QueryResponse` es un fallo de implementación y devuelve
**500**. El futuro adaptador del proveedor deberá traducir sus fallos a las
excepciones concretas; un `TimeoutError` sin clasificar sigue siendo inesperado.

Para probar la entrada inválida y el estado actual del servicio:

```bash
curl -i http://127.0.0.1:8000/query -H 'Content-Type: application/json' -d '{"question":"   "}'
curl -i http://127.0.0.1:8000/query -H 'Content-Type: application/json' -d '{"question":"Ventas de 2025"}'
```

La primera llamada devuelve 422; la segunda devuelve **503 `service_not_ready`**.
Esto no indica una caída de OpenAI: el servicio aún no está configurado. Los
casos de éxito y errores del proveedor se prueban sustituyendo la dependencia
`get_query_service` con funciones simuladas, sin red ni SQL. La documentación
OpenAPI incluye `QueryRequest`, `QueryResponse` y `ErrorResponse` para todas las
categorías, reemplazando también el esquema 422 predeterminado de FastAPI.

```bash
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest tests/test_api.py -v
```

Se prueban códigos HTTP, formato Pydantic, entradas inválidas, límites de longitud,
escalares JSON, coherencia de filas, mensajes genéricos, errores 404/405,
cabeceras HTTP, revalidación de respuestas y el esquema OpenAPI.
Referencias de implementación: [manejo de errores de FastAPI](https://fastapi.tiangolo.com/tutorial/handling-errors/),
[respuestas adicionales en OpenAPI](https://fastapi.tiangolo.com/advanced/additional-responses/)
y [modo estricto de Pydantic](https://docs.pydantic.dev/latest/concepts/strict_mode/).

## Validación y ejecución segura de SQL

`sql_policy.py` valida el SQL sin abrir una base ni depender del endpoint.
Usa SQLGlot con dialecto SQLite y recorre **todo el árbol sintáctico**, incluidas
subconsultas escalares, `FROM`, `IN`, `EXISTS` y relaciones de los joins.
`query_executor.py` vuelve a validar siempre antes de abrir su propia conexión.

La política es explícita y cerrada:

- Una sola sentencia `SELECT`, con relaciones sin prefijo de base: `pedidos`,
  `ventas_por_mes_pais` y `top_10_clientes`. Se permiten alias y subconsultas.
- Se admiten filtros, comparaciones, aritmética básica, `DISTINCT`, agrupación,
  `HAVING`, orden, `LIMIT`, `OFFSET` y joins INNER, LEFT y CROSS.
- Funciones: `COUNT`, `SUM`, `AVG`, `MIN`, `MAX`, `ROUND`, `ABS`, `COALESCE`,
  `NULLIF`, `LOWER`, `UPPER`, `LENGTH`, `SUBSTR`/`SUBSTRING`, `STRFTIME` y `DATE`.
  También se permite el operador `LIKE`, que SQLite autoriza como función.
- Se rechazan CTE (incluidas recursivas), UNION/INTERSECT/EXCEPT, ventanas, CASE,
  CAST, parámetros, funciones de tabla y cualquier nodo fuera de la lista.
  También escrituras, DDL, transacciones, `ATTACH`, `DETACH`, `PRAGMA`, tablas
  internas (`sqlite_master`, etc.), `pedidos_rechazados` y `pedidos_duplicados`.
- El texto se limita a 16.384 bytes UTF-8, el árbol a 500 nodos y su profundidad
  a 50. SQLGlot está fijado a la versión probada para controlar cambios del parser.

El SQL original validado se entrega a SQLite sin reescribirlo. Un error de
sintaxis SQLite o una columna inexistente produce `InvalidLLMResponseError`
(502); el validador no promete comprobar la existencia de todas las columnas.

La ejecución aplica capas independientes:

1. URI local escapada con `mode=ro`: una base inexistente no se crea. Se activa
   `query_only` y se desactiva `trusted_schema` antes de recibir SQL no confiable.
2. Authorizer con **denegación por defecto**: solo permite SELECT, lecturas de
   relaciones autorizadas y las funciones admitidas. Reconoce las dos vistas
   y permite sus lecturas de `pedidos`; el nombre de una vista autorizada no
   concede acceso a tablas prohibidas. Deniega incluso PRAGMA de lectura.
3. `set_progress_handler` interrumpe consultas al alcanzar el presupuesto de
   instrucciones o tiempo. También se comprueba el tiempo al recoger resultados.
4. Se recogen filas de una en una y se consulta como máximo una adicional para
   detectar truncamiento, independientemente de `LIMIT 100000` o `LIMIT -1`.
5. Se limita el JSON completo en bytes UTF-8: columnas, filas, signos y
   `truncated`. Se omiten filas completas, sin cortar celdas. Si la primera fila
   no cabe, devuelve `rows: []` y `truncated: true`; si ni los metadatos caben,
   devuelve un error de recursos. Nunca se permite BLOB ni números no finitos.

Valores predeterminados de `ExecutionLimits`:

| Límite | Valor |
| --- | ---: |
| Filas devueltas | 100 |
| Respuesta JSON | 65.536 bytes |
| Instrucciones aproximadas de la máquina virtual | 200.000 |
| Tiempo de ejecución | 1 segundo |
| Intervalo del progress handler | 1.000 instrucciones |
| Valor/fila interno de SQLite | 1.048.576 bytes |

Además se limitan las columnas SQL a 50 y las bases adjuntas a cero. El límite
de tiempo es cooperativo, comprobado en los callbacks y entre filas; no es una
interrupción de proceso de precisión exacta. El límite del JSON tampoco es una
cuota global de memoria del proceso: SQLite puede necesitar memoria de trabajo
para agrupar u ordenar antes de producir resultados.

Agotar instrucciones, tiempo o tamaños internos provoca `query_resource_limit`
(502), sin resultados parciales. Un límite de filas o bytes de resultados
devuelve `QueryResponse` con `truncated: true`. No se usa 504 para el presupuesto
SQL: ese estado queda reservado al timeout del proveedor. Fallos de infraestructura
de SQLite se clasifican como internos (500). Cada ejecución cierra su conexión.

Para probar el ejecutor directamente, sin LLM y sin modificar el endpoint:

```bash
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python - <<'PY'
from pathlib import Path
from query_executor import execute_query, ExecutionLimits

result = execute_query(
    "SELECT * FROM ventas_por_mes_pais ORDER BY anio_mes, país",
    Path("data/processed/pedidos.sqlite3"),
    limits=ExecutionLimits(max_rows=10),
)
print(result.model_dump_json(indent=2))
PY
```

Las pruebas usan una base temporal con el fixture conocido. Verifican consultas
legítimas, referencias anidadas prohibidas, authorizer sin pasar por el parser,
vistas alteradas con dependencias prohibidas, protección de solo lectura, límites
de filas y JSON multibyte, y la interrupción real de un producto cartesiano costoso:

```bash
.venv/bin/python -m pytest tests/test_query_executor.py -v
```

Referencias: [AST y parser de SQLGlot](https://sqlglot.com/sqlglot.html),
[authorizer de SQLite](https://www.sqlite.org/c3ref/set_authorizer.html) y
[conexiones, límites y progress handler de Python](https://docs.python.org/3.13/library/sqlite3.html).

## Verificación

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Los tests ejecutan la CLI dos veces y comparan CSV y manifiesto byte a byte.
Comprueban el hash, los duplicados exactos, las cantidades de errores, los
solapamientos, las causas de rechazo y la coherencia de fechas y estados
con varias semillas. `tests/test_cleaning.py` prueba normalización, fechas completas
e imposibles, céntimos exactos, importes inválidos, duplicados, conflictos de ID,
reglas de estado y múltiples motivos por fila. También contrasta la clasificación
con el manifiesto del generador para varias semillas. Los tests del endpoint con
LLM simulado acompañarán la integración real del proveedor.
Las pruebas actuales del endpoint simulan el servicio de consultas completo y
verifican los contratos y errores, sin generar SQL.
`tests/test_storage.py` ejecuta la CLI contra bases temporales y comprueba
persistencia, auditoría, céntimos enteros, repetición sin duplicados y rollback
ante fallos de escritura, además de errores de entrada claros.
También compara el contenido completo tras dos ejecuciones y prueba inserciones
SQL directas inválidas para verificar las restricciones sin pasar por la limpieza.
Puedes ejecutar solo estas pruebas con:

```bash
.venv/bin/python -m pytest tests/test_storage.py -v
```

### Comparar la limpieza con el manifiesto

Desde la raíz del proyecto, con las dependencias de pruebas instaladas:

```bash
.venv/bin/python -m pytest tests/test_cleaning.py \
  -k generated_dataset_matches_manifest -v
```

Este comando selecciona una prueba parametrizada con cinco semillas. Para cada
semilla genera un dataset y su manifiesto en memoria, ejecuta la limpieza y compara:

- Cantidades de filas aceptadas, duplicadas y rechazadas.
- IDs exactos de los pedidos rechazados.
- Distribución de estados entre los pedidos aceptados.
- Que las tres categorías sumen el total y que los originales no se modifiquen.

La limpieza no consulta el manifiesto: el test lo usa como referencia del resultado
esperado. **Esta prueba no lee ni verifica el CSV, el manifiesto o SQLite que tengas
actualmente en `data/`.** Por su parte, `manage.py clean` verifica los conteos
guardados en SQLite frente a su clasificación, sin comparar con el manifiesto.

Para ejecutar también los casos manuales de normalización, fechas, importes,
duplicados y conflictos, independientes del generador y del manifiesto:

```bash
.venv/bin/python -m pytest tests/test_cleaning.py -v
```

### Cómo interpretar los resultados

- **`PASSED` / `5 passed`** en la comparación con el manifiesto: las cinco semillas
  cumplieron todas las comprobaciones anteriores. Esto verifica los casos probados;
  no garantiza que cualquier entrada posible sea correcta ni valida tu SQLite actual.
- **`FAILED`**: alguna comprobación no coincide con lo esperado. Pytest muestra el
  nombre del caso, la semilla y la expresión que falló, junto con los valores
  comparados cuando están disponibles. Hay que revisar si el problema está en la
  limpieza, en el generador/manifiesto o en una expectativa del test que deba cambiar
  por una modificación intencional de las reglas.
- **`ERROR`**: la prueba no pudo prepararse o ejecutarse correctamente, por ejemplo
  por un fallo de importación. Si aparece `No module named pytest`, instala primero
  `requirements-dev.txt`; todavía no se han ejecutado las comprobaciones.
- **`deselected`**: son pruebas excluidas por el filtro `-k`, no fallos. Si ninguna
  prueba fue seleccionada, no se comprobó la limpieza; revisa el nombre del filtro.

## Decisiones, próximos pasos y uso de IA

Se eligió la biblioteca estándar para evitar dependencias de ejecución en esta
etapa. Los importes se generan desde centavos enteros, evitando errores de coma
flotante. El manifiesto permite auditar errores sin mezclar datos crudos con
resultados de procesamiento.

Los siguientes pasos son conectar el servicio de FastAPI con OpenAI y con el
ejecutor validado de solo lectura. Con más tiempo se añadirían
estacionalidad, cantidades por producto, descuentos y un modelo separado de
devoluciones y cobros.

Se pidió a la IA implementar un generador reproducible, una CLI y un manifiesto
de errores controlados. Se incorporaron sus propuestas de biblioteca estándar,
RNG local y manifiesto con SHA-256, junto con tests para comprobarlas. El usuario
precisó que los cancelados se conservan pero no suman ventas, que la finalización
determina el mes, que la moneda es USD y que las variables deben estar en inglés;
esas indicaciones se aplicaron al código y a esta documentación. A petición del
usuario, se simplificó el manifiesto inicial: se eliminó el historial detallado
de cada variación recuperable y se conservó la trazabilidad de los rechazos.
Para la limpieza se pidió a la IA normalizar sin inventar datos y clasificar
filas de forma excluyente. Se implementó el rechazo de todas las versiones de
un ID contradictorio y se verificaron los resultados contra el manifiesto,
sin usarlo para corregir datos. La referencia del libro se limitó a principios
de calidad y consistencia; no se incorporaron técnicas de imputación.
Después se pidió conectar la limpieza con la CLI y SQLite. Se implementaron
tablas separadas de pedidos y auditoría, reemplazo transaccional de la carga y
tests de integración que verifican los registros realmente almacenados.
Para las vistas se pidió respetar ventas completadas y comprobar los totales a
mano. Se añadió un ID de cliente independiente del nombre, se definieron
desempates explícitos y se verificaron los resultados con un CSV pequeño fijo,
incluyendo homónimos, cambios de mes y un empate en el corte del top 10.
Para la API se pidió definir contratos y errores antes de ejecutar SQL. Se
creó `main.py` como entrada, con modelos estrictos, excepciones concretas y
serialización centralizada. Se comprobaron los estados y cuerpos mediante
dependencias simuladas y se documentó explícitamente el 503 provisional del
servicio todavía no conectado.
Después se pidió construir la ejecución segura. Se implementaron validación por
AST, authorizer cerrado, conexión de solo lectura y presupuestos de recursos.
Se comprobó cada capa por separado, incluyendo pruebas que omiten el validador
para verificar que SQLite también impide accesos y escrituras no autorizados.
