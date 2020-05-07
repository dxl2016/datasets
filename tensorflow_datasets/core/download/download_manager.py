# coding=utf-8
# Copyright 2020 The TensorFlow Datasets Authors.
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

# Lint as: python3
"""Download manager interface."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import sys
from typing import Optional
import uuid

from absl import logging
import promise
import six
import tensorflow.compat.v2 as tf

from tensorflow_datasets.core import api_utils
from tensorflow_datasets.core import utils
from tensorflow_datasets.core.download import checksums
from tensorflow_datasets.core.download import downloader
from tensorflow_datasets.core.download import extractor
from tensorflow_datasets.core.download import resource as resource_lib
from tensorflow_datasets.core.download import util


class NonMatchingChecksumError(Exception):
  """The downloaded file doesn't have expected checksum."""

  def __init__(self, url, tmp_path):
    msg = (
        'Artifact {}, downloaded to {}, has wrong checksum. This might '
        'indicate:\n'
        ' * The website may be down (e.g. returned a 503 status code). Please '
        'check the url.\n'
        ' * For Google Drive URLs, try again later as Drive sometimes rejects '
        'downloads when too many people access the same URL. See '
        'https://github.com/tensorflow/datasets/issues/1482\n'
        ' * The original datasets files may have been updated. In this case '
        'the TFDS dataset builder should be updated to use the new files '
        'and checksums. Sorry about that. Please open an issue or send us a PR '
        'with a fix.\n'
        ' * If you\'re adding a new dataset, don\'t forget to register the '
        'checksums as explained in: '
        'https://www.tensorflow.org/datasets/add_dataset#2_run_download_and_prepare_locally\n'
    ).format(url, tmp_path)
    Exception.__init__(self, msg)


class DownloadConfig(object):
  """Configuration for `tfds.core.DatasetBuilder.download_and_prepare`."""

  def __init__(
      self,
      extract_dir=None,
      manual_dir=None,
      download_mode=None,
      compute_stats=None,
      max_examples_per_split=None,
      register_checksums=False,
      force_checksums_validation=False,
      beam_runner=None,
      beam_options=None,
      try_download_gcs=True,
  ):
    """Constructs a `DownloadConfig`.

    Args:
      extract_dir: `str`, directory where extracted files are stored.
        Defaults to "<download_dir>/extracted".
      manual_dir: `str`, read-only directory where manually downloaded/extracted
        data is stored. Defaults to `<download_dir>/manual`.
      download_mode: `tfds.GenerateMode`, how to deal with downloads or data
        that already exists. Defaults to `REUSE_DATASET_IF_EXISTS`, which will
        reuse both downloads and data if it already exists.
      compute_stats: `tfds.download.ComputeStats`, whether to compute
        statistics over the generated data. Defaults to `AUTO`.
      max_examples_per_split: `int`, optional max number of examples to write
        into each split (used for testing).
      register_checksums: `bool`, defaults to False. If True, checksum of
        downloaded files are recorded.
      force_checksums_validation: `bool`, defaults to False. If True, raises
        an error if an URL do not have checksums.
      beam_runner: Runner to pass to `beam.Pipeline`, only used for datasets
        based on Beam for the generation.
      beam_options: `PipelineOptions` to pass to `beam.Pipeline`, only used for
        datasets based on Beam for the generation.
      try_download_gcs: `bool`, defaults to True. If True, prepared dataset
        will be downloaded from GCS, when available. If False, dataset will be
        downloaded and prepared from scratch.
    """
    self.extract_dir = extract_dir
    self.manual_dir = manual_dir
    self.download_mode = util.GenerateMode(
        download_mode or util.GenerateMode.REUSE_DATASET_IF_EXISTS)
    self.compute_stats = util.ComputeStatsMode(
        compute_stats or util.ComputeStatsMode.AUTO)
    self.max_examples_per_split = max_examples_per_split
    self.register_checksums = register_checksums
    self.force_checksums_validation = force_checksums_validation
    self.beam_runner = beam_runner
    self.beam_options = beam_options
    self.try_download_gcs = try_download_gcs


class DownloadManager(object):
  """Manages the download and extraction of files, as well as caching.

  Downloaded files are cached under `download_dir`. The file name of downloaded
   files follows pattern "{sanitized_url}{content_checksum}.{ext}". Eg:
   'cs.toronto.edu_kriz_cifar-100-pythonJDF[...]I.tar.gz'.

  While a file is being downloaded, it is placed into a directory following a
  similar but different pattern:
  "{sanitized_url}{url_checksum}.tmp.{uuid}".

  When a file is downloaded, a "{fname}.INFO.json" file is created next to it.
  This INFO file contains the following information:
  {"dataset_names": ["name1", "name2"],
   "urls": ["http://url.of/downloaded_file"]}

  Extracted files/dirs are stored under `extract_dir`. The file name or
  directory name is the same as the original name, prefixed with the extraction
  method. E.g.
   "{extract_dir}/TAR_GZ.cs.toronto.edu_kriz_cifar-100-pythonJDF[...]I.tar.gz".

  The function members accept either plain value, or values wrapped into list
  or dict. Giving a data structure will parallelize the downloads.

  Example of usage:

  ```
  # Sequential download: str -> str
  train_dir = dl_manager.download_and_extract('https://abc.org/train.tar.gz')
  test_dir = dl_manager.download_and_extract('https://abc.org/test.tar.gz')

  # Parallel download: list -> list
  image_files = dl_manager.download(
      ['https://a.org/1.jpg', 'https://a.org/2.jpg', ...])

  # Parallel download: dict -> dict
  data_dirs = dl_manager.download_and_extract({
     'train': 'https://abc.org/train.zip',
     'test': 'https://abc.org/test.zip',
  })
  data_dirs['train']
  data_dirs['test']
  ```

  For more customization on the download/extraction (ex: passwords, output_name,
  ...), you can pass a `tfds.download.Resource` as argument.
  """

  @api_utils.disallow_positional_args
  def __init__(
      self,
      download_dir: str,
      extract_dir: Optional[str] = None,
      manual_dir: Optional[str] = None,
      manual_dir_instructions: Optional[str] = None,
      dataset_name: Optional[str] = None,
      force_download: bool = False,
      force_extraction: bool = False,
      force_checksums_validation: bool = False,
      register_checksums: bool = False,
  ):
    """Download manager constructor.

    Args:
      download_dir: Path to directory where downloads are stored.
      extract_dir: Path to directory where artifacts are extracted.
      manual_dir: Path to manually downloaded/extracted data directory.
      manual_dir_instructions: Human readable instructions on how to
        prepare contents of the manual_dir for this dataset.
      dataset_name: Name of dataset this instance will be used for. If
        provided, downloads will contain which datasets they were used for.
      force_download: If True, always [re]download.
      force_extraction: If True, always [re]extract.
      force_checksums_validation: If True, raises an error if an URL do not
        have checksums.
      register_checksums: If True, dl checksums aren't
        checked, but stored into file.
    """
    self._dataset_name = dataset_name
    self._download_dir = os.path.expanduser(download_dir)
    self._extract_dir = os.path.expanduser(
        extract_dir or os.path.join(download_dir, 'extracted'))
    self._manual_dir = manual_dir and os.path.expanduser(manual_dir)
    self._manual_dir_instructions = manual_dir_instructions
    tf.io.gfile.makedirs(self._download_dir)
    tf.io.gfile.makedirs(self._extract_dir)
    self._force_download = force_download
    self._force_extraction = force_extraction
    self._force_checksums_validation = force_checksums_validation
    self._register_checksums = register_checksums
    # All known URLs: {url: (size, checksum)}
    self._url_infos = checksums.get_all_url_infos()
    # To record what is being used: {url: (size, checksum)}
    self._recorded_url_infos = {}
    # These attributes are lazy-initialized since they must be cleared when this
    # object is pickled for Beam. They are then recreated on each worker.
    self.__downloader = None
    self.__extractor = None

  def __getstate__(self):
    """Remove un-pickleable attributes and return the state."""
    if self._register_checksums:
      raise ValueError(
          '`register_checksums` must be disabled in a parallelized '
          'DownloadManager.')
    state = self.__dict__.copy()
    state['_DownloadManager__downloader'] = None
    state['_DownloadManager__extractor'] = None
    return state

  @property
  def _downloader(self):
    if not self.__downloader:
      self.__downloader = downloader.get_downloader()
    return self.__downloader

  @property
  def _extractor(self):
    if not self.__extractor:
      self.__extractor = extractor.get_extractor()
    return self.__extractor

  @property
  def downloaded_size(self):
    """Returns the total size of downloaded files."""
    return sum(size for size, sha256 in self._recorded_url_infos.values())

  def _get_final_dl_path(self, url, sha256):
    return os.path.join(self._download_dir,
                        resource_lib.get_dl_fname(url, sha256))

  @property
  def register_checksums(self):
    """Returns whether checksums are being computed and recorded to file."""
    return self._register_checksums

  @util.build_synchronize_decorator()
  def _record_url_infos(self):
    """Store in file when recorded size/checksum of downloaded files."""
    checksums.store_checksums(self._dataset_name,
                              self._recorded_url_infos)

  def _handle_download_result(self, resource, tmp_dir_path, url_info):
    """Store dled file to definitive place, write INFO file, return path."""
    fnames = tf.io.gfile.listdir(tmp_dir_path)
    if len(fnames) > 1:
      raise AssertionError('More than one file in %s.' % tmp_dir_path)
    original_fname = fnames[0]
    tmp_path = os.path.join(tmp_dir_path, original_fname)
    self._recorded_url_infos[resource.url] = url_info
    if self._register_checksums:
      self._record_url_infos()
    elif resource.url not in self._url_infos:
      if self._force_checksums_validation:
        raise ValueError(
            'Missing checksums url: {}, yet `force_checksums_validation=True` .'
            'Did you forgot to register checksums ?')
      # Otherwise, do nothing
    elif url_info != self._url_infos.get(resource.url, None):
      raise NonMatchingChecksumError(resource.url, tmp_path)
    download_path = self._get_final_dl_path(resource.url, url_info.checksum)
    resource_lib.write_info_file(resource, download_path, self._dataset_name,
                                 original_fname)
    # Unconditionally overwrite because either file doesn't exist or
    # FORCE_DOWNLOAD=true
    tf.io.gfile.rename(tmp_path, download_path, overwrite=True)
    tf.io.gfile.rmtree(tmp_dir_path)
    return download_path

  def download_checksums(self, checksums_url):
    """Downloads checksum file from the given URL and adds it to registry."""
    checksums_path = self.download(checksums_url)
    with tf.io.gfile.GFile(checksums_path) as f:
      self._url_infos.update(checksums.parse_url_infos(f))

  # synchronize and memoize decorators ensure same resource will only be
  # processed once, even if passed twice to download_manager.
  @util.build_synchronize_decorator()
  @utils.memoize()
  def _download(self, resource):
    """Download resource, returns Promise->path to downloaded file."""
    if isinstance(resource, six.string_types):
      resource = resource_lib.Resource(url=resource)
    url = resource.url
    if url in self._url_infos:
      expected_sha256 = self._url_infos[url].checksum
      download_path = self._get_final_dl_path(url, expected_sha256)
      if not self._force_download and resource.exists_locally(download_path):
        logging.info('URL %s already downloaded: reusing %s.',
                     url, download_path)
        self._recorded_url_infos[url] = self._url_infos[url]
        return promise.Promise.resolve(download_path)
    # There is a slight difference between downloader and extractor here:
    # the extractor manages its own temp directory, while the DownloadManager
    # manages the temp directory of downloader.
    download_dir_path = os.path.join(
        self._download_dir,
        '%s.tmp.%s' % (resource_lib.get_dl_dirname(url), uuid.uuid4().hex))
    tf.io.gfile.makedirs(download_dir_path)
    logging.info('Downloading %s into %s...', url, download_dir_path)

    def callback(url_info):
      return self._handle_download_result(resource, download_dir_path, url_info)
    return self._downloader.download(url, download_dir_path).then(callback)

  @util.build_synchronize_decorator()
  @utils.memoize()
  def _extract(self, resource):
    """Extract a single archive, returns Promise->path to extraction result."""
    if isinstance(resource, six.string_types):
      resource = resource_lib.Resource(path=resource)
    path = resource.path
    extract_method = resource.extract_method
    if extract_method == resource_lib.ExtractMethod.NO_EXTRACT:
      logging.info('Skipping extraction for %s (method=NO_EXTRACT).', path)
      return promise.Promise.resolve(path)
    method_name = resource_lib.ExtractMethod(extract_method).name
    extract_path = os.path.join(self._extract_dir,
                                '%s.%s' % (method_name, os.path.basename(path)))
    if not self._force_extraction and tf.io.gfile.exists(extract_path):
      logging.info('Reusing extraction of %s at %s.', path, extract_path)
      return promise.Promise.resolve(extract_path)
    return self._extractor.extract(path, extract_method, extract_path)

  @util.build_synchronize_decorator()
  @utils.memoize()
  def _download_extract(self, resource):
    """Download-extract `Resource` or url, returns Promise->path."""
    if isinstance(resource, six.string_types):
      resource = resource_lib.Resource(url=resource)
    def callback(path):
      resource.path = path
      return self._extract(resource)
    return self._download(resource).then(callback)

  def download_kaggle_data(self, competition_name):
    """Download data for a given Kaggle competition."""
    with self._downloader.tqdm():
      kaggle_downloader = self._downloader.kaggle_downloader(competition_name)
      urls = kaggle_downloader.competition_urls
      files = kaggle_downloader.competition_files
      return _map_promise(self._download,
                          dict((f, u) for (f, u) in zip(files, urls)))

  def download(self, url_or_urls):
    """Download given url(s).

    Args:
      url_or_urls: url or `list`/`dict` of urls to download and extract. Each
        url can be a `str` or `tfds.download.Resource`.

    Returns:
      downloaded_path(s): `str`, The downloaded paths matching the given input
        url_or_urls.
    """
    # Add progress bar to follow the download state
    with self._downloader.tqdm():
      return _map_promise(self._download, url_or_urls)

  def iter_archive(self, resource):
    """Returns iterator over files within archive.

    **Important Note**: caller should read files as they are yielded.
    Reading out of order is slow.

    Args:
      resource: path to archive or `tfds.download.Resource`.

    Returns:
      Generator yielding tuple (path_within_archive, file_obj).
    """
    if isinstance(resource, six.string_types):
      resource = resource_lib.Resource(path=resource)
    return extractor.iter_archive(resource.path, resource.extract_method)

  def extract(self, path_or_paths):
    """Extract given path(s).

    Args:
      path_or_paths: path or `list`/`dict` of path of file to extract. Each
        path can be a `str` or `tfds.download.Resource`.

    If not explicitly specified in `Resource`, the extraction method is deduced
    from downloaded file name.

    Returns:
      extracted_path(s): `str`, The extracted paths matching the given input
        path_or_paths.
    """
    # Add progress bar to follow the download state
    with self._extractor.tqdm():
      return _map_promise(self._extract, path_or_paths)

  def download_and_extract(self, url_or_urls):
    """Download and extract given url_or_urls.

    Is roughly equivalent to:

    ```
    extracted_paths = dl_manager.extract(dl_manager.download(url_or_urls))
    ```

    Args:
      url_or_urls: url or `list`/`dict` of urls to download and extract. Each
        url can be a `str` or `tfds.download.Resource`.

    If not explicitly specified in `Resource`, the extraction method will
    automatically be deduced from downloaded file name.

    Returns:
      extracted_path(s): `str`, extracted paths of given URL(s).
    """
    # Add progress bar to follow the download state
    with self._downloader.tqdm():
      with self._extractor.tqdm():
        return _map_promise(self._download_extract, url_or_urls)

  @property
  def manual_dir(self):
    """Returns the directory containing the manually extracted data."""
    if not self._manual_dir:
      raise AssertionError(
          'Manual directory was enabled. '
          'Did you set MANUAL_DOWNLOAD_INSTRUCTIONS in your dataset?')
    if (not tf.io.gfile.exists(self._manual_dir) or
        not list(tf.io.gfile.listdir(self._manual_dir))):
      raise AssertionError(
          'Manual directory {} does not exist or is empty. Create it and '
          'download/extract dataset artifacts in there. Additional '
          'instructions: {}'.format(
              self._manual_dir, self._manual_dir_instructions))
    return self._manual_dir


# ============================================================================
# In Python 2.X, threading.Condition.wait() cannot be interrupted by SIGINT,
# unless it's given a timeout. Here we artificially give a long timeout to
# allow ctrl+C.
# This code should be deleted once python2 is no longer supported.
if sys.version_info[0] > 2:

  def _wait_on_promise(p):
    return p.get()

else:

  def _wait_on_promise(p):
    while True:
      result = p.get(sys.maxint)  # pylint: disable=g-deprecated-member-used
      if p.is_fulfilled:
        return result

# ============================================================================


def _map_promise(map_fn, all_inputs):
  """Map the function into each element and resolve the promise."""
  all_promises = utils.map_nested(map_fn, all_inputs)  # Apply the function
  res = utils.map_nested(_wait_on_promise, all_promises)
  return res
