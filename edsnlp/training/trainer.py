import json
import logging
import os
import time
import warnings
from collections import defaultdict
from itertools import chain
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Collection,
    Dict,
    Iterable,
    Optional,
    Union,
)

import torch
from accelerate import Accelerator
from confit import validate_arguments
from confit.utils.random import set_seed
from rich_logger import RichTablePrinter
from torch.optim import Optimizer
from tqdm import tqdm, trange
from typing_extensions import Literal

from edsnlp import Pipeline, registry
from edsnlp.core.stream import Stream
from edsnlp.metrics.ner import NerMetric
from edsnlp.metrics.span_attributes import SpanAttributeMetric
from edsnlp.pipes.base import BaseNERComponent, BaseSpanAttributeClassifierComponent
from edsnlp.utils.batching import BatchSizeArg, stat_batchify
from edsnlp.utils.bindings import BINDING_SETTERS
from edsnlp.utils.collections import chain_zip, flatten, ld_to_dl
from edsnlp.utils.span_getters import get_spans
from edsnlp.utils.typing import AsList

from .optimization import (  # noqa: F401
    LinearSchedule,
    ScheduledOptimizer,
    create_optimizer,
)

LOGGER_FIELDS = {
    "step": {},
    "(.*)loss": {
        "goal": "lower_is_better",
        "format": "{:.2e}",
        "goal_wait": 2,
    },
    "lr": {"format": "{:.2e}"},
    "speed/(.*)": {"format": "{:.2f}", r"name": r"\1"},
    "labels": {"format": "{:.2f}"},
    "(.*?)/micro/(f|r|p)$": {
        "goal": "higher_is_better",
        "format": "{:.2%}",
        "goal_wait": 1,
        "name": r"\1_\2",
    },
}


def flatten_dict(d, path=""):
    if not isinstance(d, dict):
        return {path: d}

    return {
        k: v
        for key, val in d.items()
        for k, v in flatten_dict(val, f"{path}/{key}" if path else key).items()
    }


def get_flat_stats(x, path=(), result=None):
    if result is None:
        result = {}
    if isinstance(x, dict):
        for k, v in x.items():
            get_flat_stats(v, (*path, k), result)
        return result
    if "stats" in path and "__batch_hash__" not in path[-1]:
        path = "/".join(path)
        result[path] = result.get(path, 0) + x
    return result


def set_flat_stats(x, stats):
    for k, v in stats.items():
        path = k.split("/")
        current = x
        for p in path[:-1]:
            if p not in current:
                break
            current = current[p]
        else:
            current[path[-1]] = v


@validate_arguments
class GenericScorer:
    def __init__(self, speed=True, **scorers):
        self.scorers = scorers
        self.speed = speed

    def __call__(self, nlp: Pipeline, docs: Iterable[Any]):
        scores = {}
        docs = list(docs)

        # Speed
        if self.speed:
            t0 = time.time()
            list(nlp.pipe(d.copy() for d in tqdm(docs, desc="Computing model speed")))
            duration = time.time() - t0
            scores["speed"] = dict(
                wps=sum(len(d) for d in docs) / duration,
                dps=len(docs) / duration,
            )

        # NER
        ner_pipes = [
            name for name, pipe in nlp.pipeline if isinstance(pipe, BaseNERComponent)
        ]
        ner_scorers = {
            name: scorer
            for name, scorer in self.scorers.items()
            if isinstance(scorer, NerMetric)
        }
        if ner_pipes and ner_scorers:
            clean_ner_docs = [d.copy() for d in tqdm(docs, desc="Copying docs")]
            for d in clean_ner_docs:
                d.ents = []
                d.spans.clear()
            with nlp.select_pipes(enable=ner_pipes):
                ner_preds = list(nlp.pipe(tqdm(clean_ner_docs, desc="Predicting")))
            for name, scorer in ner_scorers.items():
                scores[name] = scorer(docs, ner_preds)

        # Qualification
        qlf_pipes = [
            name
            for name, pipe in nlp.pipeline
            if isinstance(pipe, BaseSpanAttributeClassifierComponent)
        ]
        span_attr_scorers = {
            name: scorer
            for name, scorer in self.scorers.items()
            if isinstance(scorer, SpanAttributeMetric)
        }
        if qlf_pipes and span_attr_scorers:
            clean_qlf_docs = [d.copy() for d in tqdm(docs, desc="Copying docs")]
            for doc in clean_qlf_docs:
                for name in qlf_pipes:
                    pipe = nlp.get_pipe(name)
                    for span in get_spans(doc, pipe.span_getter):
                        for qlf in nlp.get_pipe(name).attributes:
                            BINDING_SETTERS[(qlf, None)](span)
            with nlp.select_pipes(disable=ner_pipes):
                qlf_preds = list(nlp.pipe(tqdm(clean_qlf_docs, desc="Predicting")))
            for name, scorer in span_attr_scorers.items():
                scores[name] = scorer(docs, qlf_preds)

        return scores


if TYPE_CHECKING:
    GenericScorer = Union[GenericScorer, Dict]


def default_optim(
    trained_pipes,
    *,
    task_lr: float = 3e-4,
    transformer_lr: float = 5e-5,
    warmup_rate: float = 0.1,
    max_steps: int,
):
    from edsnlp.pipes.trainable.embeddings.transformer.transformer import Transformer

    trf_pipe = next(
        (
            module
            for pipe in trained_pipes
            for name, module in pipe.named_component_modules()
            if isinstance(module, Transformer)
        ),
        None,
    )
    params = set(p for pipe in trained_pipes for p in pipe.parameters())
    trf_params = params & set(trf_pipe.parameters() if trf_pipe else ())

    return ScheduledOptimizer(
        torch.optim.AdamW(
            [
                {
                    "params": list(params - trf_params),
                    "lr": task_lr,
                    "schedules": LinearSchedule(
                        total_steps=max_steps,
                        warmup_rate=warmup_rate,
                        start_value=task_lr,
                    ),
                }
            ]
            + [
                {
                    "params": list(trf_params),
                    "lr": transformer_lr,
                    "schedules": LinearSchedule(
                        total_steps=max_steps,
                        warmup_rate=warmup_rate,
                        start_value=0,
                    ),
                },
            ][: 1 if transformer_lr else 0]
        )
    )


@validate_arguments
class TrainingData:
    def __init__(
        self,
        data: Stream,
        batch_size: BatchSizeArg,
        shuffle: str,
        accumulation_batch_size: Optional[BatchSizeArg] = None,
        pipe_names: Optional[Collection[str]] = None,
        post_init: bool = True,
    ):
        """
        A training data object.

        Parameters
        ----------
        data: Stream
            The stream of documents to train on. The documents will be
            preprocessed and collated according to the pipeline's components.
        batch_size: BatchSizeArg
            The batch size. Can be a batching expression like "2000 words",
            an int (number of documents), or a tuple (batch_size, batch_by).
            The batch_by argument should be a statistic produced by the
            pipes that will be trained. For instance, the `eds.span_pooler`
            component produces a "spans" statistic, that can be used to
            produce batches of no more than 16 spans by setting batch_size
            to "16 spans".
        shuffle: str
            The shuffle strategy. Can be "dataset" to shuffle the entire
            dataset (this can be memory-intensive for large file based
            datasets), "fragment" to shuffle the fragment-based datasets
            like parquet files, or a batching expression like "2000 words"
            to shuffle the dataset in chunks of 2000 words.
        accumulation_batch_size: Optional[BatchSizeArg]
            How to split each batch into sub-batches that will be fed to
            the model independently to accumulate gradients over.
        pipe_names: Optional[Collection[str]]
            The names of the pipes that should be trained on this data.
            If None, defaults to all trainable pipes.
        post_init: bool
            Whether to call the pipeline's post_init method with the data
            before training.
        """
        self.data = data
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.accumulation_batch_size = accumulation_batch_size
        self.pipe_names = set(pipe_names) if pipe_names else None
        self.post_init = post_init

    def __call__(self, nlp, device):
        data = self.data.copy(reader=True)
        data.reader.loop = True
        if self.shuffle:
            data = data.shuffle(self.shuffle)

        with nlp.select_pipes(enable=self.pipe_names):
            data = data.map(nlp.preprocess, kwargs=dict(supervision=True))
        batcher = stat_batchify(self.batch_size[1] or "docs")
        data = data.batchify(batch_size=self.batch_size[0], batch_by=batcher)
        if self.accumulation_batch_size:
            sub_batcher = stat_batchify(self.accumulation_batch_size[1] or "docs")
            data = data.map(
                lambda batch: [
                    nlp.collate(sub_batch)
                    for sub_batch in sub_batcher(batch, self.accumulation_batch_size[0])
                ]
            )
        else:
            data = data.map(nlp.collate, kwargs=dict(device=device))
        return data


@validate_arguments(registry=registry)
def train(
    *,
    nlp: Pipeline,
    train_data: AsList[TrainingData],
    val_data: AsList[Stream],
    seed: int = 42,
    max_steps: int = 1000,
    optim: Union[Optimizer, Optional[Callable[[Any, int], Optimizer]]] = None,
    validation_interval: int = 10,
    max_grad_norm: float = 5.0,
    loss_scales: Dict[str, float] = {},
    scorer: GenericScorer = GenericScorer(),
    num_workers: int = 0,
    cpu: bool = False,
    mixed_precision: Literal["no", "fp16", "bf16", "fp8"] = "no",
    output_dir: Union[Path, str] = Path("artifacts"),
    **kwargs,
):
    """
    Train a pipeline.

    Parameters
    ----------
    nlp: Pipeline
        The pipeline that will be trained in place.
    train_data: AsList[TrainingData]
        The training data. Can be a single
        [TrainingData][edsnlp.training.trainer.TrainingData] object, a dict that
        will be cast or a list of these objects.
    val_data: AsList[Stream]
        The validation data. Can be a single Stream object or a list of
        Stream.
    seed: int
        The random seed
    max_steps: int
        The maximum number of training steps
    optim: Union[Optimizer, Optional[Callable[[Any, int], Optimizer]]]
        The optimizer. If None, a default optimizer will be used.
    validation_interval: int
        The number of steps between each evaluation
    max_grad_norm: float
        The maximum gradient norm
    loss_scales: Dict[str, float]
        The loss scales for each component (useful for multi-task learning)
    scorer: GenericScorer
        How to score the model. Expects a `GenericScorer` object or a dict
        containing a mapping of metric names to metric objects.
    num_workers: int
        The number of workers to use for preprocessing the data in parallel.
        Setting it to 0 means no parallelization : data is processed on the
        main thread which may cause the GPU to be underutilized. Because
        of how EDS-NLP handles stream multiprocessing, changing this value
        will affect the order of the documents in the produces batches.
        A stream [1, 2, 3, 4, 5, 6] split in batches of size 3 will produce:

        - [1, 2, 3] and [4, 5, 6] with 1 worker
        - [1, 3, 5] and [2, 4, 6] with 2 workers
    cpu: bool
        Whether to use force training on CPU. On MacOS, this might be
        necessary to get around some `mps` backend issues.
    output_dir: Union[Path, str]
        The output directory, which will contain a `model-last` directory
        with the last model, and a `train_metrics.json` file with the
        training metrics and stats.
    kwargs: Dict
        Additional keyword arguments.

    Returns
    -------
    Pipeline
        The pipeline
    """
    # Prepare paths
    output_dir = Path(output_dir or Path.cwd() / "artifacts")
    model_path = output_dir / "model-last"
    train_metrics_path = output_dir / "train_metrics.json"
    os.makedirs(output_dir, exist_ok=True)
    optim_base = optim

    # Prepare validation docs
    val_docs = list(chain.from_iterable(val_data))

    trainable_pipe_names = {name for name, pipe in nlp.torch_components()}
    print("Trainable components: " + ", ".join(trainable_pipe_names))
    phases = nlp.connected_pipes_names()
    print(
        "Training phases:"
        + "".join(f"\n - {i + 1}: {', '.join(n)}" for i, n in enumerate(phases))
    )

    # Initialize pipeline with training documents
    nlp.post_init(chain_zip([td.data for td in train_data if td.post_init]))

    for phase_i, pipe_names in enumerate(phases):
        trained_pipes = [nlp.get_pipe(name) for name in pipe_names]

        with nlp.select_pipes(disable=trainable_pipe_names - set(pipe_names)):
            print(f"Phase {phase_i + 1}: training {', '.join(pipe_names)}")
            set_seed(seed)

            logging.debug("Build the optimizer")
            all_params = set(nlp.parameters())
            if optim_base is None:
                warnings.warn(
                    "No optimizer provided, using default optimizer with default "
                    "parameters"
                )
                optim = default_optim(
                    trained_pipes,
                    max_steps=max_steps,
                    **{
                        k: v
                        for k, v in kwargs.items()
                        if k in ("task_lr", "transformer_lr", "warmup_rate")
                    },
                )
            else:
                optim = (
                    optim_base
                    if isinstance(optim_base, Optimizer)
                    else optim_base(nlp, max_steps)
                )
            if hasattr(optim, "reset"):
                optim.reset()
            grad_params = {p for group in optim.param_groups for p in group["params"]}
            print(
                "Optimizing groups:"
                + "".join(
                    f"\n - {g.get('selector', '*') + ':' if 'selector' in g else ''} "
                    f"{len(g['params'])} weight tensors "
                    f"({sum(p.numel() for p in g['params']):,} parameters)"
                    for g in optim.param_groups
                )
            )
            print(
                f"Keeping frozen {len(all_params - grad_params):} weight tensors "
                f"({sum(p.numel() for p in all_params - grad_params):,} parameters)"
            )
            for param in all_params:
                param.requires_grad_(param in grad_params)

            logging.debug("Finished building the optimizer")

            accelerator = Accelerator(cpu=cpu, mixed_precision=mixed_precision)
            is_main_process = accelerator.is_main_process
            device = accelerator.device
            print("Device:", device)
            nlp.train(True)

            (optim, *trained_pipes) = accelerator.prepare(optim, *trained_pipes)

            cumulated_data = defaultdict(lambda: 0.0, count=0)

            iterator = iter(
                zip(
                    *(
                        td(nlp, device).set_processing(
                            num_cpu_workers=num_workers,
                            process_start_method="spawn",
                        )
                        for td in train_data
                        if td.pipe_names is None or set(td.pipe_names) & set(pipe_names)
                    )
                )
            )
            all_metrics = []
            set_seed(seed)

            with RichTablePrinter(LOGGER_FIELDS, auto_refresh=False) as logger:
                # Training loop
                for step in trange(
                    max_steps + 1,
                    desc="Training model",
                    leave=True,
                    mininterval=5.0,
                    total=max_steps,
                ):
                    if is_main_process and (step % validation_interval) == 0:
                        scores = scorer(nlp, val_docs)
                        all_metrics.append(
                            {
                                "step": step,
                                "lr": optim.param_groups[0]["lr"],
                                **cumulated_data,
                                **scores,
                            }
                        )
                        cumulated_data.clear()
                        nlp.to_disk(model_path)
                        train_metrics_path.write_text(json.dumps(all_metrics, indent=2))
                        logger.log_metrics(flatten_dict(all_metrics[-1]))

                    if step == max_steps:
                        break

                    optim.zero_grad()

                    batches = list(flatten(list(next(iterator))))

                    # Synchronize stats between sub-batches across workers
                    stats = {}
                    for b in batches:
                        get_flat_stats(b, result=stats)
                    stats = list(flatten(accelerator.gather([stats])))
                    stats = {k: sum(v) for k, v in ld_to_dl(stats).items()}
                    for b in batches:
                        set_flat_stats(b, stats)
                    if is_main_process:
                        for k, v in stats.items():
                            cumulated_data[k] += v
                    del stats

                    for batch in batches:
                        loss = torch.zeros((), device=accelerator.device)
                        with nlp.cache():
                            for name, pipe in zip(pipe_names, trained_pipes):
                                res = dict(pipe(batch[name]))
                                if "loss" in res:
                                    res["loss"] = res["loss"] * loss_scales.get(name, 1)
                                    loss += res["loss"]
                                    res[f"{name}_loss"] = res["loss"]
                                for key, value in res.items():
                                    if key.endswith("loss"):
                                        cumulated_data[key] += float(value)
                                if torch.isnan(loss):
                                    raise ValueError(f"NaN loss at component {name}")
                        accelerator.backward(loss)
                        del loss, res, key, value, batch, name, pipe

                    accelerator.clip_grad_norm_(grad_params, max_grad_norm)
                    optim.step()

                del iterator

    return nlp
