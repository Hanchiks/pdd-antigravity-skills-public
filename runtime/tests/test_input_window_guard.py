import unittest

from input_window_guard import UntrustedInputWindow, require_expected_touch_window


COMPONENT = "com.xunmeng.pinduoduo/com.xunmeng.pinduoduo.activity.NewPageActivity"
UID = 10300


def window(index, name, *, config="0x0", frame="[0,0][1080,2400]",
           region="[0,0][1080,2400]", uid=UID, transform="transform (ROT_0) (IDENTITY)",
           miui="0x0", token="0x1"):
    return (f"      {index}: name={name}, id={100 + index}, displayId=0, inputConfig={config}, "
            f"miuiFlags={miui}, alpha=1, frame={frame}, globalScale=1, "
            f"applicationInfo.name=activity, applicationInfo.token=0x1, "
            f"touchableRegion={region}, ownerPid=55, ownerUid={uid}, dispatchingTimeout=5000ms, "
            f"token={token}, touchOcclusionMode=BLOCK_UNTRUSTED, needMiuiEmbeddedEventMapping=0, "
            f"miuiEmbeddedHotRegion=<empty>, miuiEmbeddedMidRegion=<empty>\n"
            f"      {transform}\n")


def build(extra=None):
    prefix = ("Input Dispatcher State:\n  Display: 0\n    logicalSize=1080x2400\n"
              "        transform (ROT_0) (IDENTITY)\n    Windows:\n")
    gesture = window(0, "[Gesture Monitor] WMShell", config="NOT_FOCUSABLE | TRUSTED_OVERLAY | SPY",
                     frame="[0,0][0,0]", region="[-10800,-24000][10800,24000]", uid=1000)
    middle = extra if extra is not None else window(1, "StatusBar", config="NOT_FOCUSABLE | TRUSTED_OVERLAY",
                                                  frame="[0,0][1080,111]", region="[0,0][1080,111]", uid=1000)
    app = window(2, "84efaa1 " + COMPONENT)
    return prefix + gesture + middle + app + "  NEXT_SECTION\n"


class InputWindowGuardTests(unittest.TestCase):
    def check(self, dump, *, uid=UID, component=COMPONENT):
        return require_expected_touch_window(dump, component, uid, 1022, 542, "84efaa1")

    def test_accepts_observed_identity_display_and_safe_window_order(self):
        self.assertIsNone(self.check(build()))
        self.assertIsNone(self.check(("MiInput Dispatcher State:\n  unrelated\n" + build()).replace("\n", "\r\n")))

    def test_refuses_unfocusable_overlay_even_if_touchable_region_is_elsewhere(self):
        overlay = window(1, "OTHER", config="NOT_FOCUSABLE | TRUSTED_OVERLAY", uid=10190,
                         frame="[0,111][1080,2400]", region="[0,1576][1080,2400]")
        with self.assertRaises(UntrustedInputWindow):
            self.check(build(overlay))

    def test_refuses_foreign_or_same_uid_popup_at_point(self):
        for uid in (10190, UID):
            with self.subTest(uid=uid), self.assertRaises(UntrustedInputWindow):
                self.check(build(window(1, "PopupWindow:other", uid=uid)))

    def test_refuses_unknown_or_untrusted_spy(self):
        for name, uid in (("[Gesture Monitor] WMShell", 10190), ("[Gesture Monitor] fake", 1000)):
            spy = window(1, name, uid=uid, config="NOT_FOCUSABLE | TRUSTED_OVERLAY | SPY",
                         frame="[0,0][0,0]")
            with self.subTest(name=name, uid=uid), self.assertRaises(UntrustedInputWindow):
                self.check(build(spy))

    def test_refuses_changed_uid_focus_rotation_or_missing_region(self):
        cases = [build().replace(f"ownerUid={UID}", "ownerUid=10301"),
                 build().replace("logicalSize=1080x2400", "logicalSize=2400x1080"),
                 build().replace("touchableRegion=[0,0][1080,2400]", "touchableRegion=<empty>"),
                 build().replace("inputConfig=0x0", "inputConfig=UNKNOWN_FLAG"),
                 build().replace("needMiuiEmbeddedEventMapping=0", "needMiuiEmbeddedEventMapping=1"),
                 build().replace("  Display: 0", "  Display: 1")]
        for snapshot in cases:
            with self.subTest(snapshot=snapshot[-100:]), self.assertRaises(UntrustedInputWindow):
                self.check(snapshot)
        with self.assertRaises(UntrustedInputWindow):
            self.check(build(), component="other.app/.Activity")

    def test_refuses_truncated_or_ambiguous_lists_and_stale_state(self):
        sample = build()
        for snapshot in (sample.replace("      1: name=", "      3: name="),
                         sample.replace("      1: name=", "      1: invalid="),
                         sample.replace("  NEXT_SECTION\n", ""),
                         sample.replace("Input Dispatcher State:", "Old state:"),
                         sample + sample):
            with self.subTest(snapshot=snapshot[:35]), self.assertRaises(UntrustedInputWindow):
                self.check(snapshot)

    def test_rejects_unrecognised_transform_and_unsafe_region(self):
        with self.assertRaises(UntrustedInputWindow):
            self.check(build().replace("transform (ROT_0) (IDENTITY)\n  NEXT_SECTION",
                                       "transform (ROT_0) (TRANSLATE)\n  NEXT_SECTION"))
        with self.assertRaises(UntrustedInputWindow):
            require_expected_touch_window(build(), COMPONENT, UID, 1022, 2200, "84efaa1")
        with self.assertRaises(UntrustedInputWindow):
            require_expected_touch_window(build(), COMPONENT, UID, 1022, 542, "deadbeef")


if __name__ == "__main__":
    unittest.main()
