"""La tête suit ton visage avec la caméra du robot. Ctrl+C pour arrêter."""

import time

from reachy_mini import ReachyMini

with ReachyMini() as mini:
    mini.wake_up()
    mini.start_head_tracking()
    print("Suivi du visage actif. Ctrl+C pour arrêter.")
    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        mini.stop_head_tracking()
        mini.goto_sleep()
