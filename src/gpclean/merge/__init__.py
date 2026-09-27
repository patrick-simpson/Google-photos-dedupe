"""The merge: shard meta files -> one review bundle (docs/PLAN.md section 5).

Order of work (each step is its own module):

1. :mod:`~gpclean.merge.load` reads every shard and refuses mixed scan configs.
2. :mod:`~gpclean.merge.pairing` pairs media with their Takeout sidecars, globally.
3. :mod:`~gpclean.merge.localtime` derives capture time and local calendar day.
4. :mod:`~gpclean.merge.collapse` turns records of one library item into one index item.
5. :mod:`~gpclean.merge.scores` (screenshot score) -> :mod:`~gpclean.merge.group`
   (duplicates) -> :mod:`~gpclean.merge.bursts` -> :mod:`~gpclean.merge.scores` (the rest).
6. :mod:`~gpclean.merge.bundle` writes ``index.sqlite``, embeddings and ``manifest.json``;
   ``regroup`` redoes step 5 on an existing bundle.
"""
