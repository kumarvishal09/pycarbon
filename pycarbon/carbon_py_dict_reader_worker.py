#  Copyright (c) 2018-2019 Huawei Technologies, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import division

import hashlib

import numpy as np

from petastorm import utils
from petastorm.cache import NullCache
from petastorm.workers_pool.worker_base import WorkerBase
from petastorm.py_dict_reader_worker import PyDictReaderWorkerResultsQueueReader
from petastorm.py_dict_reader_worker import _select_cols, _merge_two_dicts


class PyDictCarbonReaderWorker(WorkerBase):
  def __init__(self, worker_id, publish_func, args):
    super(PyDictCarbonReaderWorker, self).__init__(worker_id, publish_func, args)

    self._filesystem = args[0]
    self._dataset_path = args[1]
    self._schema = args[2]
    self._ngram = args[3]
    self._split_pieces = args[4]
    self._local_cache = args[5]
    self._transform_spec = args[6]

    # We create datasets lazily in the first invocation of 'def process'. This speeds up startup time since
    # all Worker constructors are serialized
    self._dataset = None

  @staticmethod
  def new_results_queue_reader():
    return PyDictReaderWorkerResultsQueueReader()

  # pylint: disable=arguments-differ
  def process(self, piece_index, worker_predicate, shuffle_row_drop_partition):
    """Main worker function. Loads and returns all rows matching the predicate from a blocklet

    Looks up the requested piece (a single row-group in a carbon file). If a predicate is specified,
    columns needed by the predicate are loaded first. If no rows in the blocklet matches the predicate criteria
    the rest of the columns are not loaded.

    :param piece_index:
    :param shuffle_row_drop_partition: A tuple 2 of the current row drop partition and the total number
        of partitions.
    :return:
    """
    # start = time.time()
    piece = self._split_pieces[piece_index]

    if not isinstance(self._local_cache, NullCache):
      if worker_predicate:
        raise RuntimeError('Local cache is not supported together with predicates, '
                           'unless the dataset is partitioned by the column the predicate operates on.')
      if shuffle_row_drop_partition[1] != 1:
        raise RuntimeError('Local cache is not supported together with shuffle_row_drop_partitions > 1')

    if worker_predicate:
      all_cols = self._load_rows_with_predicate(piece, worker_predicate, shuffle_row_drop_partition)
    else:
      # Using hash of the dataset path with the relative path in order to:
      #  1. Make sure if a common cache serves multiple processes (e.g. redis), we don't have conflicts
      #  2. Dataset path is hashed, to make sure we don't create too long keys, which maybe incompatible with
      #     some cache implementations
      #  3. Still leave relative path and the piece_index in plain text to make it easier to debug
      cache_key = '{}:{}:{}'.format(hashlib.md5(self._dataset_path.encode('utf-8')).hexdigest(),
                                    piece.path, piece_index)
      # start1 = time.time()
      all_cols = self._local_cache.get(cache_key,
                                       lambda: self._load_rows(piece, shuffle_row_drop_partition))
      # print("cache time is " + str(time.time() - start))

    if self._ngram:
      all_cols = self._ngram.form_ngram(data=all_cols, schema=self._schema)

    if all_cols:
      self.publish_func(all_cols)
    # print("process time is " + str(time.time() - start))

  def _load_rows_with_predicate(self, piece, worker_predicate, shuffle_row_drop_partition):
    """Loads all rows that match a predicate from a piece"""

    # 1. Read all columns needed by predicate and decode
    # 2. Apply the predicate. If nothing matches, exit early
    # 3. Read the remaining columns and decode
    # 4. Combine with columns already decoded for the predicate.

    # Split all column names into ones that are needed by predicateand the rest.
    predicate_column_names = set(worker_predicate.get_fields())

    if not predicate_column_names:
      raise ValueError('At least one field name must be returned by predicate\'s get_field() method')

    all_schema_names = set(field.name for field in self._schema.fields.values())

    invalid_column_names = predicate_column_names - all_schema_names
    if invalid_column_names:
      raise ValueError('At least some column names requested by the predicate ({}) '
                       'are not valid schema names: ({})'.format(', '.join(invalid_column_names),
                                                                 ', '.join(all_schema_names)))

    other_column_names = all_schema_names - predicate_column_names
    other_column_names_list = list(other_column_names)

    predicate_column_names_list = list(predicate_column_names)
    # Read columns needed for the predicate
    predicate_rows = self._read_with_shuffle_row_drop(piece, predicate_column_names_list,
                                                      shuffle_row_drop_partition)

    # Decode values
    transform_func = self._transform_spec.func if self._transform_spec else (lambda x: x)
    decoded_predicate_rows = [
      transform_func(utils.decode_row(_select_cols(row, predicate_column_names), self._schema))
      for row in predicate_rows]

    # Use the predicate to filter
    match_predicate_mask = [worker_predicate.do_include(row) for row in decoded_predicate_rows]

    # Don't have anything left after filtering? Exit early.
    if not any(match_predicate_mask):
      return []

    # Remove rows that were filtered out by the predicate
    filtered_decoded_predicate_rows = [row for i, row in enumerate(decoded_predicate_rows) if
                                       match_predicate_mask[i]]

    if other_column_names:
      # Read remaining columns
      other_rows = self._read_with_shuffle_row_drop(piece, other_column_names_list,
                                                    shuffle_row_drop_partition)

      # Remove rows that were filtered out by the predicate
      filtered_other_rows = [row for i, row in enumerate(other_rows) if match_predicate_mask[i]]

      # Decode remaining columns
      decoded_other_rows = [utils.decode_row(row, self._schema) for row in filtered_other_rows]

      # Merge predicate needed columns with the remaining
      all_cols = [_merge_two_dicts(a, b) for a, b in zip(decoded_other_rows, filtered_decoded_predicate_rows)]
      return all_cols
    else:
      return filtered_decoded_predicate_rows

  def _load_rows(self, piece, shuffle_row_drop_range):
    """Loads all rows from a piece"""

    # pyarrow would fail if we request a column names that the dataset is partitioned by, so we strip them from
    # the `columns` argument.
    column_names = list(field.name for field in self._schema.fields.values())

    all_rows = self._read_with_shuffle_row_drop(piece, column_names, shuffle_row_drop_range)

    transform_func = self._transform_spec.func if self._transform_spec else (lambda x: x)
    return [transform_func(utils.decode_row(row, self._schema)) for row in all_rows]

  def _read_with_shuffle_row_drop(self, piece, column_names, shuffle_row_drop_partition):
    # start = time.time()
    data_frame = piece.read_all(
      columns=column_names,
    )
    # print(" total piece time taken is " + str(time.time() - start))
    # start = time.time()
    data_frame = data_frame.to_pandas()
    # print(" panda time is " + str(time.time() - start))

    num_rows = len(data_frame)
    num_partitions = shuffle_row_drop_partition[1]
    this_partition = shuffle_row_drop_partition[0]

    partition_indexes = np.floor(np.arange(num_rows) / (float(num_rows) / min(num_rows, num_partitions)))

    if self._ngram:
      # If we have an ngram we need to take elements from the next partition to build the sequence
      next_partition_indexes = np.where(partition_indexes >= this_partition + 1)
      if next_partition_indexes[0].size:
        next_partition_to_add = next_partition_indexes[0][0:self._ngram.length - 1]
        partition_indexes[next_partition_to_add] = this_partition

    selected_dataframe = data_frame.loc[partition_indexes == this_partition]
    return selected_dataframe.to_dict('records')
