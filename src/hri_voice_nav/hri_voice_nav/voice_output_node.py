import rclpy
from rclpy.node import Node
import subprocess
import sounddevice as sd
import soundfile as sf
from std_msgs.msg import String
import time
from hri_voice_nav.metrics import registrar_metrica, cronometro


class VoiceOutputNode(Node):

    RUTA_MODELO_VOZ = '/home/kgbilbao03/piper_tts/es_ES-davefx-medium.onnx'
    RUTA_WAV_TEMPORAL = '/tmp/voice_output_temp.wav'
    RUTA_METRICAS = '/tmp/metricas_voice_output.csv'

    def __init__(self):
        super().__init__('voice_output_node')
        self.get_logger().info('voice_output_node iniciado correctamente')

        sd.default.latency = 'high'

        self.status_publisher = self.create_publisher(String, '/tts_playback_status', 10)
        
        self.tts_subscription = self.create_subscription(
            String,
            '/tts_text',
            self.tts_text_callback,
            10
        )

    def hablar(self, texto):
        self.get_logger().info(f'Sintetizando: "{texto}"')

        try:
            subprocess.run(
                ['piper', '--model', self.RUTA_MODELO_VOZ, '--output_file', self.RUTA_WAV_TEMPORAL],
                input=texto.encode('utf-8'),
                check=True
            )

            data, samplerate = sf.read(self.RUTA_WAV_TEMPORAL)
            sd.play(data, samplerate)
            sd.wait()

            self.get_logger().info('Reproducción finalizada')

        except subprocess.CalledProcessError as e:
            self.get_logger().error(f'Piper falló al sintetizar "{texto}": {e}')
        except Exception as e:
            self.get_logger().error(f'Error inesperado al reproducir "{texto}": {e}')

    def tts_text_callback(self, msg: String):
        texto = msg.data
        self.publicar_status('started')

        t_inicio_tts = cronometro()
        self.hablar(texto)
        t_fin_tts = cronometro()

        registrar_metrica(
            self.RUTA_METRICAS,
            '',
            'tts',
            (t_fin_tts - t_inicio_tts) * 1000,
            f'longitud_texto={len(texto)}',
        )

        self.publicar_status('finished')

    def publicar_status(self, estado):
        msg = String()
        msg.data = estado
        self.status_publisher.publish(msg)
        self.get_logger().info(f'tts_playback_status -> {estado}')


def main(args=None):
    rclpy.init(args=args)
    node = VoiceOutputNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()