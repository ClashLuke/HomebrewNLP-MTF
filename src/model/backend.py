import random
import string
import typing

import mesh_tensorflow as mtf
import numpy as np
import tensorflow as tf
from tensorflow.python.ops import array_ops, gen_linalg_ops, math_ops, random_ops
from tensorflow.python.ops.init_ops import Initializer

from ..dataclass import BlockArgs, ModelParameter
from ..mtf_wrapper import einsum, scoped
from ..utils_core import random_name
from ..utils_mtf import OPT_DIMS, SHAPE, anonymize_dim, deduplicate, feature_dims_used

tf1 = tf.compat.v1


class OrthogonalInit(Initializer):
    def __init__(self, params: ModelParameter, shape: SHAPE, fan_in_dims: OPT_DIMS = None):
        if fan_in_dims is None:
            fan_in_dims = []
        self.params = params
        self.sizes = [d.size for d in shape]
        self.seed = random.randint(0, 2 ** 32)
        sizes = [d.size for d in mtf.Shape(shape) - fan_in_dims]
        features_used = feature_dims_used(params, shape)
        if fan_in_dims is None:
            if features_used:
                if shape.index(params.key_dim) == len(sizes) - 1:
                    fan_in = np.prod(sizes[:-2])
                else:
                    fan_in = np.prod([d.size for d in params.feature_dims])
            elif len(sizes) == 2:
                fan_in = sizes[0]
            else:
                raise ValueError(f"Shape: {shape}\nParams: {params}\nFeaturesUsed: {features_used}")
        else:
            fan_in = int(np.prod([d.size for d in fan_in_dims]))
        fan_out = np.prod(sizes) // fan_in
        self.transpose = transpose = fan_out > fan_in
        self.shape = (fan_out, fan_in) if transpose else (fan_in, fan_out)

    def __call__(self, shape, dtype=None, partition_info=None):
        q, r = gen_linalg_ops.qr(random_ops.random_normal(self.shape, dtype=tf.float32, seed=self.seed))
        q *= math_ops.sign(array_ops.diag_part(r))
        if self.transpose:
            q = array_ops.matrix_transpose(q)
        return tf.cast(array_ops.reshape(q, self.sizes) / self.params.n_blocks ** 0.5, dtype)


def get_variable(args: BlockArgs, shape: SHAPE, initializer: typing.Callable) -> mtf.Tensor:
    params: ModelParameter = args.params
    with tf1.variable_scope("get_variable") as scope:
        def _var():
            return mtf.get_variable(params.mesh, random_name("get_variable"), deduplicate(shape),
                                    dtype=params.variable_dtype, initializer=initializer)

        if "shared" not in args:
            return _var()

        name = scope._name
        scope = name.split('/')
        body_idx = scope.index("body") + 1
        block, fn_name = scope[body_idx:body_idx + 2]
        block, config = block.split('_')
        fn_name = ''.join(c for c in fn_name if not c.isdigit())

        cache = params.cached_parameters
        for idx in (block, config, fn_name):
            if idx not in cache:
                cache[idx] = {}
            cache = cache[idx]

        if "counter" not in cache:
            cache["counter"] = 0
        cache["counter"] += 1
        if len(cache) == cache["counter"] + 1:
            cache["counter"] = 0
        fn_id = cache["counter"]

        if fn_id not in cache:
            cache[fn_id] = {}
        cache = cache[fn_id]
        if "counter" not in cache:
            cache["counter"] = 0

        if block == "0":
            var = _var()
            cache[cache["counter"]] = var
            cache["counter"] += 1
            return var

        if len(cache) == cache["counter"] + 1:
            cache["counter"] = 0
        var = cache[cache["counter"]]
        cache["counter"] += 1
        return var


def orthogonal_var(args: BlockArgs, shape: typing.Union[typing.List[mtf.Dimension], mtf.Shape],
                   fan_in_dims: OPT_DIMS = None) -> mtf.Tensor:
    shape = deduplicate(shape)
    return scoped("orthogonal_var", get_variable, args, shape, OrthogonalInit(args.params, shape, fan_in_dims))


def normal_var(args: BlockArgs, shape: SHAPE, stddev: float = 0.02, mean: float = 0.) -> mtf.Tensor:
    shape = deduplicate(shape)
    return scoped("normal_var", get_variable, args, shape, tf.random_normal_initializer(stddev=stddev, mean=mean))


def linear(args: BlockArgs, old: typing.List[mtf.Dimension], new: typing.List[mtf.Dimension]) -> mtf.Tensor:
    return einsum([args.tensor, orthogonal_var(args, old + new)],
                  deduplicate((args.tensor.shape - old).dims + new))


def linear_to_features(args: BlockArgs, old: typing.Optional[typing.List[mtf.Dimension]] = None) -> mtf.Tensor:
    return linear(args, old, args.params.feature_dims)


def linear_from_features(args: BlockArgs, new: typing.Optional[typing.List[mtf.Dimension]] = None) -> mtf.Tensor:
    return linear(args, args.params.feature_dims, new)


def get_intermediate(args: BlockArgs):
    if 'group' not in args:
        return args.params.intermediate
    return [args.params.head_dim,
            anonymize_dim(args.params.key_dim, args.params.key_dim.size * args.params.group_linear_factor)]
