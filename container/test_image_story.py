"""Portrait image walkthrough geometry, source fidelity and routing; no paid API calls."""
import base64
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image, ImageDraw

import assets as A
import image_story as V
import inputs
import interactions as I
import followups
import pipeline as P


def image_bytes(size=(1800,1000)):
    im=Image.new('RGB',size,(235,232,220)); d=ImageDraw.Draw(im)
    d.rectangle((int(size[0]*.65),int(size[1]*.2),int(size[0]*.9),int(size[1]*.75)),fill=(80,170,220))
    buffer=io.BytesIO();im.save(buffer,'PNG');return buffer.getvalue()


def scene(**extra):
    return {'asset_id':'image_1','heading':'Подробный разбор','narration':'Разбираем отмеченную область изображения.',
            'focus':[.6,.2,.95,.8], 'callouts':[{'box':[.68,.35,.86,.55],'label':'Важная деталь'}], **extra}


class PortraitTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.path=Path(self.tmp.name)/'source.png'; self.path.write_bytes(image_bytes())
        self.asset={'id':'image_1','path':str(self.path),'width':1800,'height':1000,'credit':'Source','kind':'user'}

    def tearDown(self):
        self.tmp.cleanup()

    def test_camera_preserves_proportions_and_contains_the_highlight(self):
        s=scene(); view=V.layout(s,(1800,1000))['camera']
        sx=(V.STAGE[2]-V.STAGE[0])/(view[2]-view[0])
        sy=(V.STAGE[3]-V.STAGE[1])/(view[3]-view[1])
        self.assertAlmostEqual(sx,sy)
        target=V.project(s['callouts'][0]['box'],view,(1800,1000))
        self.assertGreaterEqual(target[0],0);self.assertLessEqual(target[2],720)
        self.assertGreaterEqual(target[1],V.STAGE[1]);self.assertLessEqual(target[3],V.STAGE[3])

    def test_two_callouts_avoid_targets_each_other_and_subtitles(self):
        s=scene(callouts=[{'box':[.64,.32,.75,.46],'label':'Первая деталь'}, {'box':[.78,.6,.88,.72],'label':'Вторая деталь'}],
                protect=[[.65,.23,.9,.28]])
        with Image.open(self.path) as im:
            geometry=V.layout(s,im.size,im)
        self.assertEqual(len(geometry['callouts']),2)
        for label in geometry['callouts']:
            self.assertGreaterEqual(label['font_size'],26)
            self.assertLessEqual(len(label['lines']),2)
            self.assertGreaterEqual(label['box'][1],108)
            self.assertLessEqual(label['box'][3],V.STAGE[3])
            self.assertTrue(all(V.intersection(label['box'],target)==0 for target in geometry['protected']))
        self.assertEqual(V.intersection(*(x['box'] for x in geometry['callouts'])),0)

    def test_real_frames_are_portrait_and_camera_moves_from_overview(self):
        renderer=V.FrameRenderer(self.asset,scene(),audio_len=5)
        before,after=renderer.frame(.2,5.8),renderer.frame(3,5.8)
        self.assertEqual(before.size,(720,1280));self.assertEqual(after.size,(720,1280))
        self.assertNotEqual(before.tobytes(),after.tobytes())
        self.assertNotEqual(before.getpixel((400,200)),(0,0,0))

    def test_small_details_get_a_second_close_up_after_the_context(self):
        s=scene(focus=[.5,.25,.95,.65],callouts=[{'box':[.7,.4,.76,.47],'label':'Важная деталь'}])
        renderer=V.FrameRenderer(self.asset,s,audio_len=10)
        self.assertTrue(renderer.has_detail)
        context,_=renderer.camera_at(2,10.8)
        detail,reveal=renderer.camera_at(9.5,10.8)
        self.assertLess(detail[2]-detail[0],(context[2]-context[0])*.8)
        self.assertLess(reveal,9.5)
        self.assertTrue(all(V.intersection(c['box'],b)==0 for c in renderer.geometry['callouts'] for b in renderer.geometry['protected']))

    def test_bad_regions_unknown_images_or_too_many_labels_are_rejected(self):
        for bad in [scene(focus=[0,0,float('nan'),1]),scene(asset_id='../other'),
                    scene(callouts=[{'box':[.3,.3,.4,.4],'label':'One'}]*3),scene(narration='x'*321)]:
            with self.subTest(bad=bad),self.assertRaises(ValueError):
                V.validate_plan({'scenes':[bad,scene()]},[self.asset])
        self.assertIsNone(V.validate_plan({'use_image_story':False},[self.asset]))

    def test_native_source_pixels_are_used_instead_of_the_smaller_vision_block(self):
        native=image_bytes((3200,1800))
        block=inputs.image_block(native,'image/png')
        with tempfile.TemporaryDirectory() as directory:
            prepared=V.source_assets({'image_originals':[native]},[block],directory)
            self.assertEqual((prepared[0]['width'],prepared[0]['height']),(3200,1800))
            with Image.open(io.BytesIO(base64.b64decode(block['source']['data']))) as small:
                self.assertLess(small.width,prepared[0]['width'])

    def test_collector_keeps_originals_aligned_after_deduplication(self):
        native=image_bytes((3200,1800))
        job={'kind':'query','text':'Explain the image','message':{'attachments':[
            {'url':'https://example.com/a.png','content_type':'image/png'},
            {'url':'https://example.com/b.png','content_type':'image/png'}]}}
        with patch.object(inputs,'fetch',return_value=(native,'image/png')):
            images,_,_=inputs.collect(job)
        self.assertEqual(len(images),1)
        self.assertEqual(job['image_originals'],[native])
        self.assertEqual(len(job['image_meta']),1)

    def test_followup_can_restore_native_pixels_without_inflating_the_model_image(self):
        native=image_bytes((3200,1800)); block=inputs.image_block(native,'image/png')
        job={'kind':'query','text':'Explain','response_channel_id':'7','response_message_id':'8',
             'prepared_inputs':([block],['Source text'],[]),'image_originals':[native]}
        cache=I.State(str(Path(self.tmp.name)/'cache.sqlite'))
        try:
            with patch.object(I,'_state',cache):
                followups.record_script(job,{'title':'Original','scenes':[{'narration':'Spoken'}]})
                followups.remember(job)
                saved=cache.get_explanation('7','8')
                child={'kind':'query','text':'Explain the detail','followup':saved}
                images,_,_=followups.prepare(child,lambda fresh:([],[],[]))
            self.assertEqual(child['image_originals'],[native])
            self.assertEqual(images,[block])
        finally:
            cache.db.close()

    def test_image_walkthrough_visual_plan_skips_decorative_assets(self):
        block=inputs.image_block(image_bytes(),'image/png'); job={}
        with tempfile.TemporaryDirectory() as directory, patch.object(A,'plan_visuals',return_value={
                'presentation':'image_walkthrough','search_queries':['irrelevant'],'image_prompt':'do not generate'}), \
                patch.object(A,'find_web_images') as web,patch.object(A,'request_codex') as generated:
            ready,_=A.prepare_visuals(job,[block],[],directory)
        self.assertTrue(job['image_walkthrough']);self.assertTrue(ready)
        web.assert_not_called();generated.assert_not_called()

    def test_malformed_review_is_retried_without_changing_the_narration(self):
        plan=V.validate_plan({'title':'Test','scenes':[scene(),scene()]},[self.asset])
        with patch.object(A,'_ask_claude',side_effect=[P.PlanError('malformed JSON'),{'ok':True}]) as model:
            revised=V.review(plan,[self.asset],[])
        self.assertEqual(revised,plan)
        self.assertEqual(model.call_count,2)

    def test_invalid_review_geometry_does_not_partially_mutate_the_original_plan(self):
        plan=V.validate_plan({'title':'Test','scenes':[scene(),scene()]},[self.asset])
        original=json.loads(json.dumps(plan))
        bad={'ok':False,'fixes':[{'index':0,'focus':[0,0,2,1]}]}
        with patch.object(A,'_ask_claude',side_effect=[bad,{'ok':True}]):
            self.assertEqual(V.review(plan,[self.asset],[]),original)
        self.assertEqual(plan,original)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg unavailable')
    def test_encoded_video_has_real_9_by_16_dimensions_and_audio(self):
        out=Path(self.tmp.name)/'test.mp4'
        with patch.object(V,'FPS',6):
            V.encode_scene(self.asset,scene(),None,(None,1),out)
        info=json.loads(subprocess.check_output(['ffprobe','-v','error','-show_streams','-of','json',str(out)],text=True))
        video=next(s for s in info['streams'] if s['codec_type']=='video')
        self.assertEqual((video['width'],video['height']),(720,1280))
        self.assertTrue(any(s['codec_type']=='audio' for s in info['streams']))


class RoutingTests(unittest.TestCase):
    def job(self,**extra):
        return {'kind':'query','text':'Explain this screenshot','application_id':'1','token':'test',**extra}

    def test_image_explanation_uses_portrait_renderer_and_saves_video_delivery(self):
        with patch.dict(os.environ,{'VISUAL_ASSETS':'0','WEB_RESEARCH':'0','IMAGE_WALKTHROUGH':'1'}), \
                patch.object(P,'collect',return_value=([{'type':'image'}],[],[])), \
                patch.object(V,'make_video',return_value=('/tmp/portrait.mp4','Title')) as portrait, \
                patch.object(P,'discord_video') as upload,patch('fullgen.make_video_fullgen') as fullgen:
            job=self.job();P.handle_job(job)
        portrait.assert_called_once();upload.assert_called_once_with(job,'/tmp/portrait.mp4','Title');fullgen.assert_not_called()

    def test_explicit_text_and_3d_modes_do_not_enter_the_image_walkthrough(self):
        for mode in ('text','3d'):
            job=self.job(response_format='text' if mode=='text' else 'video',render_mode=mode)
            with self.subTest(mode=mode),patch.dict(os.environ,{'VISUAL_ASSETS':'0','WEB_RESEARCH':'0'}), \
                    patch.object(P,'collect',return_value=([{'type':'image'}],[],[])),patch.object(V,'make_video') as portrait, \
                    patch('text_answers.answer',return_value='Text'),patch('text_answers.deliver'), \
                    patch('three_d.make_video_3d',return_value=('/tmp/three.mp4','3D')),patch.object(P,'discord_video'),patch.object(P,'_safe_text'):
                P.handle_job(job)
            portrait.assert_not_called()

    def test_new_animation_request_can_decline_image_walkthrough(self):
        with patch.dict(os.environ,{'VISUAL_ASSETS':'0','WEB_RESEARCH':'0','IMAGE_WALKTHROUGH':'1','FULL_GEN':'1'}), \
                patch.object(P,'collect',return_value=([{'type':'image'}],[],[])),patch.object(V,'make_video',return_value=None), \
                patch('fullgen.make_video_fullgen',return_value=('/tmp/animation.mp4','Animation')) as animation,patch.object(P,'discord_video'):
            P.handle_job(self.job())
        animation.assert_called_once()


if __name__=='__main__':
    unittest.main()
