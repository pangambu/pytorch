# Owner(s): ["oncall: jit"]

import io
import os
import sys
import torch
from torch.testing import FileCheck

# Make the helper files in test/ importable
pytorch_test_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
sys.path.append(pytorch_test_dir)
from torch.testing._internal.jit_utils import JitTestCase

if __name__ == '__main__':
    raise RuntimeError("This test file is not meant to be run directly, use:\n\n"
                       "\tpython test/test_jit.py TESTNAME\n\n"
                       "instead.")

class TestUpgraders(JitTestCase):
    def test_populated_upgrader_graph(self):
        @torch.jit.script
        def f():
            return 0

        buffer = io.BytesIO()
        torch.jit.save(f, buffer)
        buffer.seek(0)
        torch.jit.load(buffer)
        upgraders_size = torch._C._get_upgraders_map_size()
        upgraders_dump = torch._C._dump_upgraders_map()
        # make sure we only populate the upgrader map only once
        # so we load it again and make sure the upgrader map has
        # same content
        buffer.seek(0)
        torch.jit.load(buffer)
        upgraders_size_second_time = torch._C._get_upgraders_map_size()
        upgraders_dump_second_time = torch._C._dump_upgraders_map()
        self.assertTrue(upgraders_size == upgraders_size_second_time)
        self.assertTrue(upgraders_dump == upgraders_dump_second_time)

    def test_populated_test_upgrader_graph(self):
        @torch.jit.script
        def f():
            return 0

        buffer = io.BytesIO()
        torch.jit.save(f, buffer)
        buffer.seek(0)
        torch.jit.load(buffer)

        # upgrader map should have populated now
        upgraders_size = torch._C._get_upgraders_map_size()

        test_map = {"a": "b", "c": "d"}
        torch._C._test_only_populate_upgraders(test_map)
        upgraders_size_after_test = torch._C._get_upgraders_map_size()
        self.assertEqual(upgraders_size_after_test - upgraders_size, 2)
        upgraders_dump = torch._C._dump_upgraders_map()
        self.assertTrue("a" in upgraders_dump)
        self.assertTrue("c" in upgraders_dump)

        torch._C._test_only_remove_upgraders(test_map)
        upgraders_size_after_remove_test = torch._C._get_upgraders_map_size()
        self.assertTrue(upgraders_size_after_remove_test == upgraders_size)
        upgraders_dump_after_remove_test = torch._C._dump_upgraders_map()
        self.assertTrue("a" not in upgraders_dump_after_remove_test)
        self.assertTrue("c" not in upgraders_dump_after_remove_test)

    def test_aten_div_tensor_at_3(self):
        model_path = pytorch_test_dir + "/cpp/jit/div_at_version_3.pt"
        loaded_model = torch.jit.load(model_path)
        FileCheck().check("prim::If").run(loaded_model.graph)
        FileCheck().check_count("aten::div", 2).run(loaded_model.graph)

        buffer = io.BytesIO()
        torch.jit.save(loaded_model, buffer)
        buffer.seek(0)
        loaded_model_twice = torch.jit.load(buffer)
        # we check by its' code because graph variable names
        # can be different every time
        self.assertEqual(loaded_model.code, loaded_model_twice.code)

    # def test_aten_test_serialization(self):
    #     model_path = pytorch_test_dir + "/jit/fixtures/_test_serialization_subcmul_v2.pt"
    #     loaded_model = torch.jit.load(model_path)
    #     FileCheck().check_count("aten::mul", 2).run(loaded_model.graph)
    #     FileCheck().check_count("aten::sub", 2).run(loaded_model.graph)

    #     buffer = io.BytesIO()
    #     torch.jit.save(loaded_model, buffer)
    #     buffer.seek(0)
    #     loaded_model_twice = torch.jit.load(buffer)
    #     # we check by its' code because graph variable names
    #     # can be different every time
    #     self.assertEqual(loaded_model.code, loaded_model_twice.code)

    def test_aten_div_scalar_at_3(self):
        model_path = pytorch_test_dir + "/jit/fixtures/test_versioned_div_scalar_float_v3.pt"
        loaded_model = torch.jit.load(model_path)
        FileCheck().check("prim::If").run(loaded_model.graph)
        FileCheck().check_count("aten::div", 2).run(loaded_model.graph)

        buffer = io.BytesIO()
        torch.jit.save(loaded_model, buffer)
        buffer.seek(0)
        loaded_model_twice = torch.jit.load(buffer)

        self.assertEqual(loaded_model(torch.Tensor([5.0, 3.0]), 2.0),
                         loaded_model_twice(torch.Tensor([5.0, 3.0]), 2.0))

    def test_aten_div_tensor_out_at_3(self):
        model_path = pytorch_test_dir + "/jit/fixtures/test_versioned_div_tensor_out_v3.pt"
        loaded_model = torch.jit.load(model_path)
        FileCheck().check("prim::If").run(loaded_model.graph)
        FileCheck().check_count("aten::div", 2).run(loaded_model.graph)

        buffer = io.BytesIO()
        torch.jit.save(loaded_model, buffer)
        buffer.seek(0)
        loaded_model_twice = torch.jit.load(buffer)
        # we check by its' code because graph variable names
        # can be different every time
        self.assertEqual(loaded_model.code, loaded_model_twice.code)

    def test_aten_full_at_4(self):
        model_path = pytorch_test_dir + "/jit/fixtures/test_versioned_full_integer_value_v4.pt"
        loaded_model = torch.jit.load(model_path)
        FileCheck().check_count("aten::Float", 1).run(loaded_model.graph)
        FileCheck().check_count("aten::full", 2).run(loaded_model.graph)

        buffer = io.BytesIO()
        torch.jit.save(loaded_model, buffer)
        buffer.seek(0)
        loaded_model_twice = torch.jit.load(buffer)
        # we check by its' code because graph variable names
        # can be different every time
        self.assertEqual(loaded_model.code, loaded_model_twice.code)

    def test_aten_full_out_at_4(self):
        model_path = pytorch_test_dir + "/jit/fixtures/test_versioned_full_preserved_v4.pt"
        loaded_model = torch.jit.load(model_path)
        FileCheck().check_count("aten::full", 5).run(loaded_model.graph)
