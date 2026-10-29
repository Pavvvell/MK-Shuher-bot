"""Independent documents: paid-card purchase and separate supplier invoice."""
import unittest

from purchase_bundle import (
    validate_package, grouped_preview, missing_invoice_project,
    apply_invoice_project, validate_persistence,
)


def sample():
    return {
        "images":[
            {"index":1,"type":"card_payment"},
            {"index":2,"type":"goods_photo"},
            {"index":3,"type":"invoice"},
        ],
        "groups":[
            {"type":"card_purchase","images":[1,2],
             "supplier":"Магазин","amount":"26,82","currency":"грн",
             "description":"Лимонна кислота, 2 упаковки по 100 г",
             "quantity":"2 × 100 г"},
            {"type":"invoice","images":[3],
             "supplier":"Постачальник Р","amount":"474.00",
             "currency":"грн","invoice_no":"Р-00001086",
             "description":"Кільця ущільнювальні"},
        ],
    }


class PurchaseBundleTests(unittest.TestCase):
    def test_invoice_and_bank_charge_remain_separate(self):
        group=validate_package(sample(),3)
        self.assertEqual(group["images"],{
            1:"card_payment",2:"goods_photo",3:"invoice"})
        self.assertEqual(group["groups"][0]["type"],"card_purchase")
        self.assertEqual(group["groups"][0]["amount"],"26.82")
        self.assertEqual(group["groups"][0]["images"],[1,2])
        self.assertEqual(group["groups"][1]["amount"],"474.00")
        self.assertEqual(group["groups"][1]["images"],[3])
        self.assertIsNotNone(missing_invoice_project(group))
        self.assertEqual(group["groups"][0]["project"],"")
        self.assertTrue(apply_invoice_project(group,"Мет обробка"))
        self.assertTrue(validate_persistence(group))
        self.assertEqual(group["groups"][0]["project"],"")
        self.assertEqual(group["groups"][1]["project"],"Мет обробка")
        preview=grouped_preview(group)
        self.assertIn("26.82",preview)
        self.assertIn("474.00",preview)
        self.assertIn("ОПЛАЧЕНА ПОКУПКА",preview)
        self.assertIn("РАХУНОК",preview)

    def test_invoice_must_have_project_before_any_financial_write(self):
        group=validate_package(sample(),3)
        with self.assertRaisesRegex(ValueError,"проєкт"):
            validate_persistence(group)

    def test_bank_payment_can_never_support_invoice(self):
        raw=sample()
        raw["groups"]=[
            {"type":"invoice","images":[1,2,3],
             "amount":"474","supplier":"Постачальник Р"},
        ]
        with self.assertRaisesRegex(ValueError,"змішувати"):
            validate_package(raw,3)

    def test_cannot_attach_same_photo_to_two_unrelated_purchases(self):
        raw=sample()
        raw["groups"][1]["images"]=[2,3]
        with self.assertRaisesRegex(ValueError,"різних операцій"):
            validate_package(raw,3)

    def test_all_financial_images_must_be_assigned(self):
        raw=sample()
        raw["groups"]=raw["groups"][:1]
        with self.assertRaisesRegex(ValueError,"нерозподілені"):
            validate_package(raw,3)

    def test_missing_product_photo_blocks_an_unknown_omission(self):
        raw=sample()
        raw["groups"][0]["images"]=[1]
        with self.assertRaisesRegex(ValueError,"нерозподілені"):
            validate_package(raw,3)

    def test_unknown_images_cannot_be_silently_deleted(self):
        raw=sample()
        raw["images"][1]["type"]="other"
        raw["groups"][0]["images"]=[1]
        raw["groups"][1]["images"]=[3]
        bundle=validate_package(raw,3)
        apply_invoice_project(bundle,"Мет обробка")
        with self.assertRaisesRegex(ValueError,"невизначені"):
            validate_persistence(bundle)

    def test_invalid_amounts_and_duplicate_image_indices_rejected(self):
        for amount in ("-26.82","0","NaN","not a price",""):
            raw=sample()
            raw["groups"][0]["amount"]=amount
            with self.subTest(amount=amount),self.assertRaises(ValueError):
                validate_package(raw,3)
        raw=sample()
        raw["images"][1]["index"]=1
        with self.assertRaisesRegex(ValueError,"Номери фото"):
            validate_package(raw,3)

    def test_visible_currency_suffix_allowed_and_normalized(self):
        raw=sample()
        raw["groups"][0]["amount"]="26,82 грн"
        pkg=validate_package(raw,3)
        self.assertEqual(pkg["groups"][0]["amount"],"26.82")

    def test_confirmation_survives_google_sheets_json_serialization(self):
        import json
        package=validate_package(sample(),3)
        apply_invoice_project(package,"Мет обробка")
        stored=json.loads(json.dumps(package,ensure_ascii=False))
        self.assertEqual(stored["images"]["1"],"card_payment")
        self.assertEqual(stored["images"]["2"],"goods_photo")
        self.assertEqual(stored["images"]["3"],"invoice")
        self.assertTrue(validate_persistence(stored))
        self.assertEqual(stored["groups"][0]["images"],[1,2])
        self.assertEqual(stored["groups"][1]["images"],[3])

    def test_two_independent_invoices_need_separate_projects(self):
        raw={
            "images":[{"index":1,"type":"invoice"},
                      {"index":2,"type":"invoice"}],
            "groups":[
                {"type":"invoice","images":[1],"amount":"100"},
                {"type":"invoice","images":[2],"amount":"200"},
            ],
        }
        pkg=validate_package(raw,2)
        apply_invoice_project(pkg,"STF")
        self.assertEqual(pkg["groups"][0]["project"],"STF")
        self.assertEqual(pkg["groups"][1]["project"],"")
        with self.assertRaises(ValueError):
            validate_persistence(pkg)
        apply_invoice_project(pkg,"ДП")
        self.assertTrue(validate_persistence(pkg))

if __name__=="__main__":
    unittest.main()
