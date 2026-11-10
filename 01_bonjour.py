"""Premier test : la tête bouge, les antennes s'agitent, puis retour au repos."""

import time

from reachy_mini import ReachyMini
from reachy_mini.utils import create_head_pose

with ReachyMini() as mini:
    mini.wake_up()

    # Hoche la tête "oui"
    for pitch in (15, -10, 15, 0):
        mini.goto_target(head=create_head_pose(pitch=pitch), duration=0.4)

    # Secoue la tête "non"
    for yaw in (25, -25, 25, 0):
        mini.goto_target(head=create_head_pose(yaw=yaw), duration=0.4)

    # Antennes contentes (radians)
    for _ in range(3):
        mini.goto_target(antennas=[0.6, -0.6], duration=0.25)
        mini.goto_target(antennas=[-0.6, 0.6], duration=0.25)
    mini.goto_target(antennas=[0.0, 0.0], duration=0.3)

    # Tourne le corps puis revient
    mini.goto_target(body_yaw=0.5, duration=1.0)
    mini.goto_target(body_yaw=0.0, duration=1.0)

    time.sleep(0.5)
    mini.goto_sleep()
