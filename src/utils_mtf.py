import typing

import mesh_tensorflow as mtf
import tensorflow.compat.v1 as tf

from .utils_core import default

_NAME_INDEX = [0]


def _silu_derivative(op, dy):
    return dy * weighted_add(1, op.outputs[0], mtf.sigmoid(op.inputs[0]))


def _mish_derivative(op, dy):
    inp = op.inputs[0]
    gte = mtf.tanh(mtf.softplus(inp))
    return dy * (gte + (1 - mtf.square(gte)) * inp * mtf.sigmoid(inp))


ACTIVATIONS = {'relu': mtf.relu,
               'sigmoid': mtf.sigmoid,
               'tanh': mtf.tanh,
               'selu': mtf.selu,
               'elu': mtf.elu,
               'softplus': mtf.softplus,
               'silu': lambda x: mtf.cwise(lambda x: x * tf.sigmoid(x), [x], name=random_name("silu"),
                                           grad_function=_silu_derivative),
               'mish': lambda x: mtf.cwise(lambda x: x * tf.tanh(tf.math.softplus(x)), [x], name=random_name("mish"),
                                           grad_function=_mish_derivative),
               }


def unanonymize(inp: mtf.Tensor, dim: typing.Union[mtf.Dimension, str]) -> mtf.Tensor:
    """
    Inverse of anonymize. Un-replicates tensor across axis by removing the underscore from the name of a dimension of
    the tensor. This allows mtf to split the tensor across a given dimension again.
    :param inp: tensor to replicate
    :param dim: dimension of tensor
    :return: un-replicated tensor
    """
    dim = anonymize_dim(dim)
    if not check_for_dim(inp, dim):
        return inp
    return mtf.rename_dimension(inp, dim, dim_name(unanonymize_dim(dim)))


def new_dim(dim: typing.Union[mtf.Dimension, str], new_size: typing.Optional[int] = None,
            new_name: typing.Optional[str] = None):
    """
    Create new mesh tensorflow dimension with optional new size and/or new name to replace the old values with.
    :param dim: Dimension or name of dimension
    :param new_size: Optional new size of mtf dimension
    :param new_name: Optinal new name of dimension
    :return: new mtf.Dimension
    """
    name = default(new_name, dim_name(dim))
    if isinstance(dim, mtf.Dimension):
        return mtf.Dimension(name, default(new_size, dim.size))
    if new_size is None:
        return name
    return mtf.Dimension(name, new_size)


def unanonymize_dim(dim: typing.Union[mtf.Dimension, str], new_size: typing.Optional[int] = None):
    """
    Unanonymize mtf.Dimension by removing a leading underscore, if it exists. Optionally, the size can be changed at
    the same time.
    :param dim: mtf.Dimension to unanonymize
    :param new_size: Optional new size
    :return: mtf.Dimension without leading underscore in name
    """
    name = dim_name(dim)
    if name.startswith('_'):
        name = name[1:]
    return new_dim(dim, new_size, name)


def anonymize_dim(dim: typing.Union[mtf.Dimension, str], new_size: typing.Optional[int] = None):
    """
    Anonymize mtf.Dimension by adding a leading underscore, if it does not exist. Optionally, the size can be changed at
    the same time.
    :param dim: mtf.Dimension to anonymize
    :param new_size: Optional new size
    :return: mtf.Dimension with leading underscore in name
    """
    name = dim_name(dim)
    if not name.startswith('_'):
        name = '_' + name
    return new_dim(dim, new_size, name)


def get_dim(shape: typing.Union[mtf.Tensor, mtf.Shape, typing.List[mtf.Dimension]],
            dim: typing.Union[mtf.Dimension, str],
            index=False) -> typing.Union[int, mtf.Dimension]:
    """
    Attempts to get a dimension of a tensor. Raises a ValueError if the dimension does not exist.
    :param shape: shape, tensor or list of dimensions to check in
    :param dim: dimension (or name) to check for
    :param index: whether to return the dimension or its index
    :return: index or dimension
    """
    name = dim_name(dim)
    for idx, cdim in enumerate(shape.shape if isinstance(shape, mtf.Tensor) else shape):
        if cdim.name == name:
            return idx if index else cdim
    raise ValueError(f"Dim {dim} with name {name} not found in shape {shape}")


def concat(tensor_list: typing.List[mtf.Tensor], dim: typing.Union[mtf.Dimension, str]) -> mtf.Tensor:
    """
    Concatenate across a given (potentially non-anonymous) dimension in mtf.Tensor. This first anonymizes the dimension
    to concat in the first place, next it concats across the dimension and only then it replicates it on all devices
    again.
    Non-Anonymous shapes are not necessary, as the anonymization can skip itself if it isn't necessary.
    :param tensor_list: mtf.Tensor's to concatenate
    :param dim: dimension or name to concatenate in
    :return: concated tensorlist
    """
    dim = dim_name(dim)
    return unanonymize(mtf.concat([anonymize(t, dim) for t in tensor_list], anonymize_dim(dim)), dim)


def pad(tensor: mtf.Tensor, dim: typing.Union[mtf.Dimension, str], padding: typing.Tuple[int, int]
        ) -> mtf.Tensor:
    """
    Pad across a given (potentially non-anonymous) dimension in mtf.Tensor. This first anonymizes the dimension
    to concat in the first place, next it concats across the dimension and only then it replicates it on all devices
    again.
    Non-Anonymous shapes are not necessary, as the anonymization can skip itself if it isn't necessary.
    :param tensor: mtf.Tensor's to pad
    :param dim: dimension or name to pad in
    :param padding: padding of dimension
    :return: concated tensorlist
    """
    dim = dim_name(dim)
    return mtf.pad(anonymize(tensor, dim), padding, anonymize_dim(dim))


def random_name(prefix="") -> str:
    """
    Generates a random name based on the globally set seed using python's random module.
    Each name has 256 bits of entropy and a final length of 44 base64 encoded characters.
    For the sake of convenience, special characters are removed from the final string.
    :return: random string
    """
    _NAME_INDEX[0] += 1
    return f'{prefix}{_NAME_INDEX[0]}'


def to_float(tensor: mtf.Tensor) -> mtf.Tensor:
    """
    Cast a tensor to float
    :param tensor: tensor to be casted
    :return: casted tensor
    """
    return mtf.cast(tensor, tf.float32)


def dim_name(dim: typing.Union[mtf.Dimension, str]) -> str:
    """
    :param dim: Mesh TensorFlow dimension or name of dimension
    :return: name of dimension
    """
    return dim.name if isinstance(dim, mtf.Dimension) else dim


def check_for_dim(inp: typing.Union[typing.List[mtf.Dimension], mtf.Shape, mtf.Tensor],
                  dim: typing.Union[mtf.Dimension, str]) -> bool:
    """
    Check if a dimension exists in a Mesh TensorFlow tensor, shape or list of dimensions
    :param inp: input to check in
    :param dim: dimension to check for
    :return: true if dimension is found
    """
    return any(dim_name(dim) == cdim.name for cdim in (inp.shape if isinstance(inp, mtf.Tensor) else inp))


def deduplicate(inp: typing.Iterable) -> typing.Iterable:
    """
    Remove duplicates from any iterable while retaining the order of elements.
    :param inp: iterable to deduplicate
    :return: new, unique iterable of same type as input
    """
    return type(inp)(dict.fromkeys(list(inp)))


def anonymize(inp: mtf.Tensor,
              dim: typing.Union[typing.List[typing.Union[mtf.Dimension, str]], typing.Union[mtf.Dimension, str]]
              ) -> mtf.Tensor:
    """
    Add an underscore to the name of a dimension of a tensor. This replicates a given dimension of a tensor on all
    devices.
    :param inp: tensor to replicate
    :param dim: dimension(s) to replicate
    :return: replicated tensor
    """
    if not isinstance(dim, list):
        dim = [dim]
    shape = inp.shape.dims.copy()
    for cdim in dim:
        cdim = unanonymize_dim(dim_name(cdim))
        if not check_for_dim(inp, cdim):
            continue
        shape = [anonymize_dim(d) if cdim == d.name else d for d in shape]
    if shape != inp.shape.dims:
        return mtf.reshape(inp, shape)
    return inp


def anonymize_shape(inp: typing.Union[typing.List[mtf.Dimension], mtf.Shape],
                    dim: typing.Union[mtf.Dimension, str]) -> typing.Union[mtf.Shape, typing.List[mtf.Dimension]]:
    """
    Anonymize one dimension of a given Mesh TensorFlow shape. See anonymize for details on what anonymization does.
    :param inp: shape or list of dimensions
    :param dim: dimension to rename
    :return: new shape/list with renamed dimension
    """
    return replace_dim(inp, anonymize_dim(dim), unanonymize_dim(dim))


def replace_dim(inp: typing.Union[typing.List[mtf.Dimension], mtf.Shape],
                dim: typing.Union[mtf.Dimension, str],
                replaced: typing.Optional[typing.Union[mtf.Dimension, str]] = None
                ) -> typing.Union[mtf.Shape, typing.List[mtf.Dimension]]:
    """
    Replace a dimension in a shape
    :param inp: shape or list of dimensions
    :param dim: dimension with the same name to replace it with
    :param replaced: dimension that will be replaced
    :return: new shape/list with changed dimension
    """
    if replaced is None:
        replaced = dim
    if not check_for_dim(inp, replaced):
        return inp
    out = [dim if dim_name(replaced) == cdim.name else cdim
           for cdim in (inp.dims if isinstance(inp, mtf.Shape) else inp)]
    if isinstance(inp, list):
        return out
    return mtf.Shape(out)


def activate(fn_name: typing.Union[typing.List[str], str], block_input: mtf.Tensor) -> mtf.Tensor:
    """
    Call activation function on mtf.Tensor.
    :param fn_name: Name of activation function
    :param block_input: mtf.Tensor
    :return: activated mtf.Tensor
    """
    if isinstance(fn_name, list):
        fn_name = fn_name[0]
    if fn_name not in ACTIVATIONS:
        raise ValueError(f'Unknown activation function "{fn_name}". Known functions: {list(ACTIVATIONS.keys())}')
    return ACTIVATIONS[fn_name](block_input)


def weighted_add(left, right, alpha):
    return left * alpha + right * (1 - alpha)


def slice(tensor: mtf.Tensor, start: int, end: int, dim: typing.Union[mtf.Dimension, str]):
    """
    Slice across a given (potentially non-anonymous) dimension in mtf.Tensor. This first anonymizes the dimension to
    allow slicing in the first place, next it slices across the dimension and only then it replicates it on all devices
    again.
    Non-Anonymous shapes are not necessary, as the anonymization can skip itself if it isn't necessary.
    :param tensor: mtf.Tensor to slice
    :param start: start of slice
    :param end: end of slice
    :param dim: dimension or name to slice in
    :return: slice of tensor
    """
    dim = dim_name(dim)
    if not start and get_dim(tensor, dim).size == end:
        return tensor
    return unanonymize(mtf.slice(anonymize(tensor, dim), start, end - start, anonymize_dim(dim)), dim)
