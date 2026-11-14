"""Категория (б): офлайн-заглушки, а не устройство.

ADB/DOM/UI подменены моками; зелёный прогон не подтверждает поведение на
реальном телефоне или в браузере.
"""
import types
import unittest
from unittest.mock import patch

import pdd


class GesturePlayerTests(unittest.TestCase):
    """Раньше scroll/swipe выполняли жест и затем пытались закрыть плеер.

    Это старое ожидание было неверным: правила запрещают жесты до приёмки
    геометрии и guard'а. Теперь команды отказывают до устройства и не могут
    запускать ни жест, ни закрытие плеера.
    """

    def test_blocked_scroll_and_swipe_never_touch_player_or_device(self):
        actions = ((pdd.cmd_scroll, types.SimpleNamespace(direction="down", amount=0.6, ms=450)),
                   (pdd.cmd_swipe, types.SimpleNamespace(x1=360, y1=1200, x2=360, y2=400, ms=400)))
        for fn, args in actions:
            with self.subTest(action=fn.__name__), \
                 patch.object(pdd, "ensure_device") as device, \
                 patch.object(pdd, "run_adb") as adb, \
                 patch.object(pdd, "_close_live_player_confirmed") as close, \
                 patch.object(pdd, "tap_real") as tap, \
                 patch.object(pdd, "out") as out:
                with self.assertRaises(pdd.AdbError):
                    fn(args)
                device.assert_not_called()
                adb.assert_not_called()
                close.assert_not_called()
                tap.assert_not_called()
                out.assert_not_called()


if __name__ == "__main__":
    unittest.main()