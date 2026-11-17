# query-orders-nl2sql

Demo de pedidos y consultas en lenguaje natural. Esta etapa implementa el
generador reproducible; la limpieza, la base de datos, las vistas y FastAPI
se implementarán en las siguientes etapas.

## Generar datos

Requiere Python 3.10 o superior. El generador usa únicamente la biblioteca
estándar y no necesita una API key ni llamadas a OpenAI.

```bash
python3 manage.py generate
```

Se crean `data/raw/pedidos.csv` y `data/raw/manifest.json`. Repetir el comando
sobrescribe esos dos archivos con el mismo contenido. La ruta predeterminada
se resuelve desde el proyecto, independientemente del directorio de ejecución.
Los futuros resultados de limpieza irán en `data/processed/`; nunca reemplazarán
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
`importe`, `moneda` y `estado`. Las variables y funciones están en inglés;
las columnas y los estados del dominio se mantienen en español.
Cada pedido tiene un producto, importe con dos decimales y moneda `USD`.
Hay 120 clientes sintéticos con país estable y seis productos con precio fijo.
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

## Verificación

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Los tests ejecutan la CLI dos veces y comparan CSV y manifiesto byte a byte.
Comprueban el hash, los duplicados exactos, las cantidades de errores, los
solapamientos, las causas de rechazo y la coherencia de fechas y estados
con varias semillas. Los tests de limpieza, validación SQL y endpoint con
LLM simulado acompañarán esas etapas cuando se implementen.

## Decisiones, próximos pasos y uso de IA

Se eligió la biblioteca estándar para evitar dependencias de ejecución en esta
etapa. Los importes se generan desde centavos enteros, evitando errores de coma
flotante. El manifiesto permite auditar errores sin mezclar datos crudos con
resultados de procesamiento.

Los siguientes pasos son implementar limpieza y carga, vistas SQL, validación
de consultas de solo lectura y FastAPI con OpenAI. Con más tiempo se añadirían
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
