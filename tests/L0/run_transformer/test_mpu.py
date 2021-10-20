from typing import Tuple
import os
import subprocess
import sys
import unittest


DENY_TEST = [
    "megatron_gpt_pipeline",
]
MULTIGPU_TEST = [
    "pipeline_parallel_test",
    "p2p_comm_test",
]


def get_launch_option(test_filename) -> Tuple[bool, str]:
    should_skip = False
    for multigpu_test in MULTIGPU_TEST:
        if multigpu_test in test_filename:
            import torch
            num_available_devices = torch.cuda.device_count()
            if num_available_devices < 2:
                should_skip = True
            num_devices = min(num_available_devices, 2)
            distributed_run_options = f"-m torch.distributed.launch --nproc_per_node={num_devices}"
            return should_skip, distributed_run_options
    return should_skip, ""


def run_transformer_tests():
    python_executable_path = sys.executable
    # repository_root = os.path.join(os.path.dirname(__file__), "../../../")
    # directory = os.path.abspath(os.path.join(repository_root, "tests/mpu"))
    directory = os.path.dirname(__file__)
    files = [
        os.path.join(directory, f) for f in os.listdir(directory)
        if f.startswith("run_") and os.path.isfile(os.path.join(directory, f))
    ]
    print("#######################################################")
    print(f"# Python executable path: {python_executable_path}")
    print(f"# {len(files)} tests: {files}")
    print("#######################################################")
    errors = []
    for i, test_file in enumerate(files, 1):
        is_denied = False
        for deny_file in DENY_TEST:
            if deny_file in test_file:
                is_denied = True
        if is_denied:
            print(f"### {i} / {len(files)}: {test_file} skipped")
            continue
        should_skip, launch_option = get_launch_option(test_file)
        if should_skip:
            print(f"### {i} / {len(files)}: {test_file} skipped. Requires multiple GPUs.")
            continue
        test_run_cmd = (
            f"NVIDIA_TF32_OVERRIDE=0  {python_executable_path} {launch_option} {test_file} "
            "--micro-batch-size 2 --num-layers 1 --hidden-size 256 --num-attention-heads 8 --max-position-embeddings "
            "32 --encoder-seq-length 32 --use-cpu-initialization"
        )
        print(f"### {i} / {len(files)}: cmd: {test_run_cmd}")
        try:
            output = subprocess.check_output(
                test_run_cmd, shell=True
            ).decode(sys.stdout.encoding).strip()
        except Exception as e:
            errors.append((test_file, str(e)))
        else:
            if '>> passed the test :-)' not in output:
                errors.append((test_file, output))
    else:
        if not errors:
            print("### PASSED")
        else:
            print("### FAILED")
            short_msg = f"{len(errors)} out of {len(files)} tests failed"
            print(short_msg)
            for (filename, log) in errors:
                print(f"File: {filename}\nLog: {log}")
            raise RuntimeError(short_msg)


class TestTransformer(unittest.TestCase):

    def test_transformer(self):
        run_transformer_tests()


if __name__ == '__main__':
    unittest.main()
