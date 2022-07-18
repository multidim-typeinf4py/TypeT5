import asyncio
from typing import *
from spot.data import (
    CtxArgs,
    SrcDataset,
    TypeCheckingEnv,
    chunk_from_src,
    src_preds_to_accuracies,
    type_check_src_in_project,
)
from spot.model import ModelWrapper
from spot.tokenized_src import TokenizedSrc, feedbacks_to_tokenized_src
from spot.train import _configure_optimizers
from spot.type_check import MypyFeedback, PythonType, TypeCheckArgs, normalize_type
from spot.utils import *
from transformers.modeling_outputs import Seq2SeqLMOutput
from collections import deque as Deque

import torch
import copy
import random
import threading


class DAggerArgs(NamedTuple):
    save_dir: Path
    grad_accum_steps: int = 32
    concurrency: int = 12
    replay_buffer_size: int = concurrency * 100
    saves_per_epoch: int = 5


@dataclass
class DAggerTrainingState:
    args: DAggerArgs
    optimizer: torch.optim.Optimizer
    prog_bar: tqdm
    log_fn: Callable[[int, dict], None]
    save_every: int
    avg_loss: RunningAvg
    replay_buffer: Deque[dict] = field(default_factory=Deque)
    grad_counter: int = 0
    save_counter: int = 0


@dataclass
class DAggerRunResult:
    type_assignment: dict[int, PythonType] = field(default_factory=dict)
    batch_seq: list[dict] = field(default_factory=list)
    src_seq: list[TokenizedSrc] = field(default_factory=list)
    used_expert: list[bool] = field(default_factory=list)


class CostModel:
    SAMPLE = 1
    TRAIN = 1


@dataclass
class DAggerModel:
    wrapper: ModelWrapper
    t_logger: TimeLogger = field(default_factory=TimeLogger)

    async def rollout_on_src(
        self,
        src: TokenizedSrc,
        typecheck_env: TypeCheckingEnv,
        model_executor: ThreadPoolExecutor,
        cpu_executor: ProcessPoolExecutor,
        batch_callback: Callable[[dict], Coroutine] | None = None,
        expert_rate: float = 0.0,
    ) -> DAggerRunResult:
        """
        Run the DAgger model on the given source file, predicting one type at a time.
        """

        mr = self.wrapper
        ctx_args = mr.args.ctx_args
        t_logger = self.t_logger
        eloop = asyncio.get_event_loop()

        new_src = src
        result = DAggerRunResult()
        assignment = result.type_assignment

        for t, label in enumerate(src.types):
            use_expert = random.random() < expert_rate
            result.used_expert.append(use_expert)

            batch = await eloop.run_in_executor(
                cpu_executor, src_to_batch, new_src, t, ctx_args
            )
            result.batch_seq.append(batch)
            if use_expert:
                assignment[t] = label
            else:
                with t_logger.timed("predict next type"):
                    preds, _ = await eloop.run_in_executor(
                        model_executor, mr.predict_on_batch, batch
                    )
                assignment[t] = preds[0][0]

            if batch_callback is not None:
                # e.g., perform training here
                cb_future = batch_callback(batch)

            with t_logger.timed("type checking"):
                repo_root = typecheck_env.template_root / src.repo
                check_r = await eloop.run_in_executor(
                    cpu_executor,
                    type_check_src_in_project,
                    src,
                    assignment,
                    repo_root,
                    typecheck_env.pre_fdbks[src.file],
                )
            with t_logger.timed("generate new src"):
                new_src = await eloop.run_in_executor(
                    cpu_executor, get_typechecked_src, src, assignment, check_r
                )
                result.src_seq.append(new_src)

            if batch_callback is not None:
                await cb_future  # type: ignore

        return result

    async def train_on_data(
        self,
        src_datasets: dict[str, SrcDataset],
        dagger_args: DAggerArgs,
        log_fn: Callable[[int, dict], None],
    ):
        eloop = asyncio.get_event_loop()
        mr = self.wrapper
        train_set = src_datasets["train"]
        dev_set = src_datasets["valid"]
        all_srcs = train_set.all_srcs + dev_set.all_srcs
        train_srcs = copy.copy(train_set.all_srcs)
        random.shuffle(train_srcs)
        n_labels = sum(len(s.types) for s in train_srcs)
        mix_set = SrcDataset(train_set.repos_root, all_srcs)
        log_lock = threading.Lock()
        optimizer = _configure_optimizers(mr.model)[0][0]

        def log_fn_locked(t, d):
            with log_lock:
                log_fn(t, d)

        train_acc = RunningAvg(alpha=2 / (1 + n_labels))

        with mix_set.setup_typechecking(all_srcs) as env, tqdm(
            total=(CostModel.TRAIN + CostModel.SAMPLE) * n_labels,
            desc="train_on_data",
            smoothing=0.1,
        ) as pbar, ThreadPoolExecutor(1) as model_executor, ProcessPoolExecutor(
            DefaultWorkers
        ) as cpu_executor:
            save_every = n_labels // dagger_args.saves_per_epoch
            avg_loss = RunningAvg(alpha=2 / (1 + n_labels))
            state = DAggerTrainingState(
                dagger_args, optimizer, pbar, log_fn_locked, save_every, avg_loss
            )
            labels_counter = 0

            async def batch_callback(batch: dict):
                state.prog_bar.update(CostModel.SAMPLE)

            async def train_step(src):
                # progress = pbar.n / not_none(pbar.total)
                # assert 0 <= progress <= 1.0
                r = await self.rollout_on_src(
                    src,
                    env,
                    model_executor,
                    cpu_executor,
                    batch_callback=batch_callback,
                    # expert_rate=1 - progress,
                )
                preds = r.type_assignment
                assert_eq(len(preds), len(src.types))
                for i in range(len(src.types)):
                    if r.used_expert[i]:
                        continue
                    norm_pred = normalize_type(preds[i])
                    norm_label = normalize_type(src.types[i])
                    train_acc.update(int(norm_pred == norm_label))
                nonlocal labels_counter
                labels_counter += len(src.types)
                state.log_fn(
                    labels_counter,
                    {"train/acc": train_acc.value},
                )

                # train on the batches
                for batch in r.batch_seq:
                    await eloop.run_in_executor(
                        model_executor,
                        self._process_batch,
                        batch,
                        state,
                    )

            await throttled_async_run(train_step, train_srcs, dagger_args.concurrency)
            # train on the remaining batches
            await eloop.run_in_executor(
                model_executor,
                self._empty_buffer,
                state,
            )

    async def eval_on_data(
        self,
        dataset: SrcDataset,
        concurrency: int = DefaultWorkers,
    ):
        result = DAggerEvalResult([], [])

        with dataset.setup_typechecking(dataset.all_srcs) as env, tqdm(
            total=sum(len(s.types) for s in dataset.all_srcs),
            desc="eval_on_data",
            smoothing=0.1,
        ) as pbar, ThreadPoolExecutor(1) as model_executor, ProcessPoolExecutor(
            concurrency
        ) as cpu_executor:

            async def batch_callback(batch):
                pbar.update()

            async def eval_step(src: TokenizedSrc):
                r = await self.rollout_on_src(
                    src,
                    env,
                    model_executor,
                    cpu_executor,
                    batch_callback=batch_callback,
                    expert_rate=0.0,
                )
                result.final_srcs.append(r.src_seq[-1])
                preds = [r.type_assignment[i] for i in range(len(src.types))]
                result.final_preds.append(preds)

            await throttled_async_run(eval_step, dataset.all_srcs, concurrency)

        return result

    def _process_batch(self, batch: dict, state: DAggerTrainingState):
        """
        Add the new batch to the replay buffer and potentially train the model
        by comsuming the buffer.
        Should be called from the model thread to avoid race conditoin.
        """
        buffer_size = state.args.replay_buffer_size
        state.replay_buffer.appendleft(batch)
        if len(state.replay_buffer) > buffer_size:
            assert_eq(len(state.replay_buffer), buffer_size + 1)
            batch = state.replay_buffer.pop()
            self._train_on_batch(batch, state)

    def _empty_buffer(self, state: DAggerTrainingState):
        """
        Empty the replay buffer.
        Should be called from the model thread
        """
        while state.replay_buffer:
            batch = state.replay_buffer.pop()
            self._train_on_batch(batch, state)
        if state.grad_counter > 0:
            self._update_model(state)

    def _train_on_batch(
        self,
        batch: dict,
        state: DAggerTrainingState,
    ):
        t_logger = self.t_logger
        mr = self.wrapper
        accum_steps = state.args.grad_accum_steps
        device = mr.model.device

        with t_logger.timed("compute gradients"):
            with torch.autocast("cuda"):
                outputs = mr.model.forward(
                    input_ids=batch["input_ids"].to(device),
                    labels=batch["labels"].to(device),
                )
            assert isinstance(outputs, Seq2SeqLMOutput)
            loss = not_none(outputs.loss)
            state.avg_loss.update(loss.item())
            (loss / accum_steps).backward()
            state.prog_bar.update(CostModel.TRAIN)
            state.grad_counter += 1

        if state.grad_counter >= accum_steps:
            self._update_model(state)

    def _update_model(self, state: DAggerTrainingState):
        with self.t_logger.timed("update parameters"):
            torch.nn.utils.clip_grad_norm_(self.wrapper.model.parameters(), 1.0)
            state.optimizer.step()
            self.wrapper.model.zero_grad()

        step = state.avg_loss.count
        state.log_fn(
            step,
            {
                "train/loss": state.avg_loss.value,
                "train/replay_buffer": len(state.replay_buffer),
            },
        )

        state.save_counter += state.grad_counter
        if state.save_counter >= state.save_every:
            self.wrapper.save_pretrained(state.args.save_dir / f"step={step}")
            state.save_counter -= state.save_every

        state.grad_counter = 0


def src_to_batch(src: TokenizedSrc, t: int, ctx_args: CtxArgs):
    chunk, info = chunk_from_src(src, 0, t, ctx_args)
    assert_eq(chunk["n_labels"], 1)
    batch = {
        "input_ids": torch.tensor([chunk["input_ids"]]),
        "labels": torch.tensor([chunk["labels"]]),
        "n_labels": [1],
    }
    return batch


@dataclass
class DAggerEvalResult:
    final_srcs: list[TokenizedSrc]
    final_preds: list[list[PythonType]]

    @property
    def accuracies(self):
        return src_preds_to_accuracies(self.final_preds, self.final_srcs)


async def throttled_async_run(f, xs: Sequence, concurrency: int):
    sem = asyncio.Semaphore(concurrency)

    async def task(x):
        async with sem:
            return await f(x)

    tasks = [task(x) for x in xs]
    return await asyncio.gather(*tasks)


def get_typechecked_src(src: TokenizedSrc, assignment, check_r) -> TokenizedSrc:
    errors, current_code = check_r
    errors = [] if isinstance(errors, str) else errors
    new_src = feedbacks_to_tokenized_src(
        src,
        current_code,
        errors,
        patch_predictions=False,
    )
    new_src.prev_types = assignment
    new_src = new_src.inline_prev_predictions(as_comment=False)
    return new_src