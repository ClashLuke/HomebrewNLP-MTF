import typing

import mesh_tensorflow as mtf
import tensorflow as tf

from .activation import activate
from .basic import dropout, feed_forward, rezero
from .convolution import convolution
from .normalization import norm
from .spatial import attention, spatial_feed_forward, spatial_mixing
from ..dataclass import BlockArgs, BlockConfig, ModelParameter
from ..mtf_wrapper import scoped
from ..utils_core import random_name

ATTENTION_DIM = typing.NamedTuple("AttentionDim", (('index', int), ('dim', mtf.Dimension)))

tf1 = tf.compat.v1


def block_part_fn(params: ModelParameter, block_part_config: BlockConfig, block_input: mtf.Tensor,
                  name_prefix: str = 'block') -> mtf.Tensor:
    out = block_input
    with tf1.variable_scope(random_name(f"{name_prefix}_")):
        for layer in block_part_config.layer:
            name, *extras = layer.split('-')
            out = scoped(name, LAYER_FUNCTIONS[name], BlockArgs(params, out, extras))

        if not block_part_config.use_revnet and block_part_config.skip:
            out += block_input

    return out


def split_path(args: BlockArgs) -> mtf.Tensor:
    base, *name_extras = [block.split('_') for block in '_'.join(args.name_extras).split(',')]
    if 'add' in base:
        out = 0
        fn = mtf.add
    elif 'multiply' in base:
        out = 1
        fn = mtf.multiply
    else:
        raise ValueError

    for n in name_extras:
        name, *extras = n
        out = fn(scoped(name, LAYER_FUNCTIONS[name], args(extras)), out)

    return out


LAYER_FUNCTIONS = {'feed_forward':         feed_forward,
                   'attention':            attention,
                   'norm':                 norm,
                   'rezero':               rezero,
                   'activation':           activate,
                   'convolution':          convolution,
                   'dropout':              dropout,
                   'spatial_mixing':       spatial_mixing,
                   'spatial_feed_forward': spatial_feed_forward,
                   'split_path':           split_path
                   }
