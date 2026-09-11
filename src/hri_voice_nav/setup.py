from setuptools import find_packages, setup

package_name = 'hri_voice_nav'

setup(
    name=package_name,
    version='0.0.0',
    packages=find_packages(exclude=['test']),
    data_files=[
        ('share/ament_index/resource_index/packages',
            ['resource/' + package_name]),
        ('share/' + package_name, ['package.xml']),
        ('share/' + package_name + '/config', ['config/semantic_map.yaml']),
    ],
    install_requires=['setuptools'],
    zip_safe=True,
    maintainer='kgbilbao03',
    maintainer_email='kgbilbao03@todo.todo',
    description='Nodos ROS 2 del sistema de interacción humano-robot por voz (TFM)',
    license='Apache-2.0',
    extras_require={
        'test': [
            'pytest',
        ],
    },
    entry_points={
        'console_scripts': [
            'nav_bridge_node = hri_voice_nav.nav_bridge_node:main',
            'voice_input_node = hri_voice_nav.voice_input_node:main',
            'nlu_node = hri_voice_nav.nlu_node:main',
            'dialogue_manager_node = hri_voice_nav.dialogue_manager_node:main',
            'voice_output_node = hri_voice_nav.voice_output_node:main',
        ],
    },
)