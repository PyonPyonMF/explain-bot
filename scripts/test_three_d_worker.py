import io
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from three_d_windows_worker import extract_package


class PackageTests(unittest.TestCase):
    def check_archive(self, files):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory)
            path=work/'input.zip'
            with zipfile.ZipFile(path,'w') as z:
                for name,value in files.items():
                    z.writestr(name,value)
            return extract_package(path,work)

    def test_accepts_packed_scenes(self):
        manifest={"fps":12,"scenes":[{"index":0,"blend":"scene_0.blend","frame_count":24}]}
        self.assertEqual(self.check_archive({'manifest.json':json.dumps(manifest),'scene_0.blend':b'BLENDER'}),manifest)

    def test_rejects_path_escape_and_executable_code(self):
        for name in ('../oops.blend','C:/oops.blend','lesson.py','scene_0.blend/child'):
            with self.subTest(name=name),self.assertRaises(ValueError):
                self.check_archive({name:b'bad'})

    def test_manifest_cannot_reference_external_file(self):
        manifest={"fps":12,"scenes":[{"index":0,"blend":"../private.blend","frame_count":24}]}
        with self.assertRaises(ValueError):
            self.check_archive({'manifest.json':json.dumps(manifest),'scene_0.blend':b'BLENDER'})


if __name__=='__main__':
    unittest.main()
