# spool

Inventario de filamento y calculadora del coste real de impresion 3D, todo en
local.

[![CI](https://github.com/keivanmalhani/spool/actions/workflows/ci.yml/badge.svg)](https://github.com/keivanmalhani/spool/actions/workflows/ci.yml)
[![Licencia: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)
[![Dependencias: ninguna](https://img.shields.io/badge/dependencias%20en%20ejecucion-ninguna-brightgreen.svg)](#sin-dependencias-a-proposito)

Leer en [ingles / in English](README.md).

![Demo de spool: registra una impresion fallida y mira como el reporte de costo la contabiliza](docs/demo.gif)

`spool` registra que filamento tienes y cuanto costo, importa los trabajos
terminados desde Klipper/Moonraker, OctoPrint o un archivo `.gcode`, descuenta
el material de las bobinas y te dice cuanto costo de verdad cada impresion,
incluyendo electricidad, desgaste de la maquina y las impresiones fallidas.

Nota sobre el idioma: la interfaz de linea de comandos y los mensajes del
programa estan en ingles. Este documento traduce la documentacion, no el
software.

---

## Por que

Si le preguntas a un aficionado a la impresion 3D cuanto cuesta una pieza,
normalmente te dira el precio del filamento que lleva dentro. Ese numero
siempre se queda corto, por tres motivos:

1. **Faltan los fallos.** Una impresion que se despego de la cama al 40 por
   ciento igualmente quemo el 40 por ciento del plastico, el 40 por ciento de
   la electricidad y el 40 por ciento de las horas. Y no produjo nada.
2. **Falta la electricidad.** Una impresora con cama caliente funcionando
   durante nueve horas no es gratis, y es un numero facil de calcular una vez y
   no volver a pensar en el.
3. **Falta la maquina.** Boquillas, correas, hotends y al final la propia
   impresora se desgastan a lo largo de un numero contable de horas.

`spool` calcula las cuatro lineas y te ensena el total. Ademas mantiene el
inventario honesto: cada trabajo que registra sale de una bobina concreta, asi
que `spool list` responde a "tengo suficiente PETG gris para este fin de
semana" sin que nadie tenga que pesar nada.

## Sin dependencias a proposito

**`spool` no necesita nada en ejecucion aparte de la biblioteca estandar de
Python.** `urllib.request` para HTTP, `sqlite3` para almacenamiento, `argparse`
para la linea de comandos y `json` para todo lo demas. `pytest` es la unica
dependencia de desarrollo.

Es una restriccion deliberada, no una casualidad. El sitio natural donde
ejecutar esta herramienta es la Raspberry Pi que ya esta al lado de la
impresora ejecutando Klipper: una maquina con poca memoria, sin un compilador
que merezca ese nombre y con una tarjeta SD en la que no quieres pasar veinte
minutos compilando ruedas. Cualquier cosa que arrastre un stack cientifico para
una aritmetica que cabe en una funcion, o una libreria de graficos para un
diagrama de barras que son treinta lineas de SVG, es una dependencia que
mantendras durante anos. Aqui `pip install .` tarda un segundo y funciona sin
conexion.

---

## Instalacion

```bash
git clone https://github.com/keivanmalhani/spool.git
cd spool
pip install .
```

O para desarrollo:

```bash
pip install -e ".[dev]"
```

Python 3.11 o superior. Ningun otro requisito.

## Inicio rapido

No hace falta una impresora para probarlo. El repositorio incluye datos de
ejemplo para trabajar sin conexion.

```bash
spool init
spool add --material PLA  --brand Prusament --color "Galaxy Black" --price 29.99
spool add --material PETG --brand Overture  --color Grey --price 21.50 --remaining 180

spool printer add "Voron 2.4" --watts 150 --price 900 --life-hours 3000
spool config --set tariff_per_kwh=0.30 --set default_watts=120

spool sync --fixture examples/jobs.json --spool 1
spool cost --by month
spool dashboard --out dashboard.html
```

`spool list` muestra el inventario:

```
ID  MATERIAL  BRAND      COLOR          DIA  LEFT g  NEW g  REMAINING                          PRICE   PER g
--  --------  ---------  ------------  ----  ------  -----  -----------------------------  ---------  ------
 1  PLA       Prusament  Galaxy Black  1.75     198   1000  [####----------------]  19.8%  USD 29.99  0.0300
 2  PETG      Overture   Grey          1.75     180   1000  [####----------------]  18.0%  USD 21.50  0.0215

2 spool(s), 378 g on hand, approx USD 9.80 of unused filament.
```

`spool cost` muestra adonde fue el dinero:

```
By month (USD)
  KEY      JOBS  GRAMS  HOURS  FILAMENT  POWER  MACHINE  TOTAL  WASTED  FAIL
  -------  ----  -----  -----  --------  -----  -------  -----  ------  ----
  2026-01     3    235   12.6      7.06   0.57     3.78  11.41    3.62   33%
  2026-02     3    149    8.3      4.47   0.35     1.59   6.41    0.16    0%
  2026-03     4    418   22.4     12.52   0.89     2.69  16.10    2.31   25%

Summary
  Jobs            10  (7 ok, 2 failed, 1 cancelled)
  Failure rate    20.0%
  Filament used   802 g
  Printer time    1d 19h 16m
  Filament cost   USD 24.06
  Electricity     USD 1.80
  Machine wear    USD 8.06
  TOTAL           USD 33.92
  Wasted on fails USD 6.09  (152 g)
  Cost per gram   USD 0.04
```

`spool dashboard` escribe un unico archivo HTML autocontenido: tarjetas de
inventario con barras de porcentaje restante y aviso de stock bajo, un grafico
de barras de coste por mes, un donut con el desglose por material, la tabla de
trabajos recientes y una franja de resumen. CSS en linea, JavaScript en linea y
SVG dibujado a mano en linea. Sin CDN, sin fuentes web y sin ninguna referencia
externa. Se abre en una maquina que nunca ha estado conectada, y hay una prueba
que verifica que el archivo no contiene ninguna URL.

---

## El modelo de coste

Cada impresion cuesta cuatro cosas. `spool` calcula el precio de cada una por
separado para que veas cual te esta doliendo de verdad.

| Linea | Formula | De donde salen los datos |
| --- | --- | --- |
| Filamento | `gramos_usados * (precio bobina / peso neto bobina)` | La bobina de la que salio el trabajo |
| Electricidad | `(segundos / 3600) * (vatios / 1000) * tarifa_por_kwh` | `spool printer add --watts`, si no `default_watts`; tarifa desde `spool config` |
| Desgaste de la maquina | `(segundos / 3600) * (precio maquina / horas de vida)` | `spool printer add --price --life-hours` |
| Desperdicio por fallo | El coste completo de cualquier trabajo que no produjo nada | El `status` y el `--failed-at` del trabajo |

Y los parametros que configuras una vez:

| Parametro | Se define con | Valor por defecto | Notas |
| --- | --- | --- | --- |
| `tariff_per_kwh` | `spool config --set tariff_per_kwh=0.30` | `0.0` | Tarifa plana por kWh, en tu moneda |
| `default_watts` | `spool config --set default_watts=120` | `0.0` | Para impresoras sin perfil registrado |
| `default_machine_cost_per_hour` | `spool config --set ...` | `0.0` | Amortizacion de respaldo |
| `currency` | `spool config --set currency=EUR` | `USD` | Solo visualizacion, no se convierte nada |
| `low_stock_pct` | `spool config --set low_stock_pct=15` | `15.0` | Umbral del aviso LOW |
| Vatios por impresora | `spool printer add NAME --watts 150` | ninguno | Tiene prioridad sobre `default_watts` |
| Desgaste por impresora | `spool printer add NAME --price 900 --life-hours 3000` | ninguno | Amortizacion lineal |

### Como se calculan los fallos

Un trabajo registra el filamento y el tiempo que consumiria una ejecucion
**completa**, mas hasta donde llego en realidad:

```bash
spool use "drawer organiser" --spool 1 --grams 212 --duration 11h30m \
    --status failed --failed-at 0.35
```

Eso es el 35 por ciento del filamento, el 35 por ciento de la electricidad y el
35 por ciento de las horas de maquina, todo contabilizado como desperdicio
porque no salio nada aprovechable. Registrar la fraccion es justo el objetivo:
suponer que cada fallo desperdicio una bobina entera es tan erroneo como
ignorar los fallos por completo.

`--failed-at` acepta `0.35`, `35` o `35%`.

La **tasa de fallo** cuenta solo los trabajos con estado `failed`. Cancelar una
impresion porque cambiaste de idea no es un fallo de la maquina, asi que los
trabajos `cancelled` quedan fuera de la tasa. Si aparecen en el total de
desperdicio, porque el plastico sigue estando en la basura.

### Como se redondea el dinero

Todos los valores intermedios son numeros de coma flotante en precision
completa. El redondeo a centimos ocurre una sola vez, en el momento de mostrar
un numero a una persona, usando `ROUND_HALF_UP` en lugar del redondeo bancario
por defecto de Python, de modo que `0.125` se convierte en `0.13` igual que
haria una factura.

Alli donde se muestra un total junto a las lineas que lo componen, el total que
se ensena es la suma de las lineas que se ensenan. Puede diferir en un centimo
del total exacto redondeado, y cuando eso pasa, la version que cuadra en
pantalla es la que se imprime. El valor exacto sigue disponible en la API para
seguir calculando.

## Densidades de filamento

La conversion de longitud a masa necesita una densidad. `spool` usa estos
valores nominales:

| Material | Densidad (g/cm3) |
| --- | --- |
| PLA | 1.24 |
| PETG | 1.27 |
| ABS | 1.04 |
| ASA | 1.07 |
| TPU | 1.21 |

**Son valores nominales publicados, no medidas de la bobina que tienes en la
mano.** El filamento real varia segun la marca, la carga de pigmento y el lote.
Los filamentos con carga (madera, fibra de carbono, fosforescentes, metalicos)
pueden desviarse bastante. Cualquier bobina puede sobrescribir el valor por
defecto:

```bash
spool add --material PLA-CF --price 39.00 --density 1.30
```

Un material desconocido conserva la etiqueta que le diste, usa como respaldo el
valor del PLA y te avisa por stderr de que lo ha hecho.

La conversion, para que quede constancia:

```
masa_g = longitud_mm * pi * (diametro_mm / 2)^2 * densidad_g_cm3 / 1000
```

Un metro de PLA de 1.75 mm a 1.24 g/cm3 son 2.9825 g, lo que coincide con la
regla practica de que un metro de PLA son unos tres gramos. Las pruebas
comprueban esto contra un calculo hecho a mano, tanto a 1.75 mm como a 2.85 mm.

---

## Comandos

| Comando | Que hace |
| --- | --- |
| `spool init` | Crea o actualiza la base de datos. Seguro sobre una ya existente. |
| `spool add` | Anade una bobina. `--material` y `--price` obligatorios. |
| `spool list [--all]` | Tabla de inventario. `--all` incluye las archivadas. |
| `spool use NAME` | Registra un trabajo a mano. |
| `spool import PATH` | Importa un `.gcode` laminado como trabajo. |
| `spool sync` | Trae el historial desde Moonraker, OctoPrint o un archivo local. |
| `spool cost` | El informe de costes. `--by material\|printer\|month\|spool\|status`. |
| `spool dashboard` | Escribe el panel HTML autocontenido. |
| `spool archive ID` | Oculta una bobina agotada. Sigue en el historial de costes. |
| `spool restock ID` | Rellena una bobina y la devuelve al inventario. |
| `spool printer add\|list` | Registra impresoras para consumo y desgaste por maquina. |
| `spool config [--set K=V]` | Muestra o cambia los parametros del modelo de coste. |

La base de datos es `./spool.db` por defecto, y se puede cambiar con
`--db RUTA` o con la variable de entorno `SPOOL_DB`.

### Codigos de salida

| Codigo | Significado |
| --- | --- |
| `0` | El comando hizo lo que se le pidio. |
| `1` | Un error que hay que corregir: datos invalidos, impresora inalcanzable, bobina desconocida. |
| `2` | Nada que informar: sin bobinas, sin trabajos en el rango, sin metadatos en el archivo. |

El `2` esta separado del `0` a proposito. Que `spool cost --since 2026-01` no
encuentre trabajos no es un fallo, pero una tarea programada que redirige el
informe a algun sitio quiere distinguir entre "aqui tienes tu informe" y "no
habia nada".

### Compatibilidad con G-code

`spool import` lee el archivo linea a linea y nunca lo carga entero en memoria,
porque un gcode de 200 MB para una impresion de varios dias es algo normal.

| Laminador | Filamento | Tiempo | Notas |
| --- | --- | --- | --- |
| PrusaSlicer | `; filament used [g] / [mm] / [cm3]` | `; estimated printing time (normal mode) =` | Da los gramos directamente |
| SuperSlicer | Igual que PrusaSlicer | Igual, admite `1d 4h 30m 10s` | |
| OrcaSlicer | Igual que PrusaSlicer | `; total estimated time:` | Dos tiempos en una linea; gana el total |
| Bambu Studio | Igual que PrusaSlicer | `; total estimated time:` | Los valores multiextrusor se suman |
| Cura | `;Filament used: 4.321m` | `;TIME:3723` | En **metros**, y sin peso, asi que la masa se deduce |

Un archivo sin metadatos no rompe nada. Informa de que no encontro nada y sale
con `2`, y puedes indicar `--grams` tu mismo.

### Sincronizar desde una impresora

```bash
export MOONRAKER_KEY="..."          # solo si tu instancia lo exige
spool sync --moonraker http://printer.local:7125 \
           --api-key-env MOONRAKER_KEY \
           --spool 1 --printer "Voron 2.4"
```

```bash
export OCTOPRINT_KEY="..."
spool sync --octoprint http://octopi.local \
           --api-key-env OCTOPRINT_KEY \
           --spool 1
```

La sincronizacion es **idempotente**. Los trabajos se identifican por origen e
identificador de origen, asi que ejecutarla de forma periodica nunca cuenta una
impresion dos veces. `--dry-run` muestra lo que anadiria.

Tanto Moonraker como OctoPrint informan del filamento como **longitud**. La
masa depende del filamento realmente cargado, algo que solo sabe tu inventario,
asi que pasa `--spool ID` y `spool` hara la conversion con el diametro y la
densidad de esa bobina.

El endpoint `/api/history` de OctoPrint lo proporciona el plugin Print History,
que es lo que mantiene un registro duradero de trabajos en un OctoPrint.

---

## Seguridad

`spool` es una herramienta local que guarda un registro de lo que posees. Esta
construida para que haya muy poco que pueda salir mal.

- **Solo local.** Un unico archivo SQLite en tu disco. Sin servidor, sin nube,
  sin cuenta, sin registro y sin servicio de sincronizacion.
- **Sin telemetria.** No se mide, cuenta ni informa nada en ningun sitio. No hay
  codigo de analitica en este repositorio, y las pruebas verifican que el panel
  no contiene ninguna URL, de modo que no puede adquirir una baliza por
  accidente.
- **Sin red salvo la impresora que indiques.** Las unicas peticiones salientes
  van a la URL base que pasas con `--moonraker` o `--octoprint`. En el codigo
  fuente no hay ninguna URL de red privada escrita a fuego, y solo se aceptan
  `http` y `https`, para que una cadena de configuracion no pueda convertirse
  en un lector de archivos.
- **Toda peticion tiene un tiempo de espera explicito.** Una impresora que ha
  desaparecido hace fallar la sincronizacion en segundos en vez de quedarse
  colgada.
- **Los secretos vienen del entorno, nunca de un parametro.**
  `--api-key-env VAR` recibe el *nombre* de una variable de entorno y la lee el
  propio programa. No existe, deliberadamente, ningun parametro `--api-key`.

  El motivo: las lineas de comando no son privadas. Acaban en el historial de
  tu shell, en la salida de `ps` que cualquier usuario de la maquina puede
  leer, en ficheros de unidad de systemd, en los registros de CI y en los
  informes de fallo de cualquier cosa que capture el arbol de procesos. Una
  variable de entorno tampoco es perfecta, pero no se escribe en disco por
  defecto y no es visible en el `ps` de otros usuarios.

  La coincidencia por prefijo de argparse aceptaria encantada `--api-key
  SECRETO` como abreviatura de `--api-key-env` y luego imprimiria el secreto en
  el error, asi que las abreviaturas estan desactivadas en toda la interfaz. Si
  escribes una clave en `--api-key-env` por error, `spool` detecta que no
  parece un nombre de variable y la rechaza **sin mostrarla**.
- **Ningun secreto se escribe jamas en la base de datos ni en el panel.** Las
  claves de API viven en memoria durante una peticion y no se guardan.
- **Ningun secreto llega a un registro, una excepcion o un repr.** Los
  adaptadores sobrescriben `__repr__` para excluir la clave, y todo el texto de
  error pasa por un redactor como defensa en profundidad. Las pruebas lo
  verifican explicitamente para el repr, para errores de red, para errores de
  estado HTTP y para errores de decodificacion JSON.
- **Los datos del usuario se escapan.** Los nombres de bobinas, impresoras y
  trabajos son entrada del usuario, y todos se escapan como HTML antes de
  llegar al panel.

Lo que este modelo de amenazas *no* cubre: `spool` no cifra la base de datos, y
cualquiera con acceso de lectura al archivo puede ver tu inventario. Si eso te
importa, ponlo en un volumen cifrado.

---

## Desarrollo

```bash
pip install -e ".[dev]"
pytest
```

**370 pruebas, 628 aserciones.** Cubren:

- Conversion de unidades contra masas calculadas a mano a 1.75 mm y 2.85 mm, y
  viajes de ida y vuelta longitud a masa a longitud.
- El motor de costes contra un escenario resuelto a mano, incluyendo el caso de
  fallo al 40 por ciento, trabajos de cero gramos y cero duracion, y el
  comportamiento del redondeo.
- Un archivo G-code de ejemplo por laminador, un archivo sin metadatos y un
  archivo de 50.000 lineas cuyo pico de memoria se verifica con `tracemalloc`
  para comprobar que es una fraccion del tamano del archivo, demostrando que la
  lectura es realmente en streaming.
- Creacion del esquema, idempotencia de las migraciones, actualizacion in situ
  de una base de datos version 1, descuento de bobina incluyendo el
  comportamiento de no bajar nunca de cero e informar del deficit, y
  visibilidad del archivado.
- Ambos adaptadores HTTP ejecutados a traves de un abridor falso inyectado, de
  modo que **ninguna prueba toca la red**, mas aserciones explicitas de que una
  clave de API no aparece en ningun repr ni en ningun mensaje de error.
- Idempotencia de la sincronizacion, autocontencion del panel (verificada
  buscando `http` en la salida) y cada bloque SVG analizado como XML.
- La interfaz de linea de comandos de extremo a extremo mediante `main([...])`,
  con los codigos de salida verificados.

CI ejecuta la suite en Python 3.11 y 3.12, despues desinstala pytest e importa
todos los modulos para demostrar que en ejecucion no hay dependencias de
terceros.

### Estructura

```
src/spool/
  models.py     dataclasses, densidades, conversion longitud/masa
  db.py         esquema sqlite3 y migraciones por version
  gcode.py      analizador en streaming de metadatos del laminador
  cost.py       el motor de costes
  sources.py    adaptadores de Moonraker, OctoPrint y archivo local
  report.py     salida en texto plano
  dashboard.py  generador de HTML autocontenido
  cli.py        punto de entrada de argparse
```

---

## Limitaciones

Dichas con claridad, porque una herramienta de costes que exagera su propia
precision es peor que no tener ninguna.

- **Las densidades son nominales.** La tabla de arriba son valores tipicos
  publicados, no tu bobina. Espera un error de unos pocos por ciento en
  filamento sin carga y potencialmente mucho mas en filamento con carga o
  espumado. Usa `--density` cuando importe.
- **Las estimaciones del laminador son estimaciones.** El tiempo de impresion
  en particular se desvia habitualmente entre un 10 y un 30 por ciento segun lo
  bien que el modelo de aceleracion del laminador se ajuste a tu impresora. La
  longitud de filamento suele estar mucho mas cerca, pero aun asi supone que no
  hay purgas, ni torre de cebado, ni imperfecciones de flujo.
- **La tarifa electrica es solo de tarifa plana.** No hay soporte para
  discriminacion horaria, tramos, terminos fijos, potencia contratada ni
  excedentes solares. Si tienes una tarifa variable, el numero es como mucho un
  promedio.
- **El consumo de la impresora es un unico promedio.** El consumo real oscila
  entre varios cientos de vatios durante el calentamiento de la cama y una
  fraccion de eso durante una impresion larga de capas pequenas. Un unico valor
  medio para toda la impresion es una aproximacion razonable y nada mas.
- **La amortizacion de la maquina es lineal.** Se divide el precio de compra
  entre las horas de vida esperadas. Ignora los consumibles que reemplazas por
  el camino (boquillas, correas, PTFE), el valor de reventa y las reparaciones.
- **Las impresiones multimaterial se atribuyen a una sola bobina.** Los valores
  multiextrusor del gcode se suman y se cargan a la bobina que indiques. Quien
  use un cambiador de herramientas y quiera atribucion por herramienta tendra
  que dividir los trabajos a mano.
- **Una moneda cada vez.** Las bobinas llevan un codigo de moneda, pero no se
  hace ninguna conversion. Mezclar monedas en una misma base de datos produce
  un total sin sentido.
- **Las fracciones de fallo las aportas tu.** `spool` no puede saber hasta
  donde llego una impresion salvo que se lo digas, o que el origen informe de
  lo suficiente para deducirlo (Moonraker lo hace; el historial de OctoPrint
  no). Sin fraccion, un fallo se considera de forma conservadora como consumo
  total.
- **La sincronizacion solo lee.** `spool` nunca escribe en tu impresora, nunca
  sube nada y nunca inicia un trabajo.

## Licencia

MIT. Ver [LICENSE](LICENSE).

Copyright (c) 2026 Keivan Malhani
