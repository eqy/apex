from itertools import product
import unittest

import torch
from torch.utils.data import Dataset
from torch.utils.data import RandomSampler
from torch.utils.data import BatchSampler
from torch.utils.data import DataLoader

from apex.transformer.pipeline_parallel.utils import _split_batch_into_microbatch as split_batch_into_microbatch


class MyIterableDataset(Dataset):
    def __init__(self, start, end):
        super().__init__()
        assert end > start, "this example code only works with end >= start"
        self.start = start
        self.end = end
        self.samples = list(range(self.start, self.end))

    def __iter__(self):
        return iter(range(self.start, self.end))

    def __getitem__(self, index):
        return self.samples[index]


class MegatronPretrainingRandomSampler:

    def __init__(self, total_samples, consumed_samples, micro_batch_size,
                 data_parallel_rank, data_parallel_size):
        # Keep a copy of input params for later use.
        self.total_samples = total_samples
        self.consumed_samples = consumed_samples
        self.micro_batch_size = micro_batch_size
        self.data_parallel_rank = data_parallel_rank
        self.data_parallel_size = data_parallel_size
        self.micro_batch_times_data_parallel_size = \
            self.micro_batch_size * data_parallel_size
        self.last_batch_size = \
            self.total_samples % self.micro_batch_times_data_parallel_size

        # Sanity checks.
        assert self.total_samples > 0, \
            'no sample to consume: {}'.format(self.total_samples)
        assert self.micro_batch_size > 0
        assert data_parallel_size > 0
        assert self.data_parallel_rank < data_parallel_size, \
            'data_parallel_rank should be smaller than data size: {}, ' \
            '{}'.format(self.data_parallel_rank, data_parallel_size)

    def __len__(self):
        return self.total_samples

    def __iter__(self):
        active_total_samples = self.total_samples - self.last_batch_size
        self.epoch = self.consumed_samples // active_total_samples
        current_epoch_samples = self.consumed_samples % active_total_samples
        assert current_epoch_samples % self.micro_batch_times_data_parallel_size == 0

        # data sharding and random sampling
        bucket_size = (self.total_samples // self.micro_batch_times_data_parallel_size) * self.micro_batch_size
        bucket_offset = current_epoch_samples // self.data_parallel_size
        start_idx = self.data_parallel_rank * bucket_size

        g = torch.Generator()
        g.manual_seed(self.epoch)
        random_idx = torch.randperm(bucket_size, generator=g).tolist()
        idx_range = [start_idx + x for x in random_idx[bucket_offset:]]

        batch = []
        # Last batch if not complete will be dropped.
        for idx in idx_range:
            batch.append(idx)
            if len(batch) == self.micro_batch_size:
                self.consumed_samples += self.micro_batch_times_data_parallel_size
                yield batch
                batch = []


# Samples 8 tensors in total.
# First sample 4 tensors twice, then sample 2 tensors fourth.
class TestBatchSamplerBehavior(unittest.TestCase):
    def test_batch_sampler_behavior(self):
        dataset = MyIterableDataset(0, 100)

        for num_workers in (1, 2, 4):
            with self.subTest(f"{num_workers}"):
                torch.manual_seed(42)
                loader = DataLoader(dataset, batch_sampler=MegatronPretrainingRandomSampler(100, 0, 4, 0, 1), num_workers=num_workers)
                samples = []
                for i, batch in enumerate(loader):
                    samples.append(batch)
                    if i == 2 - 1:
                        break

                torch.manual_seed(42)
                loader = DataLoader(dataset, batch_sampler=MegatronPretrainingRandomSampler(100, 0, 2, 0, 1), num_workers=num_workers)
                samples2 = []
                for i, batch in enumerate(loader):
                    samples2.append(batch)
                    if i == 4 - 1:
                        break
                torch.testing.assert_allclose(torch.cat(samples), torch.cat(samples2))

    def test_split_batch(self):

        class MyIterableDataset(Dataset):
            def __init__(self, start, end):
                super().__init__()
                assert end > start, "this example code only works with end >= start"
                self.start = start
                self.end = end
                self.samples = list(range(self.start, self.end))

            def __len__(self):
                return self.end - self.start

            def __iter__(self):
                return iter(range(self.start, self.end))

            def __getitem__(self, index):
                return (torch.tensor([index, index]), torch.tensor([index // 2, index // 2]))

        dataset = MyIterableDataset(0, 100)
        torch.manual_seed(42)
        global_batch_size = 16
        loader = DataLoader(dataset, batch_sampler=MegatronPretrainingRandomSampler(100, 0, global_batch_size, 0, 1), num_workers=2)
        batch = next(iter(loader))
        # samples = None
        # for i, batch in enumerate(loader):
        #     # samples = batch
        #     if i == 0:
        #         break

        for _micro_batch_size in (1, 2, 4, 8):
            microbatches = list(split_batch_into_microbatch(
                batch,
                _micro_batch_size=_micro_batch_size,
                _global_batch_size=global_batch_size,
            ))
            print(batch)
            print(microbatches)
            self.assertEqual(len(microbatches), global_batch_size // _micro_batch_size)
            self.assertEqual(len(microbatches[0][0]), _micro_batch_size)

    def test_dynamic_batch_size(self):
        torch.manual_seed(42)

        n_samples = 100

        ds = MyIterableDataset(0, n_samples)

        for (replacement), drop_last in product((True, False), (True, False)):
            with self.subTest(f"replacement={replacement}, drop_last={drop_last}"):

                static_bs_samples = []
                sampler = BatchSampler(RandomSampler(list(range(n_samples)), replacement=replacement), batch_size=4, drop_last=drop_last)
                loader = DataLoader(ds, batch_sampler=sampler)
                for sample in loader:
                    static_bs_samples.append(sample)

                dynamic_bs_samples = []
                torch.manual_seed(42)
                sampler = BatchSampler(RandomSampler(list(range(n_samples)), replacement=replacement), batch_size=4, drop_last=drop_last)
                loader = DataLoader(ds, batch_sampler=sampler)
                print(f"bs 4: len(loader) = {len(loader)}")
                for _ in range(10):
                    dynamic_bs_samples.append(next(iter(loader)))

                loader.batch_size = 2
                loader.sampler.batch_size = 2
                print(f"bs: 2: len(loader) = {len(loader)}")
                for sample in loader:
                    self.assertEqual(len(sample), 2)
                    dynamic_bs_samples.append(sample)

                # print(static_bs_samples, dynamic_bs_samples)
                self.assertEqual(len(static_bs_samples), len(dynamic_bs_samples))


if __name__ == "__main__":
    unittest.main()
