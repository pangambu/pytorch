import argparse
import csv
import functools
import gc
import io
import itertools
import logging
import math
import numpy as np
import os
import re
import sys
import time
import torch
from torch import nn
from torch.jit import fuser, optimized_execution
from os.path import abspath
from scipy.stats import ttest_ind

# from caffe2.python import workspace
# workspace.GlobalInit(['caffe2', '--caffe2_log_level=-5'])

import lazy_tensor_core
import lazy_tensor_core.core.lazy_model as ltm
import lazy_tensor_core.debug.metrics as metrics
lazy_tensor_core._LAZYC._ltc_init_ts_backend()

os.environ["KALDI_ROOT"] = "/tmp"  # avoids some spam

log = logging.getLogger(__name__)

# Models that are known to crash or otherwise not work with lazy tensor are
# disabled, but should be removed from these lists once fixed
SKIP = {
    "fastNLP_Bert",
    "vision_maskrcnn",
    "speech_trasformer",
    "nvidia_deeprecommender",
    "pytorch_struct",
    "dlrm",
    "LearningToPaint",
    "vision_maskrcnn",
    "drq",
    "moco",
}
SKIP_TRAIN_ONLY = {
    "squeezenet1_1",
    "mobilenet_v2_quantized_qat"
    "hf_Reformer",
    "hf_GPT2",
    "hf_BigBird",
    "pyhpc_equation_of_state",
    "pyhpc_isoneutral_mixing",
}

current_name = ""
current_device = ""

@functools.lru_cache
def output_csv(name, headers):
    output = csv.writer(
        io.TextIOWrapper(
            open(name, "wb", buffering=0),
            "utf-8",
            write_through=True,
        )
    )
    output.writerow(headers)
    return output

class HardSwishBenchmark:
    def __init__(self, dims):
        self.name = "HardSwish[" + ','.join([str(d) for d in dims]) + ']'
        self.dims = dims

    def __call__(self, device, jit):
        return HardSwish(self.dims, device, jit)

class HardSwish(nn.Module):
    def __init__(self, dims, device='cuda', jit=False):
        super(HardSwish, self).__init__()
        self.name = "HardSwish[" + ','.join([str(d) for d in dims]) + ']'
        self.example_inputs = (
            torch.randn(*dims, device=device, dtype=torch.float32),
        )

    def get_module(self):
        return self, self.example_inputs

    def name(self):
        return self.name

    def forward(self, x):
        return x * torch.clamp(x + 3.0, 0.0, 6.0) / 6.0

class DivAddMulBenchmark:
    """This wrapper helps interface with the same iterator as torchbench models
    """
    def __init__(self, dims):
        self.name = "DivAddMul[" + ','.join([str(d) for d in dims]) + ']'
        self.dims = dims

    def __call__(self, device, jit):
        return DivAddMul(self.dims, device, jit)

class DivAddMul(nn.Module):
    def __init__(self, dims, device='cuda', jit=False):
        super(DivAddMul, self).__init__()
        self.attention_head_size = dims[1]
        self.name = "DivAddMul[" + ','.join([str(d) for d in dims]) + ']'
        self.example_inputs = (
            torch.randn(*dims, device=device, dtype=torch.float32),
            torch.randn(*dims, device=device, dtype=torch.float32),
        )

    def get_module(self):
        return self, self.example_inputs

    def name(self):
        return self.name

    def forward(self, inputs, mask):
        out1 = inputs / math.sqrt(self.attention_head_size)
        out2 = out1 + mask
        out3 = out2 * 5.0
        return out3
toy_models = [
    HardSwishBenchmark,
    DivAddMulBenchmark,
]
toy_dims = [
    [1, 1, 1, 1],
    [32, 16, 128, 128],
    [128, 16, 128, 128],
    [256, 16, 128, 128],
]
for dims in toy_dims:
    # The toy benchmarks don't support training..
    # and it's too late to add it inside the generator func below...
    SKIP_TRAIN_ONLY.add("DivAddMul[" + ','.join([str(d) for d in dims]) + ']')
    SKIP_TRAIN_ONLY.add("HardSwish[" + ','.join([str(d) for d in dims]) + ']')
def list_toy_models():
    for dims in toy_dims:
        for model in toy_models:
            yield model(dims=dims)

def pick_grad(args, name):
    if args.test == 'train':
        return torch.enable_grad()

    if name in ("maml",):
        return torch.enable_grad()
    else:
        return torch.no_grad()

def short_name(name, limit=20):
    """Truncate a model name to limit chars"""
    return name if len(name) <= limit else f"{name[:limit - 3].rstrip('_')}..."

# Iter torchbench models and toy models
def iter_models(args):
    from fastNLP.core import logger

    logger.setLevel(logging.WARNING)
    from torchbenchmark import list_models
    for benchmark_cls in itertools.chain(list_toy_models(), list_models()):
        name = benchmark_cls.name if hasattr(benchmark_cls, 'name') else benchmark_cls.name()
        if (
            (len(args.filter) and (not re.search("|".join(args.filter), name, re.I)))
            or (len(args.exclude) and re.search("|".join(args.exclude), name, re.I))
            or name in SKIP
            or (name in SKIP_TRAIN_ONLY and args.test == "train")
        ):
            continue
        # TODO(whc) better to support list of devices;
        # curently since env var needs to be set for GPU, have to launch one dev at a time
        for device in [args.device]:
            try:
                torch.manual_seed(1337)
                benchmark = benchmark_cls(device=device, jit=False)
                torch.manual_seed(1337)
                lazy_benchmark = benchmark_cls(device='lazy', jit=False)
                gc.collect()
                global current_name, current_device
                current_device = device
                current_name = short_name(benchmark.name)
                yield device, current_name, benchmark, lazy_benchmark
            except NotImplementedError:
                print("NotImplementedError")
                pass
            except Exception as e:
                print(f"Exception in iter_models for {name}", e)
                log.exception(f"misconfigured model {name}")

def call_model_with(model, inputs):
    if isinstance(inputs, tuple) or isinstance(inputs, list):
        return model(*inputs)
    elif isinstance(inputs, dict):
        return model(**inputs)
    elif isistance(inputs, torch.Tensor):
        return model(inputs)
    raise RuntimeError("invalid example inputs ", inputs)

class CudaSync:
    def __init__(self, sync_every_iter=False):
        self.sync_every_iter = sync_every_iter

    def iter_sync(self, results):
        if self.sync_every_iter:
            torch.cuda.synchronize()

    def final_sync(self, results):
        torch.cuda.synchronize()

class NoOpSync:
    def __init__(self, sync_every_iter=False):
        pass

    def iter_sync(self, results):
        pass

    def final_sync(self, results):
        pass

class LazySync:
    def __init__(self, sync_every_iter=False, skip_final_sync=False):
        self.sync_every_iter = sync_every_iter
        self.skip_final_sync = skip_final_sync

    def iter_sync(self, results):
        ltm.mark_step()
        if self.sync_every_iter:
            ltm.wait_device_ops()
            if current_device == 'cuda':
                torch.cuda.synchronize()

    def final_sync(self, results):
        ltm.mark_step()
        if self.skip_final_sync:
            return
        ltm.wait_device_ops()
        if current_device == 'cuda':
            torch.cuda.synchronize()

class ToDeviceSync:
    def __init__(self, device, sync_every_iter=False):
        self.sync_every_iter = sync_every_iter
        self.device = device

    def iter_sync(self, results):
        if self.sync_every_iter:
            to_device(results[-1], self.device)
            if current_device == 'cuda':
                torch.cuda.synchronize()

    def final_sync(self, results):
        if len(results):
            if self.sync_every_iter:
                to_device(results[-1], self.device)
            else:
                to_device(results, self.device)

        if current_device == 'cuda':
            torch.cuda.synchronize()

def dump_lazy_metrics(reset=False):
    met = {name: int(metrics.counter_value(name)) for name in metrics.counter_names() if int(metrics.counter_value(name) > 0)}
    if reset:
        metrics.reset_metrics()
    return met

def timed(args, benchmark, sync, times=1):
    results = []
    sync.final_sync(results)
    torch.manual_seed(1337)
    if args.test == 'eval':
        model, example_inputs = benchmark.get_module()

    if current_device == 'lazy':
        torch.cuda.set_sync_debug_mode(2)
    else:
        torch.cuda.set_sync_debug_mode(0)

    # keep the lazy tensor results alive until the final sync
    t0 = time.perf_counter()
    for i in range(times):
        if args.test == 'eval':
            results.append(call_model_with(model, example_inputs))
        elif args.test == 'train':
            benchmark.train(niter=1)

        # for the last i, let final_sync take care of it
        if i < times - 1:
            # may be just an async 'mark_step' for lazy, or no-op for cuda
            sync.iter_sync(results)

    torch.cuda.set_sync_debug_mode(0)

    # should be a hard sync for lazy and cuda
    # unless strictly measuring lazy trace overhead, then no-op
    sync.final_sync(results)
    t1 = time.perf_counter()
    rc = results[-1] if args.test == 'eval' else None
    return rc, t1 - t0

def to_device(tensors, device):
    """Handles moving tensor or tensors (in various containers) to a new device.
        Used for various purposes (either correctness checking, or even as an impromptu
        means of synchronization.) Note: this method doesn't apply a cuda sync, do that outside.
    """
    try:
        import transformers
        if isinstance(tensors, transformers.modeling_outputs.MaskedLMOutput) \
        or isinstance(tensors, transformers.modeling_outputs.Seq2SeqLMOutput):
            # huggingface transformers return classes as model output with many attributes
            # we don't want to sync (such as hidden states of every layer) - just sync the logits
            tensors = tensors.logits
    except ImportError:
        pass

    if isinstance(tensors, tuple) or isinstance(tensors, list):
        return tuple(to_device(i, device) for i in tensors)
    elif isinstance(tensors, dict):
        return {k: to_device(tensors[k], device) for k in tensors}
    elif isinstance(tensors, torch.Tensor):
        return tensors.to(device)
    raise RuntimeError("invalid example tensors ", tensors)

def lazy_overhead_experiment(args, results, benchmark, lazy_benchmark):
    timings = np.zeros((args.repeat, 2), np.float64)
    ref_sync = CudaSync if current_device == 'cuda' else NoOpSync
    for rep in range(args.warmup):
        # interleave the runs to handle frequency scaling and load changes
        timed(args, benchmark, sync=ref_sync(sync_every_iter=True))
        timed(args, lazy_benchmark, sync=LazySync(sync_every_iter=True))
    for rep in range(args.repeat):
        # interleave the runs to handle frequency scaling and load changes
        _, timings[rep, 0] = timed(args, benchmark, sync=ref_sync(sync_every_iter=True))
        _, timings[rep, 1] = timed(args, lazy_benchmark, sync=LazySync(skip_final_sync=True))
        ltm.wait_device_ops()
        if current_device == 'cuda':
            torch.cuda.synchronize()

    pvalue = ttest_ind(timings[:, 0], timings[:, 1]).pvalue
    median = np.median(timings, axis=0)
    overhead = median[1] / median[0]
    results.append(overhead)
    output_csv(
        os.path.join(args.output_dir, f"lazy_overheads_{args.test}.csv"),
        ("dev", "name", "overhead", "pvalue"),
    ).writerow([current_device, current_name, f"{overhead:.4f}", f"{pvalue:.4e}"])
    print(f"{short_name(name, limit=30):<30} {current_device:<4} {args.test:<5} {'trace overheads':<20} overhead: {overhead:.3f} pvalue: {pvalue:.2e}")
    return (overhead, pvalue)

def lazy_compute_experiment(args, experiment, results, benchmark, lazy_benchmark, sync_every_iter=False, to_dev_sync=None):
    timings = np.zeros((args.repeat, 2), np.float64)
    if to_dev_sync is not None:
        ref_sync = ToDeviceSync(to_dev_sync, sync_every_iter=sync_every_iter)
        lazy_sync = ToDeviceSync(to_dev_sync, sync_every_iter=sync_every_iter)
    else:
        ref_sync = CudaSync(sync_every_iter=sync_every_iter) if current_device == 'cuda' else NoOpSync()
        lazy_sync = LazySync(sync_every_iter=sync_every_iter)

    # interleave the runs to handle frequency scaling and load changes
    for rep in range(args.warmup):
        # warmup
        timed(args, benchmark, sync=ref_sync)
        timed(args, lazy_benchmark, sync=lazy_sync)

    # fresh metrics for each timed run
    dump_lazy_metrics(reset=True)
    for rep in range(args.repeat):
        # measure
        _, timings[rep, 0] = timed(args, benchmark, times=args.inner_loop_repeat, sync=ref_sync)
        _, timings[rep, 1] = timed(args, lazy_benchmark, times=args.inner_loop_repeat, sync=lazy_sync)
    lazy_metrics = dump_lazy_metrics(reset=True)
    if 'CachedCompile' not in lazy_metrics or lazy_metrics['CachedCompile'] != args.repeat * args.inner_loop_repeat:
        print("WARNING: lazy cached compile count indicates fallbacks, or something else")
    fallbacks = {k: v for (k, v) in lazy_metrics.items() if 'aten::' in k}
    if len(fallbacks):
        print("WARNING: lazy-eager fallbacks detected for ["+ ",".join(fallbacks.keys()) + ']')
    if args.dump_lazy_counters:
        print(lazy_metrics)
    pvalue = ttest_ind(timings[:, 0], timings[:, 1]).pvalue
    median = np.median(timings, axis=0)
    speedup = median[0] / median[1]
    results.append(speedup)
    output_csv(
        os.path.join(args.output_dir, f"lazy_compute_{args.test}.csv"),
        ("name", "dev", "experiment", "speedup", "pvalue"),
    ).writerow([current_name, current_device, experiment, f"{speedup:.4f}", f"{pvalue:.2e}"])
    print(f"{short_name(current_name, limit=30):<30} {current_device:<4} {args.test:<5} {experiment:<20} speedup:  {speedup:.3f} pvalue: {pvalue:.2e}")
    return (speedup, pvalue)

def check_results(name, correct_result, lazy_result, device):
    correct_result = to_device(correct_result, device)
    lazy_result = to_device(lazy_result, device)
    return torch.allclose(correct_result, lazy_result)

def check_fuser(args):
    if args.fuser == 'noopt':
        return
    if args.fuser is None:
        args.fuser = 'fuser1' if args.device == 'cpu' else 'fuser2'
    if args.device == 'cpu':
        assert args.fuser in ['fuser0', 'fuser1']
    if args.device == 'cuda':
        assert args.fuser in ['fuser0', 'fuser2']
    if args.fuser == 'fuser1':
        assert torch._C._llvm_enabled(), "Can't use fuser1 (nnc) without building torch with llvm."

def run_tracing_execute_noops(test, lazy_benchmark):
    ltm.set_noop_execution_mode(True)
    if test == 'eval':
        model, example_inputs = lazy_benchmark.get_module()
    # doesn't actualyl collect a profile, but runs just the lazy trace
    # so you can use a profiler on top of the program.
    # note: depends on making the backend do a 'no-op' for executecomputation
    results = []
    for i in range(300):
        if test == 'eval':
            results.append(call_model_with(model, example_inputs))
        elif test == 'train':
            lazy_benchmark.train(niter=1)
        # we still do a mark step, to preserve the ratio of how often we split the graph
        # and run through the process of 'compile and execute' (even though these are now noops)
        ltm.mark_step()
    ltm.set_noop_execution_mode(False)

if __name__ == "__main__" :
    parser = argparse.ArgumentParser()
    parser.add_argument("--filter", "-k", action="append", default=[], help="filter benchmarks")
    parser.add_argument("--exclude", "-x", action="append", default=[], help="filter benchmarks")
    parser.add_argument("--device", "-d", default='cuda', help="cpu or cuda")
    parser.add_argument("--warmup", type=int, default=4, help="number of warmup runs")
    parser.add_argument("--repeat", "-n", type=int, default=6, help="number of timing runs (samples)")
    parser.add_argument("--inner_loop_repeat", type=int, default=10, help="repeat the computation this many times per sample")
    parser.add_argument("--fuser", type=str, choices=['noopt', 'fuser0', 'fuser1', 'fuser2'], help="0=legacy, 1=nnc, 2=nvfuser")
    parser.add_argument("--test", type=str, choices=['eval', 'train'], default='eval')
    parser.add_argument("--torchbench_dir", type=str, help="path to torchbenchmark repo")
    parser.add_argument("--output_dir", type=str, default=".", help="path to write output files")
    parser.add_argument("--dump_lazy_counters", action='store_true', help="dump lazy counter values after each timing run")
    parser.add_argument("--run_tracing_execute_noops", action='store_true', help="Run the tracing portion only, with noop backend, useful for running under a profiler.")
    args = parser.parse_args()
    results = []

    check_fuser(args)

    torchbench_dir = abspath(args.torchbench_dir) if args.torchbench_dir else abspath("../../benchmark")
    assert os.path.exists(os.path.join(torchbench_dir, "torchbenchmark")), "set --torchbench_dir to installed torchbench repo"
    sys.path.append(torchbench_dir)
    for device, name, benchmark, lazy_benchmark in iter_models(args):

        if device == 'cuda':
            assert 'LTC_TS_CUDA' in os.environ and bool(os.environ['LTC_TS_CUDA'])

        if args.run_tracing_execute_noops:
            run_tracing_execute_noops(args.test, lazy_benchmark)
            # when profiling, we really don't want to do anything else
            exit(0)

        with pick_grad(args, name):
            try:
                if args.test == 'eval':
                    # Correctness Check
                    torch.manual_seed(1337)
                    model, example_inputs = benchmark.get_module()
                    model.eval()
                    correct_result = call_model_with(model, example_inputs)
                    torch.manual_seed(1337)
                    lazy_model, lazy_inputs = lazy_benchmark.get_module()
                    lazy_model.eval()
                    lazy_result = call_model_with(lazy_model, lazy_inputs)
                    if not check_results(name, correct_result, lazy_result, device):
                        print(f"INCORRECT ({name})")
                        continue
            except Exception:
                logging.exception("unhandled error")
                print(f"ERROR ({name})")
                continue
            lazy_overhead_experiment(args, results, benchmark, lazy_benchmark)

            with fuser(args.fuser) if args.fuser != 'noopt' else optimized_execution(False):
                if args.fuser == 'noopt':
                    # TODO(whc) cleaner way to configure the fusers; seems i have to set both optimized_execution(False)
                    # _and_ disable fusers to get no-optimization
                    torch._C._jit_override_can_fuse_on_cpu(False)
                    torch._C._jit_override_can_fuse_on_gpu(False)
                    torch._C._jit_set_texpr_fuser_enabled(False)
                    torch._C._jit_set_nvfuser_enabled(False)

                # using LazySync
                lazy_compute_experiment(args, f"amortized {args.inner_loop_repeat}x", results, benchmark, lazy_benchmark)
                lazy_compute_experiment(args, "unamortized", results, benchmark, lazy_benchmark, sync_every_iter=True)

        print()
