# Copyright 2023, Google LLC.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Library for loading serialized matrix factorizations."""

import re
from typing import Optional

from absl import flags
import numpy as np
import tensorflow as tf

_FACTORIZATION_ROOT = '/tmp/matrix_factorizations'
MATRIX_ROOT_PATH = flags.DEFINE_string(
    'matrix_root_path', _FACTORIZATION_ROOT, 'Root path for loading matrices.'
)

W_MATRIX_STRING = 'w_matrix_tensor_pb'
H_MATRIX_STRING = 'h_matrix_tensor_pb'
LR_VECTOR_STRING = 'lr_vector_tensor_pb'

# Matrix names
PREFIX_ONLINE_HONAKER = 'prefix_online_honaker'
PREFIX_FULL_HONAKER = 'prefix_full_honaker'
PREFIX_OPT = 'prefix_opt'


def _join_path(*args):
  # Always use "/" for Google storage.
  return '/'.join(args)


def get_matrix_path(n: int, mechanism_name: str) -> str:
  """Constructs the path for the given mechanism.

  No assumptions about the existence of the path are made, so this can be used
  to generate paths for reading or writing.

  Args:
    n: The number of steps / rounds / iterations.
    mechanism_name: The name of the mechanism, e.g. 'opt_prefix_sum_matrix',
      'streaming_honaker_matrix', and 'full_honaker_matrix'

  Returns:
    A path.
  """
  size_str = f'size={n:d}'
  return _join_path(MATRIX_ROOT_PATH.value, mechanism_name, size_str)


def get_momentum_path(n: int, momentum: float) -> str:
  """Constructs the directory path for the momentum mechanism."""
  if not 0.0 <= momentum <= 1.0:
    raise ValueError(f'momentum {momentum} outside of range [0, 1]')
  if round(momentum, 2) != momentum:
    raise ValueError(f'Specify momentum in hundreths. Found {momentum}')
  return get_matrix_path(n=n, mechanism_name=f'momentum_0p{100*momentum:02.0f}')


def load_w_h_and_maybe_lr(
    path: str,
) -> tuple[tf.Tensor, tf.Tensor, Optional[tf.Tensor]]:
  """Loads W, H, and possibly a vector of learning rates.

  Args:
    path: A directory to read from, usually generated by `get_matrix_path` or
      `get_momentum_path`.

  Returns:
    A tuple (w_matrix, h_matrix, learning_rate_vector).
  """
  if not (tf.io.gfile.exists(path) and tf.io.gfile.isdir(path)):
    raise ValueError(
        f'Matrix factorization directory {path} does not exist. '
        'Check flag values or ask for the files to be '
        'generated.'
    )
  w_matrix = tf.io.parse_tensor(
      tf.io.read_file(_join_path(path, W_MATRIX_STRING)), tf.float64
  )
  h_matrix = tf.io.parse_tensor(
      tf.io.read_file(_join_path(path, H_MATRIX_STRING)), tf.float64
  )
  lr_file = _join_path(path, LR_VECTOR_STRING)
  lr_tensor = None
  if tf.io.gfile.exists(lr_file):
    lr_tensor = tf.io.parse_tensor(
        tf.io.read_file(_join_path(path, LR_VECTOR_STRING)), tf.float64
    )
  return w_matrix, h_matrix, lr_tensor


def get_prefix_sum_w_h(
    n: int, aggregator_method: str
) -> tuple[tf.Tensor, tf.Tensor]:
  """Returns (W, H) for prefix sum methods.

  Args:
    n: The number of iterations.
    aggregator_method: Preferred options are PREFIX_OPT, PREFIX_ONLINE_HONAKER,
      or PREFIX_FULL_HONAKER. For legacy reasons, also supports
      'opt_prefix_sum_matrix', 'streaming_honaker_matrix', and
      'full_honaker_matrix'.

  Returns:
    A pair of matrices (W, H).
  """
  if aggregator_method in ['opt_prefix_sum_matrix', PREFIX_OPT]:
    path = get_matrix_path(n, PREFIX_OPT)
  elif aggregator_method in ['streaming_honaker_matrix', PREFIX_ONLINE_HONAKER]:
    path = get_matrix_path(n, PREFIX_ONLINE_HONAKER)
  elif aggregator_method in ['full_honaker_matrix', PREFIX_FULL_HONAKER]:
    path = get_matrix_path(n, PREFIX_FULL_HONAKER)
  else:
    raise NotImplementedError(
        f'Unexpected aggregator_method {aggregator_method}'
    )
  w_matrix, h_matrix, lr_vector = load_w_h_and_maybe_lr(path)
  assert lr_vector is None
  return w_matrix, h_matrix


def infer_momentum_from_path(path: str) -> Optional[float]:
  """For momentum paths, extracts the momentum parameter."""
  match = re.search(r'momentum_0p(\d\d)', path)
  if match:
    return float(match.group(1)) / 100
  return None


def scale_w_h_by_single_participation_sensitivity(
    w_matrix: tf.Tensor, h_matrix: tf.Tensor
) -> tuple[tf.Tensor, tf.Tensor]:
  """Returns a new pair (W, H) where H has sensitivity 1.0.

  Assumes a single participation, so we normalize H and W
  based on the maximum column norm of H.

  Args:
    w_matrix: The left matrix.
    h_matrix: The right matrix.
  Returns: The rescaled (W, H).
  """
  max_col_norm = np.max(np.linalg.norm(h_matrix, axis=0))
  return w_matrix * max_col_norm, h_matrix / max_col_norm


def verify_reconstruction(
    w_matrix: tf.Tensor, h_matrix: tf.Tensor, s_matrix: tf.Tensor
):
  """Checks that w_matrix and h_matrix are valid."""
  assert w_matrix.dtype == np.float64, w_matrix.dtype
  assert h_matrix.dtype == np.float64, h_matrix.dtype
  # Check reconstruction:
  s_reconstructed = w_matrix @ h_matrix
  np.testing.assert_allclose(s_matrix, s_reconstructed, rtol=1e-6, atol=1e-8)


def verify_and_write(
    w_matrix: tf.Tensor,
    h_matrix: tf.Tensor,
    s_matrix: tf.Tensor,
    output_dir: str,
    lr_sched: Optional[tf.Tensor] = None,
):
  """Scales, verifies factorization, and writes W, H, and maybe lr_sched.

  Args:
    w_matrix: Left matrix.
    h_matrix: Right matrix.
    s_matrix: Target matrix such that S = W @ H.
    output_dir: The directory to write to.
    lr_sched: Optional vector of learning rates to write.
  """
  verify_reconstruction(w_matrix, h_matrix, s_matrix)
  tf.io.write_file(
      _join_path(output_dir, W_MATRIX_STRING), tf.io.serialize_tensor(w_matrix)
  )
  tf.io.write_file(
      _join_path(output_dir, H_MATRIX_STRING), tf.io.serialize_tensor(h_matrix)
  )
  if lr_sched is not None:
    assert len(lr_sched) == h_matrix.shape[1]
    tf.io.write_file(
        _join_path(output_dir, LR_VECTOR_STRING),
        tf.io.serialize_tensor(lr_sched),
    )