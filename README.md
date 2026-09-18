# Sistema de Interacción Humano-Robot mediante Speech-to-Speech para Control Verbal de Navegación en Robots

Trabajo Fin de Máster — Máster Universitario en Automatización e Informática Industrial, Universitat Politècnica de València (UPV).

Sistema de interacción por voz que permite controlar verbalmente la navegación de un robot móvil, combinando reconocimiento de voz, interpretación híbrida del lenguaje natural (reglas + LLM local), gestión de diálogo y síntesis de voz, todo ejecutado en local sobre ROS 2 y Nav2.

---

## Índice

- [Descripción general](#descripción-general)
- [Arquitectura](#arquitectura)
- [Características principales](#características-principales)
- [Requisitos](#requisitos)
- [Instalación](#instalación)
- [Puesta en marcha](#puesta-en-marcha)
- [Estructura del repositorio](#estructura-del-repositorio)
- [Interfaces personalizadas](#interfaces-personalizadas)
- [Mapa semántico de ubicaciones](#mapa-semántico-de-ubicaciones)
- [Ejemplos de uso](#ejemplos-de-uso)
- [Evaluación](#evaluación)
- [Limitaciones conocidas](#limitaciones-conocidas)
- [Trabajo futuro](#trabajo-futuro)
- [Autor](#autor)

---

## Descripción general

Este proyecto implementa un pipeline completo de interacción **Speech-to-Speech** (voz → interpretación → diálogo → acción → voz) para el control de un robot móvil mediante lenguaje natural hablado, en español. El usuario puede pedir al robot que navegue a distintas ubicaciones de un entorno doméstico, encadenar varias misiones, preguntar por el estado de la tarea en curso, cancelarla, o detener el robot de inmediato mediante una orden de parada de prioridad absoluta.

El sistema se ha diseñado y validado sobre un robot **TurtleBot3** en el simulador **Gazebo**, dentro del entorno **turtlebot3_house**, apoyándose en **Nav2** como sistema de navegación. La arquitectura es agnóstica al robot concreto empleado, siempre que este exponga la acción estándar `NavigateToPose` de Nav2.

Todo el procesamiento —reconocimiento de voz, interpretación del lenguaje y síntesis de voz— se ejecuta **en local**, sin dependencia de servicios en la nube.

## Arquitectura

El sistema se compone de cinco nodos de ROS 2, cada uno con una responsabilidad única:


| Nodo | Responsabilidad |
|---|---|
| `voice_input_node` | Captura continua de audio, transcripción con `faster-whisper`, detección de la orden de parada |
| `nlu_node` | Interpretación híbrida del lenguaje natural: capa de reglas (spaCy) + capa de respaldo (LLM local vía Ollama) |
| `dialogue_manager_node` | Orquestación del diálogo: confirmación, aclaración, cola de tareas, parada de emergencia |
| `nav_bridge_node` | Puente hacia Nav2: mapa semántico de ubicaciones, cliente de la acción `NavigateToPose` |
| `voice_output_node` | Síntesis de voz con Piper y reproducción por altavoz |

## Características principales

- **Interpretación híbrida del lenguaje natural**: capa de reglas determinista (rápida, para órdenes directas) con respaldo de un modelo de lenguaje local (para formulaciones coloquiales, sinónimos y frases indirectas).
- **Gestión de confianza**: el sistema ejecuta directamente, pide confirmación, o solicita repetición según la seguridad de cada interpretación.
- **Resolución de ambigüedad**: si el usuario menciona varios destinos posibles, el sistema pregunta cuál de ellos es el correcto.
- **Comandos secuenciales**: reconocimiento de órdenes con varios destinos en un orden temporal explícito ("ve a la cocina y luego al salón").
- **Cola de misiones**: el usuario puede solicitar nuevas tareas mientras el robot ejecuta una anterior, sin necesidad de esperar.
- **Interacción durante la ejecución**: consulta del estado del robot, cancelación «suave» de la tarea en curso, y negación de destinos ya encolados, todo sin interrumpir la navegación salvo que se solicite explícitamente.
- **Parada de emergencia de prioridad absoluta**: detectada de forma temprana e independiente del resto del pipeline, por motivos de seguridad.
- **Ejecución 100% local**: STT, NLU y TTS funcionan sin conexión a internet ni servicios externos.

## Requisitos

- Ubuntu 22.04 LTS
- ROS 2 Humble Hawksbill
- Gazebo Classic + paquetes de simulación de TurtleBot3
- Nav2
- Python 3.10+
- [Ollama](https://ollama.com) instalado, con el modelo `qwen2.5:3b` descargado
- [Piper TTS](https://github.com/OHF-Voice/piper1-gpl) (paquete `piper-tts`)

### Dependencias de Python

```bash
pip3 install faster-whisper SpeechRecognition spacy ollama sounddevice soundfile --break-system-packages
python3 -m spacy download es_core_news_md
```

## Instalación

```bash
# Clonar el repositorio dentro de tu workspace de ROS 2
mkdir -p ~/tfm_ws/src
cd ~/tfm_ws/src
git clone https://github.com/kgbilbao03/tfm-ros2-speech-navigation.git

# Instalar dependencias del sistema
cd ~/tfm_ws
rosdep install --from-paths src -y --ignore-src

# Compilar
colcon build
source install/setup.bash
```

### Modelo de lenguaje (Ollama)

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen2.5:3b
```

### Modelo de voz (Piper)

Descarga el modelo de voz en español (por ejemplo `es_ES-davefx-medium`) desde el repositorio de voces de Piper, y ajusta la ruta correspondiente en `voice_output_node.py` (constante `RUTA_MODELO_VOZ`).

## Puesta en marcha

Orden recomendado de arranque, cada uno en su propia terminal:

```bash
# 1. Simulación
ros2 launch turtlebot3_gazebo turtlebot3_house.launch.py

# 2. Nav2 con el mapa ya guardado
ros2 launch nav2_bringup bringup_launch.py use_sim_time:=true map:=$HOME/tfm_ws/tfm_maps/casa_mapa.yaml

# 3. RViz
ros2 launch nav2_bringup rviz_launch.py
# En RViz: usar "2D Pose Estimate" para fijar la pose inicial del robot

# 4. Nodos del sistema (en este orden)
ros2 run hri_voice_nav nav_bridge_node
ros2 run hri_voice_nav nlu_node
ros2 run hri_voice_nav dialogue_manager_node
ros2 run hri_voice_nav voice_output_node
ros2 run hri_voice_nav voice_input_node
```
⚠️ Nota sobre la carpeta de mapas (`/tfm_maps`):

Para agilizar el despliegue del proyecto sin necesidad de recalcular las rutas compartidas del paquete en ROS 2, la carpeta `tfm_maps/` (que contiene `casa_mapa.yaml` y `casa_mapa.pgm`) se adjunta en la raíz del workspace (`~/tfm_ws/tfm_maps/`).

Asegúrate de pasar la ruta absoluta o relativa correcta (`map:=$HOME/tfm_ws/tfm_maps/casa_mapa.yaml`) al lanzar `nav2_bringup`.

Una vez arrancados los cinco nodos, el sistema queda escuchando por el micrófono.

## Estructura del repositorio

```
tfm_ws/
├── tfm_maps/
│    ├── casa_mapa.yaml
│    └── casa_mapa.pgm
└── src/
    ├── hri_interfaces/              # Mensajes y servicios personalizados (ament_cmake)
    │   ├── msg/
    │   │   ├── ParsedCommand.msg
    │   │   ├── SpeechText.msg
    │   │   ├── NavGoalRequest.msg
    │   │   └── NavResult.msg
    │   └── srv/
    │       └── GetValidLocations.srv
    │
    └── hri_voice_nav/                # Nodos del sistema (ament_python)
        ├── hri_voice_nav/
        │   ├── nav_bridge_node.py
        │   ├── voice_input_node.py
        │   ├── nlu_node.py
        │   ├── dialogue_manager_node.py
        │   ├── voice_output_node.py
        │   └── metrics.py            # Utilidad de instrumentación de tiempos
        └── config/
            └── semantic_map.yaml     # Mapa semántico de ubicaciones
```

## Interfaces personalizadas

| Mensaje/Servicio | Contenido |
|---|---|
| `SpeechText.msg` | Texto transcrito + identificador único de la interacción (`turn_id`) |
| `ParsedCommand.msg` | Intención, destino, confianza, ambigüedad, secuencia, texto original y capa que resolvió la interpretación |
| `NavGoalRequest.msg` | Petición de navegación (`turn_id` + destino) |
| `NavResult.msg` | Resultado de una navegación (éxito/fallo + motivo) |
| `GetValidLocations.srv` | Consulta de las ubicaciones válidas del entorno (claves técnicas + nombres hablados) |

## Mapa semántico de ubicaciones

Las ubicaciones del entorno se definen en `hri_voice_nav/config/semantic_map.yaml`:

```yaml
locations:
  cocina:
    x: 7.697
    y: -0.717
    yaw: 0.0
    nombre_hablado: "cocina"
  bano:
    x: 3.012
    y: 3.954
    yaw: 0.0
    nombre_hablado: "baño"
```

Solo `nav_bridge_node` lee este archivo directamente; el resto de nodos obtienen las ubicaciones válidas a través del servicio `GetValidLocations`.

## Ejemplos de uso

```
Usuario: "Ve a la cocina"
Robot:   [navega directamente] "He llegado"

Usuario: "Oye, ¿podrías llevarme al salón?"
Robot:   "¿Quieres que vaya a salón?"
Usuario: "Sí"
Robot:   [navega] "He llegado"

Usuario: "Ve a la cocina y luego al salón"
Robot:   "De acuerdo, iré a cocina, salón"
         [navega a cocina] "He llegado"
         [navega a salón, sin más órdenes] "He llegado"

Usuario: (mientras el robot navega) "¿A dónde vas?"
Robot:   "Voy a cocina. Después tengo pendiente: salón"

Usuario: "¡Para!"
Robot:   [se detiene de inmediato] "Robot detenido"
```

## Evaluación

El sistema se ha evaluado mediante un corpus de 39 frases (órdenes de navegación, consultas de estado, cancelación de tareas y comandos secuenciales), ejecutadas de forma automatizada con 10 repeticiones cada una. Se incluye instrumentación de tiempos por etapa (STT, NLU-reglas, NLU-LLM, TTS) mediante `time.perf_counter()`, con resultados exportados a CSV para su análisis estadístico (precisión por capa, matriz de confusión, tiempos medios y desviación estándar).

Los scripts de evaluación se encuentran en la carpeta `tfm_evaluacion/` (fuera del workspace de ROS 2).

## Limitaciones conocidas

- Sin cancelación de eco acústico: se recomienda el uso de auriculares para la salida de audio si el micrófono y el altavoz comparten espacio físico.
- Los comandos con varios destinos requieren un conector temporal explícito (`luego`, `después`, etc.) para reconocerse de forma fiable por la capa de reglas.
- Durante la navegación, solo la orden de parada interrumpe el trayecto de forma inmediata; el resto de interacciones se procesan sin interrumpir la tarea salvo que se solicite explícitamente.
- La detección de la palabra de parada puede generar falsos positivos si dicha palabra aparece en otro contexto gramatical (p. ej., como preposición).
- Sistema validado exclusivamente en simulación; no se ha probado sobre robot físico.

## Trabajo futuro

- Palabra de activación (wake word).
- Cancelación de eco acústico.
- Instrumentación completa y evaluación a mayor escala.
- Reconocimiento de comandos secuenciales sin conector explícito.
- Pruebas de usuario con participantes externos al desarrollo.
- Despliegue sobre robot físico y finalización de la integración con TIAGo (PAL Robotics).

## Autor

**Kerry Gorka Bilbao González**
Trabajo Fin de Máster — Máster Universitario en Automatización e Informática Industrial
Universitat Politècnica de València (UPV)
