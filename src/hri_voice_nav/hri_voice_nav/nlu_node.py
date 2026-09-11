from std_msgs.msg import String
import rclpy
from rclpy.node import Node
from hri_interfaces.srv import GetValidLocations
import spacy
from spacy.matcher import PhraseMatcher
import ollama
import json
from hri_interfaces.msg import SpeechText, ParsedCommand
from hri_voice_nav.metrics import registrar_metrica, cronometro


class NluNode(Node):

    FORMAS_IR = [
        've', 'vete', 'vaya', 'vayan', 'vayas',
        'vamos', 'id', 'anda', 'anden',
        'llévame', 'llevame', 'llévanos', 'llevanos',
        'llévales', 'llevales', 'llévate', 'llevate',
        'dirígete', 'dirigete', 'diríjase', 'dirijase',
        'acércate', 'acercate', 'acérquese', 'acerquese',
    ]

    FORMAS_CONSULTA_ESTADO = [
        'a dónde vas', 'adónde vas', 'a donde vas', 'donde vas',
        'a dónde te diriges', 'a donde te diriges',
        'qué te queda', 'que te queda',
        'qué tienes pendiente', 'que tienes pendiente',
        'qué más tienes pendiente', 'que mas tienes pendiente',
        'cuánto falta', 'cuanto falta',
        'cuánto queda', 'cuanto queda',
    ]

    FORMAS_CANCELAR_TAREA = [
        'cancela la tarea', 'cancela esto', 'cancela eso',
        'cancela la misión', 'cancela la mision',
        'olvídalo', 'olvidalo', 'olvida eso',
    ]

    CONECTORES_SECUENCIA = {
        'luego', 'después', 'despues', 'entonces',
        'posteriormente', 'seguidamente', 'a continuación', 'a continuacion',
        'primero', 'segundo',
    }

    RUTA_METRICAS = '/tmp/metricas_nlu.csv'

    UMBRAL_FALLBACK_LLM = 0.5

    def __init__(self):
        super().__init__('nlu_node')
        self.get_logger().info('nlu_node iniciado correctamente')

        self.ubicaciones_validas = []
        self.nombres_hablados = []
        self.mapa_nombre_hablado = {}
        self.cargar_ubicaciones_validas()

        self.cargar_spacy()

        self.precargar_llm()

        self.parsed_publisher = self.create_publisher(ParsedCommand, '/parsed_command', 10)
        self.speech_subscription = self.create_subscription(
            SpeechText,
            '/speech_text',
            self.speech_text_callback,
            10
        )

        self.dialogue_state_actual = 'idle'
        self.dialogue_state_subscription = self.create_subscription(
            String,
            '/dialogue_state',
            self.dialogue_state_callback,
            10
        )

    def cargar_spacy(self):
        self.get_logger().info('Cargando modelo de spaCy (es_core_news_md)...')
        self.nlp = spacy.load('es_core_news_md')

        ruler = self.nlp.add_pipe('entity_ruler', before='ner')
        patterns = []
        for clave, nombre_hablado in zip(self.ubicaciones_validas, self.nombres_hablados):
            patterns.append({'label': 'UBICACION', 'pattern': nombre_hablado, 'id': clave})
            patterns.append({'label': 'UBICACION', 'pattern': clave, 'id': clave})
        ruler.add_patterns(patterns)
        self.get_logger().info(f'EntityRuler cargado con {len(patterns)} patrones')

        self.matcher = PhraseMatcher(self.nlp.vocab, attr='LOWER')
        intent_patterns = [self.nlp.make_doc(forma) for forma in self.FORMAS_IR]
        self.matcher.add('INTENT_IR_A', intent_patterns)
        self.get_logger().info(f'PhraseMatcher cargado con {len(intent_patterns)} formas verbales')

        self.matcher_consulta = PhraseMatcher(self.nlp.vocab, attr='LOWER')
        consulta_patterns = [self.nlp.make_doc(forma) for forma in self.FORMAS_CONSULTA_ESTADO]
        self.matcher_consulta.add('CONSULTA_ESTADO', consulta_patterns)

        self.matcher_cancelar = PhraseMatcher(self.nlp.vocab, attr='LOWER')
        cancelar_patterns = [self.nlp.make_doc(forma) for forma in self.FORMAS_CANCELAR_TAREA]
        self.matcher_cancelar.add('CANCELAR_TAREA', cancelar_patterns)

        self.get_logger().info(
            f'Matchers de consulta/cancelación cargados: '
            f'{len(consulta_patterns)} consulta, {len(cancelar_patterns)} cancelación'
        )

    def cargar_ubicaciones_validas(self):
        client = self.create_client(GetValidLocations, 'get_valid_locations')

        self.get_logger().info('Esperando al servicio get_valid_locations...')
        if not client.wait_for_service(timeout_sec=10.0):
            self.get_logger().error(
                'No se pudo contactar con get_valid_locations. '
                '¿Está nav_bridge_node corriendo?'
            )
            return

        request = GetValidLocations.Request()
        future = client.call_async(request)

        rclpy.spin_until_future_complete(self, future)

        if future.result() is not None:
            self.ubicaciones_validas = list(future.result().nombres_ubicaciones)
            self.nombres_hablados = list(future.result().nombres_hablados)
            self.mapa_nombre_hablado = dict(zip(self.ubicaciones_validas, self.nombres_hablados))
            self.get_logger().info(f'Ubicaciones válidas cargadas: {self.ubicaciones_validas}')
        else:
            self.get_logger().error('La llamada al servicio falló')

    def detectar_intent(self, texto):
        doc = self.nlp(texto)
        matches = self.matcher(doc)

        if not matches:
            return None, False, doc

        match_id, start, end = matches[0]
        span = doc[start:end]

        verbo_token = span.root
        negado = any(
            hijo.dep_ == 'advmod' and hijo.lower_ == 'no'
            for hijo in verbo_token.children
        )

        return span.text, negado, doc
    
    def detectar_consulta_o_cancelacion(self, texto):
        doc = self.nlp(texto)

        if self.matcher_cancelar(doc):
            return 'cancelar_tarea'

        if self.matcher_consulta(doc):
            return 'consultar_estado'

        return None
    
    def construir_parsed_command(self, texto):
        intent_match, negado, doc = self.detectar_intent(texto)
        ubicacion, ambiguedad, secuencia = self.detectar_ubicacion(doc)

        if intent_match is None:
            return {
                'intent': 'desconocido',
                'destino': '',
                'confianza': 0.0,
                'ambiguedad': [],
                'secuencia': [],
                'texto_original': texto,
                'fuente': 'reglas',
            }

        if negado:
            return {
                'intent': 'ir_a_negado',
                'destino': ubicacion or '',
                'confianza': 0.9,
                'ambiguedad': ambiguedad,
                'secuencia': [],
                'texto_original': texto,
                'fuente': 'reglas',
            }

        if secuencia:
            return {
                'intent': 'ir_a_secuencia',
                'destino': '',
                'confianza': 0.9,
                'ambiguedad': [],
                'secuencia': secuencia,
                'texto_original': texto,
                'fuente': 'reglas',
            }

        if ambiguedad:
            return {
                'intent': 'ir_a',
                'destino': '',
                'confianza': 0.5,
                'ambiguedad': ambiguedad,
                'secuencia': [],
                'texto_original': texto,
                'fuente': 'reglas',
            }

        if ubicacion is None:
            return {
                'intent': 'ir_a',
                'destino': '',
                'confianza': 0.2,
                'ambiguedad': [],
                'secuencia': [],
                'texto_original': texto,
                'fuente': 'reglas',
            }

        return {
            'intent': 'ir_a',
            'destino': ubicacion,
            'confianza': 0.95,
            'ambiguedad': [],
            'secuencia': [],
            'texto_original': texto,
            'fuente': 'reglas',
        }

    def detectar_ubicacion(self, doc):
        ubicaciones_encontradas = [ent for ent in doc.ents if ent.label_ == 'UBICACION']

        if not ubicaciones_encontradas:
            return None, [], []

        if len(ubicaciones_encontradas) == 1:
            return ubicaciones_encontradas[0].ent_id_, [], []

        es_secuencial = True
        for i in range(len(ubicaciones_encontradas) - 1):
            fin_actual = ubicaciones_encontradas[i].end
            inicio_siguiente = ubicaciones_encontradas[i + 1].start
            texto_entre = doc[fin_actual:inicio_siguiente].text.lower()

            if not any(conector in texto_entre for conector in self.CONECTORES_SECUENCIA):
                es_secuencial = False
                break

        ids = [ent.ent_id_ for ent in ubicaciones_encontradas]

        if es_secuencial:
            return None, [], ids

        return None, ids, []
    
    def construir_system_prompt(self):
        return f"""Eres un intérprete de comandos de voz para un robot de navegación doméstica.
        Tu única tarea es analizar la frase del usuario y devolver EXCLUSIVAMENTE un JSON, sin texto adicional, sin explicaciones, sin markdown.

        El JSON debe tener exactamente estos campos:
        - "intent": uno de "ir_a", "ir_a_negado", "ir_a_secuencia", "consultar_estado", "cancelar_tarea", "desconocido"
        - "destino": debe ser EXACTAMENTE uno de estos valores, o cadena vacía: {self.ubicaciones_validas}
        - "ambiguedad": lista vacía, o lista con 2+ valores de la lista anterior si el usuario menciona varias ubicaciones posibles SIN especificar un orden claro entre ellas
        - "secuencia": lista vacía, o lista ORDENADA con 2+ valores de la lista de ubicaciones si el usuario pide ir a varios sitios EN UN ORDEN CLARO (por ejemplo "primero X, luego Y", "ve a X y después a Y")

         Usa "ir_a_secuencia" (con el campo "secuencia" relleno y "destino" vacío) cuando el usuario pide visitar varios lugares en un orden temporal explícito. Usa "ir_a" normal (con "ambiguedad" en vez de "secuencia") cuando el usuario menciona varios lugares como alternativas sin orden, por ejemplo con la palabra "o" — esto aplica igualmente si esos lugares están descritos con sinónimos en vez de sus nombres literales (por ejemplo, "ve a la sala de estar o al lugar donde se prepara la comida" debe darte intent="ir_a", destino="", ambiguedad=["salon", "cocina"]).

        Usa "consultar_estado" cuando el usuario pregunta a dónde va el robot, qué tiene pendiente, o cuánto falta para llegar (por ejemplo "¿a dónde te diriges?", "¿qué te queda por hacer?", "¿falta mucho?").
        Usa "cancelar_tarea" cuando el usuario pide explícitamente que se detenga o abandone la tarea que está haciendo ahora mismo, sin pedir un destino nuevo (por ejemplo "déjalo", "no sigas", "mejor no vayas", "olvídate de eso").

        IMPORTANTE sobre "destino": rellena siempre este campo con la ubicación mencionada, sea cual sea el intent detectado — incluso si el intent es "ir_a_negado", extrae igualmente a qué lugar se refiere la negación (por ejemplo, en "no te dirijas al salón", intent="ir_a_negado" y destino="salon").
        Solo asigna ubicaciones (en "destino", "ambiguedad" o "secuencia") si el usuario menciona esa ubicación exacta o un sinónimo muy directo y evidente de la misma (por ejemplo "sala de estar" es sinónimo válido de "salon", "dormitorio" es sinónimo válido de "habitacion", "el lugar donde se prepara la comida" es sinónimo válido de "cocina").
        Si el usuario menciona un lugar que NO está en la lista y no es un sinónimo directo y evidente, omite ese lugar en vez de inventar una equivalencia incorrecta. NUNCA elijas la opción "más parecida" si no es un sinónimo real y directo.
        """

    def consultar_llm(self, texto):
        respuesta = ollama.chat(
            model='qwen2.5:3b',
            format='json',
            messages=[
                {'role': 'system', 'content': self.construir_system_prompt()},
                {'role': 'user', 'content': texto},
            ]
        )

        contenido = respuesta['message']['content']

        try:
            data = json.loads(contenido)
        except json.JSONDecodeError:
            return None

        return data
    
    def validar_resultado_llm(self, data):
        intent = data.get('intent', 'desconocido')
        if intent not in ('ir_a', 'ir_a_negado', 'ir_a_secuencia', 'consultar_estado', 'cancelar_tarea', 'desconocido'):
            intent = 'desconocido'

        destino_raw = data.get('destino', '')
        ambiguedad_raw = data.get('ambiguedad', [])
        secuencia_raw = data.get('secuencia', [])

        if isinstance(destino_raw, list):
            if not isinstance(ambiguedad_raw, list):
                ambiguedad_raw = []
            ambiguedad_raw = destino_raw + ambiguedad_raw
            destino_raw = ''

        if not isinstance(destino_raw, str) or destino_raw not in self.ubicaciones_validas:
            destino_raw = ''

        if not isinstance(ambiguedad_raw, list):
            ambiguedad_raw = []
        ambiguedad_raw = [u for u in ambiguedad_raw if isinstance(u, str) and u in self.ubicaciones_validas]

        if not isinstance(secuencia_raw, list):
            secuencia_raw = []
        secuencia_raw = [u for u in secuencia_raw if isinstance(u, str) and u in self.ubicaciones_validas]

        if intent == 'ir_a_secuencia' and len(secuencia_raw) < 2:
            intent = 'desconocido'
            secuencia_raw = []

        return {
            'intent': intent,
            'destino': destino_raw,
            'ambiguedad': ambiguedad_raw,
            'secuencia': secuencia_raw,
        }
    
    def procesar_frase(self, texto, turn_id=''):
        intent_especial = self.detectar_consulta_o_cancelacion(texto)
        if intent_especial is not None:
            return {
                'intent': intent_especial,
                'destino': '',
                'confianza': 0.95,
                'ambiguedad': [],
                'secuencia': [],
                'texto_original': texto,
                'fuente': 'reglas',
            }

        t_inicio_reglas = cronometro()
        resultado_reglas = self.construir_parsed_command(texto)
        t_fin_reglas = cronometro()

        registrar_metrica(
            self.RUTA_METRICAS,
            turn_id,
            'nlu_reglas',
            (t_fin_reglas - t_inicio_reglas) * 1000,
            f"confianza={resultado_reglas['confianza']}",
        )

        if resultado_reglas['confianza'] >= self.UMBRAL_FALLBACK_LLM:
            return resultado_reglas

        t_inicio_llm = cronometro()
        resultado_llm = self.consultar_llm(texto)
        t_fin_llm = cronometro()

        registrar_metrica(
            self.RUTA_METRICAS,
            turn_id,
            'nlu_llm',
            (t_fin_llm - t_inicio_llm) * 1000,
            'fallo' if resultado_llm is None else 'ok',
        )

        if resultado_llm is None:
            resultado_reglas['fuente'] = 'reglas (llm_fallo)'
            return resultado_reglas

        llm_validado = self.validar_resultado_llm(resultado_llm)

        confianza_llm = 0.7
        if llm_validado['intent'] == 'ir_a' and self.destino_mencionado_literalmente(
            texto, llm_validado['destino']
        ):
            confianza_llm = 0.85
            self.get_logger().info(
                f'Destino "{llm_validado["destino"]}" mencionado literalmente, '
                f'subiendo confianza a {confianza_llm}'
            )

        return {
            'intent': llm_validado['intent'],
            'destino': llm_validado['destino'],
            'confianza': confianza_llm,
            'ambiguedad': llm_validado['ambiguedad'],
            'secuencia': llm_validado['secuencia'],
            'texto_original': texto,
            'fuente': 'llm',
        }
    
    def speech_text_callback(self, msg: SpeechText):
        self.get_logger().info(f'Recibido [{msg.turn_id}]: "{msg.texto}"')

        if self.dialogue_state_actual in ('esperando_confirmacion', 'esperando_aclaracion'):
            self.get_logger().info(
                f'[{msg.turn_id}] Omitido: dialogue_manager_node lo resuelve directamente '
                f'(dialogue_state={self.dialogue_state_actual})'
            )
            return

        resultado = self.procesar_frase(msg.texto, turn_id=msg.turn_id)

        parsed_msg = ParsedCommand()
        parsed_msg.turn_id = msg.turn_id
        parsed_msg.intent = resultado['intent']
        parsed_msg.destino = resultado['destino']
        parsed_msg.confianza = resultado['confianza']
        parsed_msg.ambiguedad = resultado['ambiguedad']
        parsed_msg.secuencia = resultado['secuencia']
        parsed_msg.texto_original = resultado['texto_original']
        parsed_msg.fuente = resultado['fuente']

        self.parsed_publisher.publish(parsed_msg)
        self.get_logger().info(
            f'Publicado [{msg.turn_id}]: intent={resultado["intent"]}, '
            f'destino={resultado["destino"]}, confianza={resultado["confianza"]}, '
            f'ambiguedad={resultado["ambiguedad"]}, secuencia={resultado["secuencia"]}, '
            f'fuente={resultado["fuente"]}'
        )

    def destino_mencionado_literalmente(self, texto, destino):
        if not destino:
            return False

        texto_normalizado = texto.strip().lower()
        clave_normalizada = destino.lower()
        nombre_hablado = self.mapa_nombre_hablado.get(destino, '').lower()

        return clave_normalizada in texto_normalizado or (
            nombre_hablado and nombre_hablado in texto_normalizado
        )

    def precargar_llm(self):
        self.get_logger().info('Precargando modelo LLM (Ollama)...')
        try:
            ollama.chat(
                model='qwen2.5:3b',
                format='json',
                messages=[
                    {'role': 'system', 'content': 'Responde solo con {"ok": true}'},
                    {'role': 'user', 'content': 'ping'},
                ]
            )
            self.get_logger().info('Modelo LLM precargado correctamente')
        except Exception as e:
            self.get_logger().error(f'Error al precargar el modelo LLM: {e}')


    def dialogue_state_callback(self, msg: String):
        self.dialogue_state_actual = msg.data

def main(args=None):
    rclpy.init(args=args)
    node = NluNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()