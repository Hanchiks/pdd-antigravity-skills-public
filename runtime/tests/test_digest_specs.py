import unittest

import pdd


def e(label, x, y, path, *, click=False, package=None):
    return {"label": label, "vx": x, "vy": y, "tree_path": path, "click": click,
            "package": pdd.PH["package"] if package is None else package}


class DigestSpecsTests(unittest.TestCase):
    def digest(self, els):
        return "\n".join(pdd._build_ui_digest(els))

    def test_search_filters_are_not_product_specs(self):
        result = self.digest([e("品牌", 496, 202, "0/0/1"), e("筛选", 631, 202, "0/0/2")])
        self.assertNotIn("Параметры", result)

    def test_explicit_detail_row_with_sibling_value_is_reported_as_seller_claim(self):
        result = self.digest([e("商品参数", 300, 100, "0/0/0"),
                              e("成分含量", 100, 300, "0/0/1/0"),
                              e("棉100%", 350, 300, "0/0/1/1")])
        self.assertIn("📝 Параметры", result)
        self.assertIn("заявлено продавцом", result)
        self.assertIn("成分含量: 棉100%", result)
        self.assertNotIn("не удалось однозначно", result)

    def test_unrelated_or_ambiguous_elements_are_not_paired(self):
        header = e("商品参数", 300, 100, "0/0/0")
        key = e("品牌", 100, 300, "0/0/1/0")
        for values in ([e("筛选", 350, 300, "0/0/1/1")],
                       [e("品牌甲", 350, 300, "0/0/2/1")],
                       [e("品牌甲", 350, 300, "0/0/1/1"), e("品牌乙", 450, 300, "0/0/1/2")],
                       [e("品牌甲", 350, 300, "0/0/1/1", click=True)]):
            with self.subTest(values=values):
                self.assertNotIn("Параметры", self.digest([header, key, *values]))

    def test_details_header_must_precede_row(self):
        result = self.digest([e("商品详情", 300, 700, "0/0/3"),
                              e("品牌", 100, 300, "0/0/1/0"),
                              e("品牌甲", 350, 300, "0/0/1/1")])
        self.assertNotIn("Параметры", result)


if __name__ == "__main__":
    unittest.main()
