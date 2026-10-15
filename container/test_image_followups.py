import base64
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import assets
import followups as F
import image_answers as A
import interactions as I
import pipeline as P


def pixels(color):
    out=io.BytesIO(); Image.new('RGBA',(32,32),color).save(out,'PNG'); return out.getvalue()


def block(data):
    return {'type':'image','source':{'type':'base64','media_type':'image/png','data':base64.b64encode(data).decode()}}


def reply(action):
    return 200,json.dumps({'stop_reason':'end_turn','content':[{'type':'text','text':json.dumps({'action':action})}]}).encode()


class ImageIntentTests(unittest.TestCase):
    def test_implicit_edit_uses_current_request_and_image_context(self):
        request='теперь сделай так чтобы он сидел на туристическом стуле и кушал бешбармак'
        job={'text':request,'response_format':'video','followup':{'history':[{'format':'image','description':'A cat in a blue chapan.'}]}}
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'test-only'}),patch.object(P,'_http',return_value=reply('edit_image')) as model:
            A.resolve_request(job)
        self.assertEqual((job['response_format'],job['image_edit'],job['context_messages']),('image',True,0))
        sent=json.loads(model.call_args.args[1])
        context=json.loads(sent['messages'][0]['content'])
        self.assertEqual(context['request'],request)
        self.assertEqual(context['previous_answer']['description'],'A cat in a blue chapan.')
        self.assertNotIn('tools',sent)

    def test_questions_keep_the_explanation_format(self):
        job={'text':'почему вода на фоне не зелёная?','response_format':'video','followup':{'history':[{'format':'image'}]}}
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'test-only'}),patch.object(P,'_http',return_value=reply('keep_format')):
            A.resolve_request(job)
        self.assertEqual(job['response_format'],'video')
        self.assertNotIn('image_edit',job)

    def test_explicit_text_video_and_non_image_context_skip_classifier(self):
        for text,history in [('объясни текстом почему цвет другой',[{'format':'image'}]),
                             ('сделай видео о фотографии',[{'format':'image'}]),
                             ('теперь покажи пример',[{'format':'text'}])]:
            with self.subTest(text=text),patch.object(P,'_http') as model:
                A.resolve_request({'text':text,'followup':{'history':history}})
                model.assert_not_called()

    def test_classifier_failure_does_not_fall_back_to_video(self):
        job={'text':'переодень его в красное','followup':{'history':[{'format':'image'}]}}
        with patch.dict(os.environ,{'ANTHROPIC_API_KEY':'test-only'}),patch.object(P,'_http',return_value=reply('unsupported')):
            with self.assertRaises(P.UserError): A.resolve_request(job)
        self.assertNotIn('image_intent_checked',job)

    def test_missing_reference_cannot_generate_a_different_character(self):
        with tempfile.TemporaryDirectory() as d,patch('followups.prepare',return_value=([],[],[])),patch.object(assets,'request_codex') as codex:
            with self.assertRaises(P.UserError): A.make_image({'text':'пусть сядет','image_edit':True},d)
        codex.assert_not_called()


class ImageMemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.old=I._state
        self.database=str(Path(self.tmp.name)/'bot.sqlite')
        I._state=I.State(self.database)
        self.red=pixels((255,0,0,128)); self.blue=pixels((0,0,255,255))

    def tearDown(self):
        I._state.db.close(); I._state=self.old; self.tmp.cleanup()

    def saved(self):
        job={'response_channel_id':'789','response_message_id':'400',
             'prepared_inputs':([block(self.blue)],['Original technical source'],[])}
        F.record_image(job,'The generated red cat',self.red)
        F.remember(job)
        I._state.db.close(); I._state=I.State(self.database)
        return I.state().get_explanation('789','400')

    def test_delivered_pixels_survive_reopen_and_replace_the_original_input(self):
        saved=self.saved()
        source=saved['source']
        self.assertTrue(source['answer_image'])
        self.assertEqual(base64.b64decode(source['native_images'][0]),self.red)
        with Image.open(io.BytesIO(base64.b64decode(source['images'][0]['source']['data']))) as image:
            self.assertEqual(image.getpixel((0,0)),(255,0,0,128))
        self.assertEqual(source['texts'],['Original technical source'])

    def test_last_output_is_primary_and_new_attachments_are_secondary(self):
        saved=self.saved()
        def collect(fresh):
            fresh['image_originals']=[self.blue]
            fresh['image_meta']=[{'caption':'new style reference'}]
            return [block(self.blue)],[],[]
        job={'text':'измени фон','response_format':'image','followup':saved}
        images,_,_=F.prepare(job,collect)
        self.assertEqual(job['image_originals'],[self.red,self.blue])
        self.assertEqual(images[0],saved['source']['images'][0])

    def test_old_description_only_memory_recovers_the_replied_to_attachment(self):
        seen=[]
        saved={'source':{'images':[block(self.blue)]},'history':[{'format':'image','description':'Old output'}]}
        job={'text':'пусть сядет','response_format':'image','followup':saved,
             'reply_image':{'id':'400','attachments':[{'filename':'image.png'}],
                            'message_reference':{'message_id':'123'}}}
        def collect(fresh):
            seen.append(fresh)
            fresh['image_originals']=[self.red]
            return [block(self.red)],[],[]
        images,_,_=F.prepare(job,collect)
        self.assertEqual(seen[0]['kind'],'message')
        self.assertEqual(seen[0]['message']['id'],'400')
        self.assertIsNone(seen[0]['message']['message_reference'])
        self.assertEqual(images,[block(self.red)])

    def test_edit_worker_receives_actual_last_output_and_edit_instruction(self):
        job={'text':'пусть сядет','response_format':'image','image_edit':True,'followup':self.saved()}
        with tempfile.TemporaryDirectory() as d,patch.object(P,'collect',return_value=([],[],[])), \
             patch.object(assets,'request_codex',return_value=(self.red,{'description':'Edited cat'})) as codex:
            A.make_image(job,d)
            reference=Path(codex.call_args.args[2][0]['path']).read_bytes()
            with Image.open(io.BytesIO(reference)) as image:
                self.assertEqual(image.getpixel((0,0)),(255,0,0,128))
        self.assertEqual(codex.call_args.kwargs['options'],{'edit':True})
