import string
from collections import deque
import rclpy
from rclpy.node import Node
from std_msgs.msg import String
from std_msgs.msg import Bool
from hri_interfaces.msg import ParsedCommand
from hri_interfaces.msg import NavGoalRequest, NavResult
from hri_interfaces.msg import SpeechText


class DialogueManagerNode(Node):

    UMBRAL_ALTA_CONFIANZA = 0.8
    UMBRAL_MEDIA_CONFIANZA = 0.5

    PALABRAS_AFIRMACION = {'si', 'sí', 'vale', 'correcto', 'afirmativo', 'claro', 'exacto'}
    PALABRAS_NEGACION = {'no', 'cancela', 'cancelar', 'negativo'}
    FRASES_CANCELAR_TAREA_EXPLICITAS = {
        'cancela la tarea', 'cancela esto', 'cancela eso',
        'cancela la mision', 'cancela la misión',
        'olvidalo', 'olvídalo', 'olvida eso',
    }

    TIMEOUT_RESPUESTA_SEGUNDOS = 15.0

    def __init__(self):
        super().__init__('dialogue_manager_node')
        self.get_logger().info('dialogue_manager_node iniciado correctamente')

        # Turno de la pregunta pendiente (confirmación/aclaración), no de la tarea en marcha
        self._turn_id_actual = None
        self._destino_candidato = None
        self._candidatos_ambiguedad = None

        # Turno y destino de la navegación realmente en curso ahora mismo
        self._tarea_turn_id = None
        self._tarea_destino = None
        self._cola_destinos = deque()

        self._timer_timeout = None

        self._turn_id_resuelto_directo = None

        self.turn_state = 'listening'

        self.state_publisher = self.create_publisher(String, '/system_state', 10)
        self.tts_publisher = self.create_publisher(String, '/tts_text', 10)
        self.dialogue_state_publisher = self.create_publisher(String, '/dialogue_state', 10)
        self.cancel_task_publisher = self.create_publisher(Bool, '/cancel_current_task', 10)

        self.cambiar_dialogue_state('idle')

        # Publicamos el estado inicial para que voice_input_node lo reciba
        # en cuanto arranque, en vez de depender de su valor por defecto ('listening').
        self.publicar_turn_state('listening')

        self.parsed_subscription = self.create_subscription(
            ParsedCommand,
            '/parsed_command',
            self.parsed_command_callback,
            10
        )

        self.speech_subscription = self.create_subscription(
            SpeechText,
            '/speech_text',
            self.speech_text_early_callback,
            10
        )

        self.emergency_subscription = self.create_subscription(
            Bool,
            '/emergency_stop',
            self.emergency_stop_callback,
            10
        )

        self.nav_goal_publisher = self.create_publisher(NavGoalRequest, '/nav_goal_request', 10)

        self.nav_result_subscription = self.create_subscription(
            NavResult,
            '/nav_result',
            self.nav_result_callback,
            10
        )

        self.tts_status_subscription = self.create_subscription(
            String,
            '/tts_playback_status',
            self.tts_status_callback,
            10
        )

    def publicar_turn_state(self, nuevo_estado):
        self.turn_state = nuevo_estado
        msg = String()
        msg.data = nuevo_estado
        self.state_publisher.publish(msg)
        self.get_logger().info(f'turn_state -> {nuevo_estado}')

    def parsed_command_callback(self, msg: ParsedCommand):
        self.get_logger().info(
            f'[{msg.turn_id}] Recibido: intent={msg.intent}, destino={msg.destino}, '
            f'confianza={msg.confianza}, ambiguedad={list(msg.ambiguedad)}, fuente={msg.fuente}'
        )

        if msg.turn_id == self._turn_id_resuelto_directo:
            self.get_logger().info(
                f'[{msg.turn_id}] Ya resuelto directamente vía /speech_text, se ignora resultado de NLU.'
            )
            return

        if self.dialogue_state == 'esperando_confirmacion':
            self.procesar_respuesta_confirmacion(msg.texto_original)
            return

        if self.dialogue_state == 'esperando_aclaracion':
            self.procesar_respuesta_aclaracion(msg.texto_original)
            return

        if self.dialogue_state == 'ejecutando':
            self.procesar_comando_durante_ejecucion(msg)
            return

        if self.dialogue_state != 'idle':
            self.get_logger().warn(
                f'Comando recibido en dialogue_state={self.dialogue_state}, '
                f'lógica de este estado aún no implementada'
            )
            return

        if msg.intent == 'ir_a_secuencia' and msg.secuencia:
            self.encolar_secuencia(msg.turn_id, list(msg.secuencia))
            return
        
        if msg.ambiguedad:
            self.pedir_aclaracion(msg.turn_id, list(msg.ambiguedad))
            return

        if msg.intent == 'ir_a' and msg.confianza >= self.UMBRAL_ALTA_CONFIANZA:
            self.get_logger().info(f'Confianza alta, ejecutando directo hacia "{msg.destino}"')
            self.enviar_goal(msg.turn_id, msg.destino)
            return

        if msg.intent == 'ir_a' and msg.confianza >= self.UMBRAL_MEDIA_CONFIANZA:
            self.pedir_confirmacion(msg.turn_id, msg.destino)
            return

        if msg.intent == 'ir_a_negado':
            if msg.destino:
                self.decir(f'De acuerdo, no voy a {msg.destino}')
            else:
                self.decir('De acuerdo')
            return

        self.decir('No te he entendido, ¿puedes repetir?')

    def procesar_comando_durante_ejecucion(self, msg: ParsedCommand):
        if msg.intent == 'consultar_estado':
            self.responder_estado()
            return

        if msg.intent == 'cancelar_tarea':
            self.get_logger().info(f'[{self._tarea_turn_id}] Cancelación suave solicitada')
            cancel_msg = Bool()
            cancel_msg.data = True
            self.cancel_task_publisher.publish(cancel_msg)
            return

        if msg.intent == 'ir_a_secuencia' and msg.secuencia:
            self.encolar_secuencia(msg.turn_id, list(msg.secuencia), ya_ejecutando=True)
            return

        if msg.ambiguedad:
            self.pedir_aclaracion(msg.turn_id, list(msg.ambiguedad))
            return

        if msg.intent == 'ir_a' and msg.confianza >= self.UMBRAL_ALTA_CONFIANZA:
            self._cola_destinos.append((msg.turn_id, msg.destino))
            self.decir(f'De acuerdo, iré a {msg.destino} después')
            return

        if msg.intent == 'ir_a' and msg.confianza >= self.UMBRAL_MEDIA_CONFIANZA:
            self.pedir_confirmacion(msg.turn_id, msg.destino)
            return
        
        if msg.intent == 'ir_a_negado' and msg.destino:
            self.gestionar_negacion_durante_ejecucion(msg.destino)
            return

        self.get_logger().info(
            f'Comando ignorado durante ejecución (intent={msg.intent}, confianza={msg.confianza})'
        )

    def responder_estado(self):
        if self._tarea_destino is None:
            self.decir('No estoy yendo a ningún sitio ahora mismo')
            return

        texto = f'Voy a {self._tarea_destino}'

        if self._cola_destinos:
            pendientes = ', '.join(destino for _, destino in self._cola_destinos)
            texto += f'. Después tengo pendiente: {pendientes}'

        self.decir(texto)

    def enviar_goal(self, turn_id, destino):
        self.cambiar_dialogue_state('ejecutando')
        self._tarea_turn_id = turn_id
        self._tarea_destino = destino

        goal_msg = NavGoalRequest()
        goal_msg.turn_id = turn_id
        goal_msg.destino = destino
        self.nav_goal_publisher.publish(goal_msg)

        self.get_logger().info(f'[{turn_id}] Goal enviado a nav_bridge_node: "{destino}"')

    def nav_result_callback(self, msg: NavResult):
        if msg.turn_id != self._tarea_turn_id:
            self.get_logger().warn(
                f'Resultado recibido para turn_id={msg.turn_id}, '
                f'pero la tarea actual es {self._tarea_turn_id}. Ignorado.'
            )
            return

        if msg.exito:
            self.get_logger().info(f'[{msg.turn_id}] Navegación exitosa')
            self.decir('He llegado')
        elif msg.motivo == 'cancelado':
            self.get_logger().info(f'[{msg.turn_id}] Tarea cancelada')
            self.decir('Tarea cancelada')
        else:
            self.get_logger().info(f'[{msg.turn_id}] Navegación fallida: {msg.motivo}')
            self.decir('No he podido llegar')

        self._tarea_turn_id = None
        self._tarea_destino = None

        self.avanzar_cola()

    def avanzar_cola(self):
        if self._cola_destinos:
            turn_id, destino = self._cola_destinos.popleft()
            self.get_logger().info(f'Iniciando siguiente tarea de la cola: "{destino}"')
            self.enviar_goal(turn_id, destino)
        else:
            self.cambiar_dialogue_state('idle')

    def decir(self, texto):
        self.publicar_turn_state('speaking')
        msg = String()
        msg.data = texto
        self.tts_publisher.publish(msg)
        self.get_logger().info(f'TTS -> "{texto}"')

    def pedir_confirmacion(self, turn_id, destino):
        self.cambiar_dialogue_state('esperando_confirmacion')
        self._turn_id_actual = turn_id
        self._destino_candidato = destino

        self.decir(f'¿Quieres que vaya a {destino}?')
        self.get_logger().info(f'[{turn_id}] Esperando confirmación para destino "{destino}"')
        # El timeout se arma en tts_status_callback, cuando el robot termina de hablar

    def procesar_respuesta_confirmacion(self, texto_original):
        self.cancelar_timeout()
        texto_normalizado = self.normalizar_texto(texto_original)

        if self._tarea_turn_id is not None and self.es_cancelacion_de_tarea(texto_normalizado):
            self.get_logger().info(
                'Se detectó una cancelación de tarea explícita durante una confirmación pendiente'
            )
            self._destino_candidato = None
            self.cambiar_dialogue_state('ejecutando')
            cancel_msg = Bool()
            cancel_msg.data = True
            self.cancel_task_publisher.publish(cancel_msg)
            return

        palabras = set(texto_normalizado.split())

        if palabras & self.PALABRAS_AFIRMACION:
            self.get_logger().info(f'Confirmación aceptada para "{self._destino_candidato}"')
            destino = self._destino_candidato
            turn_id = self._turn_id_actual
            self._destino_candidato = None

            if self._tarea_turn_id is not None:
                self._cola_destinos.append((turn_id, destino))
                self.decir(f'De acuerdo, iré a {destino} después')
                self.cambiar_dialogue_state('ejecutando')
            else:
                self.enviar_goal(turn_id, destino)
            return

        if palabras & self.PALABRAS_NEGACION:
            self.get_logger().info('Confirmación rechazada por el usuario')
            self.decir('De acuerdo, cancelado')
            self.cambiar_dialogue_state('ejecutando' if self._tarea_turn_id is not None else 'idle')
            self._turn_id_actual = None
            self._destino_candidato = None
            return

        self.get_logger().warn(
            f'Respuesta no interpretada como sí/no: "{texto_original}". Se repite la pregunta.'
        )
        self.decir(f'No te he entendido, ¿quieres que vaya a {self._destino_candidato}?')
        # El timeout se rearma en tts_status_callback

    def pedir_aclaracion(self, turn_id, candidatos):
        self.cambiar_dialogue_state('esperando_aclaracion')
        self._turn_id_actual = turn_id
        self._candidatos_ambiguedad = candidatos

        opciones = ' o '.join(candidatos)
        self.decir(f'¿Quieres ir a {opciones}?')
        self.get_logger().info(f'[{turn_id}] Esperando aclaración entre: {candidatos}')
        # El timeout se arma en tts_status_callback

    def procesar_respuesta_aclaracion(self, texto_original):
        texto_normalizado = self.normalizar_texto(texto_original)

        if self._tarea_turn_id is not None and self.es_cancelacion_de_tarea(texto_normalizado):
            self.get_logger().info(
                'Se detectó una cancelación de tarea explícita durante una aclaración pendiente'
            )
            self._candidatos_ambiguedad = None
            self.cambiar_dialogue_state('ejecutando')
            cancel_msg = Bool()
            cancel_msg.data = True
            self.cancel_task_publisher.publish(cancel_msg)
            return

        elegido = None
        for candidato in self._candidatos_ambiguedad:
            if candidato in texto_normalizado:
                elegido = candidato
                break

        if elegido is not None:
            self.get_logger().info(f'Aclaración resuelta: "{elegido}"')
            turn_id = self._turn_id_actual
            self._candidatos_ambiguedad = None

            if self._tarea_turn_id is not None:
                self._cola_destinos.append((turn_id, elegido))
                self.decir(f'De acuerdo, iré a {elegido} después')
                self.cambiar_dialogue_state('ejecutando')
            else:
                self.enviar_goal(turn_id, elegido)
            return

        self.get_logger().warn(
            f'Respuesta no reconocida como ninguno de los candidatos: "{texto_original}". '
            f'Se repite la pregunta.'
        )
        opciones = ' o '.join(self._candidatos_ambiguedad)
        self.decir(f'No te he entendido, ¿quieres ir a {opciones}?')
        # El timeout se rearma en tts_status_callback

    def armar_timeout(self):
        self.cancelar_timeout()
        self._timer_timeout = self.create_timer(
            self.TIMEOUT_RESPUESTA_SEGUNDOS,
            self.timeout_callback
        )

    def cancelar_timeout(self):
        if self._timer_timeout is not None:
            self._timer_timeout.cancel()
            self._timer_timeout = None

    def timeout_callback(self):
        self.get_logger().info(
            f'Timeout esperando respuesta en dialogue_state={self.dialogue_state}'
        )
        self.cancelar_timeout()
        self.decir('No he recibido respuesta')
        self.cambiar_dialogue_state('ejecutando' if self._tarea_turn_id is not None else 'idle')
        self._turn_id_actual = None
        self._destino_candidato = None
        self._candidatos_ambiguedad = None

    def emergency_stop_callback(self, msg: Bool):
        if not msg.data:
            return

        self.get_logger().warn('¡PARADA DE EMERGENCIA recibida en dialogue_manager_node!')

        self.cancelar_timeout()
        self.cambiar_dialogue_state('idle')
        self._turn_id_actual = None
        self._destino_candidato = None
        self._candidatos_ambiguedad = None
        self._tarea_turn_id = None
        self._tarea_destino = None
        self._cola_destinos.clear()

        self.decir('Robot detenido')

    def speech_text_early_callback(self, msg: SpeechText):
        self.publicar_turn_state('processing')

        if self.dialogue_state == 'esperando_confirmacion':
            self._turn_id_resuelto_directo = msg.turn_id
            self.procesar_respuesta_confirmacion(msg.texto)
        elif self.dialogue_state == 'esperando_aclaracion':
            self._turn_id_resuelto_directo = msg.turn_id
            self.procesar_respuesta_aclaracion(msg.texto)

    def tts_status_callback(self, msg: String):
        if msg.data == 'finished':
            if self.dialogue_state in ('esperando_confirmacion', 'esperando_aclaracion'):
                self.armar_timeout()
            self.publicar_turn_state('listening')

    def normalizar_texto(self, texto):
        texto = texto.strip().lower()
        texto = texto.translate(str.maketrans('', '', string.punctuation + '¡¿'))
        return texto

    def cambiar_dialogue_state(self, nuevo_estado):
        self.dialogue_state = nuevo_estado
        msg = String()
        msg.data = nuevo_estado
        self.dialogue_state_publisher.publish(msg)

    def encolar_secuencia(self, turn_id, secuencia, ya_ejecutando=False):
        if ya_ejecutando:
            for destino in secuencia:
                self._cola_destinos.append((turn_id, destino))
            texto = ', '.join(secuencia)
            self.decir(f'De acuerdo, después iré a {texto}')
            return

        primero, *resto = secuencia
        for destino in resto:
            self._cola_destinos.append((turn_id, destino))

        texto = ', '.join(secuencia)
        self.decir(f'De acuerdo, iré a {texto}')
        self.enviar_goal(turn_id, primero)

    def es_cancelacion_de_tarea(self, texto_normalizado):
        return any(frase in texto_normalizado for frase in self.FRASES_CANCELAR_TAREA_EXPLICITAS)
    
    def gestionar_negacion_durante_ejecucion(self, destino_negado):
        if destino_negado == self._tarea_destino:
            self.get_logger().info(
                f'Negación coincide con la tarea en curso ("{destino_negado}"), cancelando'
            )
            cancel_msg = Bool()
            cancel_msg.data = True
            self.cancel_task_publisher.publish(cancel_msg)
            return

        indices_a_quitar = [
            i for i, (_, destino) in enumerate(self._cola_destinos)
            if destino == destino_negado
        ]

        if indices_a_quitar:
            self._cola_destinos = deque(
                item for i, item in enumerate(self._cola_destinos)
                if i not in indices_a_quitar
            )
            self.get_logger().info(f'Eliminado "{destino_negado}" de la cola de pendientes')
            self.decir(f'De acuerdo, quito {destino_negado} de la lista')
            return

        self.get_logger().info(
            f'Negación de "{destino_negado}" recibida, pero no está ni en curso ni en cola'
        )
        self.decir('De acuerdo')


def main(args=None):
    rclpy.init(args=args)
    node = DialogueManagerNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()