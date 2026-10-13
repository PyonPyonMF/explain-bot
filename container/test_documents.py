"""Regression: an attached PDF must not turn into a video about unrelated Discord history."""
import os
import shutil
import unittest
from unittest.mock import patch

import inputs
import pdf_inputs
import pipeline


def pdf_bytes(texts):
    """Small real, multi-page PDF fixture using the standard Helvetica font."""
    objects = [b"", b"", b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    pages = []
    for text in texts:
        page_id, stream_id = len(objects) + 1, len(objects) + 2
        pages.append(page_id)
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream = f"BT /F1 10 Tf 40 750 Td ({escaped}) Tj ET".encode()
        objects += [f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {stream_id} 0 R >>".encode(),
                    f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream"]
    objects[0] = b"<< /Type /Catalog /Pages 2 0 R >>"
    objects[1] = f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(f'{i} 0 R' for i in pages)}] >>".encode()
    data, offsets = bytearray(b"%PDF-1.4\n"), [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(data))
        data += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    xref = len(data)
    data += f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode()
    data += b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:])
    data += f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(data)


def attachment(name="article.pdf", **extra):
    return {"filename": name, "content_type": "application/pdf", "url": "https://example.com/article.pdf", "size": 2000, **extra}


def job(file=None):
    return {"kind": "query", "text": "Explain the attached article", "channel_id": "9", "context_messages": 10,
            "message": {"id": "30", "content": "", "attachments": [file or attachment()]}}


class DocumentSelectionTests(unittest.TestCase):
    def test_pdf_is_the_subject_and_ambient_chat_is_not_fetched(self):
        request = job()
        extracted = {"text": "A paper about neurons and cognition", "pages": 12, "previews": []}
        with patch.object(inputs, "_history") as history, patch.object(inputs, "fetch", return_value=(b"%PDF-", "application/pdf")), \
                patch.object(pdf_inputs, "read_pdf", return_value=extracted):
            images, texts, _ = inputs.collect(request)
        history.assert_not_called()
        self.assertIn("neurons and cognition", "\n".join(texts))
        self.assertIn('pages="12"', "\n".join(texts))
        self.assertTrue(request["primary_document"])
        _, prompt = pipeline.build_user_content(request, images, texts)
        self.assertTrue(prompt.startswith(pipeline.DOCUMENT_RULE))

    def test_own_pdf_wins_over_quoted_unrelated_post(self):
        request = {**job(), "kind": "message", "question": "Read the article",
                   "request_message": job()["message"],
                   "message": {"id": "10", "content": "Unrelated layoffs https://example.com/unrelated"}}
        with patch.object(inputs, "_history") as history, patch.object(inputs, "_reply_chain") as parents, \
                patch.object(inputs, "fetch", return_value=(b"%PDF-", "application/pdf")), \
                patch.object(pdf_inputs, "read_pdf", return_value={"text": "Actual article", "pages": 1, "previews": []}), \
                patch("links.read_link") as link:
            _, texts, _ = inputs.collect(request)
        history.assert_not_called()
        parents.assert_not_called()
        link.assert_not_called()
        self.assertNotIn("Unrelated", "\n".join(texts))

    def test_unreadable_download_unsupported_and_oversized_files_stop_collection(self):
        cases = [(attachment(), OSError("download failed"), "download"),
                 (attachment("archive.zip", content_type="application/zip"), None, "unsupported"),
                 (attachment(size=inputs.MAX_DOWNLOAD + 1), None, "too_large")]
        for file, error, reason in cases:
            with self.subTest(reason=reason), patch.object(inputs, "fetch", side_effect=error), \
                    patch.object(inputs, "_history") as history:
                with self.assertRaises(inputs.AttachmentUnavailable) as caught:
                    inputs.collect(job(file))
            self.assertEqual(caught.exception.reason, reason)
            history.assert_not_called()

    def test_unreadable_pdf_never_reaches_research_or_any_video_renderer(self):
        for mode in ("2d", "3d"):
            for enabled in ("0", "1"):
                with self.subTest(mode=mode, enabled=enabled), \
                        patch.dict(os.environ, {"VISUAL_ASSETS": enabled, "WEB_RESEARCH": enabled}), \
                        patch.object(inputs, "fetch", return_value=(b"%PDF-broken", "application/pdf")), \
                        patch.object(pdf_inputs, "read_pdf", side_effect=pdf_inputs.PDFUnreadable("invalid_pdf")), \
                        patch.object(pipeline, "prepare_sources") as research, \
                        patch("fullgen.make_video_fullgen") as fullgen, patch("three_d.make_video_3d") as three_d, \
                        patch.object(pipeline, "make_video") as templates, patch.object(pipeline, "discord_video") as upload, \
                        patch.object(pipeline, "_safe_text") as reply:
                    pipeline.handle_job({**job(), "render_mode": mode})
                for call in (research, fullgen, three_d, templates, upload):
                    call.assert_not_called()
                reply.assert_called_once()
                self.assertIn("article.pdf", reply.call_args.args[1])
                self.assertIn("PDF", reply.call_args.args[1])


@unittest.skipUnless(all(shutil.which(c) for c in ("pdfinfo", "pdftotext", "pdftoppm")), "Poppler not installed")
class PDFReaderTests(unittest.TestCase):
    def test_real_multipage_pdf_includes_last_page_and_previews(self):
        pdf = pdf_bytes(["First page: the study measures memory in neurons. " * 2,
                         "Second page: the methods compare two groups. " * 2,
                         "Final page: the experiment has limitations. " * 2])
        result = pdf_inputs.read_pdf(pdf)
        self.assertEqual(result["pages"], 3)
        self.assertIn("[PDF page 3/3]", result["text"])
        self.assertIn("experiment has limitations", result["text"])
        self.assertEqual([p for p, _ in result["previews"]], [1, 2, 3])
        self.assertTrue(all(data.startswith(b"\xff\xd8") for _, data in result["previews"]))

    def test_broken_blank_and_excessive_pdfs_are_rejected(self):
        for data, reason in [(b"not a pdf", "invalid_pdf"), (b"%PDF-broken", "invalid_pdf"),
                             (pdf_bytes([""]), "no_pdf_text"), (pdf_bytes(["Page"] * 61), "pdf_limit")]:
            with self.subTest(reason=reason), self.assertRaises(pdf_inputs.PDFUnreadable) as caught:
                pdf_inputs.read_pdf(data)
            self.assertEqual(caught.exception.reason, reason)

    def test_parsers_do_not_inherit_bot_credentials(self):
        import subprocess
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": "must-not-leak", "DISCORD_BOT_TOKEN": "must-not-leak"}), \
                patch.object(pdf_inputs.subprocess, "run", wraps=subprocess.run) as run:
            pdf_inputs.read_pdf(pdf_bytes(["A scientific article explaining the experiment and its limitations. " * 2]))
        for call in run.call_args_list:
            self.assertNotIn("ANTHROPIC_API_KEY", call.kwargs["env"])
            self.assertNotIn("DISCORD_BOT_TOKEN", call.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
