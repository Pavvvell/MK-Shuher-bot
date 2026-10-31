"""Invoice selection and Telegram file/caption regression tests, all offline."""
import importlib
import os
import unittest
from io import BytesIO
from unittest.mock import AsyncMock, MagicMock, patch

from PIL import Image
from payable_invoices import (is_payable_request, payable_rows, invoice_caption,
                              invoice_files, send_invoice)
from natural import interpret_natural


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
    def test_user_request_and_common_variants(self):
        for text in ("покажи всі рахунки які я мав кинути на оплату",
                     "Шухер, надішли рахунки до оплати", "дай неоплачені рахунки",
                     "мені скинь рахунки",
                     "то скинеш пдф файли і фото з підписом, що куди на оплату?",
                     "Скинеш мені рахунки?", "Можеш надіслати мені рахунки?",
                     "Мені надішли оригінали рахунків", "покажи рахунок на оплату",
                     "Які рахунки до оплати?", "/invoices"):
            with self.subTest(text=text):
                self.assertTrue(is_payable_request(text))
        for text in ("це рахунок на оплату", "рахунок оплачено", "оплати рахунки",
                     "покажи оплачені рахунки", "покажи накладні",
                     "не скидай мені рахунки", "не треба надсилати рахунки",
                     "Юрій сказав: скинь рахунки", "надішли Юрію рахунки на оплату",
                     "напиши Юрію, щоб він надіслав мені рахунки",
                     "скинеш мені фото?", "я надішлю тобі рахунки"):
            with self.subTest(text=text):
                self.assertFalse(is_payable_request(text))

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
        text=invoice_caption(invoice(**{"Постачальник":"**Катана**\nТОВ", "Автор":"@buyer"}),
                             [["123","buyer","Юрій Остапенко","",""]])
        self.assertEqual(text,"Катана ТОВ\nМет обробка (будівництво)\nЮрій Остапенко")
        self.assertEqual(invoice_caption({}).splitlines(),
                         ["Постачальника не вказано","Проєкт не вказано","Хто подав — не вказано"])
        self.assertEqual(invoice_caption(invoice(**{"Автор":"@unknown"})).splitlines()[2],"@unknown")

    def test_missing_file_and_external_url_not_fetched(self):
        for value in ("", "https://example.com/invoice.pdf"):
            with self.assertRaises(ValueError):
                invoice_files(invoice(**{"Telegram файли":value}))


class FileDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_pdf_original_reused_with_caption_only_to_requesting_chat(self):
        call,fetch=AsyncMock(),AsyncMock()
        count=await send_invoice(invoice(),123,[],call,fetch)
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
            await send_invoice(row,123,[],call,fetch)
        self.assertEqual(call.await_count,4)
        for sent in call.await_args_list:
            self.assertEqual(sent.args[0],"sendPhoto")
            filename,raw,mime=sent.args[2]["photo"]
            self.assertEqual(raw,photo())
            self.assertEqual(mime,"image/jpeg")
            self.assertEqual(sent.args[1]["caption"],"Катана\nМет обробка (будівництво)\nШакін")

    async def test_unknown_legacy_pdf_and_bad_photo(self):
        call,fetch=AsyncMock(),AsyncMock(return_value=b"%PDF-test")
        await send_invoice(invoice(**{"Документ / посилання":""}),123,[],call,fetch)
        self.assertEqual(call.await_args.args[1]["document"],"pdf1")
        call.reset_mock()
        fetch.return_value=b"invalid-image"
        with self.assertRaises((ValueError,OSError)):
            await send_invoice(invoice(**{"Документ / посилання":""}),123,[],call,fetch)
        call.assert_not_awaited()

    async def test_natural_owner_only(self):
        model=AsyncMock(return_value='{"intent":"payable_invoices"}')
        for owner,expected in ((False,"other"),(True,"payable_invoices")):
            result=await interpret_natural("що треба оплатити",{"Рахунки":"read"},owner,model)
            self.assertEqual(result["intent"],expected)


class AppDelivery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Import the actual app without credentials or any external service calls.
        env={"TELEGRAM_BOT_TOKEN":"test", "TELEGRAM_WEBHOOK_SECRET":"test",
             "OPENAI_API_KEY":"test", "SPREADSHEET_ID":"test", "OWNER_IDS":"123"}
        with patch.dict(os.environ,env), patch("store.SheetsStore"), patch("openai.AsyncOpenAI"):
            self.app=importlib.import_module("app")

    async def test_permission_checked_before_any_sheet_read(self):
        with patch.object(self.app,"send",AsyncMock()) as send, \
             patch.object(self.app,"sheet",AsyncMock()) as sheet:
            await self.app.show_payable_invoices(999,999)
            sheet.assert_not_awaited()
            self.assertIn("власник",send.await_args.args[1])

    async def test_failed_attachment_does_not_hide_next_invoice_or_mutate_store(self):
        records=[invoice(ID="broken"),invoice(ID="good")]
        headers=list(records[0])
        fake=MagicMock()
        fake.read_table.return_value=(headers,[[r[h] for h in headers] for r in records])
        fake.users.return_value=[]
        with patch.object(self.app,"store",fake), patch.object(self.app,"send",AsyncMock()) as send, \
             patch.object(self.app,"send_invoice",AsyncMock(side_effect=[ValueError("missing"),1])) as deliver:
            await self.app.show_payable_invoices(123,123)
            self.assertEqual(deliver.await_count,2)
            self.assertEqual([c[0] for c in fake.mock_calls],["read_table","users"])
            self.assertIn("1 із 2",send.await_args.args[1])

    async def test_request_bypasses_pending_drafts_and_model(self):
        update={"message":{"from":{"id":123},"chat":{"id":123,"type":"private"},
                           "text":"покажи всі рахунки які я мав кинути на оплату"}}
        fake=MagicMock()
        fake.register.return_value=False
        with patch.object(self.app,"store",fake), \
             patch.object(self.app,"show_payable_invoices",AsyncMock()) as show, \
             patch.object(self.app,"ask_model",AsyncMock()) as model:
            await self.app.handle_message(update)
            show.assert_awaited_once_with(123,123)
            model.assert_not_awaited()
            fake.get_pending.assert_not_called()
            fake.save_pending.assert_not_called()

    async def test_screenshot_phrases_reach_actual_telegram_document_delivery(self):
        # Exercise the whole message -> Sheets -> sendDocument path, rather
        # than mocking the delivery handler as the original routing test did.
        for text in ("мені скинь рахунки",
                     "то скинеш пдф файли і фото з підписом, що куди на оплату?"):
            with self.subTest(text=text):
                update={"message":{"from":{"id":123},"chat":{"id":123,"type":"private"},
                                   "text":text}}
                records=[invoice(ID="SH-"+str(i),**{"Telegram файли":"pdf"+str(i)}) for i in range(3)]
                headers=list(records[0])
                fake=MagicMock()
                fake.register.return_value=False
                fake.read_table.return_value=(headers,[[row[h] for h in headers] for row in records])
                fake.users.return_value=[]
                with patch.object(self.app,"store",fake), \
                     patch.object(self.app,"bot_call",AsyncMock(return_value={"ok":True})) as telegram, \
                     patch.object(self.app,"handle_staff_outreach",AsyncMock()) as outreach, \
                     patch.object(self.app,"ask_model",AsyncMock()) as model:
                    await self.app.handle_message(update)
                    files=[call.args[1] for call in telegram.await_args_list if call.args[0]=="sendDocument"]
                    self.assertEqual([f["document"] for f in files],["pdf0","pdf1","pdf2"])
                    for f in files:
                        self.assertEqual(f["caption"],"Катана\nМет обробка (будівництво)\nШакін")
                    self.assertTrue(all(c.args[1]["chat_id"]==123 for c in telegram.await_args_list))
                    model.assert_not_awaited()
                    outreach.assert_not_awaited()
                    fake.get_pending.assert_not_called()
                    fake.append_record.assert_not_called()


if __name__=="__main__":
    unittest.main()
