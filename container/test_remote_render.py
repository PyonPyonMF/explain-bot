import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import remote_render as R


class RemoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"THREED_QUEUE_DIR":str(self.root), "THREED_REMOTE":"1",
                                           "THREED_WORKER_UID":str(os.getuid()), "THREED_WORKER_GID":str(os.getgid())})
        self.env.start()
        self.addCleanup(self.env.stop)

    def heartbeat(self, age=0, busy=None):
        (self.root / "worker.json").write_text(json.dumps({"time":time.time()-age, "busy":busy}))

    def test_offline_busy_disabled_or_stale_skips_packaging(self):
        def unexpected(*a):
            self.fail("offline worker must not prepare scenes")
        for age, busy in ((100,None),(0,"another-job")):
            self.heartbeat(age,busy)
            self.assertIsNone(R.render({},self.root,unexpected,lambda _:None))
        self.heartbeat()
        with patch.dict(os.environ, {"THREED_REMOTE":"0"}):
            self.assertIsNone(R.render({},self.root,unexpected,lambda _:None))

    def test_bake_failure_releases_reservation_and_falls_back(self):
        self.heartbeat()
        def fail(*a):
            raise RuntimeError("unsupported animated mesh")
        self.assertIsNone(R.render({"fps":8,"scenes":[]},self.root,fail,lambda _:None))
        self.assertEqual([p.name for p in self.root.iterdir()],["worker.json"])

    def test_disconnect_after_submission_cancels_queue(self):
        self.heartbeat()
        def baked(spec,*args):
            folder=Path(spec["out_dir"]);folder.mkdir()
            (folder/"scene_0.blend").write_bytes(b'BLENDER')
            self.heartbeat(age=100)
            return {"scenes":[{"index":0,"blend":"scene_0.blend","frame_count":12}]}
        self.assertIsNone(R.render({"fps":8,"scenes":[{"duration":1}]},self.root,baked,lambda _:None))
        self.assertFalse((self.root/"reserved").exists())
        self.assertFalse(any(len(p.name)==32 for p in self.root.iterdir()))

    def test_worker_failure_cleans_job_and_falls_back(self):
        self.heartbeat()
        def baked(spec,*args):
            folder=Path(spec["out_dir"]);folder.mkdir()
            (folder/"scene_0.blend").write_bytes(b'BLENDER')
            for job in self.root.iterdir():
                if len(job.name)==32:
                    (job/"done.json").write_text('{"error":"GPU stopped"}')
            return {"scenes":[{"index":0,"blend":"scene_0.blend","frame_count":12}]}
        self.assertIsNone(R.render({"fps":8,"scenes":[{"duration":1}]},self.root,baked,lambda _:None))
        self.assertFalse((self.root/"reserved").exists())

    def test_worker_restart_does_not_leave_claimed_job_waiting(self):
        self.heartbeat()
        def baked(spec,*args):
            folder=Path(spec["out_dir"]);folder.mkdir()
            (folder/"scene_0.blend").write_bytes(b'BLENDER')
            for job in self.root.iterdir():
                if len(job.name)==32:
                    claimed=job/'claimed';claimed.touch()
                    os.utime(claimed,(time.time()-15,time.time()-15))
            return {"scenes":[{"index":0,"blend":"scene_0.blend","frame_count":12}]}
        self.assertIsNone(R.render({"fps":8,"scenes":[{"duration":1}]},self.root,baked,lambda _:None))
        self.assertFalse((self.root/'reserved').exists())


if __name__ == "__main__":
    unittest.main()
