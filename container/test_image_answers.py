import base64
import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import assets
import image_answers as A
import pipeline as P
import clarifications as C
import followups as F
from render_modes import response_format


def png():
    out=io.BytesIO();Image.new('RGBA',(32,32),(255,0,0,0)).save(out,'PNG');return out.getvalue()


class ImageAnswerTests(unittest.TestCase):
    def test_explicit_image_requests_choose_the_image_format_and_skip_picker(self):
        for request in ('сгенерируй картинку','сгенирируй картинку','нарисуй кота','создай изображение магнитного трека',
                        'Сделай мне иллюстрацию','generate an image of a cat','Draw a dragon'):
            self.assertEqual(response_format(request),'image',request)
            self.assertIsNone(C.reason(request,True),request)
        for request in ('Не сгенерируй картинку, объясни текстом','Что значит «сгенерируй картинку»?',
                        'Объясни как работает генерация картинок','Generate an image. Actually, explain in text'):
            self.assertNotEqual(response_format(request),'image',request)
        self.assertEqual(response_format('сгенерируй картинку. Хотя нет, сделай видео'),'video')

    def test_pipeline_delivers_image_without_research_voice_or_animation(self):
        job={'kind':'query','text':'сгенерируй картинку кота'}
        with patch.object(A,'make_image',return_value=(png(),'Cat')) as make,patch.object(A,'deliver') as deliver, \
             patch.object(P,'_safe_text'),patch.object(P,'voice_all') as voice,patch('research.web_research') as research, \
             patch('fullgen.make_video_fullgen') as video:
            P.handle_job(job)
        self.assertEqual(job['response_format'],'image')
        make.assert_called_once();deliver.assert_called_once()
        for unused in (voice,research,video):unused.assert_not_called()

    def test_reply_uses_saved_answer_as_source_and_sends_reference_pixels_to_codex(self):
        block={'type':'image','source':{'type':'base64','media_type':'image/png','data':base64.b64encode(png()).decode()}}
        source={'images':[block],'texts':['Track end cap covers the connector.'],'image_meta':[]}
        job={'kind':'query','text':'сгенерируй картинку','response_format':'image','context_messages':10,
             'followup':{'source':source,'history':[{'format':'text','text':'Remove the end cap before inserting the connector.'}]}}
        with tempfile.TemporaryDirectory() as d,patch.object(P,'collect',return_value=([],[],[])), \
             patch.object(assets,'request_codex',return_value=(png(),{'description':'Track diagram'})) as codex:
            data,description=A.make_image(job,d)
            self.assertTrue(Path(codex.call_args.args[2][0]['path']).is_file())
        self.assertEqual(data,png());self.assertEqual(description,'Track diagram')
        self.assertEqual(job['context_messages'],0)
        self.assertEqual(codex.call_args.kwargs['mode'],'image')
        self.assertIn('Remove the end cap',codex.call_args.kwargs['source_context'])
        self.assertIn('ONE standalone image',codex.call_args.kwargs['source_context'])
        self.assertNotIn('NEW narrated video',codex.call_args.kwargs['source_context'])

    def test_missing_subject_or_disabled_provider_does_not_generate_an_arbitrary_image(self):
        for enabled,request in [('1','сгенерируй картинку'),('1','сгенирируй картинку'),('0','сгенерируй картинку кота')]:
            with tempfile.TemporaryDirectory() as d,patch.dict(os.environ,{'CODEX_IMAGES':enabled}), \
                 patch('followups.prepare',return_value=([],[],[])),patch.object(assets,'request_codex') as codex:
                with self.assertRaises(P.UserError):A.make_image({'kind':'query','text':request},d)
            codex.assert_not_called()

    def test_no_image_or_invalid_output_is_not_reported_as_success(self):
        for data in (None,b'not an image'):
            with tempfile.TemporaryDirectory() as d,patch('followups.prepare',return_value=([],[],[])), \
                 patch.object(assets,'request_codex',return_value=(data,{'description':'SECRET provider response'})):
                with self.assertRaises(P.UserError) as error:A.make_image({'kind':'query','text':'нарисуй кота'},d)
            self.assertNotIn('SECRET',str(error.exception))

    def test_png_delivery_preserves_alpha_and_uses_image_attachment(self):
        with Image.open(io.BytesIO(A.output_png(png(),1_000_000))) as image:
            self.assertEqual(image.getpixel((0,0))[3],0)
        job={'response_channel_id':'789','response_message_id':'400'}
        with patch.dict(os.environ,{'DISCORD_BOT_TOKEN':'test-only'}),patch.object(P,'_http',return_value=(200,b'{}')) as send,patch('followups.remember'):
            A.deliver(job,png(),'A transparent reference')
        self.assertEqual(send.call_args.kwargs['method'],'PATCH')
        self.assertTrue(send.call_args.args[0].endswith('/channels/789/messages/400'))
        self.assertIn(b'filename="image.png"',send.call_args.args[1])
        self.assertIn(b'Content-Type: image/png',send.call_args.args[1])
        self.assertNotIn(b'video/mp4',send.call_args.args[1])
        self.assertEqual(job['image_answer_record']['format'],'image')


if __name__=='__main__':unittest.main()
