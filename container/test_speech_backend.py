import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch
import urllib.error

import speech_backend as B
import speech as S
import pipeline as P


class SpeechBackendTests(unittest.TestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        env=patch.dict(os.environ,{'DATA_DIR':self.directory.name,'LOCAL_SPEECH_SOCKET':'/private/speech.sock',
                                  'LOCAL_SPEECH_TOKEN':'test-only','STT_PROVIDER':'auto','TTS_PROVIDER':'auto'})
        env.start();self.addCleanup(env.stop)
        state=patch.object(B,'_blocked_until',0);state.start();self.addCleanup(state.stop)

    def test_quota_failure_switches_to_local_and_blocks_both_billed_services_across_restart(self):
        remote=Mock(side_effect=S.SpeechError('quota_exceeded'));local=Mock(return_value='local')
        self.assertEqual(B.run('stt',remote,local),'local')
        B._blocked_until=0
        tts=Mock(return_value='billed')
        self.assertEqual(B.run('tts',tts,local),'local');tts.assert_not_called()
        self.assertGreater(json.loads((Path(self.directory.name)/'speech-provider.json').read_text())['until'],0)
        self.assertEqual(local.call_count,2)

    def test_transient_provider_outage_falls_back_without_a_daylong_block(self):
        remote=Mock(side_effect=urllib.error.URLError('not logged'))
        self.assertEqual(B.run('stt',remote,lambda:'local'),'local')
        self.assertFalse(B.blocked())

    def test_explicit_local_never_calls_elevenlabs_and_success_does_not_set_provider_error(self):
        reply={'text':'Привет','words':[],'provider':'local-whisper'}
        with patch.dict(os.environ,{'STT_PROVIDER':'local'}),patch.object(S,'_request_eleven') as billed,patch.object(B,'transcribe',return_value=reply):
            self.assertEqual(S.request_transcript(b'audio'),reply)
        billed.assert_not_called()

    def test_local_offline_error_is_safe_and_not_reported_as_an_eleven_quota_failure(self):
        with patch.dict(os.environ,{'STT_PROVIDER':'local'}),patch.object(B,'transcribe',side_effect=B.LocalSpeechError('local_speech_offline')):
            with self.assertRaisesRegex(S.SpeechError,'^local_speech_offline$'):S.request_transcript(b'audio')

    def test_disabled_fallback_preserves_provider_error_and_does_not_call_local(self):
        local=Mock()
        with patch.dict(os.environ,{'STT_PROVIDER':'elevenlabs'}):
            with self.assertRaisesRegex(S.SpeechError,'quota_exceeded'):
                B.run('stt',Mock(side_effect=S.SpeechError('quota_exceeded')),local)
        local.assert_not_called()

    def test_bad_response_cannot_be_used_as_a_transcript(self):
        with patch.object(B,'request',return_value=b'{"text":123}'):
            with self.assertRaisesRegex(B.LocalSpeechError,'invalid_response'):B.transcribe(b'audio')

    def test_local_tts_converts_wav_to_existing_mp3_contract_and_removes_temp_file(self):
        path=Path(self.directory.name)/'narration.mp3'
        with patch.dict(os.environ,{'TTS_PROVIDER':'local'}),patch.object(B,'synthesize',return_value=b'WAVE'), \
             patch.object(P,'_tts_eleven') as billed,patch.object(P.subprocess,'run') as convert,patch.object(P,'probe_duration',return_value=3.5):
            self.assertEqual(P.tts('Привет',None,None,path),(path,3.5))
        billed.assert_not_called();self.assertIn('libmp3lame',convert.call_args.args[0])
        self.assertFalse(Path(str(path)+'.local.wav').exists())


if __name__=='__main__':unittest.main()
