import json
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch

import object_library as L


def glb(path, height=1.0, external=False):
    binary = struct.pack('<9f3H', 0, 0, 0, 1, 0, 0, 0, 0, height, 0, 1, 2) + b'\0\0'
    data = {'asset':{'version':'2.0'}, 'buffers':[{'byteLength':len(binary)}],
            'bufferViews':[{'buffer':0,'byteOffset':0,'byteLength':36},{'buffer':0,'byteOffset':36,'byteLength':6}],
            'accessors':[{'bufferView':0,'componentType':5126,'count':3,'type':'VEC3','min':[0,0,0],'max':[1,0,height]},
                         {'bufferView':1,'componentType':5123,'count':3,'type':'SCALAR'}],
            'meshes':[{'primitives':[{'attributes':{'POSITION':0},'indices':1}]}],
            'nodes':[{'mesh':0,'name':'triangle','extras':{'library_part':'triangle'}}],
            'scenes':[{'nodes':[0]}], 'scene':0}
    if external:
        data['buffers'][0]['uri']='../../private.bin'
    raw=json.dumps(data).encode()
    raw += b' ' * (-len(raw) % 4)
    path.write_bytes(struct.pack('<4sII',b'glTF',2,12+8+len(raw)+8+len(binary)) +
                     struct.pack('<II',len(raw),0x4E4F534A)+raw+struct.pack('<II',len(binary),0x004E4942)+binary)


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.root=Path(self.tmp.name)
        self.env=patch.dict(os.environ,{'OBJECT_LIBRARY_DIR':str(self.root/'library'),
                           'OBJECT_LIBRARY_MAX_ASSETS':'500','OBJECT_LIBRARY_MAX_MB':'512'})
        self.env.start()
        self.model=self.root/'test.glb'
        glb(self.model)
        self.meta={'key':'pendulum','title':'Маятник','description':'A pendulum with a bob and a suspension.',
                   'tags':['маятник','pendulum','mechanics']}

    def tearDown(self):
        self.env.stop();self.tmp.cleanup()

    def test_publish_persists_parts_and_searches_russian_and_english(self):
        asset,created=L.publish(self.model,self.meta)
        self.assertTrue(created)
        self.assertTrue(Path(asset['path']).is_file())
        self.assertEqual(asset['parts'],['triangle'])
        self.assertEqual(L.search('маятника')[0]['id'],'pendulum')
        self.assertEqual(L.search('pendulums')[0]['id'],'pendulum')

    def test_identical_files_and_geometry_fingerprints_are_deduplicated(self):
        first,_=L.publish(self.model,{**self.meta,'fingerprint':'a'*64})
        same,created=L.publish(self.model,{**self.meta,'key':'another'})
        self.assertFalse(created);self.assertEqual(first['id'],same['id'])
        glb(self.model,height=2)
        same,created=L.publish(self.model,{**self.meta,'fingerprint':'a'*64})
        self.assertFalse(created);self.assertEqual(first['id'],same['id'])
        self.assertEqual(len(L.search()),1)

    def test_changed_geometry_gets_new_version_without_overwriting(self):
        first,_=L.publish(self.model,self.meta)
        previous=Path(first['path']).read_bytes()
        glb(self.model,height=2)
        second,created=L.publish(self.model,self.meta)
        self.assertTrue(created);self.assertNotEqual(first['id'],second['id'])
        self.assertEqual(Path(first['path']).read_bytes(),previous)

    def test_archive_is_reversible_and_automatic_saving_does_not_restore_it(self):
        asset,_=L.publish(self.model,self.meta)
        self.assertTrue(L.set_archived(asset['id']))
        self.assertEqual(L.search(),[])
        L.publish(self.model,self.meta)
        self.assertEqual(L.search(),[])
        self.assertTrue(Path(asset['path']).is_file())
        L.set_archived(asset['id'],False)
        self.assertEqual(len(L.search()),1)

    def test_usage_is_counted_once_per_video(self):
        asset,_=L.publish(self.model,self.meta)
        L.mark_used([asset['id'],asset['id']])
        self.assertEqual(L.search()[0]['uses'],1)

    def test_invalid_or_external_glb_is_rejected(self):
        glb(self.model,external=True)
        with self.assertRaisesRegex(ValueError,'embed'):
            L.publish(self.model,self.meta)
        self.model.write_bytes(b'not a model')
        with self.assertRaises(ValueError):
            L.publish(self.model,self.meta)

    def test_staging_path_escape_is_rejected(self):
        staging=self.root/'stage';staging.mkdir()
        with self.assertRaises(ValueError):
            L.publish_candidates({'candidates':[{'file':'../test.glb',**self.meta}]},staging)

    def test_capacity_limit_preserves_existing_assets(self):
        L.publish(self.model,self.meta)
        glb(self.model,height=2)
        with patch.dict(os.environ,{'OBJECT_LIBRARY_MAX_ASSETS':'1'}):
            with self.assertRaisesRegex(ValueError,'limit'):
                L.publish(self.model,{**self.meta,'key':'second'})
        self.assertEqual(len(L.search()),1)


if __name__=='__main__':
    unittest.main()
