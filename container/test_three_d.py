import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

from render_modes import mention_mode, selected_mode
import three_d


class ModeTests(unittest.TestCase):
    def test_only_explicit_prefix_enables_3d(self):
        for text in ("3d объясни маятник", "[3D] объясни маятник", "--3d объясни маятник", "3д: объясни маятник"):
            self.assertEqual(mention_mode(text), ("объясни маятник", "3d"))
        for text in ("объясни 3d модели", "3d-печать", "обычный вопрос"):
            self.assertEqual(mention_mode(text), (text, "2d"))
        self.assertEqual(selected_mode(None), "2d")
        self.assertEqual(selected_mode("3d"), "3d")


class LessonTests(unittest.TestCase):
    def test_long_narration_is_rejected_instead_of_silently_cut(self):
        plan={"title":"test","scenes":[{"narration":"x"*400},{"narration":"x"*400}]}
        with self.assertRaisesRegex(ValueError, "duration budget"):
            three_d.extract_lesson("<plan>"+json.dumps(plan)+"</plan>")

    def test_short_lesson_preserves_scene_count(self):
        plan=three_d.extract_lesson('<plan>{"title":"Маятник","scenes":[{"heading":"Причина","narration":"Сила тяготения.","board":["Сила направлена вниз"]},{"heading":"Движение","narration":"Маятник качается."}]}</plan>')
        self.assertEqual(len(plan["scenes"]),2)
        self.assertEqual(plan["scenes"][1]["board"],[])

    def test_avatar_credit_from_vrm_metadata(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{"MODELS_3D_DIR":directory,"VRM_AVATAR_PATH":""}):
            data=json.dumps({"extensions":{"VRMC_vrm":{"meta":{"name":"Teacher","authors":["Creator"],"creditNotation":"required"}}}}).encode()
            path=Path(directory)/"teacher.vrm"
            path.write_bytes(struct.pack('<4sII',b'glTF',2,20+len(data))+struct.pack('<II',len(data),0x4E4F534A)+data)
            found,credit=three_d.avatar_info()
            self.assertEqual(found,str(path.resolve()))
            self.assertIn("Creator",credit)
            self.assertIn("Teacher",credit)

    def test_missing_avatar_keeps_classroom_available(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{"MODELS_3D_DIR":directory,"VRM_AVATAR_PATH":""}):
            self.assertEqual(three_d.avatar_info(),(None,""))

    def test_catalog_cannot_reference_files_outside_models_directory(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ,{"MODELS_3D_DIR":directory}):
            root=Path(directory)
            (root/"model.glb").write_bytes(b'test')
            (root/"catalog.json").write_text(json.dumps([{"id":"good","file":"model.glb"},{"id":"bad","file":"../other.glb"}]))
            self.assertEqual([m['id'] for m in three_d.models_catalog()],["good"])


if __name__ == '__main__':
    unittest.main()
