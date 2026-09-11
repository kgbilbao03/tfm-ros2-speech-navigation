import rclpy
from rclpy.node import Node
import yaml
from ament_index_python.packages import get_package_share_directory
from std_msgs.msg import String
from rclpy.action import ActionClient
from nav2_msgs.action import NavigateToPose
from geometry_msgs.msg import PoseStamped
from action_msgs.msg import GoalStatus
from std_msgs.msg import Bool
from hri_interfaces.srv import GetValidLocations
from hri_interfaces.msg import NavGoalRequest, NavResult
import functools


class NavBridgeNode(Node):
    def __init__(self):
        super().__init__('nav_bridge_node')
        self.get_logger().info('nav_bridge_node iniciado correctamente')

        self.locations = self.load_semantic_map()
        self.get_logger().info(f'Ubicaciones cargadas: {list(self.locations.keys())}')

        # Cliente de acción hacia Nav2
        self._action_client = ActionClient(self, NavigateToPose, 'navigate_to_pose')

        # Para poder cancelar/sustituir un goal en curso
        self._current_goal_handle = None

        # Entrada de prueba manual: nombre de ubicación como string
        self.subscription = self.create_subscription(
            NavGoalRequest,
            '/nav_goal_request',
            self.goal_request_callback,
            10
        )

        self.result_publisher = self.create_publisher(NavResult, '/nav_result', 10)

        self.emergency_subscription = self.create_subscription(
            Bool,
            '/emergency_stop',
            self.emergency_stop_callback,
            10
        )

        self.locations_service = self.create_service(
            GetValidLocations,
            'get_valid_locations',
            self.get_valid_locations_callback
        )

        self.cancel_task_subscription = self.create_subscription(
            Bool,
            '/cancel_current_task',
            self.cancel_task_callback,
            10
        )

    def load_semantic_map(self):
        share_dir = get_package_share_directory('hri_voice_nav')
        yaml_path = share_dir + '/config/semantic_map.yaml'

        with open(yaml_path, 'r', encoding='utf-8') as f:
            data = yaml.safe_load(f)

        return data['locations']
    
    def goal_request_callback(self, msg: NavGoalRequest):
        location_name = msg.destino.strip().lower()
        turn_id = msg.turn_id

        if location_name not in self.locations:
            self.get_logger().warn(
                f'Ubicación "{location_name}" no reconocida. '
                f'Ubicaciones válidas: {list(self.locations.keys())}'
            )
            self.publicar_resultado(turn_id, exito=False, motivo='ubicacion_no_reconocida')
            return

        if self._current_goal_handle is not None:
            self.get_logger().info('Cancelando goal anterior para sustituirlo por el nuevo...')
            self._current_goal_handle.cancel_goal_async()

        self.send_goal(location_name, turn_id)

    def emergency_stop_callback(self, msg: Bool):
        if not msg.data:
            return

        self.get_logger().warn('¡PARADA DE EMERGENCIA recibida!')

        if self._current_goal_handle is not None:
            self._current_goal_handle.cancel_goal_async()
        else:
            self.get_logger().info('No había ningún goal activo que cancelar')

    def get_valid_locations_callback(self, request, response):
        response.nombres_ubicaciones = list(self.locations.keys())
        response.nombres_hablados = [
            loc['nombre_hablado'] for loc in self.locations.values()
        ]
        self.get_logger().info(
            f'Servicio consultado: devolviendo {len(response.nombres_ubicaciones)} ubicaciones'
        )
        return response

    def send_goal(self, location_name, turn_id):
        loc = self.locations[location_name]

        pose = PoseStamped()
        pose.header.frame_id = 'map'
        pose.header.stamp = self.get_clock().now().to_msg()
        pose.pose.position.x = float(loc['x'])
        pose.pose.position.y = float(loc['y'])
        pose.pose.position.z = 0.0
        pose.pose.orientation.x = 0.0
        pose.pose.orientation.y = 0.0
        pose.pose.orientation.z = 0.0
        pose.pose.orientation.w = 1.0

        goal_msg = NavigateToPose.Goal()
        goal_msg.pose = pose

        self.get_logger().info(f'Enviando goal hacia "{location_name}" ({loc["x"]}, {loc["y"]})')

        self._action_client.wait_for_server()
        send_goal_future = self._action_client.send_goal_async(goal_msg)
        send_goal_future.add_done_callback(
            functools.partial(self.goal_response_callback, turn_id=turn_id)
        )

    def goal_response_callback(self, future, turn_id):
        goal_handle = future.result()

        if not goal_handle.accepted:
            self.get_logger().warn('El goal fue rechazado por Nav2')
            self.publicar_resultado(turn_id, exito=False, motivo='goal_rechazado')
            return

        self.get_logger().info('Goal aceptado por Nav2')
        self._current_goal_handle = goal_handle

        result_future = goal_handle.get_result_async()
        result_future.add_done_callback(
            functools.partial(self.goal_result_callback, turn_id=turn_id, goal_handle=goal_handle)
        )

    def goal_result_callback(self, future, turn_id, goal_handle):
        result = future.result().result
        status = future.result().status

        if self._current_goal_handle is goal_handle:
            self._current_goal_handle = None

        if status == GoalStatus.STATUS_SUCCEEDED:
            self.get_logger().info(f'[{turn_id}] Navegación completada con éxito')
            self.publicar_resultado(turn_id, exito=True, motivo='')
        elif status == GoalStatus.STATUS_CANCELED:
            self.get_logger().info(f'[{turn_id}] Navegación cancelada')
            self.publicar_resultado(turn_id, exito=False, motivo='cancelado')
        elif status == GoalStatus.STATUS_ABORTED:
            self.get_logger().warn(f'[{turn_id}] Navegación abortada por Nav2')
            self.publicar_resultado(turn_id, exito=False, motivo='goal_inalcanzable')
        else:
            self.get_logger().warn(f'[{turn_id}] Navegación finalizada con estado inesperado: {status}')
            self.publicar_resultado(turn_id, exito=False, motivo='estado_inesperado')

    def publicar_resultado(self, turn_id, exito, motivo):
        msg = NavResult()
        msg.turn_id = turn_id or ''
        msg.exito = exito
        msg.motivo = motivo
        self.result_publisher.publish(msg)

    
    def cancel_task_callback(self, msg: Bool):
        if not msg.data:
            return

        self.get_logger().info('Cancelación de tarea (suave) recibida')

        if self._current_goal_handle is not None:
            self._current_goal_handle.cancel_goal_async()
        else:
            self.get_logger().info('No había ningún goal activo que cancelar (cancelación suave)')


def main(args=None):
    rclpy.init(args=args)
    node = NavBridgeNode()
    rclpy.spin(node)
    node.destroy_node()
    rclpy.shutdown()


if __name__ == '__main__':
    main()