"""Категория (б): офлайн-заглушки, а не устройство.

ADB/DOM/UI подменены моками; зелёный прогон не подтверждает поведение на
реальном телефоне или в браузере.
"""
import subprocess
import types
import unittest
from unittest.mock import patch

import pdd


SC = {"real_w": 1080, "real_h": 2400, "view_w": 720, "view_h": 1600, "scale": 1.5}
FOCUS_FN = pdd._require_pdd_focus


def element(label, x, y, w=100, h=50, **kwargs):
    return {"label": label, "desc": "", "rx": round(x * 1.5), "ry": round(y * 1.5),
            "vx": x, "vy": y, "vw": w, "vh": h, "click": True,
            "rid": "", "placeholder": False, "tree_path": "0/0", "package": pdd.PH["package"], **kwargs}


class SafetyTests(unittest.TestCase):
    def setUp(self):
        self.patchers = [patch.object(pdd, "ensure_device"), patch.object(pdd, "_require_pdd_focus"), patch.object(pdd, "screen_info", return_value=SC),
                         patch.object(pdd, "SERIAL", "test-device"), patch.object(pdd, "out")]
        for p in self.patchers:
            p.start()
            self.addCleanup(p.stop)

    def state(self, els, full=None):
        return {"ui": els, "ui_signature": pdd.ui_signature(full if full is not None else els),
                "ui_serial": "test-device", "ui_captured_at": pdd.time.time(), "ui_quality": "full",
                "ui_page_anchors": pdd._ui_page_anchors(full if full is not None else els)}

    def test_tap_checks_fresh_screen_not_saved_state(self):
        old = [element("去结算", 500, 1400)]
        fresh = [element("收藏", 500, 1400)]
        with patch.object(pdd, "load_state", return_value=self.state(old)), \
             patch.object(pdd, "read_ui_elements", return_value=fresh), \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tap(types.SimpleNamespace(x=500, y=1400, long=0))
            tap.assert_called_once_with(750, 2100, 0)

    def test_tap_refuses_forbidden_fresh_and_ui_error(self):
        with patch.object(pdd, "tap_real") as tap:
            for current in ([element("立即支付", 500, 1400)], pdd.AdbError("dump failed"), []):
                with self.subTest(current=current), \
                     patch.object(pdd, "read_ui_elements", side_effect=current if isinstance(current, Exception) else None,
                                  return_value=current):
                    with self.assertRaises(pdd.AdbError):
                        pdd.cmd_tap(types.SimpleNamespace(x=500, y=1400, long=0))
            tap.assert_not_called()

    def test_unlabeled_or_fullscreen_only_tree_refuses(self):
        tree = [element("拍照搜索", 360, 800, 720, 1600)]
        with patch.object(pdd, "read_ui_elements", return_value=tree), patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tap(types.SimpleNamespace(x=500, y=1400, long=0))
            tap.assert_not_called()

    def test_tapel_rejects_changed_screen_and_legacy_state(self):
        original = [element("收藏", 130, 1400)]
        changed = [element("去结算", 130, 1400)]
        with patch.object(pdd, "read_ui_elements", return_value=changed), patch.object(pdd, "tap_real") as tap:
            for st in (self.state(original), {"ui": original}):
                with patch.object(pdd, "load_state", return_value=st):
                    with self.assertRaises(pdd.AdbError):
                        pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_not_called()

    def test_multi_revalidates_and_stops_after_change(self):
        original = [element("第一项", 200, 200), element("第二项", 300, 300)]
        changed = [original[0], element("去结算", 300, 300)]
        with patch.object(pdd, "load_state", return_value=self.state(original)), \
             patch.object(pdd, "read_ui_elements", side_effect=[original, changed]), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "выполнено нажатий: 1"):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0, 1], long=0))
            tap.assert_called_once()

    def test_multi_works_when_screen_unchanged(self):
        original = [element("第一项", 200, 200), element("第二项", 300, 300)]
        with patch.object(pdd, "load_state", return_value=self.state(original)), \
             patch.object(pdd, "read_ui_elements", return_value=original), \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tapel(types.SimpleNamespace(indices=[0, 1], long=0))
            self.assertEqual(tap.call_count, 2)

    def test_ui_dump_empty_and_corrupt_fails_closed(self):
        other_app = '<hierarchy><node package="other.app" text="确认" bounds="[0,150][200,300]" /></hierarchy>'
        for xml in ("<hierarchy />", "<broken>", other_app):
            with self.subTest(xml=xml), patch.object(pdd, "dump_ui_xml", return_value=xml):
                with self.assertRaises(pdd.AdbError):
                    pdd.read_ui_elements(include_all=True)

    def test_dump_retries_compressed_after_idle_error_or_timeout(self):
        xml = '<?xml version="1.0"?><hierarchy></hierarchy>'
        for failure in ("ERROR: could not get idle state.", pdd.AdbError("Таймаут команды")):
            calls = []
            def adb(args, **kwargs):
                calls.append(args)
                if args[:3] == ["shell", "uiautomator", "dump"]:
                    if "--compressed" in args:
                        return "UI hierchary dumped to: /sdcard/pdd_ui.xml"
                    if isinstance(failure, Exception):
                        raise failure
                    return failure
                if args[:2] == ["exec-out", "cat"]:
                    return xml
                return ""
            with self.subTest(failure=failure), patch.object(pdd, "run_adb", side_effect=adb):
                self.assertEqual(pdd.dump_ui_xml(), xml)
                self.assertEqual(sum(c[:3] == ["shell", "rm", "-f"] for c in calls), 2)
                self.assertTrue(any("--compressed" in c for c in calls))
                dumps = [c for c in calls if c[:3] == ["shell", "uiautomator", "dump"]]
                self.assertNotEqual(dumps[0][-1], dumps[1][-1])

    def test_dump_never_reads_stale_file_after_failed_attempts(self):
        def adb(args, **kwargs):
            if args[:2] == ["exec-out", "cat"]:
                self.fail("A failed dump must not read a stale file")
            if args[:3] == ["shell", "uiautomator", "dump"]:
                raise pdd.AdbError("Таймаут команды")
            return ""
        with patch.object(pdd, "run_adb", side_effect=adb), self.assertRaisesRegex(pdd.AdbError, "Проверьте состояние экрана"):
            pdd.dump_ui_xml()

    def test_uiautomator_reports_idle_error_from_stderr_on_exit_zero(self):
        error = b"ERROR: could not get idle state."
        def subprocess_run(cmd, **kwargs):
            if "uiautomator" in cmd:
                return types.SimpleNamespace(returncode=0, stdout=b"", stderr=error)
            return types.SimpleNamespace(returncode=0, stdout=b"", stderr=b"")
        # Старое ожидание автоматического закрытия плеера при ошибке idle было
        # неверным: путь чтения обязан быть пассивным. Теперь dump_ui_xml
        # сообщает о недоступности дерева и предлагает явную команду.
        with patch.object(pdd.subprocess, "run", side_effect=subprocess_run), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "could not get idle state") as err:
                pdd.dump_ui_xml()
            tap.assert_not_called()
            self.assertIn("автоматических действий", str(err.exception))
            self.assertIn("close-player", str(err.exception))

    def test_run_adb_keeps_stderr_opt_in_for_other_callers(self):
        response = types.SimpleNamespace(returncode=0, stdout=b"output", stderr=b"diagnostic")
        with patch.object(pdd.subprocess, "run", return_value=response):
            self.assertEqual(pdd.run_adb(["shell", "test"]), "output")
            self.assertEqual(pdd.run_adb(["shell", "test"], include_stderr=True), "output\ndiagnostic")

    def test_compressed_tree_is_marked_and_never_authorizes_tap(self):
        xml = (f'<?xml version="1.0"?><hierarchy><node package="{pdd.PH["package"]}" '
               'text="发起拼单" bounds="[800,2100][1070,2300]" /></hierarchy>')
        with patch.object(pdd, "dump_ui_xml", return_value=pdd.UiDump(xml, compressed=True)), \
             patch.object(pdd, "tap_real") as tap:
            self.assertTrue(pdd.read_ui_elements(include_all=True).compressed)
            for force in (False, True):
                with self.subTest(force=force), self.assertRaisesRegex(pdd.AdbError, "сжатый дамп"):
                    pdd.cmd_tap(types.SimpleNamespace(x=630, y=1470, long=0, force=force))
            target = element("发起拼单", 630, 1470)
            with patch.object(pdd, "load_state", return_value=self.state([target])):
                with self.assertRaisesRegex(pdd.AdbError, "сжатый дамп"):
                    pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
            tap.assert_not_called()

    def test_tree_without_quality_metadata_never_authorizes_tap(self):
        xml = (f'<?xml version="1.0"?><hierarchy><node package="{pdd.PH["package"]}" '
               'text="收藏" bounds="[100,150][600,300]" /></hierarchy>')
        with patch.object(pdd, "dump_ui_xml", return_value=xml):
            self.assertIsNone(pdd.read_ui_elements(include_all=True).compressed)
            with self.assertRaisesRegex(pdd.AdbError, "Происхождение"):
                pdd.read_ui_elements(include_all=True, require_full=True)

    def test_payment_hidden_past_truncated_label_is_still_blocked(self):
        text = "a" * 121 + "立即支付"
        xml = f'<hierarchy><node package="{pdd.PH["package"]}" text="{text}" bounds="[100,150][600,300]" /></hierarchy>'
        with patch.object(pdd, "dump_ui_xml", return_value=xml):
            elements = pdd.read_ui_elements(include_all=True)
        self.assertTrue(pdd._is_forbidden(elements[0]))

    def test_signed_bounds_are_clipped_and_protected(self):
        xml = f'<hierarchy><node package="{pdd.PH["package"]}" text="去结算" bounds="[-150,1800][450,2200]" /></hierarchy>'
        with patch.object(pdd, "dump_ui_xml", return_value=xml):
            elements = pdd.read_ui_elements(include_all=True)
        self.assertEqual((elements[0]["vx"], elements[0]["vw"]), (150, 300))
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(50, 1300, elements)

    def test_clickable_node_without_usable_bounds_blocks(self):
        app = pdd.PH["package"]
        for bounds in ('bounds="invalid"', '', 'bounds="[400,200][100,300]"'):
            xml = (f'<hierarchy><node package="{app}" text="收藏" bounds="[100,150][600,300]" />'
                   f'<node package="{app}" clickable="true" {bounds} /></hierarchy>')
            with self.subTest(bounds=bounds), patch.object(pdd, "dump_ui_xml", return_value=xml):
                with self.assertRaises(pdd.AdbError):
                    pdd.read_ui_elements(include_all=True)

    def test_zero_area_and_completely_offscreen_nodes_do_not_block(self):
        app = pdd.PH["package"]
        xml = (f'<hierarchy><node package="{app}" text="收藏" bounds="[100,150][600,300]" />'
               f'<node package="{app}" clickable="true" bounds="[0,0][0,0]" />'
               f'<node package="{app}" clickable="true" bounds="[-400,-400][-100,-100]" />'
               f'</hierarchy>')
        with patch.object(pdd, "dump_ui_xml", return_value=xml):
            elements = pdd.read_ui_elements(include_all=True)
        self.assertEqual(len(elements), 1)

    def test_clickable_unknown_overlay_is_not_mistaken_for_container(self):
        label = element("收藏", 300, 300, tree_path="0/0/0")
        parent = element("<Layout>", 300, 300, tree_path="0/0", placeholder=True)
        overlay = element("<Overlay>", 300, 300, tree_path="0/1", placeholder=True)
        pdd._check_target(300, 300, [parent, label])
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(300, 300, [parent, label, overlay])

    def test_bottom_panel_background_container_allows_group_buy(self):
        target = element("发起拼单", 630, 1470, 160, 70, tree_path="0/1/0")
        background = element("<panel>", 360, 1470, 720, 120, tree_path="0/1",
                             placeholder=True, **{"class": "android.widget.FrameLayout"})
        with patch.object(pdd, "read_ui_elements", return_value=[target, background]), \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tap(types.SimpleNamespace(x=630, y=1470, long=0))
            tap.assert_called_once_with(945, 2205, 0)
        unknown_wide_layer = element("<panel>", 360, 1470, 720, 120, tree_path="0/2",
                                     placeholder=True, **{"class": "android.widget.FrameLayout"})
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(630, 1470, [target, background, unknown_wide_layer])
        overlay = element("<overlay>", 630, 1470, 160, 70, tree_path="0/3",
                          placeholder=True, **{"class": "android.widget.FrameLayout"})
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(630, 1470, [target, background, overlay])

    def test_force_skips_unknown_layer_but_preserves_hard_blocks(self):
        # --force по-прежнему пропускает безымянный слой вне опасной нижней
        # зоны, но не в ней самой: там назначение слоя доказать нельзя.
        safe_target = element("商品评价", 300, 800, 160, 70, tree_path="0/0")
        safe_overlay = element("<overlay>", 300, 800, 160, 70, tree_path="0/1", placeholder=True)
        with patch.object(pdd, "read_ui_elements", return_value=[safe_target, safe_overlay]), \
             patch.object(pdd, "tap_real") as tap:
            pdd.cmd_tap(types.SimpleNamespace(x=300, y=800, long=0, force=True))
            tap.assert_called_once()
        target = element("发起拼单", 630, 1470, 160, 70, tree_path="0/0")
        overlay = element("<overlay>", 630, 1470, 160, 70, tree_path="0/1", placeholder=True)
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(630, 1470, [target, overlay], force=True)
        for label in ("立即支付", "提交订单", "确认支付", "去结算", "全选"):
            with self.subTest(label=label), self.assertRaises(pdd.AdbError):
                pdd._check_target(630, 1470, [target, overlay, element(label, 630, 1470)], force=True)
        for x, y in ((630, 87), (-1, 1470), (720, 1470), (630, 1600)):
            with self.subTest(x=x, y=y), self.assertRaises(pdd.AdbError):
                pdd._check_target(x, y, [target], force=True)
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(50, 1470, [element("收藏", 50, 1470), element("删除", 300, 1470)], force=True)
        with self.assertRaises(pdd.AdbError):
            pdd._check_target(630, 1470, [target, element("系统", 630, 1470, package="android")], force=True)

    def test_system_overlay_and_unknown_package_refuse(self):
        label = element("收藏", 300, 300)
        for pkg in ("com.android.permissioncontroller", ""):
            with self.subTest(package=pkg):
                overlay = element("提示", 300, 300, package=pkg)
                with self.assertRaises(pdd.AdbError):
                    pdd._check_target(300, 300, [label, overlay])

    def test_current_focused_window_must_be_pdd(self):
        good = "mCurrentFocus=Window{abc u0 com.xunmeng.pinduoduo/.MainActivity}"
        with patch.object(pdd, "run_adb", return_value=good):
            FOCUS_FN()
        for other in ("", "mCurrentFocus=null", "mCurrentFocus=Window{abc u0 com.android.settings/.Settings}"):
            with self.subTest(value=other), patch.object(pdd, "run_adb", return_value=other):
                with self.assertRaises(pdd.AdbError):
                    FOCUS_FN()

    def test_window_change_between_check_and_tap_blocks(self):
        tree = [element("收藏", 300, 300)]
        with patch.object(pdd, "read_ui_elements", return_value=tree), \
             patch.object(pdd, "_require_pdd_focus", side_effect=[None, pdd.AdbError("окно изменилось")]), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaises(pdd.AdbError):
                pdd.cmd_tap(types.SimpleNamespace(x=300, y=300, long=0))
            tap.assert_not_called()

    def test_unlabelled_clickable_in_bottom_zone_is_blocked_even_with_force(self):
        target = element("发起拼单", 630, 1470, 160, 70, tree_path="0/0")
        overlay = element("<overlay>", 630, 1470, 160, 70, tree_path="0/1", placeholder=True)
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                pdd._check_target(630, 1470, [target, overlay], force=force)

    def test_unlabelled_clickable_outside_danger_zone_can_be_forced(self):
        target = element("商品评价", 300, 800, 160, 70, tree_path="0/0")
        overlay = element("<overlay>", 300, 800, 160, 70, tree_path="0/1", placeholder=True)
        pdd._check_target(300, 800, [target, overlay], force=True)

    def test_documented_mass_selection_synonym_is_forbidden(self):
        e = element("Выбрать всё", 50, 1200)
        self.assertTrue(pdd._is_forbidden(e))
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                pdd._check_target(50, 1200, [e], force=force)

    def test_forbidden_container_and_mass_selection_zone_block_nested_label(self):
        label = element("收藏", 50, 1470, 40, 40, tree_path="0/0/0")
        container = element("全选 删除", 50, 1470, 100, 60, tree_path="0/0", click=False)
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                pdd._check_target(50, 1470, [container, label], force=force)
        placeholder = element("<CheckBox>", 50, 1470, 40, 40, tree_path="0/1", placeholder=True)
        for force in (False, True):
            with self.subTest(force=force), self.assertRaises(pdd.AdbError):
                pdd._check_target(50, 1470, [label, placeholder], force=force)

    def test_tapel_refuses_multiple_unlabelled_targets(self):
        els = [element("<CheckBox>", 50, 200, placeholder=True, tree_path="0/0"),
               element("<CheckBox>", 50, 300, placeholder=True, tree_path="0/1")]
        with patch.object(pdd, "load_state", return_value=self.state(els)), \
             patch.object(pdd, "read_ui_elements", return_value=els), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "безымянн"):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0, 1], long=0))
            tap.assert_not_called()

    def test_tapel_refuses_multiple_targets_in_select_all_mode(self):
        els = [element("商品甲", 200, 200, tree_path="0/0"),
               element("商品乙", 200, 300, tree_path="0/1"),
               element("全选", 50, 1470, tree_path="0/2")]
        with patch.object(pdd, "load_state", return_value=self.state(els)), \
             patch.object(pdd, "read_ui_elements", return_value=els), \
             patch.object(pdd, "tap_real") as tap:
            with self.assertRaisesRegex(pdd.AdbError, "массов"):
                pdd.cmd_tapel(types.SimpleNamespace(indices=[0, 1], long=0))
            tap.assert_not_called()

    def test_tapel_output_does_not_claim_favorite_result(self):
        target = [element("收藏", 135, 1475)]
        with patch.object(pdd, "load_state", return_value=self.state(target)), \
             patch.object(pdd, "read_ui_elements", return_value=target), \
             patch.object(pdd, "tap_real"), patch.object(pdd, "out") as output:
            pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))
        payload = output.call_args.args[0]
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["confirmation"], "command_sent")
        text = str(payload).lower()
        for claim in ("удалено", "сохранено", "removed", "saved", "deleted"):
            self.assertNotIn(claim, text)

    def test_sales_digest_separates_brand_shop_explicit_and_unknown(self):
        els = [element("品牌已拼4万+件", 100, 100), element("本店已拼1万件", 200, 200),
               element("商品已拼200件", 300, 300), element("已拼300件", 400, 400)]
        digest = "\n".join(pdd._build_ui_digest(els))
        self.assertIn("Продажи бренда: [0]", digest)
        self.assertIn("Продажи магазина: [1]", digest)
        self.assertIn("Продажи карточки: [2]", digest)
        self.assertIn("Продажи, требующие проверки: [3]", digest)

    def test_actions_refuse_changed_geometry_and_invalidate_cache(self):
        # Кеш 1080x2400, но свежий screen_info возвращает другой размер.
        stale = {"screen": {"serial": "test-device", "real_w": 2400, "real_h": 1080,
                            "view_w": 720, "view_h": 540, "scale": 2.0}}
        cases = [
            ("tap", lambda: pdd.cmd_tap(types.SimpleNamespace(x=300, y=300, long=0))),
            ("tapel", lambda: pdd.cmd_tapel(types.SimpleNamespace(indices=[0], long=0))),
            ("text", lambda: pdd.cmd_text(types.SimpleNamespace(file=None, text="x", clear=False, enter=False))),
            ("key", lambda: pdd.cmd_key(types.SimpleNamespace(key="back"))),
        ]
        for name, call in cases:
            with self.subTest(action=name):
                state = dict(stale)
                def update(fn, state=state):
                    return fn(state)
                with patch.object(pdd, "load_state", return_value=state), \
                     patch.object(pdd, "update_state", side_effect=update), \
                     patch.object(pdd, "read_ui_elements") as read, \
                     patch.object(pdd, "tap_real") as tap:
                    with self.assertRaisesRegex(pdd.AdbError, "Геометрия экрана изменилась"):
                        call()
                    read.assert_not_called()
                    tap.assert_not_called()
                self.assertNotIn("screen", state)

    def test_tap_timeout_reports_unknown_and_never_resends(self):
        with patch.object(pdd, "run_adb",
                          side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=40)) as adb, \
             patch.object(pdd, "pace") as pace:
            with self.assertRaisesRegex(pdd.AdbError, "unknown") as err:
                pdd.tap_real(450, 450)
            self.assertIn("неизвестно", str(err.exception))
            self.assertIn("Повтор вслепую", str(err.exception))
            self.assertEqual(adb.call_count, 1)
            pace.assert_not_called()

    def test_tap_output_marks_command_sent_not_confirmed(self):
        target = [element("收藏", 300, 300)]
        with patch.object(pdd, "read_ui_elements", return_value=target), \
             patch.object(pdd, "tap_real"), patch.object(pdd, "out") as output:
            pdd.cmd_tap(types.SimpleNamespace(x=300, y=300, long=0))
        payload = output.call_args.args[0]
        self.assertEqual(payload["confirmation"], "command_sent")
        self.assertNotIn("state_confirmed", str(payload))


if __name__ == "__main__":
    unittest.main()
