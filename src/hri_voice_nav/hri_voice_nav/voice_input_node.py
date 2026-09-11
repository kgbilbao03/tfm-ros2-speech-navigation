import rclpy
from rclpy.node import Node
from faster_whisper import WhisperModel
import speech_recognition as sr
import io
import threading
from hri_interfaces.msg import SpeechText
import uuid
from std_msgs.msg import String
from std_msgs.msg import Bool
import time
import string
from hri_voice_nav.metrics import registrar_metrica, cronometro


class VoiceInputNode(Node):

    PALABRAS_PARADA = ['para', 'parar', 'detente', 'alto', 'stop']
    RUTA_METRICAS = '/tmp/metricas_voice_input.csv'
    
    def __init__(self):
        super().__init__('voice_input_node')
        self.get_logger().info('voice_input_node iniciado correctamente')

        self.get_logger().info('Cargando modelo de voz (faster-whisper, small)...')
        self.model = WhisperModel('small', device='cpu', compute_type='int8')
        self.get_logger().info('Modelo cargado correctamente')

        self.recognizer = sr.Recognizer()
        self.recognizer.dynamic_energy_threshold = True

        self.turn_state = 'listening'
        self._interrumpido_durante_captura = False

        self.speech_publisher = self.create_publisher(SpeechText, '/speech_text', 10)
        self.emergency_publisher = self.create_publisher(Bool, '/emergency_stop', 10)

        self.turn_state = 'listening'  # valor inicial, hasta que dialogue_manager_node exista y publique el real
        self.state_subscription = self.create_subscription(
            String,
            '/system_state',
            self.system_state_callback,
            10
        )

        self.listener_thread = threading.Thread(target=self.listen_loop, daemon=True)
        self.listener_thread.start()

    def system_state_callback(self, msg: String):
        nuevo_estado = msg.data
        if nuevo_estado != self.turn_state:
            self.get_logger().info(f'turn_state: {self.turn_state} -> {nuevo_estado}')
        self.turn_state = nuevo_estado

        if nuevo_estado == 'speaking':
            self._interrumpido_durante_captura = True

    def listen_loop(self):
        with sr.Microphone() as source:
            self.get_logger().info('Calibrando ruido ambiente...')
            self.recognizer.adjust_for_ambient_noise(source, duration=1)

            self.get_logger().info('Nodo listo. Escuchando en bucle...')

            estaba_en_pausa = False

            while rclpy.ok():
                if self.turn_state == 'speaking':
                    estaba_en_pausa = True
                    # Drenamos el buffer del micrófono para que no se acumule
                    # audio "viejo" mientras el robot habla.
                    try:
                        source.stream.read(source.CHUNK)
                    except Exception:
                        pass
                    continue

                if estaba_en_pausa:
                    estaba_en_pausa = False
                    time.sleep(0.15)  # margen de estabilización tras reanudar la escucha

                self._interrumpido_durante_captura = False

                try:
                    audio = self.recognizer.listen(source, timeout=1.0, phrase_time_limit=5)
                except sr.WaitTimeoutError:
                    continue

                if self.turn_state == 'speaking' or self._interrumpido_durante_captura:
                    self.get_logger().info(
                        'Audio descartado: el robot empezó a hablar durante la captura'
                    )
                    continue

                try:
                    self.get_logger().info('Transcribiendo...')
                    t_inicio_stt = cronometro()

                    wav_data = io.BytesIO(audio.get_wav_data())
                    segments, _ = self.model.transcribe(wav_data, language='es', vad_filter=True)

                    texto_completo = ' '.join(segment.text.strip() for segment in segments).strip()

                    t_fin_stt = cronometro()

                    if not texto_completo:
                        continue

                    texto_normalizado = texto_completo.lower()
                    texto_normalizado = texto_normalizado.translate(
                        str.maketrans('', '', string.punctuation + '¡¿')
                    )
                    if any(palabra in texto_normalizado.split() for palabra in self.PALABRAS_PARADA):
                        self.get_logger().warn(f'¡Palabra de parada detectada! ("{texto_completo}")')
                        stop_msg = Bool()
                        stop_msg.data = True
                        self.emergency_publisher.publish(stop_msg)
                        continue

                    turn_id = str(uuid.uuid4())
                    self.get_logger().info(f'Transcripción [{turn_id}]: "{texto_completo}"')

                    registrar_metrica(
                        self.RUTA_METRICAS,
                        turn_id,
                        'stt',
                        (t_fin_stt - t_inicio_stt) * 1000,
                    )

                    msg = SpeechText()
                    msg.turn_id = turn_id
                    msg.texto = texto_completo
                    self.speech_publisher.publish(msg)

                except sr.UnknownValueError:
                    self.get_logger().info('No se ha entendido el audio')
                except Exception as e:
                    self.get_logger().error(f'Error en la transcripción: {e}')
                    
def main(args=None):
    rclpy.init(args=args)
    node = VoiceInputNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()