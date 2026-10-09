"""Real Blender round trip: separate object, named parts, hierarchy, reuse and cleanup."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from PIL import Image
import object_library as L
from three_d import run_blender


@unittest.skipUnless(shutil.which('blender') and shutil.which('xvfb-run'), 'requires Blender')
class BlenderLibraryTests(unittest.TestCase):
    def test_baked_animation_survives_loading_without_generated_python(self):
        with tempfile.TemporaryDirectory() as directory:
            work=Path(directory);work.chmod(0o755)
            board=work/'board.png';Image.new('RGB',(80,40),'darkgreen').save(board)
            code=work/'lesson.py'
            code.write_text('''from three_d_kit import *
def build():
    obj=box("moving",(0,0,1))
    def animate(t,T):
        obj.location.x=t
    return animate
SCENES=[build]
''')
            result=run_blender({'mode':'bake','code':str(code),'out_dir':str(work/'baked'),
                'scenes':[{'board':str(board),'duration':0.5}], 'fps':12,
                'engine':'BLENDER_EEVEE_NEXT'},work,60)
            self.assertEqual(result['scenes'][0]['frame_count'],6)
            check=work/'check.py'
            check.write_text('''import bpy
s=bpy.context.scene
s.frame_set(1)
a=bpy.data.objects['moving'].location.x
s.frame_set(6)
b=bpy.data.objects['moving'].location.x
assert abs(a)<0.0001 and abs(b-5/12)<0.0001,(a,b)
assert not list(bpy.data.texts)
print('BAKED_MOTION_OK')
''')
            loaded=subprocess.run(['blender','--background','--factory-startup','--disable-autoexec',
                str(work/'baked'/result['scenes'][0]['blend']),'--python',str(check)],
                capture_output=True,text=True,timeout=30)
            self.assertIn('BAKED_MOTION_OK',loaded.stdout,loaded.stdout+loaded.stderr)

    def test_export_import_animation_and_scene_cleanup(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'OBJECT_LIBRARY_DIR':directory+'/library'}):
            work=Path(directory);work.chmod(0o755)
            code=work/'new.py'
            code.write_text('''from three_d_kit import *
def build():
    with reusable("test_oscillator", title="Test oscillator", description="A bob suspended from a joint", tags=["oscillator"]):
        box("base", (0,0,0.85), (0.5,0.4,0.1))
        joint=pivot("joint", (0,0,1.7))
        bob=sphere("bob", (0,0,1.1), 0.12)
        attach(bob,joint)
    label("lesson only", (0,0,2))
    return None
SCENES=[build]
''')
            exported=run_blender({'mode':'library','code':str(code),'out_dir':str(work/'export'),
                                  'scenes':[{}],'fps':8,'models':[]},work,60)
            self.assertEqual(len(exported['candidates']),1)
            saved=L.publish_candidates(exported,work/'export')
            asset=L.search('oscillator')[0]
            self.assertTrue(saved[0]['created'])
            self.assertIn('bob',asset['parts'])
            self.assertIn('joint',asset['parts'])
            self.assertNotIn('Label',asset['parts'])
            self.assertTrue(Path(asset['preview']).is_file())
            again=run_blender({'mode':'library','code':str(code),'out_dir':str(work/'export-again'),
                               'scenes':[{}],'fps':8,'models':[]},work,60)
            self.assertEqual(exported['candidates'][0]['fingerprint'],again['candidates'][0]['fingerprint'])
            self.assertFalse(L.publish_candidates(again,work/'export-again')[0]['created'])
            board=work/'board.png';Image.new('RGB',(800,400),'darkgreen').save(board)
            reuse=work/'reuse.py'
            reuse.write_text('''from three_d_kit import *
def build():
    root=model("test_oscillator", location=(0.2,0,0.82), size=1.0)
    bpy.context.view_layer.update()
    joint=part(root,"joint")
    bob=part(root,"bob")
    assert bob.parent.get("library_part")=="joint"
    before=bob.matrix_world.translation.copy()
    joint.rotation_euler.y=0.5
    bpy.context.view_layer.update()
    assert (bob.matrix_world.translation-before).length>0.01
    hinge=pivot("lesson hinge",joint.matrix_world.translation)
    before=bob.matrix_world.copy()
    attach(bob,hinge)
    bpy.context.view_layer.update()
    assert (bob.matrix_world.translation-before.translation).length<0.00001
    return None
SCENES=[build,build]
''')
            result=run_blender({'mode':'test','code':str(reuse),'out_dir':str(work/'reuse'), 'models':[asset],
                                'scenes':[{'board':str(board),'duration':1}]*2, 'fps':8,
                                'engine':'BLENDER_WORKBENCH','width':320,'height':180},work,60)
            self.assertEqual(result['used_models'],['test_oscillator'])
            self.assertEqual(result['candidate_keys'],[])
            self.assertEqual(result['scenes'][0]['demo_objects'],result['scenes'][1]['demo_objects'])
            self.assertGreater(result['scenes'][0]['demo_objects'],3)


if __name__=='__main__':
    unittest.main()
