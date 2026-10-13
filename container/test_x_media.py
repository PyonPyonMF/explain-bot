"""Audio-only X HLS, 120-minute speech limits, explicit visual requests and provider quota failures."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.error

from PIL import Image
import x_media as X
import youtube as Y
import speech as S
import inputs
import links
import pipeline as P
import followups

URL = "https://x.com/example/status/100"
MASTER = "https://video.twimg.com/amplify_video/123/pl/master.m3u8"
ITEM = {"id":"123","duration":60,"video_url":"https://video.twimg.com/amplify_video/123/vid/640x360/video.mp4",
        "formats":[{"url":MASTER,"container":"m3u8"}]}
TRANSCRIPT = {"status":"ok","text":"[00:01–00:03] part1/speaker_0: Spoken source.","partial":False}


def ref(mid="100", requested=False):
    return {"url":ITEM["video_url"],"type":"video/mp4","x_video":{**ITEM,"id":mid},
            "source_url":f"https://x.com/example/status/{mid}","requested":requested,"caption":"Video from X"}


def page(frames=()):
    return {"duration":60,"transcript":dict(TRANSCRIPT),"frames":list(frames),"coverage":{"sample_count":len(frames)},"frame_error":None}


class XMediaTests(unittest.TestCase):
    def test_audio_rendition_is_selected_instead_of_the_video_variant(self):
        text = '#EXTM3U\n#EXT-X-MEDIA:TYPE=SUBTITLES,URI="subs.m3u8"\n' \
               '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-64000",URI="a64.m3u8"\n' \
               '#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-128000",URI="a128.m3u8"\n' \
               '#EXT-X-STREAM-INF:BANDWIDTH=500000\nvideo.m3u8\n'
        self.assertTrue(X.audio_playlist(text,MASTER).endswith("/a128.m3u8"))
        self.assertIsNone(X.audio_playlist("#EXTM3U\nvideo.m3u8",MASTER))

    def test_cdn_redirects_and_playlist_uris_cannot_target_other_hosts(self):
        for url in ("http://video.twimg.com/a","https://video.twimg.com.evil.example/a","https://localhost/a", "file:///tmp/key",
                    "https://user:secret@video.twimg.com/a","https://video.twimg.com:8080/a"):
            with self.assertRaises(X.XMediaError): X.cdn_url(url)
        playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:3,\nhttps://127.0.0.1/secret\n#EXT-X-ENDLIST'
        with self.assertRaises(X.XMediaError): X.parse_audio_playlist(playlist,MASTER,7200)

    def test_live_and_unsupported_encrypted_playlists_fail_closed(self):
        base = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:3,\na.m4s\n'
        with self.assertRaises(X.XMediaError): X.parse_audio_playlist(base,MASTER,7200)
        with self.assertRaises(X.XMediaError): X.parse_audio_playlist(base+'#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXT-X-ENDLIST',MASTER,7200)
        init, segments, seconds = X.parse_audio_playlist(base+'#EXT-X-ENDLIST',MASTER,7200)
        self.assertEqual(seconds,3)
        self.assertEqual(len(segments),1)
        self.assertTrue(init.endswith('/init.mp4'))

    def test_hls_download_rewrites_to_local_audio_before_ffmpeg(self):
        master = '#EXTM3U\n#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio-128000",URI="audio.m3u8"'
        playlist = '#EXTM3U\n#EXT-X-MAP:URI="init.mp4"\n#EXTINF:3,\na.m4s\n#EXT-X-ENDLIST'
        def fetch(url,deadline):
            if url==MASTER: return master.encode()
            if url.endswith('audio.m3u8'): return playlist.encode()
            return b'fragment'
        def ffmpeg(args,**kwargs):
            self.assertNotIn('https://',' '.join(args))
            Path(args[-1]).write_bytes(b'audio')
        with tempfile.TemporaryDirectory() as d, patch.object(X,'_get',side_effect=fetch), patch.object(X.subprocess,'run',side_effect=ffmpeg):
            output=X._audio_hls(MASTER,Path(d),7200,1e12)
            self.assertEqual(output.read_bytes(),b'audio')

    def test_read_is_audio_only_by_default_and_does_not_decode_frames(self):
        def audio(url,directory,*args):
            path=directory/'audio.m4a'; path.write_bytes(b'audio'); return path
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'YOUTUBE_CACHE_DIR':d,'SPEECH_RECOGNITION':'1'}), \
             patch.object(X,'_audio_hls',side_effect=audio),patch.object(X,'_probe',return_value=(60,True)), \
             patch.object(Y,'_transcribe',return_value=TRANSCRIPT),patch.object(X,'_video') as video,patch.object(Y,'_frames') as frames:
            result=X.read(ITEM,URL,{'text':'summarize'})
        self.assertEqual(result['frames'],[])
        video.assert_not_called(); frames.assert_not_called()

    def test_default_collection_skips_x_video_posters_and_preserves_the_post_text(self):
        linked={'kind':'x_post','text':'Author post context','images':[ref(),{'url':'https://pbs.twimg.com/preview.jpg','x_video_preview':True}]}
        job={'kind':'query','text':'summarize '+URL,'message':{'embeds':[{'url':URL,'thumbnail':{'url':'https://pbs.twimg.com/preview.jpg'}}]}}
        with patch.object(links,'read_link',return_value=linked),patch.object(X,'read',return_value=page()) as read,patch.object(inputs,'fetch') as fetch:
            images,texts,_=inputs.collect(job)
        self.assertTrue(job['primary_x_video'])
        self.assertEqual(images,[])
        self.assertFalse(read.call_args.kwargs['want_frames'])
        self.assertIn('Spoken source','\n'.join(texts)); self.assertIn('Author post context','\n'.join(texts))
        fetch.assert_not_called()

    def test_requested_post_video_gets_budget_before_earlier_thread_videos(self):
        linked={'kind':'x_thread','text':'Thread','images':[ref('100'),ref('200'),ref('300',True)]}
        job={'kind':'query','text':'summarize https://x.com/example/status/300'}
        with patch.object(links,'read_link',return_value=linked),patch.object(X,'read',return_value=page()) as read:
            inputs.collect(job)
        self.assertEqual([c.args[0]['id'] for c in read.call_args_list],['300','100'])
        self.assertIn('speech_limit',job['source_warnings'])

    def test_explicit_frames_include_timestamps_and_preserve_repeated_frames(self):
        buf=io.BytesIO();Image.new('RGB',(100,100),'red').save(buf,'JPEG')
        frames=[{'data':buf.getvalue(),'seconds':1},{'data':buf.getvalue(),'seconds':2}]
        job={'kind':'query','text':'разбери покадрово 00:01–00:03 '+URL}
        with patch.object(links,'read_link',return_value={'kind':'x_post','text':'Post','images':[ref()]}), \
             patch.object(X,'read',return_value=page(frames)) as read:
            images,texts,_=inputs.collect(job)
        self.assertTrue(read.call_args.kwargs['want_frames'])
        self.assertEqual(len(images),2);self.assertEqual(job['video_frame_count'],2)
        self.assertIn('video_visual_samples','\n'.join(texts))

    def test_x_photos_and_reaction_gifs_remain_separate_from_video_audio(self):
        post=links._fx_post({'author':{'screen_name':'example'},'media':{'all':[
            {'type':'gif','url':'https://video.twimg.com/gif.mp4','thumbnail_url':'https://pbs.twimg.com/gif.jpg'},
            {'type':'photo','url':'https://pbs.twimg.com/photo.jpg'}]}})
        self.assertFalse(any(r.get('x_video') for r in links._x_images(post,URL)))

    def test_120_minute_limits_and_long_range_are_accepted(self):
        with patch.dict(os.environ,{'SPEECH_MAX_SECONDS':'7200','YOUTUBE_MAX_SECONDS':'7200'}):
            self.assertEqual(S.max_seconds(),7200);self.assertEqual(Y.limits()['seconds'],7200)
            times,_=Y.frame_times('покадрово 119:50–120:00',7200)
            self.assertTrue(all(7190<t<7200 for t in times))
            with self.assertRaises(X.XMediaError):X.read({**ITEM,'duration':7202},URL,{})

    def test_long_uploaded_audio_uses_chunked_recognition_instead_of_a_300_second_cut(self):
        probe={'streams':[{'codec_type':'audio','duration':'7199','start_time':'0'}],'format':{'duration':'7199','start_time':'0'}}
        result={**TRANSCRIPT,'seconds':7199,'duration':7199}
        with patch.dict(os.environ,{'SPEECH_MAX_SECONDS':'7200'}),patch.object(S.subprocess,'check_output',return_value=json.dumps(probe).encode()), \
             patch.object(Y,'_transcribe',return_value=result) as transcribe:
            output=S.transcribe(b'compressed audio')
        self.assertEqual(transcribe.call_args.args[1],7199)
        self.assertFalse(output['partial'])

    def test_quota_error_is_safe_and_visible_instead_of_an_opaque_401(self):
        raw=json.dumps({'detail':{'status':'quota_exceeded','message':'SECRET API-key value'}}).encode()
        error=urllib.error.HTTPError('https://api.elevenlabs.io',401,'unauthorized',{},io.BytesIO(raw))
        with patch.dict(os.environ,{'ELEVENLABS_API_KEY':'secret'}),patch.object(S.urllib.request,'urlopen',side_effect=error):
            with self.assertRaisesRegex(S.SpeechError,'quota_exceeded') as caught:S.request_transcript(b'audio')
        self.assertNotIn('SECRET',str(caught.exception))
        self.assertIn('кредиты',Y.error_message('quota_exceeded'))

    def test_known_speech_quota_failure_does_not_spend_a_video_generation_call(self):
        job={'kind':'query','text':'объясни видео','speech_provider_error':'quota_exceeded'}
        with patch('followups.prepare',return_value=([],[],[])),patch.object(P,'_safe_text') as reply,patch('fullgen.make_video_fullgen') as render:
            P.handle_job(job)
        self.assertIn('кредиты',reply.call_args.args[1]);render.assert_not_called()

    def test_x_followup_can_request_frames_from_the_original_post(self):
        source={'primary_x':True,'primary_x_video':True,'x_video_sources':[URL],
                'texts':["<media_transcript status='ok'><transcript_text>Speech</transcript_text></media_transcript>"], 'images':[]}
        job={'kind':'query','text':'разбери покадрово 00:10–00:20','followup':{'source':source}}
        def collect(fresh):
            self.assertIn(URL,fresh['text']);fresh.update(primary_x_video=True,youtube_frame_analysis=True)
            return [],[],[]
        followups.prepare(job,collect)
        self.assertTrue(job['primary_x_video'])


if __name__=='__main__':unittest.main()
