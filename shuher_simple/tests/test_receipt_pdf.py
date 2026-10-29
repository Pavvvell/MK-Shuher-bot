"""Offline PDF packaging checks for delivery note photos."""
import unittest
from io import BytesIO

from PIL import Image
from receipt_pdf import photos_to_pdf


def image_bytes(color,size=(320,480)):
    image=Image.new("RGB",size,color)
    out=BytesIO()
    image.save(out,format="JPEG")
    image.close()
    return out.getvalue()


class PDFTests(unittest.TestCase):
    def test_three_photos_produce_real_pdf(self):
        content=photos_to_pdf([
            image_bytes("white"),image_bytes("gray"),image_bytes("black")
        ])
        self.assertTrue(content.startswith(b"%PDF-"))
        self.assertGreater(len(content),3000)

    def test_invalid_or_excess_photos_rejected(self):
        with self.assertRaisesRegex(ValueError,"1 до 6"):
            photos_to_pdf([])
        with self.assertRaisesRegex(ValueError,"1 до 6"):
            photos_to_pdf([image_bytes("white")]*7)
        with self.assertRaisesRegex(ValueError,"прочитати"):
            photos_to_pdf([b"not-an-image"])


if __name__=="__main__":
    unittest.main()
