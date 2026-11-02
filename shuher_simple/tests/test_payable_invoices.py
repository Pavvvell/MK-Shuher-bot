"""Invoice selection and Telegram file/caption regression tests, all offline."""
import importlib
import os
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from payable_invoices import (payable_rows, invoice_caption,
                              invoice_files, send_invoice)


def invoice(**changes):
    row={"ID":"SH-TEST-1", "Постачальник":"Катана",
         "Проєкт":"Мет обробка (будівництво)", "Автор":"Шакін",
         "Статус власника":"Очікує перевірки", "Тип документа":"Рахунок",
         "Telegram файли":"pdf1", "Документ / посилання":"Фото Telegram: 0; PDF: 1"}
    row.update(changes)
    return row


def photo():
    output=BytesIO()
    with Image.new("RGB",(120,200),"white") as img:
        img.save(output,format="JPEG")
    return output.getvalue()


class InvoiceRules(unittest.TestCase):

    def test_unpaid_partial_and_unknown_included_paid_cancelled_card_excluded(self):
        records=[invoice(ID=str(i),**{"Статус оплати":status}) for i,status in enumerate(
            ("", "Не оплачено", "Частково оплачено", "На оплаті", "Оплачено", "Скасовано"))]
        records+=[invoice(ID="rejected",**{"Статус власника":"Відхилено"}),
                  invoice(ID="card",**{"Тип документа":"Підтвердження оплати карткою"})]
        headers=list(records[0]) + ["Статус оплати"]
        rows=[[r.get(h,"") for h in headers] for r in records]
        rows+=[rows[0],[]]
        self.assertEqual([r["ID"] for r in payable_rows(headers,rows)],["0","1","2","3"])

    def test_exact_three_line_caption_and_unknowns(self):
        self.assertEqual(invoice_caption(invoice()),"Катана\nМет обробка (будівництво)\nШакін")
        text=invoice_caption(invoice(**{"Постачальник":"**Катана**\nТОВ", "Автор":"@buyer"}))
        self.assertEqual(text,"Катана\nМет обробка (будівництво)\nШакін")
        self.assertEqual(invoice_caption({}).splitlines(),
                         ["Катана","Проєкт не вказано","Шакін"])
        for author in ("@unknown","Yuriy","Юрій Остапенко",""):
            self.assertEqual(invoice_caption(invoice(**{"Автор":author})).splitlines()[2],"Шакін")
        row=invoice(**{"Постачальник":"Інший постачальник", "Проєкт":"Інший проєкт", "Автор":"Yuriy"})
        self.assertEqual(invoice_caption(row),"Катана\nІнший проєкт\nШакін")
        self.assertEqual(row["Постачальник"],"Інший постачальник")
        self.assertEqual(row["Автор"],"Yuriy")

    def test_missing_file_and_external_url_not_fetched(self):
        for value in ("", "https://example.com/invoice.pdf"):
            with self.assertRaises(ValueError):
                invoice_files(invoice(**{"Telegram файли":value}))


class FileDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_original_reused_with_caption_only_to_requesting_chat(self):
        call,fetch=AsyncMock(),AsyncMock()
        count=await send_invoice(invoice(),123,call,fetch)
        self.assertEqual(count,1)
        fetch.assert_not_awaited()
        call.assert_awaited_once_with("sendDocument",{
            "chat_id":123,"document":"pdf1",
            "caption":"Катана\nМет обробка (будівництво)\nШакін"})

    async def test_photos_remain_photos_with_the_same_caption(self):
        call,fetch=AsyncMock(),AsyncMock(return_value=photo())
        for rid in ("SH-A","SH-B"):
            row=invoice(ID=rid,**{"Telegram файли":"photo1, photo2",
                                 "Документ / посилання":"Окремі фото: 1, 2"})
            await send_invoice(row,123,call,fetch)
        self.assertEqual(call.await_count,4)
        for sent in call.await_args_list:
            self.assertEqual(sent.args[0],"sendPhoto")
            filename,raw,mime=sent.args[2]["photo"]
            self.assertEqual(raw,photo())
            self.assertEqual(mime,"image/jpeg")
            self.assertEqual(sent.args[1]["caption"],"Катана\nМет обробка (будівництво)\nШакін")

    async def test_unknown_legacy_pdf_and_bad_photo(self):
        call,fetch=AsyncMock(),AsyncMock(return_value=b"%PDF-test")
        await send_invoice(invoice(**{"Документ / посилання":""}),123,call,fetch)
        self.assertEqual(call.await_args.args[1]["document"],"pdf1")
        call.reset_mock()
        fetch.return_value=b"invalid-image"
        with self.assertRaises((ValueError,OSError)):
            await send_invoice(invoice(**{"Документ / посилання":""}),123,call,fetch)
        call.assert_not_awaited()


if __name__=="__main__":
    unittest.main()
