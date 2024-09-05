from typing import Iterable, List, TypeVar

from edsnlp.utils.collections import batchify

T = TypeVar("T")


def batchify_by_words(
    iterable: Iterable[T],
    batch_size: int,
    drop_last: bool = False,
) -> Iterable[List[T]]:
    """
    Yields batch that contain at most `batch_size` words.
    If an item contains more than `batch_size` words, it will be yielded as a single
    batch.

    Parameters
    ----------
    iterable
    batch_size
    drop_last
    """
    batch = []
    total = 0
    for item in iterable:
        count = len(item)
        if total + count > batch_size and len(batch) > 0:
            yield batch
            batch = []
            total = 0
        batch.append(item)
        total += count
    if len(batch) > 0 and not drop_last:
        yield batch


def batchify_by_padded_words(
    iterable: Iterable[T],
    batch_size: int,
    drop_last: bool = False,
) -> Iterable[List[T]]:
    batch = []
    max_words = 0
    for item in iterable:
        count = len(item)
        if (1 + len(batch)) * max(max_words, count) > batch_size and len(batch) > 0:
            yield batch
            batch = []
            max_words = 0
        batch.append(item)
        max_words = max(max_words, count)
    if len(batch) > 0 and not drop_last:
        yield batch


batchify_fns = {
    "words": batchify_by_words,
    "padded_words": batchify_by_padded_words,
    "docs": batchify,
}
